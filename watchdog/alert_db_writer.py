"""
watchdog/alert_db_writer.py
----------------------------
One row per (foundry_line_id, alert_type, date, shift).

SI alerts
---------
  One row per shift. If the watchdog re-fires for the same shift
  (continuous / window mode) the existing row is UPDATED in place.

  params_json      -- array of non-STABLE parameter details
  raw_values_json  -- snapshot of every parameter value at trigger time

Prescription alerts
-------------------
  One row per shift. Each new batch processed for that shift is
  APPENDED to batches_json so the full shift history lives in one record.

  batches_json     -- array of per-batch prescription results, newest last

UNIQUE KEY: (foundry_line_id, alert_type, date, shift)
"""

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from .engines.si_engine import si_label_to_int, WARNING as _SI_WARNING

logger = logging.getLogger(__name__)


# --- DDL ----------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `watchdog_alerts` (
    `id`                BIGINT        AUTO_INCREMENT PRIMARY KEY,
    `created_at`        TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`        DATETIME      NULL,
    `alert_type`        ENUM('SI','PRESCRIPTION','COMPONENT_CHANGE','BAD_BATCH') NOT NULL,
    `foundry_line_id`   INT           NOT NULL,
    `date`              DATE          NOT NULL,
    `shift`             VARCHAR(10)   NOT NULL DEFAULT '',
    `batch_pkey`        BIGINT        NOT NULL DEFAULT 0,
    `customer_pkey`     BIGINT        NOT NULL DEFAULT 0,

    -- -- SI ------------------------------------------------------------------
    -- period_key is the unique identifier per alert:
    --   shift mode      : "2026-05-31_3"
    --   component mode  : "2026-05-31_3_C53015300010"
    --   dual mode writes both -- they never collide because period_key differs
    `period_key`        VARCHAR(120)  NOT NULL DEFAULT '',
    `si_score`          FLOAT,
    `alert_level`       VARCHAR(30),
    `root_cause`        TEXT,
    `recommendation`    TEXT,
    `params_json`       LONGTEXT,
    `raw_values_json`   LONGTEXT,

    -- -- Prescription (one row per batch) ------------------------------------
    `overall_status`    VARCHAR(20),
    `tolerance`         FLOAT,
    `component_id`      VARCHAR(120),
    `group_name`        VARCHAR(120),
    `batch_time`        DATETIME,
    `deviations_json`   LONGTEXT,

    -- -- Component Change ------------------------------------------------------
    `prev_component_id`   VARCHAR(120),
    `component_info_json` LONGTEXT,

    -- -- Bad Batch -------------------------------------------------------------
    `smc_value`           FLOAT,
    `cosp_value`          FLOAT,
    `smc_cosp_diff`       FLOAT,
    `bb_threshold`        FLOAT,

    -- -- Acknowledgement -----------------------------------------------------
    `acknowledged`      TINYINT(1)    NOT NULL DEFAULT 0,
    `acknowledged_at`   DATETIME,
    `acknowledged_by`   VARCHAR(120)  NULL,
    `ack_reason`        VARCHAR(255)  NULL,
    `ack_remarks`       TEXT          NULL,
    `notified_at`       DATETIME      NULL,

    -- period_key is unique per (foundry_line, alert_type, period)
    -- component mode: period_key includes component_id -> no collision between components
    -- dual mode: component period_key ? shift period_key -> both rows saved
    UNIQUE KEY `uk_period`  (`foundry_line_id`, `alert_type`, `period_key`),
    INDEX      `idx_date`   (`foundry_line_id`, `date`),
    INDEX      `idx_type`   (`alert_type`),
    INDEX      `idx_created`(`created_at`),
    INDEX      `idx_level`  (`alert_level`),
    INDEX      `idx_fl`     (`foundry_line_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def ensure_table(engine: Engine) -> None:
    """Create watchdog_alerts (if new) or migrate it to the current schema."""
    try:
        with engine.begin() as conn:
            conn.execute(text(_CREATE_TABLE_SQL))
        _migrate_table(engine)
        logger.info("watchdog_alerts table ready")
    except Exception as exc:
        logger.error("ensure_table failed: %s", exc)
        raise


