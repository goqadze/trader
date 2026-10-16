#!/usr/bin/env bash
# Deploy what is committed (HEAD) to the server: the steps of CLAUDE.md's Deploy section in one command.
#   preflight (HEAD pushed, nothing staged, server checkout clean) → DB backup → copy the code → build the named
#   services → server-image tests (trading-service) → restart only when asked, at a quiet moment → verify.
# Uncommitted edits on the Mac never go: the code is copied from a `git archive` of HEAD. rsync never runs with
# --delete (it once deleted a server-only dump); files the repo deleted since the server's commit are listed, to be
# removed on the server by hand.
#
#   scripts/deploy.sh --dry-run frontend                   what would change; touches nothing on the server
#   scripts/deploy.sh frontend trading-service             backup, copy, build, tests; no restart
#   scripts/deploy.sh --restart frontend trading-service   ...then restart them (refuses at a risky moment) and verify
#   scripts/deploy.sh restart trading-service              only the restart + verify, after a deploy without --restart
#   scripts/deploy.sh moment                               is now a quiet moment to restart trading-service?
#   scripts/deploy.sh verify                               check the server
# --expect "text" (with --restart, restart or verify): the new frontend bundle must contain that text.
#
# The server is $SERVER, or SERVER=user@host in the git-ignored .deploy.local (address: CLAUDE.local.md).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .deploy.local ]; then . ./.deploy.local; fi
: "${SERVER:?set SERVER=user@host, or put SERVER=... in .deploy.local}"

SERVICES="trading-service decision-service backtest-service frontend watchdog"
LOGS="${TMPDIR:-/tmp}"
EXPECT=""
remote() { ssh -o BatchMode=yes "$SERVER" "cd trading && $1"; }
say() { printf '\n== %s\n' "$*"; }
die() { printf 'deploy: %b\n' "$*" >&2; exit 1; }

# The server's checks, as one script run there: a line per check, "FAIL ..." for a problem; exits 1 on any.
verify() {
  say "verify"
  local out
  out=$(ssh -o BatchMode=yes "$SERVER" "EXPECT=$(printf %q "$EXPECT") HEAD_SHA=$(git rev-parse HEAD) bash -s" <<'EOF'
# Every docker command reads </dev/null: this script itself arrives on stdin, and `docker compose exec` would eat it
cd trading
bad=0
fail() { echo "FAIL $*"; bad=$((bad + 1)); }
up=0
for c in $(docker compose ps -q </dev/null); do
  set -- $(docker inspect -f '{{.Name}} {{.RestartCount}} {{.State.Status}}' "$c" </dev/null)
  if [ "$2" = 0 ] && [ "$3" = running ]; then up=$((up + 1)); else fail "container ${1#/}: $3, $2 restarts"; fi
done
echo "containers: $up running, 0 restarts"
curl -sf localhost:8002/status | python3 -c '
import datetime as d, json, sys
s = json.load(sys.stdin)
t = s.get("scheduler_last_tick")
age = round((d.datetime.now(d.timezone.utc) - d.datetime.fromisoformat(t.replace("Z", "+00:00"))).total_seconds()) if t else None
sched = "on" if s.get("scheduler_enabled") else "OFF"
live = "allowed" if s.get("live_trading_allowed") else "NOT allowed"
market = "open" if s.get("market_open") else "closed"
print(f"status: scheduler {sched}, last tick {age}s ago, live trading {live}, market {market}")
sys.exit(0 if s.get("scheduler_enabled") and age is not None and age < 180 else 1)
' || fail "trading-service /status: scheduler off, not ticking, or no answer"
wd=$(docker compose exec -T watchdog python watchdog.py --once </dev/null 2>&1) || true
notok=$(printf '%s\n' "$wd" | awk 'NF && $NF != "ok"')
if [ -z "$notok" ]; then echo "watchdog: $(printf '%s\n' "$wd" | grep -c ok) checks ok"; else fail "watchdog: $notok"; fi
tb=$(docker compose logs --since 10m trading-service </dev/null 2>&1 | grep -c Traceback || true)
if [ "$tb" = 0 ]; then echo "logs: no tracebacks in trading-service's last 10 min"; else fail "trading-service logs: $tb tracebacks in 10 min"; fi
at=$(git rev-parse HEAD)
extra=$(git status --porcelain | grep -vE '^\?\? (\.claude/|trading-[0-9-]+\.sql\.gz$)' || true)
if [ "$at" = "$HEAD_SHA" ] && [ -z "$extra" ]; then echo "git: clean at ${at:0:7}"; else fail "git at ${at:0:7} (Mac HEAD ${HEAD_SHA:0:7}), changes: $extra"; fi
if [ -n "$EXPECT" ]; then
  if docker compose exec -T frontend sh -c 'grep -lF "$1" /usr/share/nginx/html/assets/*.js' sh "$EXPECT" </dev/null >/dev/null; then
    echo "bundle: has \"$EXPECT\""
  else fail "bundle: no \"$EXPECT\" in /usr/share/nginx/html/assets/*.js"; fi
fi
if [ "$bad" = 0 ]; then echo "verify: all ok"; else echo "verify: $bad problem(s)"; exit 1; fi
EOF
  ) || { printf '%s\n' "$out"; return 1; }
  printf '%s\n' "$out"
  [[ "$out" == *"verify: all ok"* ]] || { echo "verify: the checks stopped before the end"; return 1; }
}

