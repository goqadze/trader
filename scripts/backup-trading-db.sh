#!/usr/bin/env bash
# Snapshot the trading history (bots, decisions, orders, equity, audit log) into backups/ and delete
# snapshots older than KEEP_DAYS (default 30). Safe to run while bots are trading: pg_dump reads a
# consistent snapshot without blocking writes.
#
#   ./scripts/backup-trading-db.sh              # or: make backup-trading-db
#   KEEP_DAYS=90 ./scripts/backup-trading-db.sh
#
# On a server, cron runs it daily (see DEPLOY.md). Restore a snapshot with:
#   gunzip -c backups/<file>.sql.gz | docker compose exec -T trading-db psql -U trading trading
set -euo pipefail  # stop on any error, including a failed pg_dump inside the pipe

cd "$(dirname "$0")/.."  # the project root, wherever this is called from (cron starts in $HOME)
KEEP_DAYS="${KEEP_DAYS:-30}"
mkdir -p backups
file="backups/trading-$(date +%Y%m%d-%H%M%S).sql.gz"

# Write to a temporary name first, so a failed or interrupted dump never looks like a good backup
docker compose exec -T trading-db pg_dump -U trading --clean --if-exists trading | gzip > "$file.partial"
mv "$file.partial" "$file"

find backups -name 'trading-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date '+%F %T') saved $file ($(du -h "$file" | cut -f1))"