def _migrate_table(engine: Engine) -> None:
    """
    Bring an existing watchdog_alerts table in line with the current schema.
    Safe to run multiple times -- all steps are conditional.
    """
    with engine.connect() as conn:
        rows = conn.execute(text("DESCRIBE `watchdog_alerts`")).fetchall()
    existing = {r[0] for r in rows}

    stmts = []

    # Rename si_alert_level -> alert_level (old column name)
    if "si_alert_level" in existing and "alert_level" not in existing:
        stmts.append("ALTER TABLE `watchdog_alerts` CHANGE `si_alert_level` `alert_level` VARCHAR(30)")

    # Add new columns that may not exist yet
    _new_cols = [
        ("updated_at",      "DATETIME NULL AFTER `created_at`"),
        ("batch_pkey",      "BIGINT NOT NULL DEFAULT 0 AFTER `shift`"),
        ("customer_pkey",   "BIGINT NOT NULL DEFAULT 0 AFTER `batch_pkey`"),
        ("recommendation",  "TEXT AFTER `root_cause`"),
        ("params_json",     "LONGTEXT AFTER `recommendation`"),
        ("raw_values_json", "LONGTEXT AFTER `params_json`"),
        ("overall_status",  "VARCHAR(20) AFTER `raw_values_json`"),
        ("tolerance",       "FLOAT AFTER `overall_status`"),
        ("component_id",        "VARCHAR(60) AFTER `tolerance`"),
        ("component_name",      "VARCHAR(255) AFTER `component_id`"),
        ("group_name",          "VARCHAR(120) AFTER `component_name`"),
        ("prev_component_name", "VARCHAR(255) AFTER `prev_component_id`"),
        ("batch_time",      "DATETIME AFTER `group_name`"),
        ("deviations_json",      "LONGTEXT AFTER `batch_time`"),
        ("prev_component_id",    "VARCHAR(120) AFTER `deviations_json`"),
        ("component_info_json",  "LONGTEXT AFTER `prev_component_id`"),
        ("smc_value",            "FLOAT AFTER `component_info_json`"),
        ("cosp_value",           "FLOAT AFTER `smc_value`"),
        ("smc_cosp_diff",        "FLOAT AFTER `cosp_value`"),
        ("bb_threshold",         "FLOAT AFTER `smc_cosp_diff`"),
        ("notified_at",          "DATETIME NULL AFTER `acknowledged_at`"),
        ("acknowledged_by",      "VARCHAR(120) NULL AFTER `acknowledged_at`"),
        ("ack_reason",           "VARCHAR(255) NULL AFTER `acknowledged_by`"),
        ("ack_remarks",          "TEXT NULL AFTER `ack_reason`"),
    ]
    for col, defn in _new_cols:
        if col not in existing:
            stmts.append(f"ALTER TABLE `watchdog_alerts` ADD COLUMN `{col}` {defn}")

    # Extend alert_type ENUM to include new alert types
    with engine.connect() as conn:
        enum_rows = conn.execute(text("DESCRIBE `watchdog_alerts`")).fetchall()
    enum_map = {r[0]: r[1] for r in enum_rows}
    if "alert_type" in enum_map:
        cur_enum = str(enum_map["alert_type"])
        if "COMPONENT_CHANGE" not in cur_enum or "BAD_BATCH" not in cur_enum \
                or "SIEVE_CHANGE" not in cur_enum:
            stmts.append(
                "ALTER TABLE `watchdog_alerts` MODIFY COLUMN `alert_type` "
                "ENUM('SI','PRESCRIPTION','COMPONENT_CHANGE','BAD_BATCH','SIEVE_CHANGE') NOT NULL"
            )

    # Migrate period_key column: widen to VARCHAR(120) and NOT NULL if needed
    with engine.connect() as conn:
        col_rows = conn.execute(text("DESCRIBE `watchdog_alerts`")).fetchall()
    col_map = {r[0]: r[1] for r in col_rows}   # col_name -> col_type
    if "period_key" in col_map:
        ctype = str(col_map["period_key"]).lower()
        if "varchar(60)" in ctype or "varchar(120)" not in ctype:
            stmts.append(
                "ALTER TABLE `watchdog_alerts` "
                "MODIFY `period_key` VARCHAR(120) NOT NULL DEFAULT ''"
            )

    # Migrate component_id: widen to VARCHAR(120)
    if "component_id" in col_map:
        ctype2 = str(col_map["component_id"]).lower()
        if "varchar(60)" in ctype2:
            stmts.append(
                "ALTER TABLE `watchdog_alerts` "
                "MODIFY `component_id` VARCHAR(120)"
            )

    # Rebuild UNIQUE KEY to use period_key instead of (date, shift, batch_pkey)
    # period_key uniquely identifies each alert:
    #   shift mode      : "2026-05-31_3"
    #   component mode  : "2026-05-31_3_C53015300010"
    # Both modes can coexist -- period_key never collides between them.
    with engine.connect() as conn:
        idx_rows = conn.execute(text("SHOW INDEX FROM `watchdog_alerts`")).fetchall()
    index_cols: dict = {}
    for r in idx_rows:
        index_cols.setdefault(r[2], set()).add(r[4])

    uk_cols = index_cols.get("uk_period", set())
    if "period_key" not in uk_cols:
        if "uk_period" in index_cols:
            stmts.append("ALTER TABLE `watchdog_alerts` DROP INDEX `uk_period`")
        stmts.append(
            "ALTER TABLE `watchdog_alerts` "
            "ADD UNIQUE KEY `uk_period` (`foundry_line_id`, `alert_type`, `period_key`)"
        )
    # Add date index if missing
    if "idx_date" not in index_cols:
        stmts.append(
            "ALTER TABLE `watchdog_alerts` "
            "ADD INDEX `idx_date` (`foundry_line_id`, `date`)"
        )

    # Drop old columns that no longer belong
    _old_cols = [
        "param_name", "param_si_score", "param_alert_level",
        "var_label", "drift_label", "osc_label", "deviation_status",
        "batch_timestamp", "prescription_param", "param_label",
        "prescribed_value", "actual_value", "diff",
        "batches_json",   # replaced by per-batch deviations_json
    ]
    for col in _old_cols:
        if col in existing:
            stmts.append(f"ALTER TABLE `watchdog_alerts` DROP COLUMN `{col}`")

    if not stmts:
        return

    logger.info("Migrating watchdog_alerts: %d change(s)", len(stmts))
    with engine.begin() as conn:
        for stmt in stmts:
            try:
                conn.execute(text(stmt))
                logger.info("  OK: %s", stmt[:80])
            except Exception as exc:
                logger.warning("  SKIP: %s -- %s", stmt[:80], exc)


