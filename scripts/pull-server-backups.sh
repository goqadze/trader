#!/usr/bin/env bash
# Copy the server's newest database backup to this machine (backups/server/) and delete copies here older than
# KEEP_DAYS (default 7). A backup that lives only on the server is lost with the server; this keeps a second copy.
# On the Mac, launchd runs it every hour (DEPLOY.md, step 9); once the newest backup is here, a run only checks.
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
# BatchMode: fail instead of waiting for a password prompt nobody sees.
# ServerAlive: a laptop that sleeps mid-download comes back to a dead connection, which without these hangs about
# two hours (the TCP keepalive) before failing; with them it fails within a minute and the next attempt starts over.
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

fail() {
  echo "$(date '+%F %T') FAILED: $1"
  # A failure the next hourly run can fix (the lid closed mid-download, no network yet) only goes to the log.
  # The macOS notification comes once no copy here is from the last 36 hours, i.e. a whole day's backup is missing.
  if [ -z "$(find "$dest" -name 'trading-*.sql.gz' -mmin -$((36 * 60)) | head -1)" ] && command -v osascript >/dev/null; then
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
# rsync only renames a file into place once it has all of it, so a copy here under the final name is complete
[ -f "$dest/$name" ] && exit 0

for attempt in 1 2 3; do
  # -a keeps the server's modification time, so a copy's age is the backup's age.
  # caffeinate keeps the Mac from idle-sleeping during the download (it can't stop a closed lid).
  caffeinate -i rsync -a -e "ssh ${ssh_opts[*]}" "$BACKUP_SERVER:$latest" "$dest/" && break
  [ "$attempt" = 3 ] && fail "download of $name failed"
  sleep 60
done
if ! gzip -t "$dest/$name" 2>/dev/null; then
  mv "$dest/$name" "$dest/$name.bad"  # kept for a look, out of the way of the next download and the cleanup
  fail "$name is damaged"
fi

old=$(find "$dest" -name 'trading-*.sql.gz' -mtime +"$KEEP_DAYS" ! -name "$name" -print -delete | wc -l | tr -d ' ')
echo "$(date '+%F %T') copied $name ($(du -h "$dest/$name" | awk '{print $1}')), deleted $old older than $KEEP_DAYS days"
