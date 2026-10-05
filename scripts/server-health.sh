#!/usr/bin/env bash
# server-health.sh — read-only health snapshot for the homelab host.
#
# Everything here is a *probe*: nothing is changed, started, stopped or
# written. It only reads /proc, df, docker inspect, journalctl and the NAS.
#
# Checks
#   host     uptime, load average vs. CPU count
#   memory   RAM + swap pressure (/proc/meminfo)
#   disk     free space and inode usage per real filesystem
#   docker   daemon reachable; unhealthy / stopped / crash-looping containers
#            (idle-managed GPU tenants are expected to be down — see ALLOW_STOPPED)
#   gpu      ROCm temperature / utilisation / power (if rocm-smi exists)
#   systemd  failed units
#   kernel   OOM kills in the last 24h, zombie processes
#   nas      NFS mounts reachable; nightly backup 'latest' freshness
#
# Exit code (cron friendly): 0 = all OK, 1 = warnings, 2 = critical.
#
# Usage
#   server-health.sh                     human-readable report
#   server-health.sh --quiet             only problems (good for cron)
#   server-health.sh --json              machine-readable summary
#   server-health.sh --notify            also push problems to ntfy
#   server-health.sh --quiet --notify    typical cron invocation
#
# Thresholds are environment overridable, e.g.:
#   DISK_WARN=90 MEM_WARN=90 server-health.sh
#
# ntfy setup (same pattern as backup.sh): topic in NTFY_TOPIC (default
# health-popos), token in NTFY_TOKEN_FILE (default /opt/secrets/ntfy-health.token).
#   docker exec -it ntfy ntfy user add health-alerts
#   docker exec -it ntfy ntfy access health-alerts health-popos rw
#   docker exec -it ntfy ntfy token add health-alerts   # copy the token
#   install -m600 /dev/stdin /opt/secrets/ntfy-health.token
# Without a token the script still runs; notify only logs a warning.

set -uo pipefail

# ------------------------------------------------------------- Configuration
HOST=$(hostname)
NOW=$(date '+%Y-%m-%d %H:%M:%S')

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
BACKUP_WARN_DAYS=${BACKUP_WARN_DAYS:-2}
BACKUP_CRIT_DAYS=${BACKUP_CRIT_DAYS:-3}
BACKUP_LATEST=${BACKUP_LATEST:-/mnt/nas-backup/popos/latest}
# Containers that are stopped *intentionally* (idle-managed GPU tenants).
ALLOW_STOPPED=${ALLOW_STOPPED:-"llama-server llama-companion comfyui"}

NTFY_HOST=${NTFY_HOST:-http://localhost:10000}
NTFY_TOPIC=${NTFY_TOPIC:-health-popos}
NTFY_TOKEN_FILE=${NTFY_TOKEN_FILE:-/opt/secrets/ntfy-health.token}

# ----------------------------------------------------------------- CLI flags
QUIET=0 JSON=0 NOTIFY=0 NOTIFY_ALWAYS=0
usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }
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
ISSUES=()   # entries are "LEVEL|message" (for notify + --json)

emit() { # emit <OK|WARN|CRIT|info> <message>
    local lvl=$1; shift
    local msg=$*
    case "$lvl" in
        WARN) WARN=$((WARN + 1)); ISSUES+=("WARN|$msg")
              printf '%s[WARN]%s %s\n' "$C_WARN" "$C_RST" "$msg" ;;
        CRIT) CRIT=$((CRIT + 1)); ISSUES+=("CRIT|$msg")
              printf '%s[CRIT]%s %s\n' "$C_CRIT" "$C_RST" "$msg" ;;
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

# ------------------------------------------------------------- Host overview
read -r LOAD1 LOAD5 LOAD15 _ < /proc/loadavg
CPUS=$(nproc)
MEM_TOTAL_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
MEM_AVAIL_KB=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
SWAP_TOTAL_KB=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)
SWAP_FREE_KB=$(awk '/^SwapFree:/{print $2}' /proc/meminfo)
UPTIME_PRETTY=$(uptime | sed -E 's/^[^,]*up +//; s/, +[0-9]+ users?.*$//')
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
if DISK_OUT=$(timeout 10 df -P -k "${DF_EXCLUDES[@]}" 2>/dev/null); then
    while read -r fs blocks used avail cap mount; do
        [ "$fs" = "Filesystem" ] && continue
        [ -z "${mount:-}" ] && continue
        p=${cap%\%}
        case "$p" in ''|*[!0-9]*) continue ;; esac
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

if INODE_OUT=$(timeout 10 df -Pi "${DF_EXCLUDES[@]}" 2>/dev/null); then
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
fi

# -------------------------------------------------------------------- Docker
section "docker"
if ! docker info >/dev/null 2>&1; then
    emit CRIT "Docker daemon unreachable"