# --- SI Alert -----------------------------------------------------------------

_SI_UPSERT = text("""
    INSERT INTO `watchdog_alerts` (
        `alert_type`, `foundry_line_id`, `date`, `shift`, `customer_pkey`,
        `period_key`, `component_id`, `component_name`,
        `si_score`, `alert_level`,
        `root_cause`, `recommendation`,
        `params_json`, `raw_values_json`
    ) VALUES (
        'SI', :foundry_line_id, :date, :shift, :customer_pkey,
        :period_key, :component_id, :component_name,
        :si_score, :alert_level,
        :root_cause, :recommendation,
        :params_json, :raw_values_json
    )
    ON DUPLICATE KEY UPDATE
        `customer_pkey`    = VALUES(`customer_pkey`),
        `component_name`   = VALUES(`component_name`),
        `si_score`         = VALUES(`si_score`),
        `alert_level`      = VALUES(`alert_level`),
        `root_cause`       = VALUES(`root_cause`),
        `recommendation`   = VALUES(`recommendation`),
        `params_json`      = VALUES(`params_json`),
        `raw_values_json`  = VALUES(`raw_values_json`)
""")


def write_si_alert(
    engine: Engine,
    result: dict,
    foundry_line_id: int,
    display_names: dict = None,
    customer_pkey: int = 0,
    pct_warn: float = 5.0,
) -> int:
    """
    Upsert one SI alert row for the shift.

    params_json -- ALL prepared-sand parameters plus any deviated non-PS params
    stored in background:
    [
      {
        "param":       "ps_active_clay",
        "label":       "Active Clay (%)",   ? human-readable display name
        "source":      "ps",                ? ps | con | add | pse
        "si_score":    78.5,
        "alert_level": "CRITICAL",
        "var_label":   "HIGH VAR",
        "drift_label": "STRONG DRIFT",
        "osc_label":   "STABLE",
        "wdv_label":   "ELEVATED",
        "deviation":   "Deviated High",
        "raw_value":   9.2
      }, ...
    ]
    """
    if not result:
        return 0

    display_names = display_names or {}

    def _label(param: str) -> str:
        bare = param
        for pfx in ("ps_", "con_", "add_", "pse_"):
            if param.startswith(pfx):
                bare = param[len(pfx):]
                break
        return display_names.get(bare) or display_names.get(param) or param

    def _source(param: str) -> str:
        for pfx in ("ps_", "con_", "add_", "pse_"):
            if param.startswith(pfx):
                return pfx.rstrip("_")
        return "other"

    alert_level = result.get("final_alert") or result.get("si_alert")
    si_score    = result.get("si_score_100")

    param_labels = result.get("param_labels",  {})
    param_si     = result.get("param_si",      {})
    var_labels   = result.get("var_labels",    {})
    drift_labels = result.get("drift_labels",  {})
    osc_labels   = result.get("osc_labels",    {})
    wdv_labels   = result.get("wdv_labels",    {})
    deviations   = result.get("deviations",    {})
    raw_values   = result.get("raw_values",    {})
    pct_changes  = result.get("pct_changes",   {})

    # All PS params from raw_values -- always include every prepared-sand parameter
    all_ps = sorted(p for p in raw_values if p.startswith("ps_"))

    # All additive params that were run through the engine (have a key in param_si).
    # Excluded params (e.g. co1 via additive_exclude in config) are never added
    # to param_si, so they are automatically omitted here.
    # Consumption (con_) params are excluded; SI score is PS-only.
    all_add = sorted(p for p in raw_values if p.startswith("add_") and p in param_si)

    params_list = []
    for param in all_ps + all_add:
        dev     = deviations.get(param)
        p_level = param_labels.get(param, "STABLE") or "STABLE"

        # Escalate per-parameter alert to WARNING when SI says STABLE but the
        # parameter has an LCL/UCL breach OR exceeds the % change threshold.
        if si_label_to_int(p_level) == 0:   # currently STABLE
            _has_dev = isinstance(dev, str) and dev.startswith("Deviated")
            _has_pct = abs(_safe_float(pct_changes.get(param)) or 0.0) > pct_warn
            if _has_dev or _has_pct:
                p_level = _SI_WARNING

        params_list.append({
            "param"      : param,
            "label"      : _label(param),
            "source"     : _source(param),
            "si_score"   : _safe_float(param_si.get(param)),
            "alert_level": p_level,
            "var_label"  : var_labels.get(param)   or "STABLE",
            "drift_label": drift_labels.get(param) or "STABLE",
            "osc_label"  : osc_labels.get(param)   or "STABLE",
            "wdv_label"  : wdv_labels.get(param)   or "STABLE",
            "deviation"  : dev if isinstance(dev, str) and dev not in ("OK", "") else "OK",
            "raw_value"  : _safe_float(raw_values.get(param)),
            "pct_change" : _safe_float(pct_changes.get(param)),
        })

    # Sort: PS params first (deviated -> highest SI score), then additive
    params_list.sort(key=lambda x: (
        0 if x["source"] == "ps" else 1,
        0 if x["deviation"] != "OK" else 1,
        -(x["si_score"] or 0),
    ))

    raw_values_clean = {k: _safe_float(v) for k, v in raw_values.items()}

    row = {
        "foundry_line_id": foundry_line_id,
        "date"           : _coerce_date(result.get("date")),
        "shift"          : str(result.get("shift") or ""),
        "customer_pkey"  : customer_pkey,
        "period_key"     : str(result.get("period_key") or ""),
        "component_id"   : str(result.get("component_id") or ""),
        "component_name" : str(result.get("component_name") or ""),
        "si_score"       : _safe_float(si_score),
        "alert_level"    : alert_level,
        "root_cause"     : result.get("root_cause", ""),
        "recommendation" : result.get("recommendation", ""),
        "params_json"    : json.dumps(params_list,      ensure_ascii=False),
        "raw_values_json": json.dumps(raw_values_clean, ensure_ascii=False),
    }

    try:
        with engine.begin() as conn:
            conn.execute(_SI_UPSERT, row)
        logger.info(
            "SI alert upserted  |  period=%s  level=%s  SI=%.1f  params=%d",
            row["period_key"], alert_level, si_score or 0, len(params_list),
        )
        return 1
    except Exception as exc:
        logger.error("write_si_alert failed: %s", exc)
        return 0


