#!/bin/bash
# Self-check: an orphaned busy loop must be reported as hung, and as heavy CPU when idle;
# a failed service must show up as a fault.
set -e
dir=$(dirname "$(readlink -f "$0")")
export IDLE_WATCH_STATE=$(mktemp -d)/state.json
(setsid python3 -c 'while 1: pass' & echo $! >"$IDLE_WATCH_STATE.pid")
pid=$(cat "$IDLE_WATCH_STATE.pid"); trap 'kill $pid' EXIT
sleep 3
python3 "$dir/check.py" --no-notify --hung-minutes 0 | grep -q "\"hung:$pid:" || { echo "FAIL hung"; exit 1; }
sleep 21
python3 "$dir/check.py" --no-notify --idle 1 --hung-minutes 999 | grep -q "\"cpu:$pid:" || { echo "FAIL idle cpu"; exit 1; }
python3 "$dir/check.py" --no-notify --hung-minutes 999 | grep -q "\"issues\": \[\]" || { echo "FAIL not idle should be clean"; exit 1; }
# A failed service must be listed, and hidden once acknowledged.
unit=idle-watch-selftest-$$.service
systemd-run --user --quiet --unit="$unit" false; sleep 1
python3 "$dir/check.py" --faults | grep -q "\"unit:user:$unit\"" || { systemctl --user reset-failed "$unit"; echo "FAIL failed unit"; exit 1; }
rm -f "$IDLE_WATCH_STATE"; python3 "$dir/check.py" --no-notify | grep -q "$unit" || { systemctl --user reset-failed "$unit"; echo "FAIL unit in check"; exit 1; }
python3 "$dir/check.py" --fix "unit:user:$unit" --action ack >/dev/null
python3 "$dir/check.py" --no-notify | grep -q "$unit" && { systemctl --user reset-failed "$unit"; echo "FAIL ack in cache"; exit 1; }
python3 "$dir/check.py" --faults | grep -q "$unit" && { systemctl --user reset-failed "$unit"; echo "FAIL ack"; exit 1; }
systemctl --user reset-failed "$unit"
echo OK
