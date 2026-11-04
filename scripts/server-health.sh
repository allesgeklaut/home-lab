#!/usr/bin/env bash
# server-health.sh — read-only health snapshot for the homelab host.
#
# Everything here is a *probe*: nothing is changed, started, stopped or
# written (apart from optional ntfy pushes). It reads /proc, sysfs, df,
# docker inspect, journalctl, drive SMART counters and the NAS.
#
# Checks
#   host     uptime, load average vs. CPU count
#   memory   RAM + swap pressure (/proc/meminfo)
#   disk     free space and inode usage per real filesystem
#   docker   daemon reachable; unhealthy / stopped / crash-looping containers
#            (intentionally stopped containers are allowed — see ALLOW_STOPPED)
#   gpu      ROCm temperature / utilisation / power (if rocm-smi exists)
#   hardware CPU/NVMe temperatures, read-only NVMe/SATA SMART health
#   systemd  failed units
#   kernel   MCE/EDAC, OOM, storage errors/lockups in last 25h across boots; zombies
#   nas      NFS mounts reachable; nightly backup 'latest' freshness
#
# Exit code (cron friendly): 0 = all OK, 1 = warnings, 2 = critical.
#
# Usage
#   server-health.sh                     human-readable report
#   server-health.sh --quiet             only problems (good for cron)
#   server-health.sh --json              machine-readable summary
#   server-health.sh --notify            also push problems to ntfy
#   server-health.sh --notify-always     also push a healthy daily heartbeat
#   server-health.sh --quiet --notify    typical cron invocation
#
# Thresholds are environment overridable, e.g.:
#   DISK_WARN=90 MEM_WARN=90 server-health.sh
#
# ntfy: reuses backup.sh's token + topic so every pop-os event (backup,
# health) lands in one ntfy topic. Defaults: NTFY_TOPIC=backup-popos,
# NTFY_TOKEN_FILE=/opt/secrets/ntfy-backup.token (created by backup.sh's
# setup). Override either env to split topics later. Health pushes carry
# their own Title/Tags so they stay distinguishable from backup pushes.
# Without the token file the script still runs; notify only logs a warning.
#
# Every completed run (including --quiet cron runs and the supervisor's
# deadline report) atomically writes the dashboard state file
# $HEALTH_STATE_DIR/latest.json (default ~/.local/state/server-health/).
# Its content is exactly the --json document; set HEALTH_STATE_DIR empty to
# disable the write.

set -uo pipefail

# ------------------------------------------------------------- Configuration
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
HOST=$(hostname)
NOW=$(date '+%Y-%m-%d %H:%M:%S')
# Worker deadline plus a short kill grace; parent can then report a stuck run.
HEALTH_RUN_TIMEOUT=${HEALTH_RUN_TIMEOUT:-180}
HEALTH_PROBE_TIMEOUT=${HEALTH_PROBE_TIMEOUT:-10}
if ! [[ "$HEALTH_RUN_TIMEOUT" =~ ^[1-9][0-9]{0,3}$ ]] \
    || ! [[ "$HEALTH_PROBE_TIMEOUT" =~ ^[0-9]+([.][0-9]+)?$ ]] \
    || ! awk -v t="$HEALTH_PROBE_TIMEOUT" 'BEGIN{exit !(t>0 && t<=60)}'; then
    echo "invalid health-check timeout configuration" >&2
    exit 64
fi

# Dashboard state file: written on every completed run (also --quiet cron
# runs). Single-dash form on purpose: an explicitly EMPTY HEALTH_STATE_DIR
# disables the state write, an unset one gets the XDG default.
HEALTH_STATE_DIR=${HEALTH_STATE_DIR-${XDG_STATE_HOME:-$HOME/.local/state}/server-health}

