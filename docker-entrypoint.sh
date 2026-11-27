#!/usr/bin/env bash
set -Eeuo pipefail

SCHEDULE_MODE="${SCHEDULE_MODE:-once}"
SCHEDULE_INTERVAL="${SCHEDULE_INTERVAL:-7200}"

if ! [[ "$SCHEDULE_INTERVAL" =~ ^[0-9]+$ ]] || (( SCHEDULE_INTERVAL < 60 )); then
  echo "ERROR: SCHEDULE_INTERVAL must be an integer >= 60 seconds" >&2
  exit 2
fi
if [[ "$SCHEDULE_MODE" != "once" && "$SCHEDULE_MODE" != "periodic" ]]; then
  echo "ERROR: SCHEDULE_MODE must be 'once' or 'periodic'" >&2
  exit 2
fi

# The image CMD is a script path (reddit_stash.py). Docker command overrides may
# be either a script path or an explicit command such as: python storage_utils.py --upload.
if (($# == 0)); then
  COMMAND=(python reddit_stash.py)
elif [[ "$1" == "python" || "$1" == "python3" ]]; then
  COMMAND=("$@")
else
  COMMAND=(python "$@")
fi

child_pid=""
shutdown_requested=0
on_signal() {
  shutdown_requested=1
  echo "$(date -Is): Shutdown signal received" >&2
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  # Do not report a successful exit when an active backup was interrupted.
  exit 143
}
trap on_signal SIGTERM SIGINT

run_once() {
  echo "$(date -Is): Starting: ${COMMAND[*]}"
  "${COMMAND[@]}" &
  child_pid=$!
  local rc=0
  wait "$child_pid" || rc=$?
  child_pid=""
  if (( rc == 0 )); then
    echo "$(date -Is): Execution completed successfully"
  else
    echo "$(date -Is): Execution FAILED with exit code ${rc}" >&2
  fi
  return "$rc"
}

if [[ "$SCHEDULE_MODE" == "once" ]]; then
  # Preserve the Python application's actual exit status for Docker/Compose/CI.
  exec "${COMMAND[@]}"
fi

echo "Reddit Stash scheduler started; interval=${SCHEDULE_INTERVAL}s"
while (( shutdown_requested == 0 )); do
  if ! run_once; then
    echo "$(date -Is): Previous run failed; scheduler will retry next cycle" >&2
  fi
  remaining="$SCHEDULE_INTERVAL"
  while (( remaining > 0 && shutdown_requested == 0 )); do
    step=60
    (( remaining < step )) && step="$remaining"
    sleep "$step" &
    child_pid=$!
    wait "$child_pid" || true
    child_pid=""
    remaining=$((remaining - step))
  done
done
