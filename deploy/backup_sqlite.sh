#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/agent-platform}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/agent-platform}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

install -d -m 0750 "$BACKUP_DIR"

backup_one() {
    local source="$1"
    local name="$2"
    if [[ ! -f "$source" ]]; then
        return 0
    fi
    sqlite3 "$source" ".timeout 10000" ".backup '$BACKUP_DIR/${name}_${STAMP}.sqlite3'"
    gzip -f "$BACKUP_DIR/${name}_${STAMP}.sqlite3"
}

backup_one "$APP_DIR/data/app.sqlite3" "app"
backup_one "$APP_DIR/data/app_lg_checkpoints.db" "langgraph"

find "$BACKUP_DIR" -type f -name '*.sqlite3.gz' -mtime "+$KEEP_DAYS" -delete
echo "SQLite backup completed: $BACKUP_DIR"