# --- Prescription Alert -------------------------------------------------------

_PRESC_UPSERT = text("""
    INSERT INTO `watchdog_alerts` (
        `alert_type`, `foundry_line_id`, `date`, `shift`, `batch_pkey`, `customer_pkey`,
        `period_key`,
        `overall_status`, `tolerance`,
        `component_id`, `group_name`, `batch_time`, `deviations_json`
    ) VALUES (
        'PRESCRIPTION', :foundry_line_id, :date, :shift, :batch_pkey, :customer_pkey,
        :period_key,
        :overall_status, :tolerance,
        :component_id, :group_name, :batch_time, :deviations_json
    )
    ON DUPLICATE KEY UPDATE
        `customer_pkey`   = VALUES(`customer_pkey`),
        `overall_status`  = VALUES(`overall_status`),
        `component_id`    = VALUES(`component_id`),
        `group_name`      = VALUES(`group_name`),
        `batch_time`      = VALUES(`batch_time`),
        `deviations_json` = VALUES(`deviations_json`)
""")


def write_prescription_alert(
    engine: Engine,
    result: dict,
    foundry_line_id: int,
    tolerance_pct: float,
    customer_pkey: int = 0,
) -> int:
    """
    Write one row per batch to watchdog_alerts.
    Re-running --check for the same batch pkey will UPDATE the existing row
    (ON DUPLICATE KEY) -- it will never create a second row for the same batch.

    deviations_json -- array of per-parameter deviation objects:
    [
      {
        "param":      "bentonite",
        "label":      "Bentonite (kg/batch)",
        "prescribed": 12.5,
        "actual":     14.2,
        "diff":       1.7,
        "within":     false
      }, ...
    ]
    """
    if not result:
        return 0

    deviations   = result.get("deviations", [])
    out_of_tol   = [d for d in deviations if not d.get("within", True)]

    # Overall batch severity based on worst per-parameter % deviation
    # ±3% -> CRITICAL, ±2% -> ALERT, ±1% -> WATCH, <1% -> OK
    _SEV_RANK = {"ok": 0, "watch": 1, "alert": 2, "critical": 3}
    worst_sev = "ok"
    for d in out_of_tol:
        sev = d.get("severity", "ok")
        if _SEV_RANK.get(sev, 0) > _SEV_RANK.get(worst_sev, 0):
            worst_sev = sev
    batch_status = worst_sev.upper() if out_of_tol else "OK"

    devs_clean = [
        {
            "param"     : d.get("param"),
            "label"     : d.get("label"),
            "prescribed": _safe_float(d.get("prescribed")),
            "actual"    : _safe_float(d.get("actual")),
            "diff"      : _safe_float(d.get("diff")),
            "pct_diff"  : _safe_float(d.get("pct_diff")),
            "within"    : bool(d.get("within", True)),
            "severity"  : d.get("severity", "ok"),
        }
        for d in deviations
    ]

    alert_date = _coerce_date(result.get("date"))
    shift      = str(result.get("shift") or "")
    batch_pkey = int(result.get("pkey") or 0)

    row = {
        "foundry_line_id": foundry_line_id,
        "date"           : alert_date,
        "shift"          : shift,
        "batch_pkey"     : batch_pkey,
        "customer_pkey"  : customer_pkey,
        "period_key"     : f"PRESC_{batch_pkey}",
        "overall_status" : batch_status,
        "tolerance"      : tolerance_pct,
        "component_id"   : str(result.get("component_id") or ""),
        "group_name"     : str(result.get("group_name") or ""),
        "batch_time"     : _coerce_datetime_str(result.get("timestamp")),
        "deviations_json": json.dumps(devs_clean, ensure_ascii=False),
    }

    try:
        with engine.begin() as conn:
            conn.execute(_PRESC_UPSERT, row)
        if out_of_tol:
            logger.warning(
                "PRESCRIPTION  |  batch=%s  comp=%s  group=%s  shift=%s  out_of_tol=%d",
                batch_pkey, row["component_id"], row["group_name"], shift, len(out_of_tol),
            )
        else:
            logger.debug(
                "PRESCRIPTION OK  |  batch=%s  comp=%s",
                batch_pkey, row["component_id"],
            )
        return 1
    except Exception as exc:
        logger.error("write_prescription_alert failed: %s", exc)
        return 0


