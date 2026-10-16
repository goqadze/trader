#!/usr/bin/env bash
# Mutation check: prove a test guards a change. Inside the service's container, copies the code, applies a sed edit
# that undoes (or breaks) the change, and runs the given tests on that copy: they must FAIL. The real files are never
# touched, and the tests use a throwaway SQLite DB with no broker keys (tests/conftest.py).
#
#   scripts/mutation-check.sh trading-service app/dip.py 's/>= fall/> fall/' tests/test_dip.py -k fall
#
# Exit 0 = caught (the tests fail on the mutant), 1 = SURVIVED (they still pass: the test doesn't guard the change),
# 2 = the edit changed nothing, no test matched, or bad usage.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ $# -lt 4 ]; then sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2; fi
svc=$1 file=$2 expr=$3
shift 3
case "$svc" in trading-service | decision-service | backtest-service) ;; *) echo "service: trading-service, decision-service or backtest-service" >&2; exit 2 ;; esac
[ -f "$svc/$file" ] || { echo "no such file: $svc/$file" >&2; exit 2; }

docker compose run --rm --no-deps -T -v "$PWD/$svc:/code" -w /code -e SENTRY_DSN= -e SMTP_HOST= \
  -e MUT_FILE="$file" -e MUT_EXPR="$expr" "$svc" sh -c '
  pip install -q -r requirements-dev.txt >/dev/null 2>&1
  rm -rf /tmp/c && cp -r /code /tmp/c && sed -i "$MUT_EXPR" "/tmp/c/$MUT_FILE"
  if cmp -s "/code/$MUT_FILE" "/tmp/c/$MUT_FILE"; then echo "the sed edit changed nothing in $MUT_FILE"; exit 2; fi
  diff "/code/$MUT_FILE" "/tmp/c/$MUT_FILE" | sed -n "1,8p"
  cd /tmp/c
  rc=0; pytest -q -p no:cacheprovider "$@" > /tmp/out 2>&1 || rc=$?
  tail -n 2 /tmp/out
  case $rc in
    0) echo "SURVIVED: the tests pass on the mutant"; exit 1 ;;
    1) echo "caught: the tests fail on the mutant" ;;
    5) echo "no test matched"; exit 2 ;;
    *) echo "caught, but pytest stopped with code $rc (an import or collection error?): check the edit breaks behavior, not syntax" ;;
  esac' sh "$@"
