#!/bin/bash
set -e

DATE=$(date +%Y-%m-%d)
BACKUP_BASE=/mnt/nas-backup/popos
BACKUP_DIR=$BACKUP_BASE/$DATE
LATEST_LINK=$BACKUP_BASE/latest

# Preflight: abort early if the NAS is unreachable (NFS hang otherwise wedges the script)
timeout 5 ls "$BACKUP_BASE" >/dev/null 2>&1 || { echo "NAS unreachable, aborting backup" >&2; exit 1; }

mkdir -p $BACKUP_DIR

# Immich Postgres dump
docker exec immich_postgres pg_dumpall -U immich > $BACKUP_DIR/immich_postgres.sql

# Paperless Postgres dump
docker exec paperless-db-1 pg_dumpall -U paperless > $BACKUP_DIR/paperless_postgres.sql

# Engram memory DB — consistent snapshot (live WAL'd SQLite; plain fs copy may be corrupt)
mkdir -p $BACKUP_DIR/engram
python3 - "$BACKUP_DIR/engram/engram.db" <<'PYEOF'
import os, sqlite3, sys
src = sqlite3.connect(os.path.expanduser('~/.engram/engram.db'))
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
dst.close(); src.close()
PYEOF

# App configs — hardlink unchanged files from last backup
RSYNC_EXCLUDE="--exclude=.local/ --exclude=__pycache__/ --exclude=.npm/ --exclude=.cache/ \
  --exclude=.git/ --exclude=venv/ --exclude=.venv/ --exclude=node_modules/ \
  --exclude=ollama/data/ --exclude=llama-cpp/models/ --exclude=qwen-flash-next/models/"
if [ -d "$LATEST_LINK" ]; then
    # Use the previous backup as a base for hard‑linked incremental copy.
    rsync -av --delete $RSYNC_EXCLUDE --link-dest="$LATEST_LINK/stacks" /opt/stacks/ "$BACKUP_DIR/stacks/"
    rsync -av --delete --link-dest="$LATEST_LINK/secrets" /opt/secrets/ "$BACKUP_DIR/secrets/"
else
    rsync -av $RSYNC_EXCLUDE /opt/stacks/ "$BACKUP_DIR/stacks/"
    rsync -av /opt/secrets/ "$BACKUP_DIR/secrets/"
fi

# Update the "latest" symlink to point to today
ln -snf "$DATE" "$LATEST_LINK"

# Keep only last 7 days
find $BACKUP_BASE -maxdepth 1 -type d -name "????-??-??" -mtime +7 -exec rm -rf {} \;

echo "Backup completed: $BACKUP_DIR"