# --- Component Change Alert ---------------------------------------------------

_COMP_CHANGE_UPSERT = text("""
    INSERT INTO `watchdog_alerts` (
        `alert_type`, `foundry_line_id`, `date`, `shift`, `batch_pkey`, `customer_pkey`,
        `period_key`,
        `component_id`, `component_name`, `prev_component_id`, `prev_component_name`,
        `group_name`, `batch_time`, `component_info_json`
    ) VALUES (
        'COMPONENT_CHANGE', :foundry_line_id, :date, :shift, :batch_pkey, :customer_pkey,
        :period_key,
        :component_id, :component_name, :prev_component_id, :prev_component_name,
        :group_name, :batch_time, :component_info_json
    )
    ON DUPLICATE KEY UPDATE
        `customer_pkey`        = VALUES(`customer_pkey`),
        `component_id`         = VALUES(`component_id`),
        `component_name`       = VALUES(`component_name`),
        `prev_component_id`    = VALUES(`prev_component_id`),
        `prev_component_name`  = VALUES(`prev_component_name`),
        `group_name`           = VALUES(`group_name`),
        `batch_time`           = VALUES(`batch_time`),
        `component_info_json`  = VALUES(`component_info_json`)
""")


def write_component_change_alert(
    engine: Engine,
    result: dict,
    foundry_line_id: int,
    customer_pkey: int = 0,
) -> int:
    """
    Write one COMPONENT_CHANGE alert row per batch where component_id switched.

    component_info_json stores:
    {
      "component_weight_kg": 12.5,
      "smr":                 4.2,
      "group_name":          "Head",
      "prescription": {
        "bentonite": 14.5, "freshSilicaSand": 3.0, "lca": 2.5, "water": 25.0
      }
    }
    """
    if not result:
        return 0

    batch_pkey = int(result.get("batch_pkey") or 0)
    info       = result.get("component_info") or {}

    row = {
        "foundry_line_id"    : foundry_line_id,
        "date"               : _coerce_date(result.get("date")),
        "shift"              : str(result.get("shift") or ""),
        "batch_pkey"         : batch_pkey,
        "customer_pkey"      : customer_pkey,
        "period_key"         : f"COMP_CHANGE_{batch_pkey}",
        "component_id"       : str(result.get("component_id")           or ""),
        "component_name"     : str(result.get("component_name")         or ""),
        "prev_component_id"  : str(result.get("prev_component_id")      or ""),
        "prev_component_name": str(result.get("prev_component_name")    or ""),
        "group_name"         : str(result.get("group_name")             or ""),
        "batch_time"         : _coerce_datetime_str(result.get("batch_time")),
        "component_info_json": json.dumps(info, ensure_ascii=False),
    }

    try:
        with engine.begin() as conn:
            conn.execute(_COMP_CHANGE_UPSERT, row)
        logger.info(
            "COMPONENT_CHANGE  |  batch=%s  comp=%s -> %s  group=%s  "
            "weight=%s kg  SMR=%s",
            batch_pkey,
            row["prev_component_id"] or "--",
            row["component_id"],
            row["group_name"] or "--",
            info.get("component_weight_kg", "--"),
            info.get("smr", "--"),
        )
        return 1
    except Exception as exc:
        logger.error("write_component_change_alert failed: %s", exc)
        return 0


