"""
wsgi.py — Production entry point for Gunicorn.

Start: gunicorn wsgi:application --bind 0.0.0.0:9700 --workers 2 --timeout 120
"""
import sys
import os
import json
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

# Load config and bootstrap registry engine before gunicorn serves any requests
_cfg_path = Path(os.path.dirname(__file__)) / "watchdog" / "config" / "watchdog_config.json"

from watchdog.alert_server import app as application
import watchdog.alert_server as _srv

if _cfg_path.exists():
    with open(_cfg_path, encoding="utf-8") as f:
        _srv._config = json.load(f)
    _srv._config_path = _cfg_path
    application.secret_key = _srv._config.get("dashboard_secret", "sandman-watchdog-dashboard-2026")

    try:
        from watchdog.config_store import get_registry_engine, ensure_config_table, load_all_configs
        _srv._reg_engine = get_registry_engine(_srv._config)
        if _srv._reg_engine:
            ensure_config_table(_srv._reg_engine)
            db_configs = load_all_configs(_srv._reg_engine)
            if db_configs:
                _srv._config.setdefault("foundry_configs", {}).update(db_configs)
    except Exception as _e:
        print(f"[wsgi] Registry DB bootstrap failed: {_e}", file=sys.stderr)
        _srv._reg_engine = None

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=9700)
