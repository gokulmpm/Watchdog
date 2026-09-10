"""
watchdog/foundry_registry.py
------------------------------
Discovers all active foundry databases from the master registry.

Master DB query
---------------
    SELECT DISTINCT c.db_properties
    FROM   customers c
    JOIN   users u ON c.pkey = u.customer_pkey
    WHERE  c.db_properties IS NOT NULL

Each row's db_properties is a JSON string:
    {"url":"jdbc:mysql://host:port/dbname","username":"admin","password":"..."}

Returns a list of fully-formed config dicts, one per (foundry_db, foundry_line_id)
pair, ready to pass to FoundrySIMonitor or prescription_watchdog.monitor.start().
"""

import copy
import json
import logging
import re

from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

_JDBC_RE = re.compile(r"jdbc:mysql://([^:/]+)(?::(\d+))?/([^?&#]+)")

FOUNDRY_DISPLAY_NAMES: dict[str, str] = {
    "caspro_sandman"     : "GPI",
    "munjalkiriu_sandman": "Munjal Kiriu",
    "mcie2_sandman"      : "CIE",
    "mcie_sandman"       : "MCIE",
    "sandman_dev"          : "Sandman",
}

def get_foundry_display_name(db_name: str) -> str:
    """Return the human-readable foundry name for a database name.
    Falls back to the raw db_name when no mapping exists."""
    return FOUNDRY_DISPLAY_NAMES.get(db_name, db_name)

def parse_jdbc_url(url: str) -> tuple[str, int, str]:
    """Parse a JDBC MySQL URL -> (host, port, dbname)."""
    m = _JDBC_RE.search(url.strip())
    if not m:
        raise ValueError(f"Unrecognised JDBC URL: {url!r}")
    host   = m.group(1)
    port   = int(m.group(2)) if m.group(2) else 3306
    dbname = m.group(3).rstrip("/")
    return host, port, dbname

def discover_foundry_databases(registry_db_cfg: dict) -> list[dict]:
    """
    Connect to the master registry DB and return one DB-info dict per foundry.

    Only the database NAME is extracted from the JDBC URL.
    Host, port, user, and password are inherited from registry_db_cfg so that
    all connections go to the same server (local or remote).

    Returns list of:
        { "host", "port", "name", "user", "password" }
    """
    engine = _make_engine(registry_db_cfg)
    sql    = text("""
        SELECT DISTINCT c.pkey AS customer_pkey, c.db_properties
        FROM   customers c
        JOIN   users u ON c.pkey = u.customer_pkey
        WHERE  c.db_properties IS NOT NULL
          AND  TRIM(c.db_properties) != ''
    """)

    raw_rows = []
    try:
        with engine.connect() as conn:
            raw_rows = conn.execute(sql).mappings().all()
    except Exception as exc:
        logger.error("Registry query failed: %s", exc)
        return []

    shared_host = registry_db_cfg.get("host", "localhost")
    shared_port = int(registry_db_cfg.get("port", 3306))
    shared_user = registry_db_cfg.get("user", "root")
    shared_pwd  = registry_db_cfg.get("password", "")

    results:  list[dict] = []
    seen_dbs: set[str]   = set()

    for row in raw_rows:
        props_raw     = row["db_properties"]
        customer_pkey = int(row["customer_pkey"] or 0)
        if not props_raw:
            continue
        try:
            props  = json.loads(props_raw) if isinstance(props_raw, str) else props_raw
            url    = props.get("url", "")
            _, _, dbname = parse_jdbc_url(url)

            if dbname in seen_dbs:
                continue
            seen_dbs.add(dbname)

            results.append({
                "host"         : shared_host,
                "port"         : shared_port,
                "name"         : dbname,
                "user"         : shared_user,
                "password"     : shared_pwd,
                "customer_pkey": customer_pkey,
            })
        except Exception as exc:
            logger.warning("Skipping invalid db_properties %r : %s", props_raw, exc)

    logger.info("Registry: discovered %d unique foundry database(s)", len(results))
    for db in results:
        logger.info("  %s", db["name"])

    return results

