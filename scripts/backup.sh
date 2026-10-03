#!/bin/bash
# Nightly backup (root crontab: 0 3 * * * /opt/stacks/scripts/backup.sh >> /var/log/docker-backup.log 2>&1)
#
# Layout on NAS:  $BACKUP_BASE/<YYYY-MM-DD>/{engram,sqlite,stacks,secrets,*_postgres.sql}
# Retention:     7 daily + 4 weekly (weekly = Sunday; kept for 31 days), hardlink-based.
#
# Failure alerts: publishes to ntfy (localhost:10000) with the token in
#                 /opt/secrets/ntfy-backup.token. Setup was:
#                   docker exec -it ntfy ntfy user add backup-alerts
#                   docker exec -it ntfy ntfy access backup-alerts backup-popos rw
#                   docker exec -it ntfy ntfy token add backup-alerts
#                 (server.yml uses auth-default-access: deny-all)
set -euo pipefail

DATE=$(date +%Y-%m-%d)
BACKUP_BASE=/mnt/nas-backup/popos
BACKUP_DIR=$BACKUP_BASE/$DATE
LATEST_LINK=$BACKUP_BASE/latest
NTFY_HOST=http://localhost:10000
NTFY_TOPIC=backup-popos
NTFY_TOKEN_FILE=/opt/secrets/ntfy-backup.token

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ------------------------------------------------------------- Log rotation
# cron appends here forever; rotate on the first run of each month (the
# current day-of-month not yet seen in a rotation stamp) so we don't depend
# on logrotate config living outside the repo. Keeps current + 2 gzip'd.
LOG_FILE=/var/log/docker-backup.log
if [ -f "$LOG_FILE" ] && [ ! -f "/var/log/docker-backup.log-$(date +%Y%m%d).gz" ]; then
    # only rotate on day-of-month <= 3 (first run of the month window)
    if [ "$(date +%-d)" -le 3 ]; then
        keep=2
        find /var/log -maxdepth 1 -name 'docker-backup.log-*.gz' -printf '%f\n' 2>/dev/null \
            | sort | head -n -"$keep" | while read -r f; do rm -f "/var/log/$f"; done
        # write via temp name so a failed gzip can't leave a partial stamp
        # that would suppress the rest of the rotation window
        if gzip -c "$LOG_FILE" > "/var/log/.docker-backup.log-$(date +%Y%m%d).gz.tmp" \
            && mv "/var/log/.docker-backup.log-$(date +%Y%m%d).gz.tmp" \
                  "/var/log/docker-backup.log-$(date +%Y%m%d).gz"; then
            : > "$LOG_FILE"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] rotated log -> docker-backup.log-$(date +%Y%m%d).gz"
        fi
    fi
fi

notify() { # notify <title> <priority> <body>
    local title=$1 priority=$2 body=${3:-}
    local curl_args=(-s --max-time 10 --fail-with-body
        -H "Title: $title" -H "Priority: $priority" -H "Tags: floppy_disk")
    if [ -s "$NTFY_TOKEN_FILE" ]; then
        curl_args+=(-H "Authorization: Bearer $(cat "$NTFY_TOKEN_FILE")")
    fi
    curl "${curl_args[@]}" -d "$body" "$NTFY_HOST/$NTFY_TOPIC" >/dev/null 2>&1 \
        || log "WARN: ntfy notification failed (is ntfy up?)"
}

fail() {
    log "FAILED: $*"
    notify "Backup FAILED" "urgent" "$*"
    exit 1
}
trap 'fail "uncaught error at line $LINENO"' ERR

# rsync_rc <label> <exit-code> — classify an rsync exit code.
#   0  = clean
#   24 = "some files vanished before transfer" — benign (apps rotating caches
#         during the run); log and continue so the symlink/retention still run.
#   anything else (incl. 23 partial transfer) = real failure, abort.
rsync_rc() {
    local label=$1 rc=$2
    case "$rc" in
        0)  ;;
        24) log "WARN: $label rsync: files vanished during transfer (rc 24) — continuing" ;;
        *)  fail "$label rsync failed (rc=$rc)" ;;
    esac
    # Pin the status to 0 for tolerated codes (0 and 24); without this, set -e
    # would act on whatever `log` happened to return and could abort on rc 24.
    return 0
}

# Preflight: abort early if the NAS is unreachable (NFS hang otherwise wedges the script)
timeout 5 ls "$BACKUP_BASE" >/dev/null 2>&1 || fail "NAS unreachable"
mkdir -p "$BACKUP_DIR"

# ---------------------------------------------------------------- Postgres dumps
docker exec immich_postgres pg_dumpall -U immich > "$BACKUP_DIR/immich_postgres.sql" \
    || fail "immich pg_dumpall failed"
docker exec paperless-db-1 pg_dumpall -U paperless > "$BACKUP_DIR/paperless_postgres.sql" \
    || fail "paperless pg_dumpall failed"

