#!/usr/bin/env bash
# Copy the server's newest database backup to this machine (backups/server/) and delete copies here older than
# KEEP_DAYS (default 7). A backup that lives only on the server is lost with the server; this keeps a second copy.
# On the Mac, launchd runs it every morning at 09:00 (DEPLOY.md, step 9).
#
#   BACKUP_SERVER=trader@SERVER_IP ./scripts/pull-server-backups.sh
#
# Old copies are deleted only after a good download, and never the newest one: if the server is gone, the last
# backups stay here instead of aging out one by one.
set -euo pipefail
cd "$(dirname "$0")/.."  # the project root, wherever this is called from (launchd starts in /)
: "${BACKUP_SERVER:?set BACKUP_SERVER=user@host}"
KEEP_DAYS="${KEEP_DAYS:-7}"
dest=backups/server
mkdir -p "$dest"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=20)  # fail instead of waiting for a password prompt nobody sees

fail() {
  echo "$(date '+%F %T') FAILED: $1"
  # a macOS notification, so a failure doesn't go unnoticed in a log file
  if command -v osascript >/dev/null; then
    osascript -e "display notification \"$1\" with title \"Trading backup copy failed\"" || true
  fi
  exit 1
}

latest=""
for attempt in 1 2 3; do  # right after the Mac wakes up, the network may need a minute
  latest=$(ssh "${ssh_opts[@]}" "$BACKUP_SERVER" 'ls -t trading/backups/trading-*.sql.gz | head -1') && [ -n "$latest" ] && break
  [ "$attempt" = 3 ] || sleep 60
done
[ -n "$latest" ] || fail "can't list the backups on $BACKUP_SERVER"

name=$(basename "$latest")
# -a keeps the server's modification time, so a copy's age is the backup's age
rsync -a -e "ssh ${ssh_opts[*]}" "$BACKUP_SERVER:$latest" "$dest/" || fail "download of $name failed"
if ! gzip -t "$dest/$name" 2>/dev/null; then
  mv "$dest/$name" "$dest/$name.bad"  # kept for a look, out of the way of the next download and the cleanup
  fail "$name is damaged"
fi

old=$(find "$dest" -name 'trading-*.sql.gz' -mtime +"$KEEP_DAYS" ! -name "$name" -print -delete | wc -l | tr -d ' ')
echo "$(date '+%F %T') copied $name ($(du -h "$dest/$name" | awk '{print $1}')), deleted $old older than $KEEP_DAYS days"
