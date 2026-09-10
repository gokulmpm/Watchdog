"""
watchdog/data_flow/run.py
--------------------------
Standalone entry point for the Data Flow Monitor.
Can be started as a daemon thread from run_alert_monitor.py
OR run independently.

To add to run_alert_monitor.py — add these 5 lines:

    from watchdog.data_flow.run import start_data_flow_monitor
    import threading
    t = threading.Thread(target=start_data_flow_monitor, args=(config,), daemon=True)
    t.start()

That's it. Everything else is autonomous.
"""

import logging
import logging.handlers
from pathlib import Path

logger = logging.getLogger(__name__)


def _setup_log() -> None:
    """Add a dedicated data_flow.log file handler."""
    log_dir = Path(__file__).parents[2] / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "data_flow.log"

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    # Attach to data_flow package logger
    pkg_logger = logging.getLogger("watchdog.data_flow")
    pkg_logger.addHandler(handler)
    pkg_logger.setLevel(logging.DEBUG)


def start_data_flow_monitor(config: dict) -> None:
    """
    Full autonomous startup.
    Pass the same config dict used by the rest of the watchdog.

    Storage split:
      foundry_engine   -> caspro_sandman (or whichever foundry DB)
                         stores: source_registry, source_rhythm, data_health, annotations
                         queries: additive, preparedsand, scada_data etc.

      registry_engine  -> sandman_dev (central config DB)
                         used only for: foundry_line table discovery

    Each foundry keeps its OWN data flow tables in its own DB.
    sandman_dev is only used to discover which foundry lines exist.
    """
    from watchdog.pipeline.db_connector import get_engine
    from watchdog.data_flow.monitor     import DataFlowMonitor

    _setup_log()
    try:
        # Foundry engine — caspro_sandman / munjalkiriu_sandman etc.
        # All data flow tables are created HERE (per-foundry, not in sandman_dev)
        foundry_engine = get_engine(config)

        # Registry engine — sandman_dev (only for foundry_line discovery)
        reg_cfg = {
            **config,
            "database": config.get("registry_database", config.get("database", {})),
        }
        registry_engine = get_engine(reg_cfg)

        logger.info("[data_flow] engines connected — starting monitor ...")

        # Pass foundry_engine as BOTH foundry and registry
        # because all data flow tables live in the foundry DB
        monitor = DataFlowMonitor(
            foundry_engine  = foundry_engine,
            registry_engine = foundry_engine,   # data flow tables live in foundry DB; sandman_dev passed via _discovery_engine
            config          = config,
            _discovery_engine = registry_engine, # ← sandman_dev only for line discovery
        )
        monitor.start()

    except Exception as exc:
        logger.error("[data_flow] failed to start: %s", exc)