# ------------------------------------------------- Consistent SQLite snapshots
# Live WAL'd SQLite DBs MUST NOT be plain-file-copied while the app is writing
# (db + wal/shm copied at different instants => corrupt snapshot). Instead take
# a transactionally consistent snapshot via sqlite3's backup API into the
# backup dir, and exclude the live db (+wal/shm) from the stacks rsync below.
# Restore: copy the snapshot from <backup>/sqlite/... back to the live path.
SQLITE_DB=(
    "/home/johannes/.engram/engram.db"                 # engram memory (abs path! cron runs as root)
    "/opt/stacks/webui/data/webui.db"                  # open webui (chats, config)
    "/opt/stacks/trilium/data/document.db"             # trilium notes
    "/opt/stacks/memos/data/memos_prod.db"
    "/opt/stacks/navidrome/data/navidrome.db"
    "/opt/stacks/freshrss/data/users/johannes/db.sqlite"
    "/opt/stacks/jellyfin/config/data/data/jellyfin.db"
    "/opt/stacks/audiobookshelf/config/absdatabase.sqlite"
    "/opt/stacks/audiobookshelf/storyteller/data/storyteller.db"
    "/opt/stacks/webui/data/vector_db/chroma.sqlite3"  # open webui RAG vectors
    "/opt/stacks/trainlocks/training_log_data/training_log.db"
    # NOT portainer.db: it is not SQLite (bolt-format, root-only perms) and
    # portainer manages its own snapshots; rsync covers it.
)
for db in "${SQLITE_DB[@]}"; do
    rel=${db#/}
    out="$BACKUP_DIR/sqlite/$rel"          # keep full path layout to avoid name clashes
    [ -f "$db" ] || { log "SKIP (missing): $db"; continue; }
    mkdir -p "$(dirname "$out")"
    if python3 - "$db" "$out" <<'PYEOF'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PYEOF
    then
        log "sqlite snapshot ok: $db"
    else
        fail "sqlite snapshot failed: $db"
    fi
done

# ------------------------------------------------ App configs (hardlink-based)
RSYNC_EXCLUDES=(
    --exclude=.local/ --exclude=__pycache__/ --exclude=.npm/ --exclude=.cache/
    --exclude=.git/ --exclude=venv/ --exclude=.venv/ --exclude=node_modules/
    --exclude=ollama/data/ --exclude=llama-cpp/models/
    --exclude=.playwright-out/
    # FreshRSS retry/cache dirs churn zero-byte files constantly; copying them
    # makes rsync exit 24 ("files vanished") and adds nothing to a backup.
    --exclude=freshrss/data/Retry-After/ --exclude=freshrss/data/cache/
)
# Live SQLite files are snapshotted above — never copy them (and their
# wal/shm sidecars) with rsync or we'd overwrite the good snapshot.
# Only DBs under /opt/stacks need an rsync exclude (they live inside the
# transfer root); outside paths (e.g. ~/.engram) aren't copied anyway.
for db in "${SQLITE_DB[@]}"; do
    case "$db" in
        /opt/stacks/*)
            rel=${db#/opt/stacks/}
            RSYNC_EXCLUDES+=(--exclude="$rel" --exclude="$rel-wal" --exclude="$rel-shm")
            ;;
    esac
done

# Resolve 'latest' once: if its target is gone (retention deleted it),
# fall back to a full copy — but say so loudly instead of silently
# paying a 30+ GB rsync every night.
LATEST_RESOLVED=$(readlink -f "$LATEST_LINK" 2>/dev/null || true)
if [ -n "$LATEST_RESOLVED" ] && [ ! -e "$LATEST_LINK/" ]; then
    log "WARN: 'latest' dangling ($LATEST_RESOLVED) — full copy, no hardlinks tonight"
fi

if [ -e "$LATEST_LINK/" ]; then
    # Use the previous backup as a base for hard-linked incremental copy.
    # NOTE: link-dest needs the resolved dir (latest is a symlink); rsync
    # follows it for the link target, only -d handling needs the trailing /.
    rsync -a --delete "${RSYNC_EXCLUDES[@]}" \
        --link-dest="$LATEST_LINK/stacks/" /opt/stacks/ "$BACKUP_DIR/stacks/"
    rsync_rc "stacks" "$?"
    rsync -a --delete --link-dest="$LATEST_LINK/secrets/" /opt/secrets/ "$BACKUP_DIR/secrets/"
    rsync_rc "secrets" "$?"
else
    rsync -a "${RSYNC_EXCLUDES[@]}" /opt/stacks/ "$BACKUP_DIR/stacks/"
    rsync_rc "stacks" "$?"
    rsync -a /opt/secrets/ "$BACKUP_DIR/secrets/"
    rsync_rc "secrets" "$?"
fi

# Update the "latest" symlink to point to today
ln -snf "$DATE" "$LATEST_LINK"

# ---------------------------------------------------------------- Retention
# Daily: keep 7 days. Weekly (Sundays): keep 31 days => ~1 month total reach,
# hardlinks make weeklies nearly free on the NAS.
find "$BACKUP_BASE" -maxdepth 1 -type d -name "????-??-??" -mtime +7 | \
    while read -r d; do
        b=$(basename "$d")
        dow=$(date -d "$b" +%u 2>/dev/null || echo x)   # 7 = Sunday
        if [ "$dow" = "7" ]; then
            age=$(( ( $(date +%s) - $(date -d "$b" +%s) ) / 86400 ))
            if [ "$age" -le 31 ]; then
                continue    # keep weekly backups up to a month
            fi
        fi
        rm -rf "$d"
    done

log "SUCCESS: $BACKUP_DIR"
notify "Backup completed" "low" "$BACKUP_DIR"