# --- Bad Batch Alert ----------------------------------------------------------

_BAD_BATCH_UPSERT = text("""
    INSERT INTO `watchdog_alerts` (
        `alert_type`, `foundry_line_id`, `date`, `shift`, `batch_pkey`, `customer_pkey`,
        `period_key`,
        `component_id`, `group_name`, `batch_time`,
        `smc_value`, `cosp_value`, `smc_cosp_diff`, `bb_threshold`,
        `overall_status`
    ) VALUES (
        'BAD_BATCH', :foundry_line_id, :date, :shift, :batch_pkey, :customer_pkey,
        :period_key,
        :component_id, :group_name, :batch_time,
        :smc_value, :cosp_value, :smc_cosp_diff, :bb_threshold,
        :overall_status
    )
    ON DUPLICATE KEY UPDATE
        `customer_pkey`  = VALUES(`customer_pkey`),
        `component_id`   = VALUES(`component_id`),
        `group_name`     = VALUES(`group_name`),
        `batch_time`     = VALUES(`batch_time`),
        `smc_value`      = VALUES(`smc_value`),
        `cosp_value`     = VALUES(`cosp_value`),
        `smc_cosp_diff`  = VALUES(`smc_cosp_diff`),
        `bb_threshold`   = VALUES(`bb_threshold`),
        `overall_status` = VALUES(`overall_status`)
""")