DISK_WARN=${DISK_WARN:-85}
DISK_CRIT=${DISK_CRIT:-92}
INODE_WARN=${INODE_WARN:-85}
INODE_CRIT=${INODE_CRIT:-92}
MEM_WARN=${MEM_WARN:-85}
MEM_CRIT=${MEM_CRIT:-95}
SWAP_WARN=${SWAP_WARN:-50}
LOAD_WARN_MULT=${LOAD_WARN_MULT:-1.0}      # per CPU core
LOAD_CRIT_MULT=${LOAD_CRIT_MULT:-2.0}
GPU_TEMP_WARN=${GPU_TEMP_WARN:-85}
GPU_TEMP_CRIT=${GPU_TEMP_CRIT:-95}
RESTART_WARN=${RESTART_WARN:-5}
RECENT_EXIT_DAYS=${RECENT_EXIT_DAYS:-7}   # exited longer ago than this = stale orphan, not a fault
# A nightly job: one missed run should be visible. These assume the check runs
# AFTER the 03:00 backup (backup.sh); a check in the 00:00–03:00 window would
# see yesterday's dir and want WARN=2 instead.
BACKUP_WARN_DAYS=${BACKUP_WARN_DAYS:-1}
BACKUP_CRIT_DAYS=${BACKUP_CRIT_DAYS:-2}
BACKUP_LATEST=${BACKUP_LATEST:-/mnt/nas-backup/popos/latest}
# Space-separated network mounts to reachability-check; empty = auto-detect from
# /proc/mounts (nfs/nfs4/cifs/smb3/sshfs).
NAS_MOUNTS=${NAS_MOUNTS:-}
# Containers that are stopped *intentionally*: idle-managed GPU tenants and
# power-monitor (unused; kept stopped pending security hardening).
# Space-separated; the word-splitting in the loops below is deliberate.
ALLOW_STOPPED=${ALLOW_STOPPED:-"llama-server llama-companion llama-bolt comfyui power-monitor"}

NTFY_HOST=${NTFY_HOST:-http://localhost:10000}
NTFY_TOPIC=${NTFY_TOPIC:-backup-popos}                # same topic as backup.sh
NTFY_TOKEN_FILE=${NTFY_TOKEN_FILE:-/opt/secrets/ntfy-backup.token}  # same token (topic-scoped)

# ----------------------------------------------------------------- CLI flags
QUIET=0 JSON=0 NOTIFY=0 NOTIFY_ALWAYS=0
# Private re-entry flag for the unprivileged deadline supervisor, not a sudo API.
HEALTH_WORKER=0
if [ "${1:-}" = "--_health-worker" ]; then HEALTH_WORKER=1; shift; fi
ORIGINAL_ARGS=("$@")
usage() { # print the leading comment block (stop at the first non-# line)
    awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
    exit 0
}
while [ $# -gt 0 ]; do
    case "$1" in
        -q|--quiet)        QUIET=1 ;;
        --json)            JSON=1; QUIET=1 ;;
        -n|--notify)       NOTIFY=1 ;;
        --notify-always)   NOTIFY=1; NOTIFY_ALWAYS=1 ;;
        --no-color)        NO_COLOR=1 ;;
        -h|--help)         usage ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 64 ;;
    esac
    shift
done

# Colours only when attached to a terminal (never in cron / --json).
if [ -t 1 ] && [ "${NO_COLOR:-}" = "" ] && [ "$JSON" = 0 ]; then
    C_OK=$'\e[32m'; C_WARN=$'\e[33m'; C_CRIT=$'\e[31m'; C_DIM=$'\e[2m'; C_RST=$'\e[0m'
else
    C_OK=""; C_WARN=""; C_CRIT=""; C_DIM=""; C_RST=""
fi

# ------------------------------------------------------------------- Findings
WARN=0
CRIT=0
ISSUES=()      # "LEVEL|message" WARN/CRIT for notify + --json issues array
DISK_STATE=()  # "mount|pct|free_kb" for the --json disks array
DETAILS=()     # "level|message" OK/info findings for the --json details array

emit() { # emit <OK|WARN|CRIT|info> <message>
    local lvl=$1; shift
    local msg=$*
    case "$lvl" in
        WARN) WARN=$((WARN + 1)); ISSUES+=("WARN|$msg") ;;
        CRIT) CRIT=$((CRIT + 1)); ISSUES+=("CRIT|$msg") ;;
        OK|info) DETAILS+=("$lvl|$msg") ;;
    esac
    # In --json mode stdout must stay pure JSON: record findings but never print.
    [ "$JSON" = 1 ] && return 0
    case "$lvl" in
        WARN) printf '%s[WARN]%s %s\n' "$C_WARN" "$C_RST" "$msg" ;;
        CRIT) printf '%s[CRIT]%s %s\n' "$C_CRIT" "$C_RST" "$msg" ;;
        OK)   [ "$QUIET" = 1 ] || printf '%s[ OK ]%s %s\n' "$C_OK" "$C_RST" "$msg" ;;
        *)    [ "$QUIET" = 1 ] || printf '%s[info]%s %s\n' "$C_DIM" "$C_RST" "$msg" ;;
    esac
    return 0
}

section() { [ "$QUIET" = 1 ] || printf '\n%s── %s %s\n' "$C_DIM" "$1" "$C_RST"; }

human_kb() { # 1024-blocks -> e.g. 31.3G / 235M
    awk -v k="$1" 'BEGIN{
        if (k >= 1073741824) printf "%.1fT", k/1073741824;
        else if (k >= 1048576) printf "%.1fG", k/1048576;
        else if (k >= 1024) printf "%.0fM", k/1024;
        else printf "%dK", k;
    }'
}
pct() { awk -v a="$1" -v b="$2" 'BEGIN{ if (b>0) printf "%d", (a/b)*100; else print 0 }'; }

