"""
watchdog/property_alert_db.py
-------------------------------
DB table + writer for individual property-based deviation alerts.

Table: property_alerts
  One row per (foundry_line_id, period_key, parameter).
  ON DUPLICATE KEY UPDATE refreshes the alert if the same period is re-analysed.

Alert fields:
  category, parameter, parameter_display
  alert_title, alert_type  (watch | warning | critical)
  current_value, lcl, ucl
  variance_label, drift_label, oscillation_label
  lcl_ucl_breach, breach_direction
  root_cause, recommendation
  date, shift, component_id, mode
"""

import json
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `property_alerts` (
    `id`                 BIGINT       AUTO_INCREMENT PRIMARY KEY,
    `created_at`         DATETIME     NULL,
    `updated_at`         TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                      ON UPDATE CURRENT_TIMESTAMP,

    -- Context
    `foundry_line_id`    INT          NOT NULL,
    `date`               DATE         NOT NULL,
    `shift`              VARCHAR(10)  NOT NULL DEFAULT '',
    `period_key`         VARCHAR(120) NOT NULL DEFAULT '',
    `mode`               VARCHAR(20)  NOT NULL DEFAULT '',
    `component_id`       VARCHAR(120)          DEFAULT NULL,

    -- Property identity
    `category`           VARCHAR(60)  NOT NULL,
    `parameter`          VARCHAR(100) NOT NULL,
    `parameter_display`  VARCHAR(200),

    -- Alert info
    `alert_title`        VARCHAR(255),
    `alert_type`         VARCHAR(20)  NOT NULL DEFAULT 'watch',

    -- Values
    `current_value`      FLOAT,
    `lcl`                FLOAT        DEFAULT NULL,
    `ucl`                FLOAT        DEFAULT NULL,

    -- Engine states
    `variance_label`     VARCHAR(40),
    `drift_label`        VARCHAR(40),
    `oscillation_label`  VARCHAR(40),
    `lcl_ucl_breach`     TINYINT(1)   NOT NULL DEFAULT 0,
    `breach_direction`   VARCHAR(10)  DEFAULT NULL,

    -- Engine scores
    `variance_score`     FLOAT        DEFAULT NULL,
    `drift_score`        FLOAT        DEFAULT NULL,
    `oscillation_score`  FLOAT        DEFAULT NULL,

    -- Trigger count (how many of 3 engines fired)
    `engines_triggered`  INT          NOT NULL DEFAULT 0,

    -- Root cause & recommendation
    `root_cause`         TEXT,
    `recommendation`     TEXT,

    -- Acknowledgement
    `acknowledged`       TINYINT(1)   NOT NULL DEFAULT 0,
    `acknowledged_at`    DATETIME     DEFAULT NULL,

    UNIQUE KEY `uk_prop` (`foundry_line_id`, `period_key`, `parameter`),
    INDEX `idx_pa_date`    (`foundry_line_id`, `date`),
    INDEX `idx_pa_type`    (`alert_type`),
    INDEX `idx_pa_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_UPSERT_SQL = text("""
    INSERT INTO `property_alerts` (
        `foundry_line_id`, `date`, `shift`, `period_key`, `mode`, `component_id`,
        `category`, `parameter`, `parameter_display`,
        `alert_title`, `alert_type`,
        `current_value`, `lcl`, `ucl`,
        `variance_label`, `drift_label`, `oscillation_label`,
        `lcl_ucl_breach`, `breach_direction`,
        `variance_score`, `drift_score`, `oscillation_score`,
        `engines_triggered`,
        `root_cause`, `recommendation`
    ) VALUES (
        :foundry_line_id, :date, :shift, :period_key, :mode, :component_id,
        :category, :parameter, :parameter_display,
        :alert_title, :alert_type,
        :current_value, :lcl, :ucl,
        :variance_label, :drift_label, :oscillation_label,
        :lcl_ucl_breach, :breach_direction,
        :variance_score, :drift_score, :oscillation_score,
        :engines_triggered,
        :root_cause, :recommendation
    )
    ON DUPLICATE KEY UPDATE
        `mode`               = VALUES(`mode`),
        `component_id`       = VALUES(`component_id`),
        `category`           = VALUES(`category`),
        `parameter_display`  = VALUES(`parameter_display`),
        `alert_title`        = VALUES(`alert_title`),
        `alert_type`         = VALUES(`alert_type`),
        `current_value`      = VALUES(`current_value`),
        `lcl`                = VALUES(`lcl`),
        `ucl`                = VALUES(`ucl`),
        `variance_label`     = VALUES(`variance_label`),
        `drift_label`        = VALUES(`drift_label`),
        `oscillation_label`  = VALUES(`oscillation_label`),
        `lcl_ucl_breach`     = VALUES(`lcl_ucl_breach`),
        `breach_direction`   = VALUES(`breach_direction`),
        `variance_score`     = VALUES(`variance_score`),
        `drift_score`        = VALUES(`drift_score`),
        `oscillation_score`  = VALUES(`oscillation_score`),
        `engines_triggered`  = VALUES(`engines_triggered`),
        `root_cause`         = VALUES(`root_cause`),
        `recommendation`     = VALUES(`recommendation`)
""")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_property_alerts_table(engine: Engine) -> None:
    """Create property_alerts table if it does not exist."""
    try:
        with engine.begin() as conn:
            conn.execute(text(_CREATE_TABLE_SQL))
        logger.info("property_alerts table ready")
    except Exception as exc:
        logger.error("ensure_property_alerts_table failed: %s", exc)
        raise


def write_property_alerts(
    engine:          Engine,
    alerts:          list[dict],
    foundry_line_id: int,
) -> int:
    """
    Upsert property alert rows.

    Parameters
    ----------
    engine          : SQLAlchemy engine for the foundry DB
    alerts          : list of alert dicts from property_alert_engine.detect_property_deviations()
                      Each dict must have root_cause and recommendation already populated.
    foundry_line_id : int

    Returns
    -------
    Number of rows written.
    """
    if not alerts:
        return 0

    written = 0
    for a in alerts:
        row = {
            "foundry_line_id"  : foundry_line_id,
            "date"             : _coerce_date(a.get("date")),
            "shift"            : str(a.get("shift") or ""),
            "period_key"       : str(a.get("period_key") or ""),
            "mode"             : str(a.get("mode") or "shift"),
            "component_id"     : str(a.get("component_id") or "") or None,

            "category"         : str(a.get("category", "Other")),
            "parameter"        : str(a.get("parameter", "")),
            "parameter_display": str(a.get("parameter_display", "")),

            "alert_title"      : str(a.get("alert_title", "")),
            "alert_type"       : str(a.get("alert_type", "watch")),

            "current_value"    : _safe_float(a.get("current_value")),
            "lcl"              : _safe_float(a.get("lcl")),
            "ucl"              : _safe_float(a.get("ucl")),

            "variance_label"   : str(a.get("variance_label",    "STABLE")),
            "drift_label"      : str(a.get("drift_label",       "STABLE")),
            "oscillation_label": str(a.get("oscillation_label", "STABLE")),
            "lcl_ucl_breach"   : int(bool(a.get("lcl_ucl_breach", False))),
            "breach_direction" : str(a.get("breach_direction") or "") or None,

            "variance_score"   : _safe_float(a.get("variance_score")),
            "drift_score"      : _safe_float(a.get("drift_score")),
            "oscillation_score": _safe_float(a.get("oscillation_score")),

            "engines_triggered": int(a.get("engines_triggered", 0)),

            "root_cause"       : str(a.get("root_cause") or ""),
            "recommendation"   : str(a.get("recommendation") or ""),
        }

        try:
            with engine.begin() as conn:
                conn.execute(_UPSERT_SQL, row)
            written += 1
            logger.info(
                "PROPERTY ALERT  |  period=%-25s  param=%-25s  type=%-8s  engines=%d%s",
                row["period_key"], row["parameter"], row["alert_type"],
                row["engines_triggered"],
                "  [BREACH]" if row["lcl_ucl_breach"] else "",
            )
        except Exception as exc:
            logger.error(
                "write_property_alerts: failed for param=%s period=%s  %s",
                row["parameter"], row["period_key"], exc,
            )

    return written


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _coerce_date(v):
    if v is None:
        return None
    if hasattr(v, "date"):
        return v.date()
    if hasattr(v, "year"):
        return v
    try:
        from datetime import date
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None