def _bb_severity(diff: float, threshold: float) -> str:
    """Map SMC deviation to severity level matching JS display logic."""
    thr = float(threshold or 2.0)
    abd = abs(float(diff or 0.0))
    if abd > thr * 1.5:  return "CRITICAL"
    if abd > thr:         return "ALERT"
    return "WATCH"


def write_bad_batch_alert(
    engine: Engine,
    result: dict,
    foundry_line_id: int,
    customer_pkey: int = 0,
) -> int:
    """
    Write one BAD_BATCH alert row for a batch where
    |SMC discharge - COSP| exceeds the configured threshold.
    """
    if not result:
        return 0

    batch_pkey = int(result.get("batch_pkey") or 0)
    diff       = result.get("smc_cosp_diff", 0.0)
    direction  = "HIGH" if diff > 0 else "LOW"

    row = {
        "foundry_line_id": foundry_line_id,
        "date"           : _coerce_date(result.get("date")),
        "shift"          : str(result.get("shift") or ""),
        "batch_pkey"     : batch_pkey,
        "customer_pkey"  : customer_pkey,
        "period_key"     : f"BAD_BATCH_{batch_pkey}",
        "component_id"   : str(result.get("component_id") or ""),
        "group_name"     : str(result.get("group_name")   or ""),
        "batch_time"     : _coerce_datetime_str(result.get("batch_time")),
        "smc_value"      : _safe_float(result.get("smc_value")),
        "cosp_value"     : _safe_float(result.get("cosp_value")),
        "smc_cosp_diff"  : _safe_float(diff),
        "bb_threshold"   : _safe_float(result.get("threshold")),
        "overall_status" : _bb_severity(diff, result.get("threshold", 2.0)),
        "alert_level"    : _bb_severity(diff, result.get("threshold", 2.0)),
    }

    try:
        with engine.begin() as conn:
            conn.execute(_BAD_BATCH_UPSERT, row)
        logger.warning(
            "BAD_BATCH  |  batch=%s  comp=%s  SMC=%.2f  COSP=%.2f  diff=%+.2f  "
            "(%s  threshold=±%.2f)",
            batch_pkey, row["component_id"],
            row["smc_value"] or 0, row["cosp_value"] or 0,
            row["smc_cosp_diff"] or 0, direction,
            row["bb_threshold"] or 0,
        )
        return 1
    except Exception as exc:
        logger.error("write_bad_batch_alert failed: %s", exc)
        return 0