# Inner timeouts stay in the worker's process group, so the outer deadline can
# terminate the whole unprivileged probe tree instead of orphaning a subgroup.
probe() { timeout --foreground --kill-after=2s "${HEALTH_PROBE_TIMEOUT}s" "$@"; }

push_notification() { # title priority tags body — token never copied to argv/env/tempfiles
    local token
    [ -r "$NTFY_TOKEN_FILE" ] || return 1
    token=$(< "$NTFY_TOKEN_FILE")
    # Bearer-token grammar also prevents newline/header injection.
    [ "${#token}" -le 4096 ] && [[ "$token" =~ ^[-A-Za-z0-9._~+/]+=*$ ]] || return 1
    # printf is a shell builtin; only the anonymous pipe contains the header.
    # -q disables curlrc, and --data-raw never interprets a body as a filename.
    printf 'Authorization: Bearer %s\n' "$token" | \
        curl -q -s --max-time 10 --fail-with-body --proto '=http,https' \
            -H @- -H "Title: $1" -H "Priority: $2" -H "Tags: $3" \
            --data-raw "$4" "$NTFY_HOST/$NTFY_TOPIC" >/dev/null 2>&1
}

if [ "$HEALTH_WORKER" = 0 ]; then
    # Buffer output: a killed/partial worker must never leave a partial OK JSON
    # document behind. GNU timeout terminates the worker's process group.
    if RUN_OUTPUT=$(timeout --kill-after=5s "${HEALTH_RUN_TIMEOUT}s" \
            bash "$SCRIPT_DIR/server-health.sh" --_health-worker "${ORIGINAL_ARGS[@]}"); then
        RUN_EXIT=0
    else
        RUN_EXIT=$?
    fi
    case "$RUN_EXIT" in
        0|1|2)
            [ -z "$RUN_OUTPUT" ] || printf '%s\n' "$RUN_OUTPUT"
            exit "$RUN_EXIT" ;;
    esac
    emit CRIT "health-check worker did not complete (timeout/termination, exit $RUN_EXIT)"
    if [ "$NOTIFY" = 1 ]; then
        push_notification "Health CRIT: $HOST" urgent rotating_light \
            "health-check worker did not complete (exit $RUN_EXIT); coverage unknown" \
            || emit WARN "ntfy notification failed (delivery unconfirmed; check ntfy/token)"
    fi
    # Assemble the report on EVERY run so the dashboard state file is also
    # written for --quiet cron runs; stdout stays pure JSON only in --json
    # mode (the heredoc prints only when JSON=1, stderr always passes through).
    HOST="$HOST" NOW="$NOW" ISSUES="$(printf '%s\n' "${ISSUES[@]}")" \
        WARN="$WARN" CRIT="$CRIT" JSON="$JSON" STATE_DIR="$HEALTH_STATE_DIR" \
        python3 -I -B - <<'PY'
import json, os, sys, tempfile

def write_state(state_dir, text):
    # Atomic dashboard state: dir 0700, temp file 0600, os.replace so readers
    # never see a partial document. A failure is ONE stderr line only — never
    # stdout, never part of the JSON — and never changes the run's exit code.
    if not state_dir:
        return
    tmp = None
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=state_dir)
        with os.fdopen(fd, "w") as handle:
            handle.write(text + "\n")
        os.replace(tmp, os.path.join(state_dir, "latest.json"))
        tmp = None
    except Exception as exc:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        print("server-health: state file not written (%s)" % exc, file=sys.stderr)

issues = [{"level": level, "message": message}
          for level, message in (line.split("|", 1) for line in os.environ["ISSUES"].splitlines())]
text = json.dumps({"host": os.environ["HOST"], "time": os.environ["NOW"],
                   "status": "crit", "warnings": int(os.environ["WARN"]),
                   "critical": int(os.environ["CRIT"]), "load": None, "cpus": None,
                   "memory_percent": None, "disks": [], "issues": issues,
                   "details": []}, indent=2)
write_state(os.environ.get("STATE_DIR", ""), text)
if os.environ.get("JSON") == "1":
    print(text)
PY
    if [ "$JSON" = 0 ]; then
        printf '\nRESULT: CRITICAL — health-check worker did not complete\n'
    fi
    exit 2
fi

