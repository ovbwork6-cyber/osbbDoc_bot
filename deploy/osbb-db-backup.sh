#!/usr/bin/env bash
set -euo pipefail

DB_PATH="${OSBB_DB_PATH:-/var/lib/osbb/osbb_acts.db}"
BACKUP_DIR="${OSBB_BACKUP_DIR:-/var/backups/osbb}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

install -d -m 0700 "$BACKUP_DIR"
sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/osbb-$STAMP.db'"
gzip -9 "$BACKUP_DIR/osbb-$STAMP.db"
find "$BACKUP_DIR" -type f -name 'osbb-*.db.gz' -mtime +14 -delete