# A restart of trading-service (or decision-service) mid-check could cut a decision short. Quiet = market closed,
# or open but: not 10:00-10:30 / 15:30-16:00 New York, 5-12 minutes past a quarter hour (the 15-minute checks are
# done), and no bot with an order still open. Prints why; returns 1 when it isn't quiet.
quiet_moment() {
  local now hm qm state open pending
  now=$(TZ=America/New_York date +%H%M)
  hm=$((10#$now)); qm=$(( 10#${now:2:2} % 15 ))
  state=$(remote "curl -sf localhost:8002/status | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"market_open\"])'; \
    curl -sf localhost:8002/bots | python3 -c 'import json,sys; print(sum(1 for b in json.load(sys.stdin) if b.get(\"pending_order\")))'")
  open=$(echo "$state" | sed -n 1p); pending=$(echo "$state" | sed -n 2p)
  if [ "$open" != True ]; then echo "quiet: market closed (New York $now)"; return 0; fi
  if (( (hm >= 1000 && hm < 1030) || (hm >= 1530 && hm < 1600) )); then
    echo "not quiet: New York $now is in the 10:00-10:30 or 15:30-16:00 decision window"; return 1; fi
  if (( qm < 5 || qm > 12 )); then
    echo "not quiet: New York $now, a 15-minute check may be running (restart 5-12 min past a quarter hour)"; return 1; fi
  if [ "$pending" != 0 ]; then echo "not quiet: ${pending:-?} bot(s) with an open order"; return 1; fi
  echo "quiet: market open, New York $now, between checks, no open orders"
}

restart() {
  if [[ " $* " == *" trading-service "* || " $* " == *" decision-service "* ]]; then
    quiet_moment || die "not restarting now; try again in a few minutes: scripts/deploy.sh restart $*"
  fi
  say "restart $*"
  remote "docker compose up -d --no-deps $*" > "$LOGS/deploy-restart.log" 2>&1 \
    || { cat "$LOGS/deploy-restart.log"; die "docker compose up failed"; }
  grep -E 'Started|Running|Recreated' "$LOGS/deploy-restart.log" | sed 's/^ */  /' || true
  sleep 40  # the scheduler's first tick, and nginx/uvicorn up, before the checks
  verify
}

check_services() {
  [ $# -gt 0 ] || die "name the services, e.g. frontend trading-service (from: $SERVICES)"
  for s in "$@"; do [[ " $SERVICES " == *" $s "* ]] || die "unknown service $s (from: $SERVICES)"; done
}

command=deploy
case "${1:-}" in verify | moment | restart) command=$1; shift ;; esac
dry=0 again=0 services=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1 ;;
    --restart) again=1 ;;
    --expect) EXPECT=${2:?--expect needs a text}; shift ;;
    -*) die "unknown option $1" ;;
    *) services+=("$1") ;;
  esac
  shift
done
case $command in
  verify) verify; exit ;;
  moment) quiet_moment; exit ;;
  restart) check_services "${services[@]+"${services[@]}"}"; restart "${services[@]}"; exit ;;
esac
check_services "${services[@]+"${services[@]}"}"