# ------------------------------------------------------------- Host overview
read -r LOAD1 LOAD5 LOAD15 _ < /proc/loadavg
CPUS=$(nproc)
MEM_TOTAL_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
MEM_AVAIL_KB=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
# MemAvailable is absent on kernels < 3.14; fall back to MemFree so the
# arithmetic below can never break (and `set -u` never trips on an empty var).
[ -n "$MEM_AVAIL_KB" ] || MEM_AVAIL_KB=$(awk '/^MemFree:/{print $2}' /proc/meminfo)
SWAP_TOTAL_KB=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)
SWAP_FREE_KB=$(awk '/^SwapFree:/{print $2}' /proc/meminfo)
# Derive uptime from /proc/uptime instead of parsing the locale-dependent
# `uptime` text output.
UPTIME_PRETTY=$(awk '{d=int($1/86400); h=int(($1%86400)/3600); m=int(($1%3600)/60);
    if (d>0) printf "%dd %dh %dm", d, h, m; else if (h>0) printf "%dh %dm", h, m; else printf "%dm", m}' /proc/uptime)
MEM_USED_KB=$((MEM_TOTAL_KB - MEM_AVAIL_KB))

if [ "$QUIET" = 0 ] && [ "$JSON" = 0 ]; then
    printf 'Server health — %s — %s\n' "$HOST" "$NOW"
    printf '  %s  ·  %s CPUs  ·  mem %s/%s  ·  swap %s free\n' \
        "up $UPTIME_PRETTY" "$CPUS" \
        "$(human_kb "$MEM_USED_KB")" "$(human_kb "$MEM_TOTAL_KB")" \
        "$(human_kb "$SWAP_FREE_KB")"
fi

# ---------------------------------------------------------------------- Load
section "load"
LOAD_PCT_1=$(awk -v l="$LOAD1" -v n="$CPUS" 'BEGIN{ printf "%d", (l/n)*100 }')
if awk -v l="$LOAD1" -v n="$CPUS" -v t="$LOAD_CRIT_MULT" 'BEGIN{exit !(l > n*t)}'; then
    emit CRIT "load ${LOAD1} ${LOAD5} ${LOAD15} — above ${LOAD_CRIT_MULT}x ${CPUS} cores"
elif awk -v l="$LOAD1" -v n="$CPUS" -v t="$LOAD_WARN_MULT" 'BEGIN{exit !(l > n*t)}'; then
    emit WARN "load ${LOAD1} ${LOAD5} ${LOAD15} — above ${LOAD_WARN_MULT}x ${CPUS} cores"
else
    emit OK "load ${LOAD1} ${LOAD5} ${LOAD15} (${LOAD_PCT_1}% of ${CPUS} cores)"
fi

# -------------------------------------------------------------------- Memory
section "memory"
MEM_PCT=$(pct "$MEM_USED_KB" "$MEM_TOTAL_KB")
if [ "$MEM_PCT" -ge "$MEM_CRIT" ]; then
    emit CRIT "memory ${MEM_PCT}% used ($(human_kb "$MEM_USED_KB")/$(human_kb "$MEM_TOTAL_KB"))"
elif [ "$MEM_PCT" -ge "$MEM_WARN" ]; then
    emit WARN "memory ${MEM_PCT}% used ($(human_kb "$MEM_USED_KB")/$(human_kb "$MEM_TOTAL_KB"))"
else
    emit OK "memory ${MEM_PCT}% used ($(human_kb "$MEM_USED_KB")/$(human_kb "$MEM_TOTAL_KB"))"
fi
if [ "$SWAP_TOTAL_KB" -gt 0 ]; then
    SWAP_USED_KB=$((SWAP_TOTAL_KB - SWAP_FREE_KB))
    SWAP_PCT=$(pct "$SWAP_USED_KB" "$SWAP_TOTAL_KB")
    if [ "$SWAP_PCT" -ge "$SWAP_WARN" ]; then
        emit WARN "swap ${SWAP_PCT}% used ($(human_kb "$SWAP_USED_KB")/$(human_kb "$SWAP_TOTAL_KB"))"
    else
        emit OK "swap ${SWAP_PCT}% used ($(human_kb "$SWAP_USED_KB")/$(human_kb "$SWAP_TOTAL_KB"))"
    fi
else
    emit info "no swap configured"
fi

# ---------------------------------------------------------------------- Disk
section "disk"
DF_EXCLUDES=(-x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs -x ramfs)
if DISK_OUT=$(probe df -P -k "${DF_EXCLUDES[@]}" 2>/dev/null); then
    while read -r fs blocks used avail cap mount; do
        [ "$fs" = "Filesystem" ] && continue
        [ -z "${mount:-}" ] && continue
        p=${cap%\%}
        case "$p" in ''|*[!0-9]*) continue ;; esac
        DISK_STATE+=("$mount|$p|$avail")
        if [ "$p" -ge "$DISK_CRIT" ]; then
            emit CRIT "disk $mount ${p}% used ($(human_kb "$avail") free)"
        elif [ "$p" -ge "$DISK_WARN" ]; then
            emit WARN "disk $mount ${p}% used ($(human_kb "$avail") free)"
        else
            emit OK "disk $mount ${p}% used ($(human_kb "$avail") free)"
        fi
    done <<< "$DISK_OUT"