def discover_foundry_line_ids(db_cfg: dict) -> list[int]:
    """
    Return all active foundry_line_ids from the `foundry_line` table.

    Falls back to scanning distinct values in `preparedsand` if the
    `foundry_line` table does not exist.

    Returns [] if the database is unreachable or has no active lines.
    """
    dbname = db_cfg.get("name", "?")
    engine = _make_engine(db_cfg)

    # Primary: authoritative active lines
    sql_primary = text("""
        SELECT `pkey` AS foundry_line_id
        FROM   `foundry_line`
        WHERE  `is_active` = 1
        ORDER  BY `pkey`
    """)
    # Fallback: infer from data
    sql_fallback = text("""
        SELECT DISTINCT `foundry_line_id`
        FROM   `preparedsand`
        WHERE  `deleted` = 0
          AND  `foundry_line_id` IS NOT NULL
        ORDER  BY `foundry_line_id`
    """)

    try:
        with engine.connect() as conn:
            try:
                rows = conn.execute(sql_primary).fetchall()
                ids  = [int(r[0]) for r in rows if r[0] is not None]
                if ids:
                    logger.info("%s -- %d active foundry line(s) from foundry_line table: %s",
                                dbname, len(ids), ids)
                    return ids
                logger.info("%s -- foundry_line table empty, falling back to preparedsand", dbname)
            except Exception:
                logger.info("%s -- foundry_line table unavailable, falling back to preparedsand", dbname)

            rows = conn.execute(sql_fallback).fetchall()
            ids  = [int(r[0]) for r in rows if r[0] is not None]
            if not ids:
                logger.info("Skipping %s -- no foundry line data found", dbname)
            return ids
    except Exception as exc:
        logger.info("Skipping %s -- not available (%s)", dbname, _short_err(exc))
        return []

def _short_err(exc: Exception) -> str:
    """Return a compact error description without the full SQLAlchemy stack."""
    msg = str(exc)
    # Extract just the MySQL error message from the verbose SQLAlchemy output
    for line in msg.splitlines():
        line = line.strip()
        if line.startswith("(pymysql.") or line.startswith("(sqlalchemy."):
            return line[:120]
    return msg.splitlines()[0][:120] if msg else str(type(exc).__name__)