# --- Sieve Change Alert -------------------------------------------------------

_SIEVE_CHANGE_UPSERT = text("""
    INSERT INTO `watchdog_alerts` (
        `alert_type`, `foundry_line_id`, `date`, `shift`, `batch_pkey`, `customer_pkey`,
        `period_key`, `alert_level`, `si_score`, `root_cause`, `params_json`
    ) VALUES (
        'SIEVE_CHANGE', :foundry_line_id, :date, :shift, :sieve_pkey, :customer_pkey,
        :period_key, :alert_level, :max_pct_change, :root_cause, :params_json
    )
    ON DUPLICATE KEY UPDATE
        `customer_pkey`   = VALUES(`customer_pkey`),
        `alert_level`     = VALUES(`alert_level`),
        `si_score`        = VALUES(`si_score`),
        `root_cause`      = VALUES(`root_cause`),
        `params_json`     = VALUES(`params_json`)
""")


def write_sieve_change_alert(
    engine: Engine,
    result: dict,
    foundry_line_id: int,
    customer_pkey: int = 0,
) -> int:
    """
    Write one SIEVE_CHANGE alert row when one or more sieve bands changed
    by more than the configured % threshold.

    result keys:
      sieve_pkey      -- pkey of the sieves row that triggered this alert
      date            -- date of the sieve entry
      shift           -- shift of the sieve entry
      changes         -- list of {sand_type, sand_label, sand_short, band, prev, curr, pct_change}
      threshold       -- pct_change_warning threshold used
      max_pct_change  -- max |pct_change| across all flagged bands
    """
    if not result or not result.get("changes"):
        return 0

    sieve_pkey    = int(result.get("sieve_pkey") or 0)
    max_pct       = float(result.get("max_pct_change") or 0)
    changes       = result["changes"]
    threshold     = float(result.get("threshold") or 5.0)

    # Derive a coarse severity label from the max % change
    if max_pct >= threshold * 4:
        alert_level = "CRITICAL"
    elif max_pct >= threshold * 2:
        alert_level = "ALERT"
    elif max_pct >= threshold:
        alert_level = "WATCH"
    else:
        alert_level = "STABLE"

    band_summary = "; ".join(
        f"{c['sand_short']} {c['band']} {c['pct_change']:+.1f}%" for c in changes
    )
    root_cause = f"Sieve % change exceeded ±{threshold}%: {band_summary}"

    changes_with_thr = [{**c, "threshold": threshold} for c in changes]

    row = {
        "foundry_line_id": foundry_line_id,
        "date"           : _coerce_date(result.get("date")),
        "shift"          : str(result.get("shift") or ""),
        "sieve_pkey"     : sieve_pkey,
        "customer_pkey"  : customer_pkey,
        "period_key"     : f"SIEVE_{sieve_pkey}",
        "alert_level"    : alert_level,
        "max_pct_change" : max_pct,
        "root_cause"     : root_cause,
        "params_json"    : json.dumps(changes_with_thr, ensure_ascii=False),
    }

    try:
        with engine.begin() as conn:
            conn.execute(_SIEVE_CHANGE_UPSERT, row)
        logger.warning(
            "SIEVE_CHANGE  |  sieve=%d  bands=%d  max_pct=%.1f%%  level=%s",
            sieve_pkey, len(changes), max_pct, alert_level,
        )
        return 1
    except Exception as exc:
        logger.error("write_sieve_change_alert failed: %s", exc)
        return 0


# --- Helpers ------------------------------------------------------------------

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


def _coerce_datetime_str(v) -> Optional[str]:
    """Return ISO-format datetime string or None."""
    if v is None:
        return None
    try:
        if isinstance(v, str):
            return v
        return v.isoformat()
    except Exception:
        return str(v)