else
    RUNNING=$(docker ps -q 2>/dev/null | wc -l)
    emit OK "Docker daemon up — ${RUNNING} running container(s)"

    # Unhealthy containers.
    while IFS='|' read -r name status; do
        [ -z "$name" ] && continue
        case "$status" in *unhealthy*) emit WARN "container '$name' unhealthy ($status)" ;; esac
    done < <(docker ps --format '{{.Names}}|{{.Status}}' 2>/dev/null)

    # Stopped containers — unless they are intentionally idle-managed. A
    # container that exited a long time ago with no restart policy is stale
    # cruft (e.g. a leftover `hello-world`), not an outage — report it as info
    # so the warning only fires for something that should actually be up.
    IDLE_DOWN=()
    while IFS='|' read -r name state status; do
        [ -z "$name" ] && continue
        skip=0
        for a in $ALLOW_STOPPED; do [ "$name" = "$a" ] && skip=1; done
        if [ "$skip" = 1 ]; then IDLE_DOWN+=("$name"); continue; fi
        info_line=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}|{{.State.FinishedAt}}|{{.Config.Image}}' "$name" 2>/dev/null)
        IFS='|' read -r policy finished image <<< "$info_line"
        fin_epoch=$(date -d "$finished" +%s 2>/dev/null || echo 0)
        [ "$fin_epoch" = 0 ] && age_days=9999 || age_days=$(( ( $(date +%s) - fin_epoch ) / 86400 ))
        if [ "$policy" != "no" ] || [ "$age_days" -lt "$RECENT_EXIT_DAYS" ]; then
            emit WARN "container '$name' not running ($status)"
        else
            emit info "stale exited container '$name' (${image:-?}, ${age_days}d ago) — safe to 'docker rm'"
        fi
    done < <(docker ps -a --filter status=exited --filter status=created \
                --filter status=dead --filter status=paused \
                --format '{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null)
    [ ${#IDLE_DOWN[@]} -gt 0 ] && emit info "idle-managed containers stopped: ${IDLE_DOWN[*]}"

    # Crash loops — a container that keeps restarting is worse than one down.
    while read -r name rc; do
        [ -z "$name" ] && continue
        if [ "${rc:-0}" -ge "$RESTART_WARN" ]; then
            emit WARN "container '$name' restarted ${rc}x (crash loop?)"
        fi
    done < <(docker inspect -f '{{.Name}}|{{.RestartCount}}' $(docker ps -q 2>/dev/null) 2>/dev/null \
             | sed 's#^/##')

    # Running containers not managed by a compose project (informational).
    STRAY=""
    for id in $(docker ps -q 2>/dev/null); do
        proj=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$id" 2>/dev/null)
        if [ -z "$proj" ]; then
            STRAY+=" $(docker inspect -f '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##')"
        fi
    done
    [ -n "$STRAY" ] && emit info "unmanaged running container(s):$STRAY"
fi

# ----------------------------------------------------------------------- GPU
section "gpu"
if command -v rocm-smi >/dev/null 2>&1; then
    if GPU_JSON=$(timeout 15 rocm-smi --json --showtemp --showuse --showpower 2>/dev/null) \
        && [ -n "$GPU_JSON" ]; then
        GPU_LINES=$(GPU_JSON="$GPU_JSON" python3 - <<'PY'
import os, json
try:
    d = json.loads(os.environ["GPU_JSON"])
except Exception:
    raise SystemExit(0)
for card, v in d.items():
    if not isinstance(v, dict):
        continue
    print("%s|%s|%s|%s" % (
        card,
        v.get("Temperature (Sensor edge) (C)", "?"),
        v.get("GPU use (%)", "?"),
        v.get("Average Graphics Package Power (W)", "?"),
    ))
PY
        )
        if [ -z "$GPU_LINES" ]; then
            emit info "rocm-smi returned no GPU data"
        else
            while IFS='|' read -r card t u p; do
                [ -z "$card" ] && continue
                if awk -v t="$t" -v c="$GPU_TEMP_CRIT" 'BEGIN{exit !(t>=c)}'; then
                    emit CRIT "GPU $card ${t}C (util ${u}%, ${p}W) — overheating"
                elif awk -v t="$t" -v c="$GPU_TEMP_WARN" 'BEGIN{exit !(t>=c)}'; then
                    emit WARN "GPU $card ${t}C (util ${u}%, ${p}W) — running hot"
                else
                    emit OK "GPU $card ${t}C, util ${u}%, ${p}W"
                fi
            done <<< "$GPU_LINES"
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
    FAILED=$(systemctl --failed --no-legend --plain 2>/dev/null | awk 'NF{print $1}')
    if [ -n "$FAILED" ]; then
        n=$(printf '%s\n' "$FAILED" | wc -l)
        emit WARN "$n failed systemd unit(s): $(echo $FAILED)"
    else
        emit OK "no failed systemd units"
    fi
else
    emit info "systemctl not available"
fi

# ------------------------------------------------------- Kernel / processes
section "kernel"
if command -v journalctl >/dev/null 2>&1; then
    OOM=$(journalctl -k --since '24 hours ago' --no-pager 2>/dev/null | grep -ci 'oom-kill\|out of memory' || true)
    if [ -z "${OOM:-}" ]; then
        emit info "OOM check skipped (journal not readable)"
    elif [ "$OOM" -gt 0 ]; then
        emit WARN "${OOM} kernel OOM kill(s) in the last 24h"
    else
        emit OK "no kernel OOM kills in the last 24h"
    fi
else
    emit info "journalctl not available"
fi

ZOMBIES=$(ps -eo stat= 2>/dev/null | grep -c '^Z' || true)
if [ "${ZOMBIES:-0}" -gt 5 ]; then
    emit WARN "${ZOMBIES} zombie processes"
else
    emit OK "no significant zombie processes"
fi

# ------------------------------------------------------------------ NAS / backup
section "nas"
for m in /mnt/nas /mnt/nas-backup /mnt/nas-photos /mnt/nas-audiobookshelf; do
    if timeout 5 ls "$m" >/dev/null 2>&1; then
        emit OK "NFS mount $m reachable"
    else
        emit WARN "NFS mount $m unreachable"
    fi
done

if [ -L "$BACKUP_LATEST" ]; then
    target=$(basename "$(readlink -f "$BACKUP_LATEST" 2>/dev/null)")
    if [[ "$target" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        age_days=$(( ( $(date +%s) - $(date -d "$target" +%s) ) / 86400 ))
        if [ "$age_days" -ge "$BACKUP_CRIT_DAYS" ]; then
            emit CRIT "nightly backup stale: latest=$target (${age_days}d old)"
        elif [ "$age_days" -ge "$BACKUP_WARN_DAYS" ]; then
            emit WARN "nightly backup older than expected: latest=$target (${age_days}d old)"
        else
            emit OK "nightly backup fresh: latest=$target (${age_days}d old)"
        fi
        [ -n "$(ls -A "$BACKUP_LATEST/stacks" 2>/dev/null | head -n1)" ] \
            || emit WARN "backup tree looks empty: $BACKUP_LATEST/stacks"
    else
        emit WARN "cannot parse backup date from '$target'"
    fi
else
    emit WARN "backup 'latest' symlink missing ($BACKUP_LATEST)"
fi

# ------------------------------------------------------------------- Summary
if [ "$CRIT" -gt 0 ]; then STATUS=crit; elif [ "$WARN" -gt 0 ]; then STATUS=warn; else STATUS=ok; fi

if [ "$JSON" = 1 ]; then
    ISSUES_STR=$(printf '%s\n' "${ISSUES[@]:-}")
    STATUS="$STATUS" HOST="$HOST" NOW="$NOW" WARN="$WARN" CRIT="$CRIT" \
        LOAD="$LOAD1 $LOAD5 $LOAD15" CPUS="$CPUS" \
        MEM_PCT="$MEM_PCT" DISK_NOTE="$(df -P -k "${DF_EXCLUDES[@]}" 2>/dev/null | awk 'NR>1{print $6"="$5}' | tr '\n' ' ')" \
        ISSUES="$ISSUES_STR" python3 - <<'PY'
import os, json
issues = []
for line in os.environ.get("ISSUES", "").splitlines():
    if "|" in line:
        lvl, msg = line.split("|", 1)
        issues.append({"level": lvl, "message": msg})
print(json.dumps({
    "host": os.environ["HOST"],
    "time": os.environ["NOW"],
    "status": os.environ["STATUS"],
    "warnings": int(os.environ["WARN"]),
    "critical": int(os.environ["CRIT"]),
    "load": os.environ["LOAD"],
    "cpus": int(os.environ["CPUS"]),
    "memory_percent": int(os.environ["MEM_PCT"]),
    "issues": issues,
}, indent=2))
PY
else
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

# ------------------------------------------------------------------- Notify
if [ "$NOTIFY" = 1 ]; then
    if [ ${#ISSUES[@]} -gt 0 ]; then
        if [ "$CRIT" -gt 0 ]; then prio="urgent"; else prio="default"; fi
        title="Health ${STATUS^^}: $HOST"
        body=$(printf '%s\n' "${ISSUES[@]}" | sed 's/^[A-Z]*|//')
        curl_args=(-s --max-time 10 --fail-with-body
            -H "Title: $title" -H "Priority: $prio" -H "Tags: rotating_light")
        [ -s "$NTFY_TOKEN_FILE" ] && curl_args+=(-H "Authorization: Bearer $(cat "$NTFY_TOKEN_FILE")")
        curl "${curl_args[@]}" -d "$body" "$NTFY_HOST/$NTFY_TOPIC" >/dev/null 2>&1 \
            || echo "WARN: ntfy notification failed (is ntfy up? token set?)" >&2
    elif [ "$NOTIFY_ALWAYS" = 1 ]; then
        curl_args=(-s --max-time 10 --fail-with-body
            -H "Title: Health OK: $HOST" -H "Priority: low" -H "Tags: white_check_mark")
        [ -s "$NTFY_TOKEN_FILE" ] && curl_args+=(-H "Authorization: Bearer $(cat "$NTFY_TOKEN_FILE")")
        curl "${curl_args[@]}" -d "all checks passed" "$NTFY_HOST/$NTFY_TOPIC" >/dev/null 2>&1 || true
    fi
fi

[ "$CRIT" -gt 0 ] && exit 2
[ "$WARN" -gt 0 ] && exit 1
exit 0