_FOUNDRY_DEFAULTS: dict = {
    "monitoring_enabled"   : False,
    "si_alerts_enabled"    : False,
    "property_alerts_enabled": True,
    "dual_mode"            : False,
    "dual_mode_secondary"  : "shift",
    "report_window"        : 5,
    "pct_change_warning"   : 3.0,
    "optimal_variance"     : {},
    "optimal_values"       : {},
    "parameters"           : {"prepared_sand": [], "consumption": [], "additive": [], "prepared_sand_extra": []},
    "display_names"        : {},
    "aggregation": {
        "mode"       : "component",
        "window_size": 10,
        "agg_func"   : "mean",
        "sum_columns": [
            "no_of_moulds", "liq_metal_poured", "total_preparedsand_qty",
            "bentonite", "freshsilicasand", "lca", "water", "returnsand", "core_sand",
        ],
    },
    "baseline": {
        "start_date": "2025-07-01",
        "end_date"  : "2025-07-31",
    },
    "engines": {
        "window"     : 5,
        "drift"      : {"slight_sigma": 1.0, "strong_sigma": 3.0, "slope_sigma": 0.5},
        "oscillation": {"mild_ratio": 0.4, "osc_ratio": 0.7},
        "variance"   : {
            "high_var_ratio"     : 7.0,   # (8x-1)/1 = 7 with (x-a)/a formula
            "score_watch_max"    : 0.35,
            "score_elevated_max" : 0.6,
            "score_high_var_min" : 0.85,
        },
    },
    "si_params"      : {"params": []},
    "si_weights"     : {"variance": 0.25, "drift": 0.30, "oscillation": 0.15,
                        "acceleration": 0.15, "recovery": 0.15},
    "si_param_weights": {},
    "alert_thresholds": {"stable_max": 20, "watch_max": 49, "alert_max": 69},
    "alert_labels"   : {"stable": "STABLE", "watch": "WATCH", "alert": "ALERT", "critical": "CRITICAL"},
    "trigger": {
        "mode"                  : "shift",
        "day_end_hour"          : 6,
        "day_end_minute"        : 0,
        "window_n"              : 10,
        "cooldown_seconds"      : 300,
        "check_interval_seconds": 300,
        "skip_additive"         : True,
    },
    "additive_exclude": ["co1_pct"],
    "prescription_regression": {
        "enabled"         : True,
        "monitored_params": ["bentonite", "freshSilicaSand", "lca"],
        "deviation_metric": "pct_diff",
    },
    "prescription_watchdog": {
        "tolerance"        : 1.0,
        "tolerance_pct"    : 1.0,
        "watch_thr"        : 1.0,
        "alert_thr"        : 3.0,
        "critical_thr"     : 6.0,
        "poll_interval_sec": 30,
        "idle_timeout_min" : 60,
        "output_excel"     : "prescription_alerts.xlsx",
        "skip_zero_batches": True,
    },
    "component_change_watchdog": {
        "enabled"          : True,
        "poll_interval_sec": 30,
        "idle_timeout_min" : 0,
    },
    "bad_batch_watchdog": {
        "enabled"          : True,
        "threshold"        : 2.0,
        "smc_col"          : "compactability_smc_pct",
        "cosp_col"         : "cosp_percentage_pct",
        "poll_interval_sec": 30,
        "idle_timeout_min" : 0,
    },
    "prediction_monitor": {
        "enabled"          : False,
        "grace_minutes"    : 30,
        "poll_interval_sec": 300,
    },
    # Data flow source monitoring toggles.
    # List source names in disabled_sources to stop monitoring them.
    # Available: scada, additive, preparedsand_extra, preparedsand, consumption, rejections
    "data_flow": {
        "disabled_sources": [],
    },
    # Cost saving calculation config.
    # baseline_start / baseline_end: period before SandMan (used to compute baseline rejection %)
    # cost_per_mt: cost of 1 MT of rejection in local currency (default ₹7,000)
    "cost_saving": {
        "enabled"        : False,
        "baseline_start" : "",
        "baseline_end"   : "",
        "cost_per_mt"    : 7000,
        "currency"       : "INR",
    },
    # Phase 2: return sand cycle time (hours).
    # After moulding, return sand re-enters the preparation system after this many hours.
    # Used to define the rolling data window for SI computation.
    # Override per foundry via watchdog_si_config in the registry DB.
    # GPI default = 18h.  Single-shift foundries may be 8h.
    "phase2": {
        "return_sand_hours": 18,
    },

    # Minutes between cron job runs that insert bulk data.
    # Set to 0 for real-time PLC/SCADA sources.
    # When > 0: poll_interval for bad batch and prescription monitors should
    # match this value, and data flow presence windows scale accordingly.
    "cron_interval_minutes": 0,
    # Shift name mapping — varies per foundry (1 shift / 2 shifts / 3 shifts).
    # Override per foundry via watchdog_si_config webhook.shift_names in the DB.
    "webhook": {
        "shift_names": {
            "1": "Morning",
            "2": "Afternoon",
            "3": "Night",
        },
        "send_prediction": False,
    },
}

