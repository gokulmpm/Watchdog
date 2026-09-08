web: gunicorn wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --log-level info
worker: python -m watchdog.run_alert_monitor
