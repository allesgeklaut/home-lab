#!/bin/bash
# Nightly backup (root crontab: 0 3 * * * /opt/scripts/backup.sh >> /var/log/docker-backup.log 2>&1)
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

notify() { # notify <title> <priority> <body>
    local title=$1 priority=$2 body=${3:-}
    local curl_args=(-s --max-time 10
        -H "Title: $title" -H "Priority: $priority" -H "Tags: floppy_disk")
    if [ -f "$NTFY_TOKEN_FILE" ]; then
        curl_args+=(-H "Authorization: Bearer $(cat "$NTFY_TOKEN_FILE")")
    fi
    curl "${curl_args[@]}" -d "$body" "$NTFY_HOST/$NTFY_TOPIC" >/dev/null 2>&1 || true
}

fail() {
    log "FAILED: $*"
    notify "Backup FAILED" "urgent" "$*"
    exit 1
}
trap 'fail "uncaught error at line $LINENO"' ERR

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
RSYNC_EXCLUDE="--exclude=.local/ --exclude=__pycache__/ --exclude=.npm/ --exclude=.cache/ \
--exclude=.git/ --exclude=venv/ --exclude=.venv/ --exclude=node_modules/ \
--exclude=ollama/data/ --exclude=llama-cpp/models/ \
--exclude=.playwright-out/"
# Live SQLite files are snapshotted above — never copy them (and their
# wal/shm sidecars) with rsync or we'd overwrite the good snapshot.
for db in "${SQLITE_DB[@]}"; do
    RSYNC_EXCLUDE+="--exclude=${db#/opt/stacks/} --exclude=${db#/opt/stacks/}-wal --exclude=${db#/opt/stacks/}-shm "
done

if [ -e "$LATEST_LINK/" ]; then
    # Use the previous backup as a base for hard-linked incremental copy.
    # NOTE: link-dest needs the resolved dir (latest is a symlink); rsync
    # follows it for the link target, only -d handling needs the trailing /.
    rsync -a --delete $RSYNC_EXCLUDE \
        --link-dest="$LATEST_LINK/stacks/" /opt/stacks/ "$BACKUP_DIR/stacks/" \
        || fail "stacks rsync failed"
    rsync -a --delete --link-dest="$LATEST_LINK/secrets/" /opt/secrets/ "$BACKUP_DIR/secrets/" \
        || fail "secrets rsync failed"
else
    rsync -a $RSYNC_EXCLUDE /opt/stacks/ "$BACKUP_DIR/stacks/" || fail "stacks rsync failed"
    rsync -a /opt/secrets/ "$BACKUP_DIR/secrets/" || fail "secrets rsync failed"
fi

# Update the "latest" symlink to point to today
ln -snf "$DATE" "$LATEST_LINK"

# ---------------------------------------------------------------- Retention
# Daily: keep 7 days. Weekly (Sundays): keep 31 days => ~1 month total reach,
# hardlinks make weeklies nearly free on the NAS.
find "$BACKUP_BASE" -maxdepth 1 -type d -name "????-??-??" -mtime +7 | \
    while read -r d; do
        dow=$(date -d "$(basename "$d")" +%u 2>/dev/null || echo x)   # 7 = Sunday
        age=$(( ( $(date +%s) - $(date -d "$(basename "$d")" +%s) ) / 86400 ))
        if [ "$dow" = "7" ] && [ "$age" -le 31 ]; then
            continue    # keep weekly backups up to a month
        fi
        rm -rf "$d"
    done

log "SUCCESS: $BACKUP_DIR"
notify "Backup completed" "low" "$BACKUP_DIR"