def build_foundry_config(base_config: dict, db_info: dict, foundry_line_id: int) -> dict:
    """
    Build a complete per-foundry config by:
      1. Starting from infrastructure settings (DB connections, API keys, webhook base, output).
      2. Merging hardcoded per-foundry defaults (_FOUNDRY_DEFAULTS).
      3. Overriding with per-foundry config loaded from watchdog_si_config in the registry DB.

    All per-foundry analysis settings (si_params, engines, thresholds, baseline,
    trigger, watchdog settings) come from the DB. The JSON file supplies only
    infrastructure: DB connections, API keys, webhook base URL, and output paths.
    """
    # Step 1 — infrastructure from JSON (DB connections, keys, service URLs)
    cfg: dict = {
        "dashboard_url"          : base_config.get("dashboard_url", ""),
        "anthropic_api_key"      : base_config.get("anthropic_api_key", ""),
        "groq_api_key"           : base_config.get("groq_api_key", ""),
        "registry_database"      : copy.deepcopy(base_config.get("registry_database", {})),
        "database"               : copy.deepcopy(base_config.get("database", {})),
        "output"                 : copy.deepcopy(base_config.get("output", {})),
        "webhook"                : copy.deepcopy(base_config.get("webhook", {})),
        "notifications"          : copy.deepcopy(base_config.get("notifications", {})),
    }

    # Stamp foundry identity
    cfg["database"].update({
        "host"    : db_info["host"],
        "port"    : db_info["port"],
        "name"    : db_info["name"],
        "user"    : db_info["user"],
        "password": db_info["password"],
    })
    cfg["foundry_line_id"] = foundry_line_id
    cfg["customer_pkey"]   = int(db_info.get("customer_pkey", 0))

    # Step 2 — apply hardcoded per-foundry defaults
    for key, val in _FOUNDRY_DEFAULTS.items():
        cfg[key] = copy.deepcopy(val)

    # Step 3 — override with per-foundry config from watchdog_si_config (DB)
    label = f"{db_info['name']}_L{foundry_line_id}"
    try:
        from .config_store import get_registry_engine, load_foundry_config
        _reg_eng = get_registry_engine(base_config)
        if _reg_eng:
            db_fc = load_foundry_config(_reg_eng, label)
            if db_fc:
                # Scalar overrides
                for key in ("monitoring_enabled", "dual_mode", "dual_mode_secondary", "report_window",
                            "pct_change_warning", "si_alerts_enabled",
                            "property_alerts_enabled", "additive_exclude"):
                    if key in db_fc:
                        cfg[key] = db_fc[key]
                # Full-replace keys
                for key in ("si_params", "si_weights", "si_param_weights",
                            "alert_thresholds", "alert_labels", "optimal_values",
                            "optimal_variance", "baseline", "aggregation",
                            "prescription_regression", "trigger"):
                    if key in db_fc:
                        cfg[key] = copy.deepcopy(db_fc[key])
                # Deep-merge engines (allow partial engine overrides)
                if "engines" in db_fc:
                    for eng_name, eng_overrides in db_fc["engines"].items():
                        cfg.setdefault("engines", {}).setdefault(eng_name, {}).update(
                            copy.deepcopy(eng_overrides)
                        )
                # Watchdog settings — merge so poll_interval etc. can be partial
                for wkey in ("prescription_watchdog", "component_change_watchdog",
                             "bad_batch_watchdog", "prediction_monitor",
                             "sieve_watchdog", "data_flow", "cost_saving"):
                    if wkey in db_fc:
                        cfg.setdefault(wkey, {}).update(copy.deepcopy(db_fc[wkey]))
                # Webhook per-foundry overrides (enabled, foundry_key, send_si, etc.)
                if "webhook" in db_fc:
                    cfg.setdefault("webhook", {}).update(copy.deepcopy(db_fc["webhook"]))
                # Email notification settings
                if "notifications" in db_fc:
                    cfg.setdefault("notifications", {}).update(
                        copy.deepcopy(db_fc["notifications"])
                    )
                logger.debug("build_foundry_config: DB config applied for %s", label)
            else:
                logger.debug("build_foundry_config: no DB config found for %s — using defaults", label)
    except Exception as _exc:
        logger.debug("build_foundry_config: DB config load skipped for %s: %s", label, _exc)

    return cfg

