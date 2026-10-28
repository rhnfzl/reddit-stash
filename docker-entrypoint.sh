#!/bin/bash
set -e

CHILD_PID=""

# Function to handle shutdown signals gracefully. A running Python child gets the
# signal too and is waited for, so storage_utils.py --cycle can release its S3 lease.
cleanup() {
    echo "$(date): Received shutdown signal. Exiting gracefully..."
    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -TERM "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
    exit 0
}

# Set up signal handlers for graceful shutdown
trap cleanup SIGTERM SIGINT

# Default values for scheduling
SCHEDULE_MODE=${SCHEDULE_MODE:-"once"}
SCHEDULE_INTERVAL=${SCHEDULE_INTERVAL:-7200}  # Default: 2 hours (7200 seconds)

# Accept both "storage_utils.py --upload" and "python storage_utils.py --upload";
# this script already runs python, so a leading "python" would become "python python".
if [[ "$1" == "python" || "$1" == "python3" ]]; then
    shift
fi

# Get the Python script to run (default: reddit_stash.py)
PYTHON_SCRIPT=${1:-"reddit_stash.py"}
SCRIPT_ARGS="${@:2}"  # All arguments after the first one

# Run the script as a child (bash only runs traps between commands, so a foreground
# child would never be told to stop). Exit code 3 from --cycle means "skipped:
# another runner holds the lease or the last run is recent", which is not a failure.
run_python() {
    python "$PYTHON_SCRIPT" $SCRIPT_ARGS &
    CHILD_PID=$!
    local status=0
    wait "$CHILD_PID" || status=$?
    CHILD_PID=""
    if [ "$status" -eq 3 ] && [[ " $SCRIPT_ARGS " == *" --cycle "* ]]; then
        echo "$(date): Skipped (another runner is active or the last run is recent)"
        status=0
    fi
    return "$status"
}

# Validate schedule interval (must be positive integer)
if ! [[ "$SCHEDULE_INTERVAL" =~ ^[0-9]+$ ]] || [ "$SCHEDULE_INTERVAL" -lt 60 ]; then
    echo "Error: SCHEDULE_INTERVAL must be a positive integer >= 60 seconds"
    exit 1
fi

# Validate schedule mode
if [[ "$SCHEDULE_MODE" != "once" && "$SCHEDULE_MODE" != "periodic" ]]; then
    echo "Error: SCHEDULE_MODE must be 'once' or 'periodic'"
    exit 1
fi

echo "Reddit Stash Docker Container"
echo "=============================="
echo "Schedule Mode: $SCHEDULE_MODE"
echo "Python Script: $PYTHON_SCRIPT"
if [ "$SCHEDULE_MODE" = "periodic" ]; then
    echo "Schedule Interval: $SCHEDULE_INTERVAL seconds ($(($SCHEDULE_INTERVAL / 60)) minutes)"
fi
echo "=============================="

if [ "$SCHEDULE_MODE" = "periodic" ]; then
    echo "$(date): Starting Reddit Stash in periodic mode"

    # Run the first execution immediately
    echo "$(date): Running initial execution..."
    if run_python; then
        echo "$(date): Initial execution completed successfully"
    else
        echo "$(date): Initial execution failed, but continuing with schedule..."
    fi

    # Continue with periodic execution
    while true; do
        echo "$(date): Sleeping for $SCHEDULE_INTERVAL seconds..."
        echo "$(date): Next execution scheduled at $(date -d "+$SCHEDULE_INTERVAL seconds" 2>/dev/null || date -r $(($(date +%s) + SCHEDULE_INTERVAL)) 2>/dev/null || echo "$(($SCHEDULE_INTERVAL / 60)) minutes from now")"

        # Use a loop to handle interruptions during sleep
        remaining=$SCHEDULE_INTERVAL
        while [ $remaining -gt 0 ]; do
            if [ $remaining -gt 60 ]; then
                sleep 60
                remaining=$((remaining - 60))
            else
                sleep $remaining
                remaining=0
            fi
        done

        echo "$(date): Running scheduled execution..."
        if run_python; then
            echo "$(date): Scheduled execution completed successfully"
        else
            echo "$(date): Scheduled execution failed, continuing with next cycle..."
        fi
    done
else
    echo "$(date): Running Reddit Stash once..."
    run_python
    echo "$(date): Single execution completed"
fi