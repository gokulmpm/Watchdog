"""
test_bad_batch_alerts.py
------------------------
Manual test: fire one bad batch single-alert (email + webhook)
and one daily summary (email + webhook) for a hardcoded date.

Run from the project root:
    python test_bad_batch_alerts.py
"""

import json
import logging
import sys
from pathlib import Path

# ── logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("test_bad_batch")

# ── load global config ─────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "watchdog" / "config" / "watchdog_config.json"
with open(CONFIG_PATH) as f:
    global_config = json.load(f)

# ── load per-foundry config from DB and merge ──────────────────────────────────
def load_merged_config(foundry_label: str) -> dict:
    from watchdog.pipeline.db_connector import get_engine
    from watchdog.config_store import load_foundry_config

    # get_engine reads config["database"] — point it at the registry DB
    registry_cfg = dict(global_config)
    registry_cfg["database"] = global_config["registry_database"]
    registry_engine = get_engine(registry_cfg)
    foundry_cfg = load_foundry_config(registry_engine, foundry_label)

    # Deep-merge: foundry values override global
    merged = dict(global_config)
    for key, val in foundry_cfg.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **val}
        else:
            merged[key] = val
    return merged


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG — adjust these to match your foundry
# ══════════════════════════════════════════════════════════════════════════════

FOUNDRY_LABEL = "caspro_sandman_L1"   # label in watchdog_si_config
TEST_DATE     = "2026-09-08"           # date that has additive data

# ══════════════════════════════════════════════════════════════════════════════
#  TEST 1 — single bad batch alert (email + webhook)
# ══════════════════════════════════════════════════════════════════════════════

def test_single_bad_batch_alert(config: dict) -> None:
    logger.info("=" * 60)
    logger.info("TEST 1 — Single bad-batch alert (email + webhook)")
    logger.info("=" * 60)

    from watchdog.bad_batch_monitor import BadBatchMonitor

    monitor = BadBatchMonitor(config, label=FOUNDRY_LABEL)

    # Fake result that would have come from _poll()
    fake_result = {
        "batch_pkey"   : 99999,
        "date"         : TEST_DATE,
        "shift"        : "1",
        "component_id" : "TEST-COMP-001",
        "smc_value"    : 47.5,
        "cosp_value"   : 42.0,
        "smc_cosp_diff": 5.5,
        "pct_deviation": 13.1,
        "severity"     : "critical",
        "batch_time"   : f"{TEST_DATE}T10:30:00",
    }

    logger.info("Firing single bad-batch alert for batch_pkey=99999 ...")
    try:
        monitor._send_webhook(fake_result)
        logger.info("  Webhook: done (check webhook_api.log for POST status)")
    except Exception as exc:
        logger.warning("  Webhook failed: %s", exc)

    # Email alert (uses send_bad_batch_shift_summary with single-row data)
    try:
        from watchdog.email_notifier import send_bad_batch_shift_summary
        send_bad_batch_shift_summary(
            config   = config,
            date_str = TEST_DATE,
            shift    = "1",
            total    = 20,
            bad      = 3,
            pct      = 15.0,
            label    = FOUNDRY_LABEL,
        )
        logger.info("  Email: done")
    except Exception as exc:
        logger.warning("  Email failed: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 2 — daily summary (email + webhook) with hardcoded rows
# ══════════════════════════════════════════════════════════════════════════════

def test_daily_summary(config: dict) -> None:
    logger.info("=" * 60)
    logger.info("TEST 2 — Daily summary (email + webhook) for %s", TEST_DATE)
    logger.info("=" * 60)

    # Hardcoded shift rows — edit to match real data if you want
    test_rows = [
        {"shift": "1", "total": 100, "bad": 5,  "pct": 5.0},
        {"shift": "2", "total": 120, "bad": 40, "pct": 33.3},
        {"shift": "3", "total": 90,  "bad": 2,  "pct": 2.2},
    ]

    logger.info("Sending daily summary email ...")
    try:
        from watchdog.email_notifier import send_bad_batch_daily_summary
        ok = send_bad_batch_daily_summary(
            config   = config,
            date_str = TEST_DATE,
            rows     = test_rows,
            label    = FOUNDRY_LABEL,
        )
        logger.info("  Email: %s", "sent" if ok else "skipped (check enabled/recipients)")
    except Exception as exc:
        logger.warning("  Email failed: %s", exc)

    logger.info("Sending daily summary webhook ...")
    try:
        from watchdog.webhook_notifier import send_bad_batch_daily_webhook
        ok = send_bad_batch_daily_webhook(
            config   = config,
            date_str = TEST_DATE,
            rows     = test_rows,
            label    = FOUNDRY_LABEL,
        )
        logger.info("  Webhook: %s", "sent" if ok else "skipped (check enabled/send_bad_batch)")
    except Exception as exc:
        logger.warning("  Webhook failed: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("Loading config for foundry: %s", FOUNDRY_LABEL)
    try:
        config = load_merged_config(FOUNDRY_LABEL)
        logger.info("  bad_batch_watchdog.enabled = %s",
                    config.get("bad_batch_watchdog", {}).get("enabled"))
        logger.info("  webhook.enabled            = %s",
                    config.get("webhook", {}).get("enabled"))
        logger.info("  webhook.send_bad_batch     = %s",
                    config.get("webhook", {}).get("send_bad_batch"))
    except Exception as exc:
        logger.error("Config load failed — using global config only. Error: %s", exc)
        config = global_config

    test_single_bad_batch_alert(config)
    print()
    test_daily_summary(config)
    print()
    logger.info("Done. Check your email inbox and webhook_api.log")
