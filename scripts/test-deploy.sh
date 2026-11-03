#!/usr/bin/env bash
# Tests of scripts/deploy.sh's restart + verify, with no server: a fake ssh runs what would run on the server in a
# throwaway folder, where curl, docker and sleep are fakes too (nothing waits, nothing real is reached). The shell and
# Python that verify sends to the server are the real ones.
#
#   scripts/test-deploy.sh                     test scripts/deploy.sh
#   scripts/test-deploy.sh <edited copy>       test a copy instead, for a mutation check: it must fail
set -euo pipefail
script=${1:-$(dirname "$0")/deploy.sh}

box=$(mktemp -d "${TMPDIR:-/tmp}/test-deploy.XXXXXX")
trap 'rm -rf "$box"' EXIT
mkdir -p "$box/bin" "$box/home" "$box/repo/scripts"
cp "$script" "$box/repo/scripts/deploy.sh"
chmod +x "$box/repo/scripts/deploy.sh"
git -C "$box/repo" init -q
git -C "$box/repo" add .
git -C "$box/repo" -c user.name=test -c user.email=test@example.com -c commit.gpgsign=false commit -qm test
ln -s "$box/repo" "$box/home/trading"

cat > "$box/bin/ssh" <<'EOF'
#!/usr/bin/env bash
# ssh -o BatchMode=yes SERVER "command": runs here, in the fake server's home
shift 3
cd "$BOX/home" && exec bash -c "$*"
EOF

cat > "$box/bin/sleep" <<'EOF'
#!/usr/bin/env bash
echo "sleep $1" >> "$BOX/events"
# A wait that never ends must fail the tests, not hang them
[ "$(wc -l < "$BOX/events")" -lt 500 ] || kill "$PPID"
EOF

# GET /status answers the next line of the ticks file (the last line stays):
# refused = no answer; none = no finished pass yet; old / fresh = a stamp 200 / 5 seconds old
cat > "$box/bin/curl" <<'EOF'
#!/usr/bin/env bash
case "$2" in */bots) echo '[]'; exit 0 ;; esac
n=$(($(cat "$BOX/calls") + 1)); echo "$n" > "$BOX/calls"
tick=$(sed -n "${n}p" "$BOX/ticks"); [ -n "$tick" ] || tick=$(tail -n 1 "$BOX/ticks")
echo "status $tick" >> "$BOX/events"
stamp() {
  python3 -c 'import datetime as d, sys
print("\"" + (d.datetime.now(d.timezone.utc) - d.timedelta(seconds=int(sys.argv[1]))).isoformat().replace("+00:00", "Z") + "\"")' "$1"
}
case "$tick" in
  refused) exit 7 ;;
  none) last=null ;;
  old) last=$(stamp 200) ;;
  fresh) last=$(stamp 5) ;;
esac
echo "{\"scheduler_enabled\":true,\"live_trading_allowed\":true,\"market_open\":false,\"scheduler_last_tick\":$last}"
EOF

cat > "$box/bin/docker" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  "compose ps -q") echo "docker compose ps -q" >> "$BOX/events"; echo c1 ;;
  "inspect "*) echo "/trading-trading-service-1 0 running" ;;
  "compose exec -T watchdog "*) echo "scheduler ok" ;;
  "compose up -d "*) echo "docker $*" >> "$BOX/events"; echo " Container x Started" ;;
esac
EOF
chmod +x "$box"/bin/*

out="" rc=0 fails=0

# run "<ticks>" <deploy.sh arguments>: its output goes in $out, its exit code in $rc
run() {
  printf '%s\n' $1 > "$box/ticks"; shift
  : > "$box/events"; echo 0 > "$box/calls"
  rc=0
  out=$(BOX=$box SERVER=fake-server TMPDIR=$box PATH="$box/bin:$PATH" "$box/repo/scripts/deploy.sh" "$@" 2>&1) || rc=$?
}

check() {  # check "<what>" <command...>
  local what=$1; shift
  if "$@"; then echo "ok    $what"; else
    echo "FAIL  $what"; printf '%s\n' "$out" | sed 's/^/        /'; sed 's/^/        event: /' "$box/events"
    fails=$((fails + 1))
  fi
}
says() { [[ "$out" == *"$1"* ]]; }
never_says() { [[ "$out" != *"$1"* ]]; }
exits() { [ "$rc" = "$1" ]; }
sleeps() { [ "$(grep -c "^sleep $1\$" "$box/events" || true)" = "$2" ]; }
# What happened from the restart on, in order
since_restart() { [ "$(sed -n '/compose up/,$p' "$box/events")" = "$(printf '%s\n' "$@")" ]; }

# The first line of the ticks answers the quiet-moment question, before the restart.

run "fresh refused none none fresh" restart trading-service
check "restart: no failure while the first pass runs" exits 0
check "restart: all ok once it has finished" says "verify: all ok"
check "restart: says how long it waited" says "scheduler: waited 30s for a finished pass"
check "restart: every check comes after the wait" since_restart \
  "docker compose up -d --no-deps trading-service" "sleep 40" \
  "status refused" "sleep 10" "status none" "sleep 10" "status none" "sleep 10" "status fresh" \
  "docker compose ps -q" "status fresh"

run "fresh none old fresh" restart trading-service
check "slow first pass: its old stamp doesn't count, the next pass does" says "waited 20s"
check "slow first pass: all ok" exits 0

run "fresh none" restart trading-service
check "no pass at all: still a failure" exits 1
check "no pass at all: the status check reports it" says "FAIL trading-service /status"
check "no pass at all: gives up after 5 minutes" sleeps 10 30
check "no pass at all: says so" says "waited 300s"
check "no pass at all: the other checks still ran" says "git: clean at"

run "fresh" restart frontend
check "the scheduler didn't restart: no wait" sleeps 10 0
check "the scheduler didn't restart: no wait line" never_says "scheduler: waited"
check "the scheduler didn't restart: all ok" says "verify: all ok"

run "none" verify
check "verify alone: doesn't wait" sleeps 10 0
check "verify alone: reports what it sees now" says "FAIL trading-service /status"
check "verify alone: exit 1" exits 1

run "fresh" verify
check "verify alone on a ticking scheduler: all ok" says "verify: all ok"

if [ "$fails" = 0 ]; then echo "test-deploy: all ok"; else echo "test-deploy: $fails failed"; exit 1; fi