def expand_all_foundries(base_config: dict, skip_enabled_check: bool = False) -> list[tuple[str, dict]]:
    """
    High-level helper used by run_alert_monitor.py.

    1. Reads registry_database from base_config.
    2. Queries master DB for all customer DBs.
    3. For each DB, discovers all foundry_line_ids.
    4. Returns list of (label, foundry_config) tuples.

    If registry_database is missing, falls back to a single entry
    using the main database block + foundry_line_id from config.
    """
    registry_cfg = base_config.get("registry_database")

    if not registry_cfg:
        label = base_config["database"].get("name", "default")
        fl_id = base_config.get("foundry_line_id", 1)
        logger.info(
            "No registry_database configured -- single-foundry mode: %s  line=%d",
            label, fl_id,
        )
        return [(f"{label}_L{fl_id}", base_config)]

    foundry_dbs = discover_foundry_databases(registry_cfg)

    if not foundry_dbs:
        # Registry returned nothing -- could mean db_properties are all NULL in
        # this registry DB, or the registry DB itself is the wrong one.
        # Fall back gracefully to single-foundry mode.
        label = base_config["database"].get("name", "default")
        fl_id = base_config.get("foundry_line_id", 1)
        logger.warning(
            "Registry returned 0 foundry DBs -- falling back to single-foundry mode: "
            "%s  line=%d  "
            "(Check: registry_database in watchdog_config.json should point to the "
            "central platform DB that has db_properties populated in customers table)",
            label, fl_id,
        )
        return [(f"{label}_L{fl_id}", base_config)]

    registry_db_name = registry_cfg.get("name", "").lower()

    # Always include the registry DB itself as a foundry candidate.
    # Deduplicate by name in case it also appears in the customers table.
    existing_names = {db["name"].lower() for db in foundry_dbs}
    if registry_db_name not in existing_names:
        foundry_dbs = list(foundry_dbs) + [{
            "host"         : registry_cfg.get("host", "localhost"),
            "port"         : int(registry_cfg.get("port", 3306)),
            "name"         : registry_cfg.get("name", ""),
            "user"         : registry_cfg.get("user", "root"),
            "password"     : registry_cfg.get("password", ""),
            "customer_pkey": 0,
        }]

    entries: list[tuple[str, dict]] = []
    for db_info in foundry_dbs:
        line_ids = discover_foundry_line_ids(db_info)
        for fl_id in line_ids:
            label  = f"{db_info['name']}_L{fl_id}"
            config = build_foundry_config(base_config, db_info, fl_id)
            if not skip_enabled_check and not config.get("monitoring_enabled", False):
                logger.info("expand_all_foundries: skipping %s (monitoring disabled)", label)
                continue
            entries.append((label, config))

    logger.info(
        "Registry expansion complete: %d monitor instance(s) across %d foundry DB(s)",
        len(entries), len(foundry_dbs),
    )
    return entries

