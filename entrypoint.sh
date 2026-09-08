#!/bin/bash
set -e

mkdir -p /app/logs

echo "Starting Background Monitor..."
python -m watchdog.run_alert_monitor >> /app/logs/monitor.log 2>> /app/logs/monitor_err.log &
MONITOR_PID=$!

# Forward SIGTERM/SIGINT to the background monitor so it shuts down cleanly
trap "echo 'Shutting down monitor...'; kill $MONITOR_PID 2>/dev/null; wait $MONITOR_PID 2>/dev/null" SIGTERM SIGINT

echo "Starting Dashboard Server on port ${PORT:-9700}..."
exec gunicorn wsgi:application \
    --bind "0.0.0.0:${PORT:-9700}" \
    --workers 2 \
    --timeout 120 \
    --log-level info \
    --access-logfile /app/logs/access.log \
    --error-logfile /app/logs/gunicorn_err.log