say "preflight"
git diff --cached --quiet || die "staged changes: commit or unstage them first (the server gets the Mac's git index)"
head=$(git rev-parse HEAD)
GIT_TERMINAL_PROMPT=0 git fetch -q origin 2>/dev/null || echo "  note: couldn't fetch origin"
upstream=$(git rev-parse '@{u}' 2>/dev/null || echo none)
if [ "$upstream" != "$head" ]; then
  if [ $dry = 1 ]; then echo "  note: HEAD isn't pushed"; else die "HEAD ${head:0:7} isn't pushed: git push first"; fi
fi
echo "  deploying ${head:0:7} $(git log -1 --format=%s)"
dirty=$(git status --porcelain --untracked-files=no | wc -l | tr -d ' ')
[ "$dirty" = 0 ] || echo "  $dirty uncommitted file(s) stay on the Mac"
old=$(remote "git rev-parse HEAD")
extra=$(remote "git status --porcelain" | grep -vE '^\?\? (\.claude/|trading-[0-9-]+\.sql\.gz$)' || true)
[ -z "$extra" ] || die "the server's checkout has changes nobody committed:\n$extra"
echo "  server at ${old:0:7}, clean"
gone=$(git diff --no-renames --name-only --diff-filter=D "$old" HEAD 2>/dev/null) \
  || gone="(can't compare: ${old:0:7} isn't in the Mac's history)"

if [ $dry = 0 ]; then
  say "backup the server DB"
  remote "./scripts/backup-trading-db.sh" | tail -n 1
fi

say "copy HEAD's files$([ $dry = 1 ] && echo ' (dry run)' || true)"
tree=$(mktemp -d "$LOGS/deploy-tree.XXXXXX")
trap 'rm -rf "$tree"' EXIT
chmod 755 "$tree"  # rsync -p gives the server's trading/ the mode of this folder
git archive HEAD | tar -x -C "$tree"
n=(); [ $dry = 0 ] || n=(--dry-run)
log="$LOGS/deploy-${head:0:7}.log"
# -c: compare by content (the exported files carry the commit's time, not the Mac's); no -t: keep the server's times
rsync -rlpcz ${n[@]+"${n[@]}"} --itemize-changes --exclude .env "$tree/" "$SERVER:trading/" > "$log"
rsync -az ${n[@]+"${n[@]}"} .git/ "$SERVER:trading/.git/"
changed=$(grep -E '^[<c][fL]' "$log" || true)
echo "  $(printf '%s' "$changed" | grep -c . || true) file(s) to change (list: $log); nothing deleted (rsync never deletes)"
[ -z "$changed" ] || printf '%s\n' "$changed" | sed -n '1,40s/^/  /p'
if [ -n "$gone" ]; then
  echo "  removed from the repo since ${old:0:7}, still on the server (a stale .tsx breaks the frontend build):"
  printf '%s\n' "$gone" | sed 's/^/    /'
  echo "  remove them by hand: ssh \$SERVER \"cd trading && rm <paths>\""
fi
if [ $dry = 1 ]; then printf '\ndry run: nothing changed on the server\n'; exit 0; fi

say "build ${services[*]}"
remote "docker compose build ${services[*]}" > "$log.build" 2>&1 \
  || { tail -n 40 "$log.build"; die "build failed (log: $log.build)"; }
grep -E ' Built' "$log.build" | sed 's/^ */  /' || true

if [[ " ${services[*]} " == *" trading-service "* ]]; then
  say "trading-service tests in the new server image (throwaway SQLite, no keys)"
  remote "docker compose run --rm --no-deps -T -v \"\$PWD/trading-service:/code\" -w /code -e SENTRY_DSN= -e SMTP_HOST= \
    -e ALPACA_PAPER_KEY_ID= -e ALPACA_PAPER_SECRET_KEY= -e ALPACA_LIVE_KEY_ID= -e ALPACA_LIVE_SECRET_KEY= \
    -e ALLOW_LIVE_TRADING=false -e SCHEDULER_ENABLED=false trading-service \
    sh -c 'pip install -q -r requirements-dev.txt && pytest -q -p no:cacheprovider'" > "$log.tests" 2>&1 \
    || { tail -n 30 "$log.tests"; die "server-image tests failed (log: $log.tests); nothing restarted"; }
  tail -n 1 "$log.tests" | sed 's/^/  /'
fi

if [ $again = 1 ]; then
  restart "${services[@]}"
else
  printf '\nbuilt, not restarted. When it is quiet: scripts/deploy.sh restart %s\n' "${services[*]}"
fi