def get_line_names(db_cfg: dict) -> dict:
    """
    Return a mapping of {foundry_line_id: line_name} for all lines in this DB.
    Falls back to an empty dict if the foundry_line table is unavailable.
    """
    engine = _make_engine(db_cfg)
    sql = text("""
        SELECT `pkey`, COALESCE(`name`, `line_name`, `line_code`, '') AS line_name
        FROM   `foundry_line`
        ORDER  BY `pkey`
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return {int(r[0]): str(r[1]) for r in rows if r[0] is not None}
    except Exception:
        return {}

def check_line_has_live_data(db_cfg: dict, foundry_line_id: int, days: int = 30) -> dict:
    """
    Check whether a foundry line has data in preparedsand.

    Parameters
    ----------
    db_cfg           -- database connection dict (host/port/name/user/password)
    foundry_line_id  -- the line to check
    days             -- a line is considered "active" if it has data within the last N days
                       (default 30 -- suitable for local / dev databases)

    Returns
    -------
    {
      "active"           : bool,   True if data exists within `days`
      "has_any_data"     : bool,   True if ANY data exists at all
      "last_date"        : "YYYY-MM-DD" | None,
      "row_count_recent" : int,    rows in the last `days` window
      "error"            : str | None,
    }
    """
    from datetime import date, timedelta

    cutoff = date.today() - timedelta(days=days)
    result = {
        "active"           : False,
        "has_any_data"     : False,
        "last_date"        : None,
        "row_count_recent" : 0,
        "error"            : None,
    }

    sql_last = text("""
        SELECT DATE(MAX(`date`)) AS last_date
        FROM   `preparedsand`
        WHERE  `foundry_line_id` = :fl_id
          AND  `deleted` = 0
    """)
    sql_cnt = text("""
        SELECT COUNT(*) AS cnt
        FROM   `preparedsand`
        WHERE  `foundry_line_id` = :fl_id
          AND  `deleted` = 0
          AND  DATE(`date`) >= :cutoff
    """)

    try:
        engine = _make_engine(db_cfg)
        with engine.connect() as conn:
            row = conn.execute(sql_last, {"fl_id": foundry_line_id}).mappings().first()
            if row and row["last_date"]:
                last = row["last_date"]
                if hasattr(last, "date"):
                    last = last.date()
                result["last_date"]    = str(last)
                result["has_any_data"] = True
                result["active"]       = last >= cutoff

            cnt = conn.execute(sql_cnt, {"fl_id": foundry_line_id, "cutoff": cutoff}).scalar()
            result["row_count_recent"] = int(cnt or 0)
    except Exception as exc:
        result["error"] = _short_err(exc)

    return result

# Columns that are infrastructure / metadata -- never returned as measurement parameters.
# Only actual sand-quality measurement columns survive this filter.
_INFRA_COLS = frozenset({
    # Identity / PKs
    "pkey", "id", "uuid", "foundry_line_id", "foundry_line",
    # Dates and time
    "date", "shift", "date_time", "datetime", "timestamp", "time",
    "created_at", "updated_at", "created_on", "updated_on",
    "last_updated_on", "last_updated_at", "modified_at", "modified_on",
    "entry_date", "entry_time", "record_date", "record_time",
    # Soft delete / active flags
    "deleted", "is_deleted", "active", "is_active",
    # Audit trail -- user tracking columns
    "created_by", "updated_by", "last_updated_by", "modified_by",
    "created_user", "updated_user", "created_user_id", "updated_user_id",
    "quality_inspector", "inspector", "inspector_id", "entered_by",
    "approved_by", "reviewed_by", "checked_by",
    # Versioning
    "version", "version_no", "revision", "rev_no", "rev",
    # Session / auth
    "session_id", "session", "token", "auth_token",
    # Inventory / item
    "inventory", "inventory_id", "item", "item_code", "item_name", "item_id",
    # Component / batch / process tracking (not sand measurements)
    "component_id", "component", "component_name",
    "batch_no", "batch_number", "batch_id", "batch",
    "machine", "machine_id", "machine_name",
    "mixer", "mixer_id", "mixer_name",
    "mould_number", "no_of_moulds", "moulds",
    # Remarks / notes / description
    "remarks", "notes", "comment", "comments", "description", "note", "narration",
    # Approval / workflow status flags
    "status", "is_approved", "approved_at", "workflow_status",
    "sync_status", "sync_at", "export_status",
    # Network / system tracking
    "created_ip", "updated_ip", "ip_address", "device_id",
    # Upload session / inventory detail
    "upload_session_id", "inventory_detail_json",
    # Foundry-process quantities that are NOT sand properties
    "liq_metal_poured", "total_preparedsand_qty", "returnsand", "core_sand",
    "no_of_moulds", "no_of_castings",
})

def get_available_parameters(db_cfg: dict, foundry_line_id: int, param_columns: dict = None) -> dict:
    """
    Return all parameter columns that have at least one non-NULL value for
    the given foundry line.

    Uses a SINGLE database connection for every internal query -- previously
    each helper opened its own connection, adding a full TCP handshake per call
    (~500 ms x 12 calls = 6 s).  Now everything shares one connection.

    Returns
    -------
    {
      "prepared_sand" : ["active_clay", "compactibility", ...],
      "additive"      : ["bentonite", "freshsilicasand", ...],
      "sieve"         : ["fines", "gfn_afs", ...],
      "consumption"   : [...],
      "metal"         : ["pouring_temp", "pouring_time", ...],
    }
    """
    engine = _make_engine(db_cfg)

    _TABLE_EXTRA: dict[str, frozenset] = {
        "additive": frozenset({"component_id", "group_name", "batch_time",
                               "recipe_id", "recipe_name"}),
        "metal"   : frozenset({"pouring_id", "metal_id"}),
    }

    with engine.connect() as conn:

        def _table_exists(table: str) -> bool:
            try:
                conn.execute(text(f"SELECT 1 FROM `{table}` LIMIT 1"))
                return True
            except Exception:
                return False

        def _schema_cols(table: str) -> list[str]:
            """
            Return measurement columns from the table schema only -- no table scan.

            Previously we ran MAX(col) IS NOT NULL per column to confirm data
            existence, but that caused a full-table aggregate on every load
            (~6 s on large tables without an index on foundry_line_id).

            Schema-only discovery is instant (DESCRIBE ? 1 ms).  Columns with
            no data simply contribute NaN->0 in the SI engines, so selecting an
            empty column has no effect on the score.
            """
            exclude = _INFRA_COLS | _TABLE_EXTRA.get(table, frozenset())
            try:
                meta = conn.execute(text(f"DESCRIBE `{table}`")).fetchall()
                return [r[0] for r in meta if r[0] not in exclude]
            except Exception as exc:
                logger.warning("get_available_parameters: DESCRIBE %s -- %s",
                               table, _short_err(exc))
                return []

        def _sieve_band_types() -> list[str]:
            """Sieve params are band_type rows in sieve_band_data, not columns."""
            try:
                rows = conn.execute(text("""
                    SELECT DISTINCT b.band_type
                    FROM   sieve_band_data b
                    JOIN   sieves s ON s.pkey = b.sieve_id
                    WHERE  b.sand_type = 1
                    ORDER  BY b.band_type
                """)).fetchall()
                return [str(r[0]) for r in rows if r[0]]
            except Exception as exc:
                logger.warning("get_available_parameters: sieve_band_data -- %s",
                               _short_err(exc))
                return []

        # -- Prescription params: discovered dynamically from additive table schema --
        def _prescription_params() -> list:
            if not _table_exists("additive"):
                return []
            try:
                all_cols = {r[0] for r in conn.execute(text("DESCRIBE `additive`")).fetchall()}
            except Exception as exc:
                logger.warning("get_available_parameters: DESCRIBE additive -- %s", _short_err(exc))
                return []

            def _find_setpoint_col(stem: str) -> str:
                for suffix in ("_set_point", "_setpoint", "_sp", "_target", "_set"):
                    if f"{stem}{suffix}" in all_cols:
                        return f"{stem}{suffix}"
                return ""

            _pc = param_columns or {}
            available = []
            for act_col in sorted(c for c in all_cols if c.endswith("_actual")):
                stem    = act_col[:-len("_actual")]
                ov      = _pc.get(stem, {})
                eff_act = ov.get("actual_col")   or act_col
                eff_sp  = ov.get("setpoint_col") or _find_setpoint_col(stem)
                label   = ov.get("label")        or stem.replace("_", " ").title()
                try:
                    row = conn.execute(text(
                        f"SELECT 1 FROM `additive` "
                        f"WHERE `foundry_line_id` = :fl_id AND `deleted` = 0 "
                        f"AND `{eff_act}` IS NOT NULL AND `{eff_act}` > 0 LIMIT 1"
                    ), {"fl_id": foundry_line_id}).fetchone()
                    if row:
                        available.append({
                            "key"         : stem,
                            "label"       : label,
                            "actual_col"  : eff_act,
                            "setpoint_col": eff_sp,
                            "column"      : eff_act,
                        })
                except Exception:
                    pass
            return available

        result = {
            "prepared_sand"      : _schema_cols("preparedsand"),
            "additive"           : _schema_cols("additive")           if _table_exists("additive")           else [],
            "sieve"              : _sieve_band_types()                 if _table_exists("sieves")             else [],
            "consumption"        : _schema_cols("consumption")         if _table_exists("consumption")        else [],
            "metal"              : _schema_cols("metal")               if _table_exists("metal")              else [],
            "prepared_sand_extra": _schema_cols("prepared_sand_extra") if _table_exists("prepared_sand_extra") else [],
            "prescription"       : _prescription_params(),
        }

    return result

def discover_all_with_status(base_config: dict, live_days: int = 3) -> list[dict]:
    """
    Discover all foundry databases + line IDs and check live-data status.

    The central registry database (registry_database.name in config) is
    automatically excluded -- it holds customer records for all foundries
    but is not itself a sand-monitoring target.

    Returns a list of dicts -- one per (foundry_db, foundry_line_id):
    [
      {
        "db_name"          : "caspro_sandman",
        "display_name"     : "GPI",
        "foundry_line_id"  : 1,
        "label"            : "caspro_sandman_L1",
        "active"           : True,
        "has_any_data"     : True,
        "last_date"        : "2026-05-31",
        "row_count_recent" : 18,
        "error"            : None,
      }, ...
    ]
    """
    # Name of the central registry DB -- skip it in the foundry list
    registry_db_name = (base_config.get("registry_database") or {}).get("name", "")

    entries = expand_all_foundries(base_config, skip_enabled_check=True)
    result  = []

    for label, cfg in entries:
        db_name = cfg["database"]["name"]

        # Skip the central platform / registry database -- it is not a foundry
        if registry_db_name and db_name.lower() == registry_db_name.lower():
            logger.debug("discover_all_with_status: skipping registry DB %s", db_name)
            continue

        fl_id  = cfg.get("foundry_line_id", 1)
        status = check_line_has_live_data(cfg["database"], fl_id, days=live_days)
        result.append({
            "db_name"           : db_name,
            "display_name"      : get_foundry_display_name(db_name),
            "foundry_line_id"   : fl_id,
            "label"             : label,
            "monitoring_enabled": cfg.get("monitoring_enabled", False),
            "active"            : status["active"],
            "has_any_data"      : status["has_any_data"],
            "last_date"         : status["last_date"],
            "row_count_recent"  : status["row_count_recent"],
            "error"             : status.get("error"),
        })

    active_count = sum(1 for r in result if r["active"])
    data_count   = sum(1 for r in result if r["has_any_data"])
    logger.info(
        "discover_all_with_status: %d line(s) total -- %d active, %d have data",
        len(result), active_count, data_count,
    )
    return result

def _make_engine(db_cfg: dict):
    """Create a lightweight SQLAlchemy engine from a database config dict."""
    host     = db_cfg.get("host", "localhost")
    port     = int(db_cfg.get("port", 3306))
    user     = db_cfg.get("user", "root")
    password = db_cfg.get("password", "")
    dbname   = db_cfg.get("name", "")
    timeout  = int(db_cfg.get("connect_timeout", 10))

    url = (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{dbname}?charset=utf8mb4"
    )
    return create_engine(
        url,
        pool_size       = 1,
        max_overflow    = 1,
        pool_pre_ping   = True,
        pool_recycle    = 3600,
        connect_args    = {"connect_timeout": timeout},
        echo            = False,
    )