else
    emit WARN "df failed or timed out (stale NFS mount?)"
fi

if INODE_OUT=$(probe df -Pi "${DF_EXCLUDES[@]}" 2>/dev/null); then
    while read -r fs inodes iused ifree cap mount; do
        [ "$fs" = "Filesystem" ] && continue
        [ -z "${mount:-}" ] && continue
        p=${cap%\%}
        case "$p" in ''|*[!0-9]*) continue ;; esac
        if [ "$p" -ge "$INODE_CRIT" ]; then
            emit CRIT "inodes $mount ${p}% used ($ifree free)"
        elif [ "$p" -ge "$INODE_WARN" ]; then
            emit WARN "inodes $mount ${p}% used ($ifree free)"
        else
            emit OK "inodes $mount ${p}% used ($ifree free)"
        fi
    done <<< "$INODE_OUT"
else
    emit WARN "inode check failed or timed out (coverage unknown)"
fi

# -------------------------------------------------------------------- Docker
section "docker"
if ! probe docker info >/dev/null 2>&1; then
    emit CRIT "Docker daemon unreachable"
else
    RUNNING_IDS=""
    if RUNNING_IDS=$(probe docker ps -q 2>/dev/null); then
        RUNNING=$(awk 'NF{n++} END{print n+0}' <<< "$RUNNING_IDS")
        emit OK "Docker daemon up — ${RUNNING} running container(s)"
    else
        emit WARN "Docker container listing failed (coverage unknown)"
    fi

    # Unhealthy containers.
    if RUNNING_STATE=$(probe docker ps --format '{{.Names}}|{{.Status}}' 2>/dev/null); then
        while IFS='|' read -r name status; do
            [ -z "$name" ] && continue
            case "$status" in
                *Restarting*) emit CRIT "container '$name' restarting ($status)" ;;
                *unhealthy*) emit WARN "container '$name' unhealthy ($status)" ;;
            esac
        done <<< "$RUNNING_STATE"
    else
        emit WARN "Docker health-state check failed (coverage unknown)"
    fi

    # Stopped containers — unless they are intentionally allowed down. A
    # container that exited a long time ago with no restart policy is stale
    # cruft (e.g. a leftover `hello-world`), not an outage — report it as info
    # so the warning only fires for something that should actually be up.
    IDLE_DOWN=()
    if STOPPED_STATE=$(probe docker ps -a --filter status=exited --filter status=created \
            --filter status=dead --filter status=paused \
            --format '{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null); then
        while IFS='|' read -r name state status; do
            [ -z "$name" ] && continue
            skip=0
            for a in $ALLOW_STOPPED; do [ "$name" = "$a" ] && skip=1; done
            if [ "$skip" = 1 ]; then IDLE_DOWN+=("$name"); continue; fi
            info_line=$(probe docker inspect -f '{{.HostConfig.RestartPolicy.Name}}|{{.State.FinishedAt}}|{{.Config.Image}}' "$name" 2>/dev/null)
            # A container removed between `docker ps` and `docker inspect` yields
            # nothing here — skip it rather than misreporting it as down.
            [ -n "$info_line" ] || { emit info "container '$name' vanished before inspect — skipped"; continue; }
            IFS='|' read -r policy finished image <<< "$info_line"
            fin_epoch=$(date -d "$finished" +%s 2>/dev/null || echo 0)
            [ "$fin_epoch" = 0 ] && age_days=9999 || age_days=$(( ( $(date +%s) - fin_epoch ) / 86400 ))
            if [ "$policy" != "no" ] || [ "$age_days" -lt "$RECENT_EXIT_DAYS" ]; then
                emit WARN "container '$name' not running ($status)"
            else
                emit info "stale exited container '$name' (${image:-?}, ${age_days}d ago) — safe to 'docker rm'"
            fi
        done <<< "$STOPPED_STATE"
    else
        emit WARN "Docker stopped-container check failed (coverage unknown)"
    fi
    [ ${#IDLE_DOWN[@]} -gt 0 ] && emit info "intentionally stopped containers: ${IDLE_DOWN[*]}"

    # Crash loops — a container that keeps restarting is worse than one down.
    if [ -n "$RUNNING_IDS" ]; then
        # IDs intentionally word-split; Docker emits one whitespace-free ID per line.
        if RESTARTS=$(probe docker inspect -f '{{.Name}}|{{.RestartCount}}' $RUNNING_IDS 2>/dev/null); then
            while IFS='|' read -r name rc; do
                [ -z "$name" ] && continue
                name=${name#/}
                case "$rc" in
                    ''|*[!0-9]*) emit WARN "container '$name' restart count unreadable"; continue ;;
                esac
                if [ "$rc" -ge "$RESTART_WARN" ]; then
                    emit WARN "container '$name' restarted ${rc}x (crash loop?)"
                fi
            done <<< "$RESTARTS"
        else
            emit WARN "Docker restart-count check failed (coverage unknown)"
        fi
    fi

    # Running containers not managed by a compose project (informational).
    # One batched inspect instead of two calls per container.
    STRAY=""
    if [ -n "$RUNNING_IDS" ]; then
        if PROJECTS=$(probe docker inspect -f '{{.Name}}|{{if .Config.Labels}}{{index .Config.Labels "com.docker.compose.project"}}{{end}}' \
                $RUNNING_IDS 2>/dev/null); then
            while IFS='|' read -r name proj; do
                [ -z "$name" ] && continue
                [ -z "$proj" ] && STRAY+=" ${name#/}"
            done <<< "$PROJECTS"
        else
            emit WARN "Docker compose-project check failed (coverage unknown)"
        fi
    fi
    [ -n "$STRAY" ] && emit info "unmanaged running container(s):$STRAY"
fi

# -------------------------------------------------------------- Hardware
hardware_probe() {
    local output line level message
    if output=$(timeout --foreground --kill-after=2s 60s python3 -I -B "$SCRIPT_DIR/server-health-hardware.py" "$1"); then
        if [ -z "$output" ]; then
            emit WARN "$1 check returned no findings (coverage unknown)"
        else
            while IFS= read -r line; do
                # Exactly one delimiter and a printable, nonempty message.
                if ! [[ "$line" =~ ^(OK|WARN|CRIT|info)\|[^\|[:cntrl:]]+$ ]]; then
                    emit WARN "$1 check returned invalid output (coverage unknown)"
                    continue
                fi
                IFS='|' read -r level message <<< "$line"
                if ! [[ "$message" =~ [^[:space:]] ]]; then
                    emit WARN "$1 check returned an empty message (coverage unknown)"
                    continue
                fi
                case "$level" in
                    OK|WARN|CRIT|info) emit "$level" "$message" ;;
                    *) emit WARN "$1 check returned invalid output (coverage unknown)" ;;
                esac
            done <<< "$output"
        fi
    else
        emit WARN "$1 check failed (coverage unknown)"
    fi
}
section "temperatures"
hardware_probe temperatures
section "drive health"
hardware_probe smart

# ----------------------------------------------------------------------- GPU
section "gpu"
if command -v rocm-smi >/dev/null 2>&1; then
    if GPU_JSON=$(timeout --foreground --kill-after=2s 15s rocm-smi --json --showtemp --showuse --showpower 2>/dev/null) \
        && [ -n "$GPU_JSON" ]; then
        if GPU_LINES=$(GPU_JSON="$GPU_JSON" python3 - <<'PY'
import os, json, re
try:
    d = json.loads(os.environ["GPU_JSON"])
except Exception:
    raise SystemExit(1)
ok = 0
for card, v in d.items():
    if not isinstance(v, dict):
        continue
    ok += 1
    t = v.get("Temperature (Sensor edge) (C)", "")
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", str(t)):
        t = "?"   # key missing/renamed -> surface it, don't silently pass
    print("%s|%s|%s|%s" % (
        card, t,
        v.get("GPU use (%)", "?"),
        v.get("Average Graphics Package Power (W)", "?"),
    ))
raise SystemExit(0 if ok else 1)
PY
        ); then
            while IFS='|' read -r card t u p; do
                [ -z "$card" ] && continue
                if [ "$t" = "?" ]; then
                    emit WARN "GPU $card — temperature not reported (util ${u}%, ${p}W)"
                elif awk -v t="$t" -v c="$GPU_TEMP_CRIT" 'BEGIN{exit !(t>=c)}'; then
                    emit CRIT "GPU $card ${t}C (util ${u}%, ${p}W) — overheating"
                elif awk -v t="$t" -v c="$GPU_TEMP_WARN" 'BEGIN{exit !(t>=c)}'; then
                    emit WARN "GPU $card ${t}C (util ${u}%, ${p}W) — running hot"
                else
                    emit OK "GPU $card ${t}C, util ${u}%, ${p}W"
                fi
            done <<< "$GPU_LINES"
        else
            emit WARN "could not parse rocm-smi output (GPU health unknown)"
        fi
    else
        emit WARN "rocm-smi present but failed to report"
    fi
else
    emit info "no GPU tooling (rocm-smi) present"
fi

# ------------------------------------------------------------------- Systemd
section "systemd"
if command -v systemctl >/dev/null 2>&1; then
    if FAILED_OUT=$(probe systemctl --failed --no-legend --plain 2>/dev/null); then
        FAILED=$(awk 'NF{print $1}' <<< "$FAILED_OUT")
        if [ -n "$FAILED" ]; then
            n=$(printf '%s\n' "$FAILED" | wc -l)
            emit WARN "$n failed systemd unit(s): $(echo $FAILED)"
        else
            emit OK "no failed systemd units"
        fi
    else
        emit WARN "systemd failed-unit check unavailable (coverage unknown)"
    fi
else
    emit WARN "systemctl not available (systemd health unknown)"
fi

# ------------------------------------------------------- Kernel / processes
section "kernel"
hardware_probe kernel

if PROCESS_STATES=$(probe ps -eo stat= 2>/dev/null); then
    ZOMBIES=$(grep -c '^Z' <<< "$PROCESS_STATES" || true)
    if [ "${ZOMBIES:-0}" -gt 5 ]; then
        emit WARN "${ZOMBIES} zombie processes"
    else
        emit OK "no significant zombie processes"
    fi
else
    emit WARN "process/zombie check unavailable (coverage unknown)"
fi

# ------------------------------------------------------------------ NAS / backup
section "nas"
# Reachability of network filesystems. Auto-detected from /proc/mounts so it
# tracks mount changes; override with NAS_MOUNTS="/mnt/a /mnt/b".
if [ -n "${NAS_MOUNTS:-}" ]; then
    read -ra NAS_LIST <<< "$NAS_MOUNTS"
else
    mapfile -t NAS_LIST < <(awk '$3 ~ /^(nfs|nfs4|cifs|smb3|fuse.sshfs)$/ {print $2}' /proc/mounts 2>/dev/null | sort -u)
fi
if [ ${#NAS_LIST[@]} -eq 0 ]; then
    emit info "no network mounts detected"
else
    for m in "${NAS_LIST[@]}"; do
        if timeout --foreground --kill-after=2s 5s ls "$m" >/dev/null 2>&1; then
            emit OK "NFS mount $m reachable"
        else
            emit WARN "NFS mount $m unreachable"
        fi
    done
fi

if [ -L "$BACKUP_LATEST" ]; then
    # Both NAS reads below are timeout-bounded: a stale NFS mount must not
    # wedge the whole run (NFS death hung the nightly backup during the
    # 2026-09-07 NAS outage — same failure mode).
    resolved=$(timeout --foreground --kill-after=2s 5s readlink -f "$BACKUP_LATEST" 2>/dev/null)
    if [ -z "$resolved" ]; then
        emit WARN "backup 'latest' unreadable — NAS stale? ($BACKUP_LATEST)"
    else
        target=$(basename "$resolved")
        if [[ "$target" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
            age_days=$(( ( $(date +%s) - $(date -d "$target" +%s) ) / 86400 ))
            if [ "$age_days" -ge "$BACKUP_CRIT_DAYS" ]; then
                emit CRIT "nightly backup stale: latest=$target (${age_days}d old)"
            elif [ "$age_days" -ge "$BACKUP_WARN_DAYS" ]; then
                emit WARN "nightly backup older than expected: latest=$target (${age_days}d old)"
            else
                emit OK "nightly backup fresh: latest=$target (${age_days}d old)"
            fi
            [ -n "$(timeout --foreground --kill-after=2s 5s ls -A "$BACKUP_LATEST/stacks" 2>/dev/null | head -n1)" ] \
                || emit WARN "backup tree looks empty: $BACKUP_LATEST/stacks"
        else
            emit WARN "cannot parse backup date from '$target'"
        fi
    fi
else
    emit WARN "backup 'latest' symlink missing ($BACKUP_LATEST)"
fi

# ------------------------------------------------------------------- Notify
# Send before rendering the final report, so delivery failures are warnings in
# both JSON and the exit status. Do not recursively notify about a failed send.
if [ "$CRIT" -gt 0 ]; then STATUS=crit; elif [ "$WARN" -gt 0 ]; then STATUS=warn; else STATUS=ok; fi
if [ "$NOTIFY" = 1 ]; then
    if [ ${#ISSUES[@]} -gt 0 ] || [ "$NOTIFY_ALWAYS" = 1 ]; then
        if [ ${#ISSUES[@]} -gt 0 ]; then
            if [ "$CRIT" -gt 0 ]; then prio="urgent"; else prio="default"; fi
            title="Health ${STATUS^^}: $HOST"
            body=$(printf '%s\n' "${ISSUES[@]}" | sed 's/^[A-Z]*|//')
            tags="rotating_light"
        else
            prio="low"; title="Health OK: $HOST"; body="all checks passed"; tags="white_check_mark"
        fi
        if ! push_notification "$title" "$prio" "$tags" "$body"; then
            emit WARN "ntfy notification failed (delivery unconfirmed; check ntfy/token)"
        fi
    fi
fi

# ------------------------------------------------------------------- Summary
if [ "$CRIT" -gt 0 ]; then STATUS=crit; elif [ "$WARN" -gt 0 ]; then STATUS=warn; else STATUS=ok; fi

# Assemble the machine report on EVERY completed run — the dashboard state
# file is written even for --quiet cron runs; stdout is pure JSON only in
# --json mode (the heredoc prints only when JSON=1, stderr passes through).
ISSUES_STR=$(printf '%s\n' "${ISSUES[@]:-}")
DISKS_STR=$(printf '%s\n' "${DISK_STATE[@]:-}")
DETAILS_STR=$(printf '%s\n' "${DETAILS[@]:-}")
STATUS="$STATUS" HOST="$HOST" NOW="$NOW" WARN="$WARN" CRIT="$CRIT" \
    LOAD="$LOAD1 $LOAD5 $LOAD15" CPUS="$CPUS" \
    MEM_PCT="$MEM_PCT" DISKS="$DISKS_STR" \
    ISSUES="$ISSUES_STR" DETAILS="$DETAILS_STR" JSON="$JSON" \
    STATE_DIR="$HEALTH_STATE_DIR" python3 - <<'PY'
import os, json, sys, tempfile

def write_state(state_dir, text):
    # Atomic dashboard state: dir 0700, temp file 0600, os.replace so readers
    # never see a partial document. A failure is ONE stderr line only — never
    # stdout, never part of the JSON — and never changes the run's exit code.
    if not state_dir:
        return
    tmp = None
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=state_dir)
        with os.fdopen(fd, "w") as handle:
            handle.write(text + "\n")
        os.replace(tmp, os.path.join(state_dir, "latest.json"))
        tmp = None
    except Exception as exc:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        print("server-health: state file not written (%s)" % exc, file=sys.stderr)

def findings(env_name):
    parsed = []
    for line in os.environ.get(env_name, "").splitlines():
        if "|" in line:
            lvl, msg = line.split("|", 1)
            parsed.append({"level": lvl, "message": msg})
    return parsed

issues = findings("ISSUES")
details = findings("DETAILS")
disks = []
for line in os.environ.get("DISKS", "").splitlines():
    parts = line.split("|")
    if len(parts) == 3 and parts[1].isdigit():
        disks.append({"mount": parts[0],
                      "used_percent": int(parts[1]),
                      "free_kb": int(parts[2])})
text = json.dumps({
    "host": os.environ["HOST"],
    "time": os.environ["NOW"],
    "status": os.environ["STATUS"],
    "warnings": int(os.environ["WARN"]),
    "critical": int(os.environ["CRIT"]),
    "load": os.environ["LOAD"],
    "cpus": int(os.environ["CPUS"]),
    "memory_percent": int(os.environ["MEM_PCT"]),
    "disks": disks,
    "issues": issues,
    "details": details,
}, indent=2)
write_state(os.environ.get("STATE_DIR", ""), text)
if os.environ.get("JSON") == "1":
    print(text)
PY
if [ "$JSON" = 0 ]; then
    # In --quiet mode a clean run prints nothing at all (cron-friendly).
    if [ "$QUIET" = 1 ] && [ "$CRIT" = 0 ] && [ "$WARN" = 0 ]; then
        :
    else
        printf '\n%s%s%s\n' "$C_DIM" "──────────────────────────────────────────" "$C_RST"
        if [ "$CRIT" -gt 0 ]; then
            printf '%sRESULT: CRITICAL%s — %d critical, %d warning(s)\n' "$C_CRIT" "$C_RST" "$CRIT" "$WARN"
        elif [ "$WARN" -gt 0 ]; then
            printf '%sRESULT: WARNING%s — %d warning(s)\n' "$C_WARN" "$C_RST" "$WARN"
        else
            printf '%sRESULT: OK%s — all checks passed\n' "$C_OK" "$C_RST"
        fi
    fi
fi

[ "$CRIT" -gt 0 ] && exit 2
[ "$WARN" -gt 0 ] && exit 1
exit 0
