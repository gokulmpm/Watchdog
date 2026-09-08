"""
watchdog/alert_server.py
-------------------------
Minimal Flask server that powers the alert notification dashboard.

Endpoints
---------
  GET  /                           serve the dashboard HTML
  GET  /api/alerts                 return all active (non-acknowledged) alerts
                                   ?since_id=<n>  -- return only rows with id > n (polling)
                                   ?type=SI|PRESCRIPTION
  GET  /api/alerts/all             return ALL alerts (including acknowledged), last 100
  POST /api/alerts/<id>/acknowledge mark a row acknowledged
  POST /api/alerts/<id>/dismiss    client-side only (no DB change) -- kept for symmetry

Usage
-----
    python -m watchdog.alert_server
    python -m watchdog.alert_server --config /path/to/watchdog_config.json --port 5055
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Allow running as a standalone script: python watchdog/alert_server.py
if __package__ is None or __package__ == "":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "watchdog"  # noqa: F841 -- enables relative imports below

import hashlib as _hashlib
import io
import time as _time

from flask import Flask, jsonify, render_template, request, redirect, session
from sqlalchemy import text

_DEFAULT_CONFIG = Path(__file__).parent / "config" / "watchdog_config.json"

app = Flask(__name__)
logger = logging.getLogger(__name__)

_SESSION_MAX_AGE = 1 * 3600  # 1-hour session lifetime

_engine        = None
_config:       dict       = {}
_config_path:  Path | None = None   # set in main()
_reg_engine                = None   # registry DB engine for config storage

# Cache for discover_all_with_status — expensive scan of all foundry DBs
_foundries_cache:      list | None = None
_foundries_cache_time: float       = 0.0
_FOUNDRIES_CACHE_TTL   = 300.0     # seconds (5 minutes)


# --- Bootstrap ----------------------------------------------------------------

def _get_engine():
    global _engine
    if _engine is None:
        from .pipeline.db_connector import get_engine
        _engine = get_engine(_config)
    return _engine


# --- Routes -------------------------------------------------------------------

def _verify_sandman_password(username: str, raw_password: str) -> bool:
    """Verify credentials against sandman_dev using Spring ShaPasswordEncoder(256).

    Formula: SHA256(password + "{" + username + "}")
    """
    if not _reg_engine:
        return False
    try:
        with _reg_engine.connect() as conn:
            row = conn.execute(text(
                "SELECT user_name, password FROM users WHERE user_name = :un LIMIT 1"
            ), {"un": username}).mappings().first()
        if not row:
            return False
        db_username = str(row["user_name"])          # use DB-stored case as salt
        stored      = str(row["password"] or "").strip()
        salted      = raw_password + "{" + db_username + "}"
        computed    = _hashlib.sha256(salted.encode("utf-8")).hexdigest()
        return computed == stored
    except Exception as exc:
        logger.warning("password verification failed for '%s': %s", username, exc)
        return False


@app.route("/favicon.ico")
def favicon():
    # Return the SVG favicon as an ICO-compatible response
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#3d6b45"/>'
        '<text x="16" y="22" text-anchor="middle" font-family="Arial" '
        'font-weight="900" font-size="18" fill="white">S</text>'
        '</svg>'
    )
    from flask import Response
    return Response(svg, mimetype="image/svg+xml")

@app.route("/")
def index():
    user = request.args.get("user", "").strip()
    pwd  = request.args.get("password", "").strip()

    if user and pwd:
        if _verify_sandman_password(user, pwd):
            session["user"] = user
            session["_ts"]  = int(_time.time())
            resp = redirect("/")
            resp.set_cookie("sandman_user", user,
                            max_age=_SESSION_MAX_AGE, samesite="Lax")
            return resp
        return ("Invalid credentials", 401)

    if user and not pwd:
        return ("Unauthorized. Access via /?user=USERNAME&password=PASSWORD", 401)

    if session.get("user"):
        if _time.time() - session.get("_ts", 0) < _SESSION_MAX_AGE:
            return render_template("alerts.html")
        session.clear()

    # Fail open when registry DB not configured (backward compat / local dev)
    if not _reg_engine:
        return render_template("alerts.html")

    return ("Unauthorized. Access via /?user=USERNAME&password=PASSWORD", 401)

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

@app.route("/contact")
def contact():
    return render_template("contact.html")


def _alerts_engine(db_name: str | None):
    """Return engine for the requested foundry DB, or the server default."""
    if db_name:
        try:
            return _get_engine_for(_foundry_cfg(db_name, 1))
        except Exception as exc:
            logger.warning("_alerts_engine: cannot connect to %s (%s) — using default", db_name, exc)
    return _get_engine()


_ALERTS_SELECT = """
    SELECT `id`, `alert_type`, `foundry_line_id`, `date`, `shift`,
           `batch_pkey`, `period_key`, `si_score`, `alert_level`,
           `root_cause`, `recommendation`, `params_json`,
           `overall_status`, `component_id`, `component_name`, `group_name`, `batch_time`, `deviations_json`,
           `prev_component_id`, `prev_component_name`, `component_info_json`,
           `smc_value`, `cosp_value`, `smc_cosp_diff`, `bb_threshold`,
           `acknowledged`, `acknowledged_at`,
           `acknowledged_by`, `ack_reason`, `ack_remarks`,
           `created_at`, `updated_at`
    FROM   `watchdog_alerts`
"""


def _fill_component_names(engine, alerts: list):
    """Fill missing component_name on alert dicts from the components table."""
    missing = {str(a["component_id"]) for a in alerts
               if a.get("component_id") and not a.get("component_name")}
    if not missing:
        return
    try:
        ph = ", ".join(f":c{i}" for i in range(len(missing)))
        params = {f"c{i}": cid for i, cid in enumerate(missing)}
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT component_id, component_name
                FROM components
                WHERE component_id IN ({ph})
            """), params).mappings().fetchall()
        name_map = {str(r["component_id"]): r["component_name"] for r in rows
                    if r["component_name"]}
        for a in alerts:
            if a.get("component_id") and not a.get("component_name"):
                a["component_name"] = name_map.get(str(a["component_id"]))
    except Exception as exc:
        logger.debug("component name enrichment skipped: %s", exc)


@app.route("/api/alerts")
def get_alerts():
    since_id    = request.args.get("since_id",  0,    type=int)
    alert_type  = request.args.get("type",       None)
    date_filter = request.args.get("date",       None)
    db_name     = request.args.get("db",         None)
    line_id     = request.args.get("line_id",    None, type=int)

    where  = ["(`acknowledged` = 0 OR `updated_at` > `acknowledged_at` OR `acknowledged_at` IS NULL)"]
    params: dict = {}

    if since_id:
        where.append("`id` > :since_id")
        params["since_id"] = since_id
    if line_id:
        where.append("`foundry_line_id` = :line_id")
        params["line_id"] = line_id

    _valid_types = ("SI", "PRESCRIPTION", "COMPONENT_CHANGE", "BAD_BATCH")
    if alert_type in _valid_types:
        where.append("`alert_type` = :atype")
        params["atype"] = alert_type
    if date_filter:
        where.append("`date` = :date_filter")
        params["date_filter"] = date_filter

    sql = text(_ALERTS_SELECT + f"WHERE {' AND '.join(where)} ORDER BY `id` DESC LIMIT 1000")
    try:
        engine = _alerts_engine(db_name)
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().fetchall()
        result = [_row_to_dict(r) for r in rows]
        _fill_component_names(engine, result)
        return jsonify(result)
    except Exception as exc:
        logger.error("get_alerts failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/alerts/all")
def get_all_alerts():
    db_name = request.args.get("db",      None)
    line_id = request.args.get("line_id", None, type=int)

    where  = []
    params: dict = {}
    if line_id:
        where.append("`foundry_line_id` = :line_id")
        params["line_id"] = line_id

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sql = text(_ALERTS_SELECT + f"{where_sql} ORDER BY `id` DESC LIMIT 2000")
    try:
        engine = _alerts_engine(db_name)
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().fetchall()
        result = [_row_to_dict(r) for r in rows]
        _fill_component_names(engine, result)
        return jsonify(result)
    except Exception as exc:
        logger.error("get_all_alerts failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/alerts/<int:alert_id>/acknowledge", methods=["POST"])
def acknowledge_alert(alert_id: int):
    body = request.get_json(silent=True) or {}
    ack_by     = str(body.get("acknowledged_by", "")).strip() or None
    ack_reason = str(body.get("reason", "")).strip() or None
    ack_remark = str(body.get("remarks", "")).strip() or None

    try:
        engine = _get_engine()
        # Add columns if they don't exist yet (silent migration)
        try:
            with engine.begin() as conn:
                for col, typ in [("acknowledged_by","VARCHAR(120)"),
                                  ("ack_reason","VARCHAR(255)"),
                                  ("ack_remarks","TEXT")]:
                    try:
                        conn.execute(text(f"ALTER TABLE watchdog_alerts ADD COLUMN `{col}` {typ} NULL"))
                    except Exception:
                        pass
        except Exception:
            pass

        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE `watchdog_alerts`
                SET    `acknowledged`    = 1,
                       `acknowledged_at` = :now,
                       `acknowledged_by` = :by,
                       `ack_reason`      = :reason,
                       `ack_remarks`     = :remarks
                WHERE  `id` = :id
            """), {"id": alert_id, "now": datetime.now(),
                   "by": ack_by, "reason": ack_reason, "remarks": ack_remark})
        return jsonify({"ok": True, "id": alert_id})
    except Exception as exc:
        logger.error("acknowledge_alert failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/features")
def get_features():
    """Return which optional monitors are enabled for the active foundry line."""
    fl_id   = _config.get("foundry_line_id", 1)
    db_name = _config.get("database", {}).get("name", "")
    label   = f"{db_name}_L{fl_id}"
    fc      = _config.get("foundry_configs", {}).get(label, {})
    return jsonify({
        "bad_batch_enabled"   : bool(fc.get("bad_batch_watchdog",        {}).get("enabled", False)),
        "comp_change_enabled" : bool(fc.get("component_change_watchdog", {}).get("enabled", False)),
        "sieve_enabled"       : bool(fc.get("sieve_watchdog",            {}).get("enabled", False)),
        "db_name"             : db_name,
        "foundry_line_id"     : fl_id,
    })


@app.route("/api/alerts/<int:alert_id>/dismiss", methods=["POST"])
def dismiss_alert(alert_id: int):
    # Client-side only -- just return OK so the front-end can remove the card
    return jsonify({"ok": True, "id": alert_id})


# --- Property-Deviation Alerts (REMOVED — endpoints kept as stubs for compat) -

_PROP_ALERTS_SELECT = """
    SELECT `id`, `foundry_line_id`, `date`, `shift`, `period_key`, `mode`, `component_id`,
           `category`, `parameter`, `parameter_display`,
           `alert_title`, `alert_type`,
           `current_value`, `lcl`, `ucl`,
           `variance_label`, `drift_label`, `oscillation_label`,
           `lcl_ucl_breach`, `breach_direction`,
           `variance_score`, `drift_score`, `oscillation_score`,
           `engines_triggered`,
           `root_cause`, `recommendation`,
           `acknowledged`, `acknowledged_at`,
           `created_at`, `updated_at`
    FROM   `property_alerts`
"""


@app.route("/api/property-alerts")
@app.route("/api/property-alerts/all")
def get_property_alerts():
    return jsonify([])   # feature removed


@app.route("/api/property-alerts/<int:alert_id>/acknowledge", methods=["POST"])
def acknowledge_property_alert(alert_id: int):
    return jsonify({"ok": True, "id": alert_id})


# --- Configuration UI ---------------------------------------------------------

@app.route("/config")
def config_page():
    """Serve the watchdog configuration page."""
    return render_template("config.html")


@app.route("/api/config/foundries")
def get_config_foundries():
    """
    Discover all foundry databases + active lines and check live-data status.
    Result is cached for _FOUNDRIES_CACHE_TTL seconds to avoid scanning all
    foundry DBs on every page load (very expensive with 15+ databases).
    Pass ?refresh=1 to force a fresh scan.
    """
    import time as _time
    global _foundries_cache, _foundries_cache_time

    force_refresh = request.args.get("refresh", "0") == "1"
    age = _time.monotonic() - _foundries_cache_time

    if not force_refresh and _foundries_cache is not None and age < _FOUNDRIES_CACHE_TTL:
        return jsonify(_foundries_cache)

    from .foundry_registry import discover_all_with_status
    try:
        entries = discover_all_with_status(_config, live_days=30)
        _foundries_cache      = entries
        _foundries_cache_time = _time.monotonic()
        return jsonify(entries)
    except Exception as exc:
        logger.error("get_config_foundries failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/user-foundry")
def get_user_foundry():
    """
    Resolve the foundry DB and available lines for a given username.

    Query params: user=<user_name>
    Returns:
      { db_name, display_name, customer_pkey, lines: [{id, name}] }

    Lookup chain:
      users.user_name -> users.customer_pkey
      -> customers.db_properties (JDBC URL -> db_name)
      -> foundry_line table in that DB for line names
    """
    user_name = request.args.get("user", "").strip()
    if not user_name or not _reg_engine:
        return jsonify({"error": "user or registry unavailable"}), 400

    import json as _json, re as _re
    from sqlalchemy import text as _text

    default_db = _config.get("database", {}).get("name", "")

    try:
        with _reg_engine.connect() as conn:
            row = conn.execute(_text("""
                SELECT u.customer_pkey, c.name AS customer_name, c.db_properties
                FROM   users     u
                JOIN   customers c ON c.pkey = u.customer_pkey
                WHERE  u.user_name = :un
                  AND  c.deleted   = 0
                LIMIT 1
            """), {"un": user_name}).mappings().first()

        if not row:
            logger.warning("user-foundry: no user '%s' found in registry", user_name)
            return jsonify({"error": f"User '{user_name}' not found in the system"}), 404
        else:
            db_properties_str = row["db_properties"]
            if not db_properties_str:
                logger.warning("user-foundry: no db_properties for '%s', using default", user_name)
                db_name = default_db
            else:
                # Parse db_properties — same logic as db.py
                try:
                    db_props = _json.loads(db_properties_str)
                    db_url   = db_props.get("url", "")
                except (_json.JSONDecodeError, TypeError):
                    # Fallback: extract url via regex from raw string
                    m_url  = _re.search(r'"url"\s*:\s*"([^"]+)"', str(db_properties_str))
                    db_url = m_url.group(1) if m_url else ""

                m_db = _re.search(r"/([^/?]+)$", db_url)
                if not m_db:
                    logger.warning("user-foundry: cannot parse db_name from '%s', using default", db_url)
                    db_name = default_db
                else:
                    db_name = m_db.group(1)

            display_name  = row["customer_name"] or db_name
            customer_pkey = int(row["customer_pkey"] or 0)

        # Fetch available foundry lines from the resolved DB
        # Falls back to [Line 1] when the remote DB is unreachable
        from .foundry_registry import discover_foundry_line_ids, get_line_names
        shared_cfg = {**_config.get("registry_database", _config.get("database", {})), "name": db_name}
        try:
            line_ids   = discover_foundry_line_ids(shared_cfg)
            line_names = get_line_names(shared_cfg)
            lines      = [{"id": lid, "name": line_names.get(lid) or f"Line {lid}"} for lid in line_ids]
        except Exception:
            lines = []
        if not lines:
            lines = [{"id": 1, "name": "Line 1"}]

        return jsonify({
            "db_name"       : db_name,
            "display_name"  : display_name,
            "customer_pkey" : customer_pkey,
            "lines"         : lines,
        })
    except Exception as exc:
        logger.error("get_user_foundry failed: %s", exc, exc_info=True)
        # Graceful fallback — return default foundry so the UI still works
        return jsonify({
            "db_name"      : default_db,
            "display_name" : default_db,
            "customer_pkey": 0,
            "lines"        : [{"id": 1, "name": "Line 1"}],
        })


@app.route("/api/config/customers")
def get_customers():
    """Return list of customers from sandman_dev registry for webhook foundry_key selection."""
    if not _reg_engine:
        return jsonify([])
    try:
        from sqlalchemy import text as _text
        with _reg_engine.connect() as conn:
            rows = conn.execute(_text(
                "SELECT pkey, name FROM customers "
                "WHERE deleted=0 AND db_properties IS NOT NULL "
                "ORDER BY name"
            )).fetchall()
        return jsonify([{"pkey": r[0], "name": r[1]} for r in rows])
    except Exception as exc:
        logger.warning("get_customers failed: %s", exc)
        return jsonify([])


@app.route("/api/config/foundry-line-names")
def get_foundry_line_names():
    """Return {foundry_line_id: line_name} for a foundry DB (e.g. {1: 'SAVELLI'})."""
    from .foundry_registry import get_line_names
    db_name = request.args.get("db")
    if not db_name:
        return jsonify({"error": "db required"}), 400
    try:
        db_cfg = {**_config.get("database", {}), "name": db_name}
        return jsonify(get_line_names(db_cfg))
    except Exception as exc:
        logger.error("get_foundry_line_names failed: %s", exc)
        return jsonify({}), 200   # non-fatal -- UI will fall back to "Line N"


@app.route("/api/config/foundry-param-names")
def get_foundry_param_names():
    """
    Return UI display names for parameters from the properties + measures tables.

    Query: SELECT p.java_name, p.ui_name, p.alias_name, m.name, p.defect_group
           FROM properties p JOIN measures m ON m.pkey = p.measure_pkey
           WHERE p.is_active=1 AND p.deleted=0 AND m.isActive=1 AND m.foundry_line_id=:line_id

    Returns {java_name: ui_name} -- java_name matches the preparedsand/additive column names.
    Falls back gracefully if the tables do not exist.
    """
    from .foundry_registry import _make_engine
    from sqlalchemy import text as _text

    db_name = request.args.get("db")
    line_id = request.args.get("line_id", 1, type=int)
    if not db_name:
        return jsonify({"error": "db required"}), 400

    sql = _text("""
        SELECT DISTINCT
            p.java_name,
            p.ui_name,
            p.alias_name,
            m.name        AS measure_name,
            p.defect_group
        FROM  properties p
        JOIN  measures m ON m.pkey = p.measure_pkey
        WHERE p.is_active       = 1
          AND p.deleted         = 0
          AND m.isActive        = 1
          AND m.foundry_line_id = :line_id
    """)

    try:
        db_cfg = {**_config.get("database", {}), "name": db_name}
        engine = _make_engine(db_cfg)
        with engine.connect() as conn:
            rows = conn.execute(sql, {"line_id": line_id}).mappings().fetchall()

        import re as _re

        def _camel_to_snake(s: str) -> str:
            """activeClay -> active_clay,  GFNAfs -> gfn_afs"""
            s1 = _re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', s)
            return _re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

        result = {}
        for row in rows:
            java  = str(row["java_name"]    or "").strip()
            alias = str(row["alias_name"]   or "").strip()
            mname = str(row["measure_name"] or "").strip()
            ui    = str(row["ui_name"]      or "").strip()
            display = ui or alias or mname or java
            if not display:
                continue

            # Register every name variant so column lookups always find a match.
            # java_name is camelCase; DB column names are snake_case -- add both.
            for key in filter(None, {java, alias, mname}):
                result[key] = display
                snake = _camel_to_snake(key)
                if snake and snake != key:
                    result[snake] = display

        logger.info("foundry-param-names: %d mappings for %s L%d", len(result), db_name, line_id)
        return jsonify(result)
    except Exception as exc:
        logger.warning("get_foundry_param_names failed (%s L%d): %s", db_name, line_id, exc)
        return jsonify({}), 200   # non-fatal -- UI auto-formats column names


@app.route("/api/config/foundry-params")
def get_foundry_params():
    """
    Return every parameter column that has data for a specific foundry line.

    Query params:
      db       -- database name  (e.g. caspro_sandman)
      line_id  -- foundry line ID (default 1)

    Response:
      {
        "prepared_sand" : ["active_clay", ...],
        "additive"      : ["bentonite",   ...],
        "sieve"         : ["fines",       ...],
        "consumption"   : [...]
      }
    """
    from .foundry_registry import get_available_parameters
    db_name = request.args.get("db")
    line_id = request.args.get("line_id", 1, type=int)
    if not db_name:
        return jsonify({"error": "db parameter is required"}), 400

    try:
        db_cfg = {**_config.get("database", {}), "name": db_name}
        params = get_available_parameters(db_cfg, line_id)
        return jsonify(params)
    except Exception as exc:
        logger.error("get_foundry_params failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


def _build_param_chart_limits(db_name: str, line_id: int, saved: dict) -> dict:
    """
    Merge properties.cpk_min / cpk_max into param_chart_limits so the UI
    shows the actual LCL/UCL from the properties table when no manual
    override has been saved by the user.
    """
    try:
        from .foundry_registry import _make_engine
        from sqlalchemy import text as _t
        db_cfg = {**_config.get("database", {}), "name": db_name}
        engine = _make_engine(db_cfg)
        sql = _t("""
            SELECT p.java_name, p.cpk_min, p.cpk_max
            FROM   properties p
            JOIN   measures   m ON m.pkey = p.measure_pkey
            WHERE  m.foundry_line_id = :lid
              AND  p.deleted  = 0
              AND  p.is_active = 1
              AND  m.isActive  = 1
              AND  (p.cpk_min IS NOT NULL OR p.cpk_max IS NOT NULL)
        """)
        with engine.connect() as conn:
            rows = conn.execute(sql, {"lid": line_id}).mappings().fetchall()

        import re as _re
        def _snake(s):
            s1 = _re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', s)
            return _re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

        result = dict(saved)   # user overrides take priority
        for row in rows:
            java  = str(row["java_name"] or "").strip()
            snake = _snake(java)
            lcl   = float(row["cpk_min"]) if row["cpk_min"] is not None else None
            ucl   = float(row["cpk_max"]) if row["cpk_max"] is not None else None
            # Use both camelCase and snake_case keys so UI always finds a match
            for key in filter(None, {java, snake}):
                if key not in result:
                    result[key] = {}
                if result[key].get("min") is None and lcl is not None:
                    result[key]["min"] = lcl
                if result[key].get("max") is None and ucl is not None:
                    result[key]["max"] = ucl
        return result
    except Exception as exc:
        logger.debug("_build_param_chart_limits failed: %s", exc)
        return saved   # fall back to whatever was saved


@app.route("/api/config/foundry-config", methods=["GET"])
def get_foundry_config():
    """
    Return the monitoring config for a specific foundry line.

    Query params: db, line_id

    Falls back to global defaults when no per-foundry override exists.
    Always includes display_names so the UI can label parameters correctly.
    """
    db_name = request.args.get("db")
    line_id = request.args.get("line_id", 1, type=int)
    if not db_name:
        return jsonify({"error": "db parameter is required"}), 400

    label = f"{db_name}_L{line_id}"

    # Load from DB -- DB is the single source of truth for per-foundry config
    fc = {}
    if _reg_engine:
        from .config_store import load_foundry_config
        fc = load_foundry_config(_reg_engine, label)

    # Defaults -- used only when a key has never been saved for this foundry line
    _default_weights     = {"variance": 0.35, "drift": 0.45, "oscillation": 0.20}
    _default_thresholds  = {"stable_max": 20, "watch_max": 49, "alert_max": 69}
    _default_labels      = {"stable": "STABLE", "watch": "WATCH",
                             "alert": "ALERT",  "critical": "CRITICAL"}
    _global_weights     = fc.get("si_weights",       _default_weights)
    _global_thresholds  = fc.get("alert_thresholds", _default_thresholds)
    _global_labels      = fc.get("alert_labels",     _default_labels)

    def _fc_or_global(key, default):
        return fc.get(key, default)

    # Resolve customer_pkey by matching db_name against customers.db_properties in sandman_dev.
    resolved_customer_pkey = fc.get("customer_pkey", _config.get("customer_pkey", ""))
    if db_name:
        try:
            import pymysql, json as _json
            _rdb = _config.get("registry_database", {})
            _conn = pymysql.connect(
                host=_rdb.get("host", "localhost"),
                port=int(_rdb.get("port", 3306)),
                user=_rdb.get("user", "root"),
                password=_rdb.get("password", ""),
                database=_rdb.get("name", "sandman_dev"),
                connect_timeout=5,
            )
            with _conn:
                with _conn.cursor() as _cur:
                    _cur.execute(
                        "SELECT pkey, db_properties FROM customers "
                        "WHERE deleted=0 AND db_properties IS NOT NULL AND db_properties != ''"
                    )
                    _rows = _cur.fetchall()
            for _r in _rows:
                try:
                    _props = _json.loads(_r[1])
                    _url   = _props.get("url", "")
                    _db    = _url.split("/")[-1] if "/" in _url else ""
                    if _db.lower() == db_name.lower():
                        resolved_customer_pkey = _r[0]
                        break
                except Exception:
                    pass
        except Exception as _e:
            logger.warning("customer_pkey lookup failed for db=%s: %s", db_name, _e)

    return jsonify({
        "label"               : label,
        "customer_pkey"       : resolved_customer_pkey,
        "foundry_line_id"     : line_id,
        "monitoring_enabled"  : fc.get("monitoring_enabled", False),
        "si_params"           : fc.get("si_params", {}).get("params",
                                    _config.get("si_params", {}).get("params", [])),
        "aggregation_mode"    : fc.get("aggregation", {}).get("mode",
                                    _config.get("aggregation", {}).get("mode", "shift")),
        "dual_mode"           : _fc_or_global("dual_mode",            False),
        "dual_mode_secondary" : _fc_or_global("dual_mode_secondary",  "shift"),
        "report_window"       : fc.get("report_window",
                                    _config.get("report_window",
                                    _config.get("engines", {}).get("window", 10))),
        "display_names"       : _config.get("display_names", {}),
        # Per-section parameter selections
        "add_params"          : fc.get("add_params",   []),
        "smc_params"          : fc.get("smc_params",   []),
        "sv_params"           : fc.get("sv_params",    []),
        "metal_params"        : fc.get("metal_params", []),
        # Optimal values per parameter (drift reference mean)
        "optimal_values"      : fc.get("optimal_values",
                                    _config.get("optimal_values", {})),
        # Per-parameter engine config overrides {col: {si_weights, alert_thresholds, alert_labels, engines}}
        "property_engine_config": fc.get("property_engine_config", {}),
        # Data source enable flags
        "enable_additive"     : fc.get("enable_additive", True),
        "enable_smc"          : fc.get("enable_smc",      True),
        "enable_sieve"        : fc.get("enable_sieve",    True),
        "enable_metal"        : fc.get("enable_metal",    True),
        # Watchdog engine config -- overall SI
        "si_weights"          : fc.get("si_weights",       _global_weights),
        "alert_thresholds"    : fc.get("alert_thresholds", _global_thresholds),
        "alert_labels"        : fc.get("alert_labels",     _global_labels),
        # Per-engine thresholds and labels
        "engines"             : {
            "variance": {
                **_config.get("engines", {}).get("variance", {}),
                **fc.get("engines", {}).get("variance", {}),
            },
            "drift": {
                **_config.get("engines", {}).get("drift", {}),
                **fc.get("engines", {}).get("drift", {}),
            },
            "oscillation": {
                **_config.get("engines", {}).get("oscillation", {}),
                **fc.get("engines", {}).get("oscillation", {}),
            },
        },
        # SI scoring thresholds (sigma bands)
        "sigma_ok_thr"        : fc.get("sigma_ok_thr",    _config.get("sigma_ok_thr",    1.0)),
        "sigma_warn_thr"      : fc.get("sigma_warn_thr",  _config.get("sigma_warn_thr",  2.0)),
        "sigma_alert_thr"     : fc.get("sigma_alert_thr", _config.get("sigma_alert_thr", 3.0)),
        # Fetch window for parameter charts
        "window_fetch_days"   : int(fc.get("window_fetch_days", _config.get("window_fetch_days", 14))),
        # Deviation threshold
        "pct_change_warning"  : fc.get("pct_change_warning",
                                    _config.get("pct_change_warning", 5.0)),
        # LLM root-cause analysis
        "llm_analysis": {
            "enabled": fc.get("llm_analysis", {}).get("enabled", False),
            "model"  : fc.get("llm_analysis", {}).get("model",   "claude-haiku-4-5"),
            "timeout": fc.get("llm_analysis", {}).get("timeout", 10),
        },
        # Baseline period
        "baseline"            : {
            **_config.get("baseline", {"start_date": "2025-07-01", "end_date": "2025-07-31"}),
            **fc.get("baseline", {}),
        },
        # Alert trigger
        "trigger"             : {
            **_config.get("trigger", {}),
            **fc.get("trigger", {}),
        },
        # Prescription deviation monitoring
        "prescription_watchdog": {
            "enabled"            : fc.get("prescription_watchdog", {}).get("enabled",             True),
            "use_scada"          : fc.get("prescription_watchdog", {}).get("use_scada",            False),
            "monitored_params"   : fc.get("prescription_watchdog", {}).get("monitored_params",
                                       ["bentonite", "freshSilicaSand", "lca", "water"]),
            "tolerance_pct"      : fc.get("prescription_watchdog", {}).get("tolerance_pct",       1.0),
            "watch_thr"          : fc.get("prescription_watchdog", {}).get("watch_thr",            1.0),
            "alert_thr"          : fc.get("prescription_watchdog", {}).get("alert_thr",            3.0),
            "critical_thr"       : fc.get("prescription_watchdog", {}).get("critical_thr",         6.0),
            "setpoint_monitoring": fc.get("prescription_watchdog", {}).get("setpoint_monitoring",  True),
            "setpoint_tolerance" : fc.get("prescription_watchdog", {}).get("setpoint_tolerance",   0.5),
            "trend_window"       : fc.get("prescription_watchdog", {}).get("trend_window",         5),
            "trend_min_batches"  : fc.get("prescription_watchdog", {}).get("trend_min_batches",    3),
            "poll_interval_sec"  : fc.get("prescription_watchdog", {}).get("poll_interval_sec",    30),
            "idle_timeout_min"   : fc.get("prescription_watchdog", {}).get("idle_timeout_min",     60),
            "skip_zero_batches"  : fc.get("prescription_watchdog", {}).get("skip_zero_batches",    True),
        },
        # Component change monitoring
        "component_change_watchdog": {
            "enabled"          : fc.get("component_change_watchdog", {}).get("enabled",           False),
            "use_scada"        : fc.get("component_change_watchdog", {}).get("use_scada",          False),
            "poll_interval_sec": fc.get("component_change_watchdog", {}).get("poll_interval_sec", 30),
            "idle_timeout_min" : fc.get("component_change_watchdog", {}).get("idle_timeout_min",  0),
        },
        # SMC batch LCL/UCL monitoring
        "smc_batch_watchdog": {
            "enabled"             : fc.get("smc_batch_watchdog", {}).get("enabled",              False),
            "sigma_alerts_enabled": fc.get("smc_batch_watchdog", {}).get("sigma_alerts_enabled", True),
            "sigma_threshold"     : fc.get("smc_batch_watchdog", {}).get("sigma_threshold",      3.0),
            "sigma_window_days"   : fc.get("smc_batch_watchdog", {}).get("sigma_window_days",    30),
            "poll_interval_sec"   : fc.get("smc_batch_watchdog", {}).get("poll_interval_sec",    30),
            "cooldown_sec"        : fc.get("smc_batch_watchdog", {}).get("cooldown_sec",         300),
        },
        # Bad batch (SMC vs COSP) monitoring
        "bad_batch_watchdog": {
            "enabled"          : fc.get("bad_batch_watchdog", {}).get("enabled",           False),
            "detection_mode"   : fc.get("bad_batch_watchdog", {}).get("detection_mode",   "absolute"),
            "threshold"        : fc.get("bad_batch_watchdog", {}).get("threshold",         2.0),
            "abs_ok_thr"       : fc.get("bad_batch_watchdog", {}).get("abs_ok_thr",   fc.get("bad_batch_watchdog", {}).get("watch_thr",    1.0)),
            "abs_warn_thr"     : fc.get("bad_batch_watchdog", {}).get("abs_warn_thr", fc.get("bad_batch_watchdog", {}).get("alert_thr",    2.0)),
            "abs_crit_thr"     : fc.get("bad_batch_watchdog", {}).get("abs_crit_thr",      3.0),
            "pct_ok_thr"       : fc.get("bad_batch_watchdog", {}).get("pct_ok_thr",        0.02),
            "pct_warn_thr"     : fc.get("bad_batch_watchdog", {}).get("pct_warn_thr",      0.05),
            "pct_critical_thr" : fc.get("bad_batch_watchdog", {}).get("pct_critical_thr",  0.10),
            "smc_col"          : fc.get("bad_batch_watchdog", {}).get("smc_col",  "compactability_smc_pct"),
            "cosp_col"         : fc.get("bad_batch_watchdog", {}).get("cosp_col", "cosp_percentage_pct"),
            "poll_interval_sec": fc.get("bad_batch_watchdog", {}).get("poll_interval_sec", 30),
            "idle_timeout_min" : fc.get("bad_batch_watchdog", {}).get("idle_timeout_min",  0),
        },
        # Sieve % change monitoring
        "sieve_watchdog": {
            "enabled"            : fc.get("sieve_watchdog", {}).get("enabled",             False),
            "ok_thr"             : fc.get("sieve_watchdog", {}).get("ok_thr",              1.0),
            "warn_thr"           : fc.get("sieve_watchdog", {}).get("warn_thr",            3.0),
            "critical_thr"       : fc.get("sieve_watchdog", {}).get("critical_thr",        5.0),
            "pct_change_warning" : fc.get("sieve_watchdog", {}).get("pct_change_warning",  5.0),
            "band_thresholds"    : fc.get("sieve_watchdog", {}).get("band_thresholds",     {}),
            "poll_interval_sec"  : fc.get("sieve_watchdog", {}).get("poll_interval_sec",   60),
            "idle_timeout_min"   : fc.get("sieve_watchdog", {}).get("idle_timeout_min",    0),
            "email_enabled"      : fc.get("sieve_watchdog", {}).get("email_enabled",       False),
        },
        # Prediction monitor
        "prediction_monitor": {
            "enabled"          : fc.get("prediction_monitor", {}).get("enabled",           False),
            "grace_minutes"    : fc.get("prediction_monitor", {}).get("grace_minutes",     30),
            "poll_interval_sec": fc.get("prediction_monitor", {}).get("poll_interval_sec", 300),
        },
        "data_flow": {
            "disabled_sources": fc.get("data_flow", {}).get("disabled_sources", []),
        },
        "cost_saving": {
            "enabled"        : fc.get("cost_saving", {}).get("enabled",         False),
            "baseline_start" : fc.get("cost_saving", {}).get("baseline_start",  ""),
            "baseline_end"   : fc.get("cost_saving", {}).get("baseline_end",    ""),
            "cost_per_mt"    : fc.get("cost_saving", {}).get("cost_per_mt",     7000),
            "currency"       : fc.get("cost_saving", {}).get("currency",        "INR"),
        },
        # Push notification webhook
        "webhook": {
            "enabled"          : fc.get("webhook", {}).get("enabled",               False),
            "base_url"         : fc.get("webhook", {}).get("base_url",
                                    _config.get("webhook", {}).get("base_url", "")),
            "foundry_key"      : fc.get("webhook", {}).get("foundry_key",
                                    _config.get("webhook", {}).get("foundry_key", "")),
            "timeout_sec"      : fc.get("webhook", {}).get("timeout_sec",  10),
            "shift_names"      : fc.get("webhook", {}).get("shift_names",
                                    _config.get("webhook", {}).get("shift_names",
                                        {"1": "Morning", "2": "Afternoon", "3": "Night"})),
            "send_si"          : fc.get("webhook", {}).get("send_si",          True),
            "send_bad_batch"   : fc.get("webhook", {}).get("send_bad_batch",   False),
            "send_smc_batch"   : fc.get("webhook", {}).get("send_smc_batch",   False),
            "send_prescription": fc.get("webhook", {}).get("send_prescription",False),
            "send_sieve"       : fc.get("webhook", {}).get("send_sieve",       False),
            "send_prediction"  : fc.get("webhook", {}).get("send_prediction",  False),
            "min_severity"     : fc.get("webhook", {}).get("min_severity",     "warning"),
        },
        "notifications": fc.get("notifications", _config.get("notifications", {})),
        "param_chart_limits": _build_param_chart_limits(db_name, line_id, fc.get("param_chart_limits", {})),
        "additive_delay_seconds": int(fc.get("additive_delay_seconds") or 0),
        "material_tolerance_pct": float(fc.get("material_tolerance_pct") or 3.0),
    })


@app.route("/api/config/foundry-config", methods=["POST"])
def save_foundry_config():
    """
    Save monitoring configuration for a specific foundry line.
    Stored under watchdog_config.json > foundry_configs > {db}_L{line_id}.

    Query params: db, line_id

    Accepted JSON body keys (all optional):
      si_params           -- list of bare param names  (e.g. ["active_clay", "moisture"])
      aggregation_mode    -- "component" | "shift" | "day" | "window"
      dual_mode           -- bool
      dual_mode_secondary -- "shift" | "day" | "window"
      report_window       -- int
    """
    global _config, _config_path
    db_name = request.args.get("db")
    line_id = request.args.get("line_id", 1, type=int)
    if not db_name:
        return jsonify({"error": "db parameter is required"}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    label = f"{db_name}_L{line_id}"

    try:
        # Always load the current DB state first so we don't overwrite keys
        # that were added/changed in the DB after server startup.
        if _reg_engine:
            from .config_store import load_foundry_config as _lfc_save
            _db_fc = _lfc_save(_reg_engine, label) or {}
            _config.setdefault("foundry_configs", {})[label] = _db_fc
        else:
            _config.setdefault("foundry_configs", {}).setdefault(label, {})
        fc = _config["foundry_configs"][label]
    except Exception:
        _config.setdefault("foundry_configs", {}).setdefault(label, {})
        fc = _config["foundry_configs"][label]

    try:
        if "monitoring_enabled" in data:
            fc["monitoring_enabled"] = bool(data["monitoring_enabled"])
        if "si_params" in data:
            fc.setdefault("si_params", {})["params"] = list(data["si_params"])
        # Per-section parameter selections
        for key in ("add_params", "smc_params", "sv_params", "metal_params"):
            if key in data:
                fc[key] = list(data[key])
        # Data source enable flags
        for key in ("enable_additive", "enable_smc", "enable_sieve", "enable_metal"):
            if key in data:
                fc[key] = bool(data[key])
        if "aggregation_mode" in data:
            fc.setdefault("aggregation", {})["mode"] = str(data["aggregation_mode"])
        if "dual_mode" in data:
            fc["dual_mode"] = bool(data["dual_mode"])
        if "dual_mode_secondary" in data:
            fc["dual_mode_secondary"] = str(data["dual_mode_secondary"])
        if "report_window" in data:
            fc["report_window"] = int(data["report_window"])
        if "si_weights" in data:
            fc["si_weights"] = {
                "variance"   : float(data["si_weights"].get("variance",    0.35)),
                "drift"      : float(data["si_weights"].get("drift",       0.45)),
                "oscillation": float(data["si_weights"].get("oscillation", 0.20)),
            }
        if "alert_thresholds" in data:
            fc["alert_thresholds"] = {
                "stable_max": float(data["alert_thresholds"].get("stable_max", 20)),
                "watch_max" : float(data["alert_thresholds"].get("watch_max",  49)),
                "alert_max" : float(data["alert_thresholds"].get("alert_max",  69)),
            }
        if "alert_labels" in data:
            fc["alert_labels"] = {
                "stable"  : str(data["alert_labels"].get("stable",   "STABLE")),
                "watch"   : str(data["alert_labels"].get("watch",    "WATCH")),
                "alert"   : str(data["alert_labels"].get("alert",    "ALERT")),
                "critical": str(data["alert_labels"].get("critical", "CRITICAL")),
            }
        if "engines" in data:
            fc.setdefault("engines", {})
            eng = data["engines"]
            if "variance" in eng:
                fc["engines"]["variance"] = dict(eng["variance"])
            if "drift" in eng:
                fc["engines"]["drift"] = dict(eng["drift"])
            if "oscillation" in eng:
                fc["engines"]["oscillation"] = dict(eng["oscillation"])
        if "pct_change_warning" in data:
            fc["pct_change_warning"] = float(data["pct_change_warning"])
        if "sigma_ok_thr" in data:
            fc["sigma_ok_thr"] = float(data["sigma_ok_thr"])
        if "sigma_warn_thr" in data:
            fc["sigma_warn_thr"] = float(data["sigma_warn_thr"])
        if "sigma_alert_thr" in data:
            fc["sigma_alert_thr"] = float(data["sigma_alert_thr"])
        if "window_fetch_days" in data:
            fc["window_fetch_days"] = int(data["window_fetch_days"])
        if "llm_analysis" in data:
            la = data["llm_analysis"]
            fc["llm_analysis"] = {
                "enabled": bool(la.get("enabled", False)),
                "model"  : str(la.get("model",   "claude-haiku-4-5")),
                "timeout": int(la.get("timeout", 10)),
            }
        if "baseline" in data:
            fc["baseline"] = {
                "start_date": str(data["baseline"].get("start_date", "2025-07-01")),
                "end_date"  : str(data["baseline"].get("end_date",   "2025-07-31")),
            }
        if "optimal_values" in data:
            fc["optimal_values"] = {
                str(k): float(v)
                for k, v in data["optimal_values"].items()
                if v is not None and str(v).strip() != ""
            }
        if "property_engine_config" in data:
            fc["property_engine_config"] = {
                str(col): dict(cfg)
                for col, cfg in data["property_engine_config"].items()
                if isinstance(cfg, dict)
            }
        if "trigger" in data:
            t = data["trigger"]
            fc["trigger"] = {
                "cooldown_seconds"      : int(t.get("cooldown_seconds",       300)),
                "check_interval_seconds": int(t.get("check_interval_seconds", 300)),
                "day_end_hour"          : int(t.get("day_end_hour",           6)),
                "day_end_minute"        : int(t.get("day_end_minute",         0)),
                "window_n"              : int(t.get("window_n",               10)),
                "skip_additive"         : bool(t.get("skip_additive",         True)),
            }

        if "prescription_watchdog" in data:
            pw = data["prescription_watchdog"]
            fc["prescription_watchdog"] = {
                "enabled"            : bool(pw.get("enabled",             True)),
                "use_scada"          : bool(pw.get("use_scada",            False)),
                "monitored_params"   : list(pw.get("monitored_params",    ["bentonite", "freshSilicaSand", "lca", "water"])),
                "tolerance_pct"      : float(pw.get("tolerance_pct",      1.0)),
                "watch_thr"          : float(pw.get("watch_thr",           1.0)),
                "alert_thr"          : float(pw.get("alert_thr",           3.0)),
                "critical_thr"       : float(pw.get("critical_thr",        6.0)),
                "setpoint_monitoring": bool(pw.get("setpoint_monitoring",  True)),
                "setpoint_tolerance" : float(pw.get("setpoint_tolerance",  0.5)),
                "trend_window"       : int(pw.get("trend_window",          5)),
                "trend_min_batches"  : int(pw.get("trend_min_batches",     3)),
                "poll_interval_sec"  : int(pw.get("poll_interval_sec",     30)),
                "idle_timeout_min"   : int(pw.get("idle_timeout_min",      60)),
                "skip_zero_batches"  : bool(pw.get("skip_zero_batches",    True)),
            }

        if "component_change_watchdog" in data:
            ccw = data["component_change_watchdog"]
            fc["component_change_watchdog"] = {
                "enabled"          : bool(ccw.get("enabled",           False)),
                "use_scada"        : bool(ccw.get("use_scada",          False)),
                "poll_interval_sec": int(ccw.get("poll_interval_sec",  30)),
                "idle_timeout_min" : int(ccw.get("idle_timeout_min",   0)),
            }

        if "bad_batch_watchdog" in data:
            bbw = data["bad_batch_watchdog"]
            fc["bad_batch_watchdog"] = {
                "enabled"          : bool(bbw.get("enabled",           False)),
                "detection_mode"   : str(bbw.get("detection_mode",    "absolute")).lower(),
                "threshold"        : float(bbw.get("threshold",         2.0)),
                "abs_ok_thr"       : float(bbw.get("watch_thr",    bbw.get("abs_ok_thr",   1.0))),
                "abs_warn_thr"     : float(bbw.get("alert_thr",    bbw.get("abs_warn_thr", 2.0))),
                "abs_crit_thr"     : float(bbw.get("critical_thr", bbw.get("abs_crit_thr", 3.0))),
                "pct_ok_thr"       : float(bbw.get("pct_ok_thr",        2.0)),
                "pct_warn_thr"     : float(bbw.get("pct_warn_thr",      5.0)),
                "pct_critical_thr" : float(bbw.get("pct_critical_thr",  10.0)),
                "smc_col"          : str(bbw.get("smc_col",  "compactability_smc_pct")),
                "cosp_col"         : str(bbw.get("cosp_col", "cosp_percentage_pct")),
                "poll_interval_sec": int(bbw.get("poll_interval_sec",  30)),
                "idle_timeout_min" : int(bbw.get("idle_timeout_min",   0)),
            }

        if "smc_batch_watchdog" in data:
            smcbw = data["smc_batch_watchdog"]
            fc["smc_batch_watchdog"] = {
                "enabled"             : bool(smcbw.get("enabled",              False)),
                "sigma_alerts_enabled": bool(smcbw.get("sigma_alerts_enabled", True)),
                "sigma_threshold"     : float(smcbw.get("sigma_threshold",     3.0)),
                "sigma_window_days"   : int(smcbw.get("sigma_window_days",     30)),
                "poll_interval_sec"   : int(smcbw.get("poll_interval_sec",     30)),
                "cooldown_sec"        : int(smcbw.get("cooldown_sec",          300)),
            }

        if "sieve_watchdog" in data:
            sw = data["sieve_watchdog"]
            fc["sieve_watchdog"] = {
                "enabled"            : bool(sw.get("enabled",             False)),
                "ok_thr"             : float(sw.get("ok_thr",             1.0)),
                "warn_thr"           : float(sw.get("warn_thr",           3.0)),
                "critical_thr"       : float(sw.get("critical_thr",       5.0)),
                "pct_change_warning" : float(sw.get("pct_change_warning", sw.get("critical_thr", 5.0))),
                "band_thresholds"    : {str(k): float(v)
                                        for k, v in sw.get("band_thresholds", {}).items()
                                        if v is not None},
                "poll_interval_sec"  : int(sw.get("poll_interval_sec",    60)),
                "idle_timeout_min"   : int(sw.get("idle_timeout_min",     0)),
                "email_enabled"      : bool(sw.get("email_enabled",        False)),
            }

        if "prediction_monitor" in data:
            pm = data["prediction_monitor"]
            fc["prediction_monitor"] = {
                "enabled"          : bool(pm.get("enabled",           False)),
                "grace_minutes"    : int(pm.get("grace_minutes",      30)),
                "poll_interval_sec": int(pm.get("poll_interval_sec",  300)),
            }

        if "data_flow" in data:
            df = data["data_flow"]
            raw = df.get("disabled_sources", [])
            if isinstance(raw, str):
                raw = [s.strip() for s in raw.split(",") if s.strip()]
            _valid = {"scada", "additive", "preparedsand_extra",
                      "preparedsand", "consumption", "rejections"}
            fc["data_flow"] = {
                "disabled_sources": [s for s in raw if s in _valid],
            }

        if "cost_saving" in data:
            cs = data["cost_saving"]
            fc["cost_saving"] = {
                "enabled"        : bool(cs.get("enabled",        False)),
                "baseline_start" : str(cs.get("baseline_start",  "")).strip(),
                "baseline_end"   : str(cs.get("baseline_end",    "")).strip(),
                "cost_per_mt"    : float(cs.get("cost_per_mt",   7000)),
                "currency"       : str(cs.get("currency",        "INR")).strip().upper(),
            }

        if "notifications" in data:
            fc.setdefault("notifications", {})
            if "email" in data["notifications"]:
                email_data = data["notifications"]["email"]
                existing_email = fc.get("notifications", {}).get("email", {})
                # Merge but handle recipients list specially
                existing_email.update({k: v for k, v in email_data.items() if k != "recipients"})
                if "recipients" in email_data:
                    _valid_types = {"SI", "BAD_BATCH", "PRESCRIPTION",
                                    "DATA_FLOW", "PREDICTION_MISSING", "SMC_BATCH"}
                    clean_recip = []
                    for r in email_data["recipients"]:
                        addr = str(r.get("email", "")).strip()
                        if not addr:
                            continue
                        types = [t for t in r.get("types", []) if t in _valid_types]
                        clean_recip.append({"email": addr, "types": types})
                    existing_email["recipients"] = clean_recip
                fc.setdefault("notifications", {})["email"] = existing_email

        if "webhook" in data:
            wh = data["webhook"]
            fc["webhook"] = {
                "enabled"          : bool(wh.get("enabled",           False)),
                "base_url"         : str(wh.get("base_url",           "")).strip().rstrip("/"),
                "foundry_key"      : str(wh.get("foundry_key",        "")).strip(),
                "timeout_sec"      : int(wh.get("timeout_sec",        10)),
                "shift_names"      : {
                    str(k): str(v)
                    for k, v in wh.get("shift_names", {}).items()
                },
                "send_si"          : bool(wh.get("send_si",           True)),
                "send_bad_batch"   : bool(wh.get("send_bad_batch",    False)),
                "send_smc_batch"   : bool(wh.get("send_smc_batch",    False)),
                "send_prescription": bool(wh.get("send_prescription", False)),
                "send_sieve"       : bool(wh.get("send_sieve",        False)),
                "send_prediction"  : bool(wh.get("send_prediction",   False)),
                "min_severity"     : str(wh.get("min_severity",       "warning")).lower(),
            }

        if "param_chart_limits" in data:
            def _lim(v):
                try: return float(v) if v not in (None, "", "null") else None
                except (TypeError, ValueError): return None
            raw_lim = data["param_chart_limits"]
            fc["param_chart_limits"] = {
                col: {"min": _lim(bounds.get("min")), "max": _lim(bounds.get("max"))}
                for col, bounds in raw_lim.items()
                if isinstance(bounds, dict)
            }

        if "additive_delay_seconds" in data:
            try:
                fc["additive_delay_seconds"] = max(0, int(data["additive_delay_seconds"]))
            except (TypeError, ValueError):
                fc["additive_delay_seconds"] = 0

        if "material_tolerance_pct" in data:
            try:
                fc["material_tolerance_pct"] = max(0.1, float(data["material_tolerance_pct"]))
            except (TypeError, ValueError):
                fc["material_tolerance_pct"] = 3.0

        # Save to registry DB only -- watchdog_config.json is connection credentials only
        if not _reg_engine:
            return jsonify({"error": "Registry DB not available -- cannot save config"}), 503

        from .config_store import save_foundry_config_db
        saved = save_foundry_config_db(_reg_engine, label, fc)
        if not saved:
            return jsonify({"error": "DB write failed -- check logs"}), 500

        return jsonify({"ok": True, "label": label, "saved": True, "source": "DB"})
    except Exception as exc:
        logger.error("save_foundry_config failed (%s): %s", label, exc)
        return jsonify({"error": str(exc)}), 500


# --- Helper -------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("date", "created_at", "updated_at", "acknowledged_at", "batch_time"):
        if d.get(key) is not None:
            d[key] = str(d[key])
    for key in ("params_json", "deviations_json"):
        raw = d.get(key)
        if isinstance(raw, str):
            try:
                d[key] = json.loads(raw)
            except Exception:
                d[key] = []
        elif raw is None:
            d[key] = []
    # component_info_json -> dict (not list)
    ci = d.get("component_info_json")
    if isinstance(ci, str):
        try:
            d["component_info_json"] = json.loads(ci)
        except Exception:
            d["component_info_json"] = {}
    elif ci is None:
        d["component_info_json"] = {}
    return d


# --- Entry point --------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Watchdog Alert Dashboard Server")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--port",   type=int,  default=5055)
    parser.add_argument("--host",   default="0.0.0.0")
    parser.add_argument("--debug",  action="store_true")
    args = parser.parse_args()

    cfg_path = args.config.resolve()
    if not cfg_path.exists():
        print(f"ERROR: config not found: {cfg_path}", file=sys.stderr)
        sys.exit(1)

    global _config, _config_path, _reg_engine
    _config_path = cfg_path
    with open(cfg_path, encoding="utf-8") as f:
        _config = json.load(f)

    # -- Env var overrides (env vars win over file values) ---------------------
    import os as _os
    _ENV_MAP = {
        "ANTHROPIC_API_KEY"   : ("anthropic_api_key",),
        "GROQ_API_KEY"        : ("groq_api_key",),
        "DASHBOARD_SECRET"    : ("dashboard_secret",),
        "REGISTRY_DB_HOST"    : ("registry_database", "host"),
        "REGISTRY_DB_NAME"    : ("registry_database", "name"),
        "REGISTRY_DB_USER"    : ("registry_database", "user"),
        "REGISTRY_DB_PASSWORD": ("registry_database", "password"),
        "DB_HOST"             : ("database", "host"),
        "DB_NAME"             : ("database", "name"),
        "DB_USER"             : ("database", "user"),
        "DB_PASSWORD"         : ("database", "password"),
    }
    for env_key, path in _ENV_MAP.items():
        val = _os.environ.get(env_key)
        if val:
            if len(path) == 1:
                _config[path[0]] = val
            else:
                _config.setdefault(path[0], {})[path[1]] = val
            logger.info("Config: %s overridden from environment", env_key)

    app.secret_key = _config.get("dashboard_secret", "sandman-watchdog-dashboard-2026")

    # -- Bootstrap DB config store ---------------------------------------------
    from .config_store import get_registry_engine, ensure_config_table, load_all_configs
    try:
        _reg_engine = get_registry_engine(_config)
        if _reg_engine:
            ensure_config_table(_reg_engine)
            db_configs = load_all_configs(_reg_engine)
            if db_configs:
                _config.setdefault("foundry_configs", {}).update(db_configs)
                logger.info("Loaded %d per-foundry config(s) from DB", len(db_configs))
    except Exception as exc:
        logger.warning("Config DB bootstrap failed -- using JSON file only: %s", exc)
        _reg_engine = None

    # -- Migrate watchdog_alerts table (adds new columns if missing) -----------
    try:
        from .alert_db_writer import ensure_table
        ensure_table(_get_engine())
        logger.info("watchdog_alerts schema up to date")
    except Exception as exc:
        logger.warning("watchdog_alerts migration failed: %s", exc)

    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    )
    logger.info("Alert dashboard -> http://%s:%d/", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


# ── Per-foundry config + engine helpers for multi-DB endpoints ────────────────

def _foundry_cfg(db_name: str, line_id: int) -> dict:
    """Build a minimal config dict for a specific foundry DB + line."""
    # Start from the global config's database block (host/user/pass)
    base_db = dict(_config.get("database", {}))
    base_db["name"] = db_name
    # Overlay any per-foundry config stored in foundry_configs
    label = f"{db_name}_L{line_id}"
    overlay = _config.get("foundry_configs", {}).get(label, {})
    cfg = {**_config, "database": base_db, "foundry_line_id": line_id, **overlay}
    return cfg


def _get_engine_for(cfg: dict):
    """Return a SQLAlchemy engine for an arbitrary config dict."""
    from .pipeline.db_connector import get_engine as _ge
    return _ge(cfg)


# ── Prepared sand snapshot for a component's production window ────────────────

_PS_PROPERTIES = [
    "active_clay", "compactibility", "gcs", "gfn_afs", "inert_fines",
    "loi", "moisture", "permeability", "shear_strength", "split_strength",
    "temp_of_sand_after_mix", "volatile_matter",
]

def _fetch_ps_for_component(engine, line_id: int, component_id: str,
                             batch_time, alert_date) -> dict:
    """
    Return prepared sand property averages for the time window during which
    `component_id` was being produced.

    Strategy:
      1. Find the production window for this component:
           start = first additive batch time for this component on alert_date
           end   = last  additive batch time for this component on alert_date
         (fallback: use the alert batch_time ±4 hours if no additive data)
      2. Query preparedsand for rows in [start, end] for this foundry line.
      3. Average values if multiple rows; use single row if only one.
      4. For any property that is NULL in the window, fall back to the most
         recent non-null value before the window.

    Returns a dict: {property: {value, n_readings, source}}
      source is "window_avg" | "window_single" | "last_available" | "unavailable"
    """
    from sqlalchemy import text as _text
    from datetime import timedelta

    if not component_id or component_id in ("", "nan", "None"):
        return {}

    # ── Step 1: Find component production window from additive table ──────────
    try:
        window_sql = _text("""
            SELECT MIN(timestamp) AS t_start, MAX(timestamp) AS t_end
            FROM   `additive`
            WHERE  foundry_line_id = :fl_id
              AND  deleted = 0
              AND  component_id = :comp
              AND  DATE(timestamp) = :dt
        """)
        with engine.connect() as conn:
            w = conn.execute(window_sql, {
                "fl_id": line_id,
                "comp" : component_id,
                "dt"   : str(alert_date or ""),
            }).mappings().first()

        if w and w["t_start"] and w["t_end"]:
            t_start = w["t_start"]
            t_end   = w["t_end"]
        elif batch_time:
            # fallback: ±4 h around the alert batch time
            t_start = batch_time
            t_end   = batch_time
        else:
            return {}
    except Exception:
        return {}

    # Add a 30-min buffer each side to catch PS readings taken just outside the window
    try:
        t_start_buf = t_start - timedelta(minutes=30)
        t_end_buf   = t_end   + timedelta(minutes=30)
    except Exception:
        t_start_buf = t_start
        t_end_buf   = t_end

    # ── Step 2: Query preparedsand for readings within the window ─────────────
    ps_cols = ", ".join(f"`{c}`" for c in _PS_PROPERTIES)
    try:
        ps_sql = _text(f"""
            SELECT {ps_cols}, `date`, `timestamp`
            FROM   `preparedsand`
            WHERE  `foundry_line_id` = :fl_id
              AND  `deleted` = 0
              AND  `timestamp` BETWEEN :t_start AND :t_end
            ORDER  BY `timestamp` ASC
        """)
        with engine.connect() as conn:
            rows = conn.execute(ps_sql, {
                "fl_id"  : line_id,
                "t_start": t_start_buf,
                "t_end"  : t_end_buf,
            }).mappings().fetchall()
    except Exception:
        rows = []

    result = {}

    for prop in _PS_PROPERTIES:
        values = [float(r[prop]) for r in rows if r[prop] is not None
                  and str(r[prop]) not in ("", "nan")]
        if values:
            avg = round(sum(values) / len(values), 4)
            source = "window_avg" if len(values) > 1 else "window_single"
            result[prop] = {"value": avg, "n_readings": len(values), "source": source}
        else:
            # Step 4: fallback to last available value before the window
            try:
                fb_sql = _text(f"""
                    SELECT `{prop}`
                    FROM   `preparedsand`
                    WHERE  `foundry_line_id` = :fl_id
                      AND  `deleted` = 0
                      AND  `{prop}` IS NOT NULL
                      AND  `timestamp` < :t_start
                    ORDER  BY `timestamp` DESC
                    LIMIT  1
                """)
                with engine.connect() as conn:
                    fb = conn.execute(fb_sql, {"fl_id": line_id, "t_start": t_start_buf}).mappings().first()
                if fb and fb[prop] is not None:
                    result[prop] = {
                        "value"    : round(float(fb[prop]), 4),
                        "n_readings": 0,
                        "source"   : "last_available",
                    }
                else:
                    result[prop] = {"value": None, "n_readings": 0, "source": "unavailable"}
            except Exception:
                result[prop] = {"value": None, "n_readings": 0, "source": "unavailable"}

    result["_window"] = {
        "component_id": component_id,
        "t_start"     : str(t_start),
        "t_end"       : str(t_end),
        "ps_rows_in_window": len(rows),
    }
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS API
#  GET /api/notifications?db=caspro_sandman&line_id=1&since_minutes=60
#
#  Returns a ready-to-notify payload for any Bad Batch or Prescription alert
#  fired in the last N minutes. The consumer (external system, email sender,
#  Slack bot, etc.) calls this endpoint and sends the notification itself.
#  The watchdog does NOT send email — it only builds the structured alert.
#
#  Response shape per alert:
#  {
#    "id": 42,
#    "alert_type": "BAD_BATCH" | "PRESCRIPTION",
#    "severity": "CRITICAL" | "WARNING",
#    "foundry_line_id": 1,
#    "component_id": "51012600",
#    "group_name": "Housing Group",
#    "date": "2026-05-31",
#    "shift": "2",
#    "batch_pkey": 18442,
#    "batch_time": "2026-05-31T14:23:00",
#    "title": "Bad Batch Detected — Component 51012600",
#    "summary": "SMC discharge (42.3%) vs COSP (38.1%) differ by +4.2%...",
#    "detail": {                 # type-specific detail
#       ... (see below per type)
#    },
#    "related_bad_batches": [],  # Prescription only: simultaneous bad batches
#    "notified_at": null,        # set to now on first read via mark-notified
#  }
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/notifications")
def get_notifications():
    """
    Return unnotified Bad Batch, Prescription, and SI alerts for a foundry line
    fired in the last `since_minutes` minutes (default 120).
    Also auto-sends email for each unnotified alert and marks it as notified.

    Query params:
      db             -- foundry DB name (required)
      line_id        -- foundry line id (default 1)
      since_minutes  -- look-back window in minutes (default 120, max 1440)
      types          -- comma-separated: BAD_BATCH,PRESCRIPTION,SI (default all three)
      include_read   -- 1 to include already-notified alerts (default 0)
    """
    db_name       = request.args.get("db")
    line_id       = request.args.get("line_id", 1, type=int)
    since_min     = min(request.args.get("since_minutes", 120, type=int), 1440)
    types_param   = request.args.get("types", "BAD_BATCH,PRESCRIPTION")
    include_read  = request.args.get("include_read", 0, type=int)

    if not db_name:
        return jsonify({"error": "db parameter is required"}), 400

    valid_types = {"BAD_BATCH", "PRESCRIPTION", "SI"}
    req_types   = [t.strip().upper() for t in types_param.split(",") if t.strip().upper() in valid_types]
    if not req_types:
        req_types = list(valid_types)

    try:
        cfg    = _foundry_cfg(db_name, line_id)
        engine = _get_engine_for(cfg)
        # Merge DB-stored foundry config (has email/notifications settings from Config UI)
        if _reg_engine:
            from .config_store import load_foundry_config as _lfc
            _db_fc = _lfc(_reg_engine, f"{db_name}_L{line_id}")
            if _db_fc.get("notifications"):
                cfg = {**cfg, "notifications": _db_fc["notifications"]}
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    type_placeholders = ", ".join(f":t{i}" for i in range(len(req_types)))
    type_params       = {f"t{i}": v for i, v in enumerate(req_types)}

    read_filter = "" if include_read else "AND (`notified_at` IS NULL)"

    sql = text(f"""
        SELECT
            `id`, `alert_type`, `foundry_line_id`, `date`, `shift`,
            `batch_pkey`, `period_key`, `alert_level`,
            `component_id`, `group_name`, `batch_time`,
            `deviations_json`,
            `smc_value`, `cosp_value`, `smc_cosp_diff`, `bb_threshold`,
            `root_cause`, `recommendation`,
            `notified_at`, `created_at`
        FROM `watchdog_alerts`
        WHERE `foundry_line_id` = :line_id
          AND `alert_type`      IN ({type_placeholders})
          AND `created_at`      >= NOW() - INTERVAL :mins MINUTE
          AND (`acknowledged`   = 0 OR `acknowledged` IS NULL)
          {read_filter}
        ORDER BY `id` DESC
        LIMIT 100
    """)

    with engine.connect() as conn:
        rows = conn.execute(
            sql,
            {"line_id": line_id, "mins": since_min, **type_params}
        ).mappings().fetchall()

    alerts = []
    for r in rows:
        atype  = r["alert_type"]
        detail = {}

        if atype == "BAD_BATCH":
            smc       = float(r["smc_value"])  if r["smc_value"]  is not None else None
            cosp      = float(r["cosp_value"]) if r["cosp_value"] is not None else None
            diff      = float(r["smc_cosp_diff"]) if r["smc_cosp_diff"] is not None else None
            threshold = float(r["bb_threshold"])  if r["bb_threshold"]  is not None else 2.0
            direction = "SMC higher than COSP" if (diff or 0) > 0 else "SMC lower than COSP"
            severity  = "CRITICAL" if diff is not None and abs(diff) > threshold * 1.5 else "WARNING"
            summary   = (
                f"SMC discharge ({smc:.2f}%) vs COSP ({cosp:.2f}%) differ by "
                f"{diff:+.2f}% — threshold is ±{threshold}. {direction}."
            ) if smc is not None and cosp is not None else "Bad batch detected."
            detail = {
                "smc_value"   : smc,
                "cosp_value"  : cosp,
                "difference"  : diff,
                "threshold"   : threshold,
                "direction"   : direction,
            }

        elif atype == "PRESCRIPTION":
            import json as _json
            devs_raw = r["deviations_json"]
            devs     = _json.loads(devs_raw) if devs_raw else []
            out_of_tol  = [d for d in devs if not d.get("within", True)]
            total       = len(devs)
            n_out       = len(out_of_tol)
            severity    = "CRITICAL" if n_out >= 2 else "WARNING" if n_out == 1 else "OK"
            summary     = (
                f"{n_out} of {total} additive parameter(s) deviated from the AI prescription. "
                + (
                    "Parameters out of tolerance: "
                    + ", ".join(
                        f"{d.get('label', d.get('param',''))} "
                        f"(prescribed {d.get('prescribed', 0):.2f}, "
                        f"actual {d.get('actual', 0):.2f}, "
                        f"{d.get('pct_diff', 0):+.1f}%)"
                        for d in out_of_tol
                    ) + "."
                    if out_of_tol else "All parameters within tolerance."
                )
            )
            detail = {
                "deviations"   : devs,
                "out_of_tolerance": out_of_tol,
                "total_params" : total,
                "n_out"        : n_out,
            }

            # Check for simultaneous bad batches on the same component + date + shift
            try:
                bb_sql = text("""
                    SELECT `id`, `batch_pkey`, `smc_value`, `cosp_value`, `smc_cosp_diff`, `bb_threshold`
                    FROM `watchdog_alerts`
                    WHERE `foundry_line_id` = :line_id
                      AND `alert_type`      = 'BAD_BATCH'
                      AND `component_id`    = :comp
                      AND `date`            = :dt
                      AND `shift`           = :shift
                    ORDER BY `id` DESC LIMIT 10
                """)
                with engine.connect() as conn2:
                    bb_rows = conn2.execute(bb_sql, {
                        "line_id": line_id,
                        "comp"   : str(r["component_id"] or ""),
                        "dt"     : str(r["date"] or ""),
                        "shift"  : str(r["shift"] or ""),
                    }).mappings().fetchall()
                detail["simultaneous_bad_batches"] = [
                    {
                        "id"        : bb["id"],
                        "batch_pkey": bb["batch_pkey"],
                        "smc"       : float(bb["smc_value"])      if bb["smc_value"]      else None,
                        "cosp"      : float(bb["cosp_value"])     if bb["cosp_value"]     else None,
                        "diff"      : float(bb["smc_cosp_diff"])  if bb["smc_cosp_diff"]  else None,
                        "threshold" : float(bb["bb_threshold"])   if bb["bb_threshold"]   else 2.0,
                    }
                    for bb in bb_rows
                ]
            except Exception:
                detail["simultaneous_bad_batches"] = []

        elif atype == "SI":
            import json as _json
            params_raw = r.get("params_json") or "[]"
            params     = _json.loads(params_raw) if isinstance(params_raw, str) else (params_raw or [])
            si_score   = float(r["si_score"]) if r["si_score"] is not None else None
            import re as _re
            al         = _re.sub(r'[^A-Z]', '', str(r["alert_level"] or "").upper())
            severity   = al if al in ("CRITICAL", "ALERT", "WARNING", "WATCH") else "WATCH"
            crit_params = [p for p in params if (p.get("alert_level") or "").upper() not in ("STABLE", "OK", "")]
            crit_names  = ", ".join(p.get("label") or p.get("param", "") for p in crit_params[:5])
            summary = (
                f"Stability Index {si_score:.1f} — {severity}. "
                f"{len(crit_params)} parameter(s) flagged"
                + (f": {crit_names}" if crit_names else "") + "."
            ) if si_score is not None else f"SI alert — {severity}."
            detail = {
                "si_score"   : si_score,
                "alert_level": al,
                "params"     : params,
                "n_flagged"  : len(crit_params),
            }

        comp = str(r["component_id"] or "")
        title = (
            f"Bad Batch Detected — Component {comp}"   if atype == "BAD_BATCH"   else
            f"Prescription Deviation — Component {comp}" if atype == "PRESCRIPTION" else
            f"SI {detail.get('alert_level','ALERT')} — {str(r['period_key'] or comp or 'Shift ' + str(r['shift'] or ''))}"
        )

        # ── Prepared sand snapshot for the component's production window ──────
        ps_data = _fetch_ps_for_component(engine, line_id, comp, r["batch_time"], r["date"])

        alerts.append({
            "id"              : r["id"],
            "alert_type"      : atype,
            "severity"        : severity,
            "foundry_line_id" : r["foundry_line_id"],
            "component_id"    : comp,
            "group_name"      : str(r["group_name"] or ""),
            "date"            : str(r["date"] or ""),
            "shift"           : str(r["shift"] or ""),
            "period_key"      : str(r["period_key"] or ""),
            "batch_pkey"      : r["batch_pkey"],
            "batch_time"      : str(r["batch_time"] or ""),
            "title"           : title,
            "summary"         : summary,
            "detail"          : detail,
            "prepared_sand"   : ps_data,
            "root_cause"      : str(r["root_cause"]     or ""),
            "recommendation"  : str(r["recommendation"] or ""),
            "notified_at"     : str(r["notified_at"] or ""),
            "created_at"      : str(r["created_at"] or ""),
        })

    # ── Auto-send email for every unnotified alert and mark as notified ──────
    if not include_read:
        try:
            from .email_notifier import (
                send_bad_batch_email, send_prescription_email,
                send_si_alert_email, check_and_send_combined,
            )
            notified_ids = []
            for a in alerts:
                if a["notified_at"]:       # already notified — skip
                    continue
                atype   = a["alert_type"]
                sent    = False
                result  = {
                    "component_id" : a["component_id"],
                    "group_name"   : a["group_name"],
                    "date"         : a["date"],
                    "shift"        : a["shift"],
                    "batch_pkey"   : a["batch_pkey"],
                    "batch_time"   : a["batch_time"],
                }
                if atype == "BAD_BATCH":
                    d = a["detail"]
                    result.update({
                        "smc_value"    : d.get("smc_value"),
                        "cosp_value"   : d.get("cosp_value"),
                        "smc_cosp_diff": d.get("difference"),
                        "threshold"    : d.get("threshold", 2.0),
                    })
                    sent = send_bad_batch_email(result, cfg, label=f"{db_name}_L{line_id}")

                elif atype == "PRESCRIPTION":
                    d    = a["detail"]
                    devs = d.get("deviations", [])
                    result["pkey"] = a["batch_pkey"]
                    sent = send_prescription_email(
                        result, devs, cfg, label=f"{db_name}_L{line_id}"
                    )

                elif atype == "SI":
                    al = (a["detail"].get("alert_level") or a.get("severity", "")).upper()
                    # Skip WATCH and STABLE — only email CRITICAL / WARNING / ALERT
                    if al not in ("CRITICAL", "WARNING", "ALERT"):
                        continue
                    d = a["detail"]
                    result.update({
                        "period_key"    : a.get("period_key", ""),
                        "si_score"      : d.get("si_score"),
                        "alert_level"   : al,
                        "root_cause"    : a.get("root_cause", ""),
                        "recommendation": a.get("recommendation", ""),
                        "params_json"   : d.get("params", []),
                    })
                    sent = send_si_alert_email(result, cfg, label=f"{db_name}_L{line_id}")

                if sent:
                    notified_ids.append(a["id"])

            # Mark successfully emailed alerts as notified in one query
            if notified_ids:
                placeholders = ", ".join(f":nid{i}" for i in range(len(notified_ids)))
                id_params    = {f"nid{i}": v for i, v in enumerate(notified_ids)}
                with engine.begin() as conn:
                    conn.execute(
                        text(f"UPDATE `watchdog_alerts` SET `notified_at` = NOW() WHERE `id` IN ({placeholders})"),
                        id_params,
                    )
                # Reflect notified_at in the response payload
                for a in alerts:
                    if a["id"] in set(notified_ids):
                        a["notified_at"] = "sent"

            # Check for combined (bad batch + prescription on same component) after
            # individual emails are done — avoids double-sending the combined email
            comp_shifts_seen = set()
            for a in alerts:
                key = (a["component_id"], a["date"], a["shift"])
                if key not in comp_shifts_seen:
                    comp_shifts_seen.add(key)
                    check_and_send_combined(
                        engine, cfg,
                        component_id=a["component_id"],
                        date_str=a["date"],
                        shift=a["shift"],
                        foundry_line_id=line_id,
                        label=f"{db_name}_L{line_id}",
                    )
        except Exception:
            import traceback as _tb
            logger.warning("auto-email in /api/notifications failed:\n%s", _tb.format_exc())

    return jsonify({"alerts": alerts, "count": len(alerts)})


@app.route("/api/notifications/<int:alert_id>/mark-notified", methods=["POST"])
def mark_notified(alert_id: int):
    """
    Mark an alert as notified so it won't appear in the next /api/notifications poll.
    Call this after your external system has successfully sent the notification.

    POST /api/notifications/42/mark-notified?db=caspro_sandman&line_id=1
    """
    db_name = request.args.get("db")
    line_id = request.args.get("line_id", 1, type=int)
    if not db_name:
        return jsonify({"error": "db parameter is required"}), 400
    try:
        cfg    = _foundry_cfg(db_name, line_id)
        engine = _get_engine_for(cfg)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE `watchdog_alerts` SET `notified_at` = NOW() WHERE `id` = :id"),
                {"id": alert_id},
            )
        return jsonify({"ok": True, "id": alert_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Test-email endpoint (Config UI) ───────────────────────────────────────────
@app.route("/api/run-once", methods=["POST"])
def run_once():
    """
    Run all alert modes for a specific date and send emails for critical/alert results.

    POST /api/run-once
    Body JSON:
      {
        "db"      : "caspro_sandman",
        "line_id" : 1,
        "date"    : "2026-04-14",   // required
        "shift"   : "2"             // optional — if omitted runs all shifts on that date
      }

    Runs: SI (window + shift + component) -> bad batch -> prescription -> send email notifications.
    Returns a summary of what was written and emailed.
    """
    import copy
    import traceback as _tb

    data    = request.get_json(silent=True) or {}
    db_name = data.get("db")
    line_id = int(data.get("line_id", 1))
    date_str= str(data.get("date", "")).strip()
    shift_f = data.get("shift")   # optional filter

    if not db_name:
        return jsonify({"error": "db is required"}), 400
    if not date_str:
        return jsonify({"error": "date is required (YYYY-MM-DD)"}), 400

    try:
        import pandas as _pd
        target_date = _pd.to_datetime(date_str).date()
    except Exception:
        return jsonify({"error": f"Invalid date: {date_str}"}), 400

    label = f"{db_name}_L{line_id}"
    summary = {"label": label, "date": date_str, "written": [], "errors": []}

    try:
        # ── Build config (DB config takes priority over JSON) ─────────────────
        cfg    = _foundry_cfg(db_name, line_id)
        engine = _get_engine_for(cfg)
        if _reg_engine:
            from .config_store import load_foundry_config as _lfc
            _db_fc = _lfc(_reg_engine, label)
            if _db_fc:
                import copy as _cp
                merged = {**cfg}
                for k, v in _db_fc.items():
                    if k == "database":
                        continue
                    # Deep-merge dicts so nested keys (e.g. engines.window) are preserved
                    if isinstance(v, dict) and isinstance(merged.get(k), dict):
                        merged[k] = {**merged[k], **v}
                    else:
                        merged[k] = v
                cfg = merged
        # Ensure engines.window always exists (required by run_for_period)
        cfg.setdefault("engines", {}).setdefault("window", 5)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    try:
        from .alert_db_writer import ensure_table, write_si_alert
        from .pipeline.db_connector import get_engine as _ge
        from .pipeline.data_fetcher import (
            fetch_monitored_parameters, fetch_display_names, fetch_control_limits,
        )
        from .run_watchdog import run_for_period
        from .run_alert_monitor import _run_component_pipeline

        ensure_table(engine)

        # Load live params / display names / limits from DB
        try:
            monitored = fetch_monitored_parameters(cfg)
            for key in ("prepared_sand", "consumption", "additive", "prepared_sand_extra"):
                if monitored.get(key):
                    cfg["parameters"][key] = monitored[key]
        except Exception as e:
            summary["errors"].append(f"fetch_monitored_parameters: {e}")

        try:
            cfg["display_names"] = fetch_display_names(cfg)
        except Exception:
            pass

        try:
            db_limits = fetch_control_limits(cfg)
        except Exception:
            db_limits = {}

        display_names = cfg.get("display_names", {})
        customer_pkey = cfg.get("customer_pkey", 0)
        pct_warn      = float(cfg.get("pct_change_warning", 5.0))

        def _write_si(result, mode_lbl):
            if not result:
                return
            try:
                write_si_alert(engine, result, line_id,
                               display_names=display_names,
                               customer_pkey=customer_pkey,
                               pct_warn=pct_warn)
                lvl = result.get("final_alert", "?")
                si  = result.get("si_score_100", 0)
                summary["written"].append({"mode": mode_lbl, "level": lvl, "si_score": round(float(si or 0), 1)})
            except Exception as e:
                summary["errors"].append(f"{mode_lbl} write_si: {e}")

        def _write_prop(result, mode_lbl):
            pass  # property alerts removed

        # ── Resolve shifts on target date ─────────────────────────────────────
        try:
            rows = engine.connect().execute(text(
                "SELECT DISTINCT `shift` FROM `preparedsand` "
                "WHERE `foundry_line_id`=:fl AND `deleted`=0 "
                "  AND DATE(`date`)=:dt AND `shift` IS NOT NULL ORDER BY `shift`"
            ), {"fl": line_id, "dt": str(target_date)}).fetchall()
            shifts = [str(r[0]) for r in rows if r[0] is not None]
        except Exception:
            shifts = []

        if shift_f:
            shifts = [s for s in shifts if str(s) == str(shift_f)] or [str(shift_f)]
        if not shifts:
            shifts = [None]

        # ── 1. SHIFT mode ─────────────────────────────────────────────────────
        for sh in shifts:
            try:
                sh_cfg = copy.deepcopy(cfg)
                sh_cfg["aggregation"]["mode"] = "shift"
                sh_cfg.setdefault("trigger", {})["mode"] = "shift"
                result = run_for_period(sh_cfg, "shift",
                                        trigger_date=target_date, trigger_shift=sh,
                                        db_limits=db_limits)
                _write_si(result, f"SHIFT-{sh}")
                _write_prop(result, f"SHIFT-{sh}")
            except Exception as e:
                summary["errors"].append(f"SHIFT-{sh}: {e}")

        # ── 2. COMPONENT mode ─────────────────────────────────────────────────
        try:
            _comp_target, _day_results = _run_component_pipeline(cfg, db_limits, target_date, shifts[0] if shifts else None)
            _to_write = [r for r in (_day_results or []) if r] or ([_comp_target] if _comp_target else [])
            for r in _to_write:
                _cid = str(r.get("component_id") or "?")[:14]
                _write_si(r, f"COMP-{_cid}")
                _write_prop(r, f"COMP-{_cid}")
        except Exception as e:
            summary["errors"].append(f"COMPONENT: {e}")

        # ── 3. BAD BATCH ──────────────────────────────────────────────────────
        try:
            from .bad_batch_monitor import run_check as bb_check
            bb_check(cfg, write_db=True, target_date=str(target_date))
            summary["written"].append({"mode": "BAD_BATCH"})
        except TypeError:
            # run_check doesn't accept target_date — run without it
            try:
                from .bad_batch_monitor import run_check as bb_check
                bb_check(cfg, write_db=True)
                summary["written"].append({"mode": "BAD_BATCH"})
            except Exception as e:
                summary["errors"].append(f"BAD_BATCH: {e}")
        except Exception as e:
            summary["errors"].append(f"BAD_BATCH: {e}")

        # ── 4. PRESCRIPTION ───────────────────────────────────────────────────
        try:
            from .prescription_watchdog import run_check as presc_check
            presc_check(cfg, write_db=True, target_date=str(target_date))
            summary["written"].append({"mode": "PRESCRIPTION"})
        except TypeError:
            try:
                from .prescription_watchdog import run_check as presc_check
                presc_check(cfg, write_db=True)
                summary["written"].append({"mode": "PRESCRIPTION"})
            except Exception as e:
                summary["errors"].append(f"PRESCRIPTION: {e}")
        except Exception as e:
            summary["errors"].append(f"PRESCRIPTION: {e}")

    except Exception as exc:
        summary["errors"].append(f"fatal: {_tb.format_exc()}")
        return jsonify(summary), 500

    # ── 5. Send emails for any newly created critical/alert alerts ────────────
    try:
        from .email_notifier import (
            send_bad_batch_email, send_prescription_email,
            send_si_alert_email, check_and_send_combined,
        )
        if _reg_engine:
            from .config_store import load_foundry_config as _lfc2
            _db_fc2 = _lfc2(_reg_engine, label)
            if _db_fc2.get("notifications"):
                cfg = {**cfg, "notifications": _db_fc2["notifications"]}

        # Reset notified_at for this date so run-once always re-sends (historical backfill)
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE watchdog_alerts
                SET notified_at = NULL
                WHERE foundry_line_id = :fl
                  AND DATE(`date`) = :dt
                  AND alert_type IN ('BAD_BATCH','PRESCRIPTION','SI')
            """), {"fl": line_id, "dt": date_str})

        # Fetch all unnotified alerts for this date
        rows = engine.connect().execute(text("""
            SELECT id, alert_type, alert_level, component_id, group_name,
                   `date`, `shift`, period_key, batch_pkey, batch_time,
                   smc_value, cosp_value, smc_cosp_diff, bb_threshold,
                   deviations_json, params_json, si_score,
                   root_cause, recommendation, notified_at
            FROM watchdog_alerts
            WHERE foundry_line_id = :fl
              AND DATE(`date`) = :dt
              AND (notified_at IS NULL)
              AND alert_type IN ('BAD_BATCH','PRESCRIPTION','SI')
            ORDER BY id DESC
        """), {"fl": line_id, "dt": date_str}).mappings().fetchall()

        import json as _json, re as _re
        from .email_notifier import (
            send_bad_batch_email, send_prescription_email,
            send_si_alert_email, check_and_send_combined,
        )

        notified_ids = []
        emails_sent  = []
        for r in rows:
            atype = r["alert_type"]
            al    = _re.sub(r'[^A-Z]', '', str(r["alert_level"] or "").upper())
            sent  = False
            base  = {
                "component_id": str(r["component_id"] or ""),
                "group_name"  : str(r["group_name"] or ""),
                "date"        : str(r["date"] or ""),
                "shift"       : str(r["shift"] or ""),
                "batch_pkey"  : r["batch_pkey"],
                "batch_time"  : str(r["batch_time"] or ""),
            }
            if atype == "BAD_BATCH":
                base.update({
                    "smc_value"    : float(r["smc_value"])     if r["smc_value"]     else None,
                    "cosp_value"   : float(r["cosp_value"])    if r["cosp_value"]    else None,
                    "smc_cosp_diff": float(r["smc_cosp_diff"]) if r["smc_cosp_diff"] else None,
                    "threshold"    : float(r["bb_threshold"])  if r["bb_threshold"]  else 2.0,
                })
                sent = send_bad_batch_email(base, cfg, label=label)
            elif atype == "PRESCRIPTION":
                devs = _json.loads(r["deviations_json"]) if r["deviations_json"] else []
                base["pkey"] = r["batch_pkey"]
                sent = send_prescription_email(base, devs, cfg, label=label)
            elif atype == "SI":
                if al not in ("CRITICAL", "WARNING", "ALERT"):
                    continue
                params = _json.loads(r["params_json"]) if r["params_json"] else []
                base.update({
                    "period_key"    : str(r["period_key"] or ""),
                    "si_score"      : float(r["si_score"]) if r["si_score"] else None,
                    "alert_level"   : al,
                    "root_cause"    : str(r["root_cause"] or ""),
                    "recommendation": str(r["recommendation"] or ""),
                    "params_json"   : params,
                })
                sent = send_si_alert_email(base, cfg, label=label)
            if sent:
                notified_ids.append(r["id"])
                emails_sent.append({"id": r["id"], "type": atype, "level": al,
                                    "component": base.get("component_id"), "shift": base.get("shift")})

        if notified_ids:
            placeholders = ", ".join(f":nid{i}" for i in range(len(notified_ids)))
            id_params    = {f"nid{i}": v for i, v in enumerate(notified_ids)}
            with engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE `watchdog_alerts` SET `notified_at` = NOW() WHERE `id` IN ({placeholders})"),
                    id_params,
                )

        summary["emails_sent"]  = emails_sent
        summary["emails_count"] = len(notified_ids)
        summary["_debug"] = {
            "rows_found": len(rows),
            "email_enabled": cfg.get("notifications", {}).get("email", {}).get("enabled"),
            "alert_types_cfg": cfg.get("notifications", {}).get("email", {}).get("alert_types"),
        }

    except Exception as exc:
        summary["email_error"] = str(exc)

    return jsonify(summary)


@app.route("/api/config/test-webhook", methods=["POST"])
def test_webhook():
    """
    Proxy a test alert POST to the external push-notification API.
    Called by the browser to avoid CORS — server-to-server requests have no CORS restriction.
    """
    data = request.get_json(silent=True) or {}
    base_url = str(data.get("base_url", "")).rstrip("/")
    payload  = data.get("payload", {})
    timeout  = int(data.get("timeout_sec", 10))

    if not base_url:
        return jsonify({"ok": False, "error": "base_url is required"}), 400

    import requests as _req
    try:
        r = _req.post(
            f"{base_url}/api/sandman/alert",
            json=payload,
            timeout=timeout,
        )
        if r.ok:
            return jsonify({"ok": True, "status": r.status_code})
        else:
            try:
                body = r.json()
            except Exception:
                body = r.text[:200]
            return jsonify({"ok": False, "status": r.status_code, "error": body}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200


@app.route("/api/config/test-email", methods=["POST"])
def test_email():
    """Send a test email using the SMTP settings from the request body."""
    data = request.get_json(silent=True) or {}
    try:
        from .email_notifier import _send, _html_to_plain, _wrap_email, _C
        test_html = _wrap_email(
            title     = "Test Email",
            badge_txt = "TEST",
            badge_col = _C["sage"],
            badge_bg  = _C["sage_lt"],
            headline  = "SandMan® AI Watchdog — Test Notification",
            subline   = "This is a test email from the Watchdog configuration panel.",
            body      = "<p style='padding:16px;font-size:13px;color:#333;line-height:1.7'>"
                        "Your email notification settings are working correctly. "
                        "You will receive alerts like this when a Bad Batch or Prescription "
                        "Deviation is detected by the watchdog.</p>",
            dashboard_url = data.get("dashboard_url", "http://localhost:5055"),
        )
        _send(data, "Test Email — SandMan® AI Watchdog", test_html)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# ══════════════════════════════════════════════════════════════════════════════
#  SIGMA ZONE TREND API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/si-param-trend")
def si_param_trend():
    """
    Return time-series trend for a PS parameter + baseline sigma bands.

    Query params:
      db       -- foundry DB name  (required)
      line_id  -- foundry line id  (default 1)
      param    -- column name e.g. ps_inert_fines  (required)
      days     -- how many recent days to return   (default 60)
    """
    db_name  = request.args.get("db")
    line_id  = request.args.get("line_id", 1, type=int)
    param    = request.args.get("param", "")
    days     = request.args.get("days", 60, type=int)

    if not db_name or not param:
        return jsonify({"error": "db and param are required"}), 400

    try:
        import pandas as _pd
        from datetime import date as _dt, timedelta as _td
        from .pipeline.data_fetcher import fetch_all as _fa
        from .pipeline.aggregator   import build_dataset as _bd

        cfg = _foundry_cfg(db_name, line_id)

        # Baseline
        bl_start = _pd.to_datetime(cfg["baseline"]["start_date"]).date()
        bl_end   = _pd.to_datetime(cfg["baseline"]["end_date"]).date()
        raw_bl   = _fa(cfg, start_date=bl_start, end_date=bl_end)
        df_bl    = _bd(raw_bl, {**cfg, "aggregation": {**cfg.get("aggregation", {}), "mode": "shift"}})

        bl_mean = bl_std = None
        if not df_bl.empty and param in df_bl.columns:
            s = df_bl[param].dropna()
            if len(s) >= 3:
                bl_mean = round(float(s.mean()), 4)
                bl_std  = round(float(s.std(ddof=1)), 4)

        if bl_mean is None:
            return jsonify({"error": f"No baseline data for {param}"}), 404

        # Recent trend
        today   = _dt.today()
        raw_cur = _fa(cfg, start_date=today - _td(days=days), end_date=today)
        df_cur  = _bd(raw_cur, {**cfg, "aggregation": {**cfg.get("aggregation", {}), "mode": "shift"}})

        series = []
        if not df_cur.empty and param in df_cur.columns:
            for _, row in df_cur.iterrows():
                val = row.get(param)
                if val is None or (hasattr(val, "__float__") and val != val):
                    continue
                val = float(val)
                d   = str(row.get("date", ""))
                sh  = str(row.get("shift", ""))
                z   = abs(val - bl_mean) / bl_std if bl_std else 0
                series.append({
                    "label" : f"{d} S{sh}",
                    "date"  : d,
                    "shift" : sh,
                    "value" : round(val, 4),
                    "z"     : round(z, 3),
                })

        return jsonify({
            "param"   : param,
            "bl_mean" : bl_mean,
            "bl_std"  : bl_std,
            "series"  : series,
        })

    except Exception as exc:
        logger.error("si_param_trend error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  COMPONENT REPORT API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/component-report/data")
def component_report_data():
    """
    Return structured component report data (JSON).

    Query params:
      db           -- foundry DB name (required)
      line_id      -- foundry line id (default 1)
      component_id -- component ID (required)
      date         -- YYYY-MM-DD (required)
      shift        -- shift number (optional)
      threshold    -- bad batch threshold (default 2.0)
    """
    db_name      = request.args.get("db")
    line_id      = request.args.get("line_id", 1, type=int)
    component_id = request.args.get("component_id", "")
    date_str     = request.args.get("date", "")
    shift        = request.args.get("shift") or None
    threshold    = request.args.get("threshold", 2.0, type=float)

    if not db_name or not component_id or not date_str:
        return jsonify({"error": "db, component_id and date are required"}), 400

    try:
        cfg    = _foundry_cfg(db_name, line_id)
        engine = _get_engine_for(cfg)

        # Load SI config (watchdog_si_config) — contains additive_delay_seconds,
        # prescription thresholds etc. This is separate from the connection config (cfg).
        from .config_store import load_foundry_config as _lfc
        _si_cfg = _lfc(_reg_engine, f"{db_name}_L{line_id}") if _reg_engine else {}

        from .report_generator import fetch_component_data
        data = fetch_component_data(engine, line_id, component_id, date_str, shift, threshold, foundry_cfg=_si_cfg)

        data["additive_delay_seconds"] = int(_si_cfg.get("additive_delay_seconds") or 0)
        data["param_chart_limits"]    = _si_cfg.get("param_chart_limits", {})

        # Inject prescription monitored params from config so frontend only shows
        # parameters that are enabled in Prescription Monitoring tab
        pw_cfg = _si_cfg.get("prescription_watchdog", {})
        _default_params = ["bentonite", "freshSilicaSand", "lca", "water"]
        monitored = pw_cfg.get("monitored_params") or _default_params
        data["presc_monitored_params"] = monitored

        # Prescription deviation thresholds (from Prescription Monitoring config tab)
        # Used for ±tolerance bands in the per-parameter additive charts
        data["presc_ok_thr"]   = float(pw_cfg.get("pct_ok_thr",       pw_cfg.get("ok_threshold",       1.0)))
        data["presc_warn_thr"] = float(pw_cfg.get("pct_warn_thr",      pw_cfg.get("warn_threshold",     2.0)))
        data["presc_crit_thr"] = float(pw_cfg.get("pct_critical_thr",  pw_cfg.get("critical_threshold", 3.0)))

        # Inject bad-batch detection mode so the report uses the correct comparison
        bb_cfg = _si_cfg.get("bad_batch_watchdog", {})
        _bb_mode = str(bb_cfg.get("detection_mode", "absolute")).lower()
        _bb_thr  = float(bb_cfg.get("threshold", 2.0))
        data["bb_detection_mode"]  = _bb_mode
        # Percentage mode thresholds
        data["bb_pct_ok_thr"]      = float(bb_cfg.get("pct_ok_thr",       2.0))
        data["bb_pct_warn_thr"]    = float(bb_cfg.get("pct_warn_thr",      5.0))
        data["bb_pct_crit_thr"]    = float(bb_cfg.get("pct_critical_thr", 10.0))
        # Absolute mode thresholds (3-tier: OK / WARN / CRIT)
        data["bb_abs_ok_thr"]      = float(bb_cfg.get("abs_ok_thr",   bb_cfg.get("watch_thr",    _bb_thr * 0.5)))
        data["bb_abs_warn_thr"]    = float(bb_cfg.get("abs_warn_thr", bb_cfg.get("alert_thr",    _bb_thr)))
        data["bb_abs_crit_thr"]    = float(bb_cfg.get("abs_crit_thr",       _bb_thr * 1.5))

        # Serialise datetime/timedelta objects
        import json as _json, datetime as _dt
        def _serial(obj):
            if hasattr(obj, "strftime"):
                return obj.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(obj, _dt.timedelta):
                return obj.total_seconds()  # frontend uses Math.round() for seconds
            raise TypeError(f"Not serialisable: {type(obj)}")
        return app.response_class(
            _json.dumps(data, default=_serial),
            mimetype="application/json"
        )
    except Exception as exc:
        logger.error("component_report_data error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/component-report/download")
def component_report_download():
    """
    Download a component report as PPTX or PDF.

    Query params: db, line_id, component_id, date, shift, threshold, fmt (pptx|pdf)
    """
    from flask import send_file
    db_name      = request.args.get("db")
    line_id      = request.args.get("line_id", 1, type=int)
    component_id = request.args.get("component_id", "")
    date_str     = request.args.get("date", "")
    shift        = request.args.get("shift") or None
    threshold    = request.args.get("threshold", 2.0, type=float)
    fmt          = request.args.get("fmt", "pptx").lower()

    if not db_name or not component_id or not date_str:
        return jsonify({"error": "db, component_id and date are required"}), 400

    if fmt not in ("pptx", "pdf"):
        return jsonify({"error": "fmt must be pptx or pdf"}), 400

    try:
        from pathlib import Path
        safe_comp  = component_id.replace("/", "_").replace(" ", "_")
        pre_path   = (Path(__file__).parent.parent / "logs" / "reports"
                      / db_name / date_str / f"{safe_comp}.{fmt}")
        mime = ("application/vnd.openxmlformats-officedocument.presentationml.presentation"
                if fmt == "pptx" else "application/pdf")
        filename = f"SandMan_Report_{safe_comp}_{date_str}.{fmt}"

        # Serve pre-generated file instantly if it exists
        # For PDF: check if a pre-generated PPTX exists and convert it (same content)
        pre_pptx = (Path(__file__).parent.parent / "logs" / "reports"
                    / db_name / date_str / f"{safe_comp}.pptx")
        if fmt == "pdf" and pre_pptx.exists():
            logger.info("Converting pre-generated PPTX to PDF: %s", pre_pptx.name)
            pdf_bytes = _pptx_to_pdf_via_com(pre_pptx.read_bytes())
            return send_file(io.BytesIO(pdf_bytes), mimetype=mime,
                             as_attachment=True, download_name=filename)
        if fmt == "pptx" and pre_path.exists():
            logger.info("Serving pre-generated report: %s", pre_path.name)
            return send_file(str(pre_path), mimetype=mime,
                             as_attachment=True, download_name=filename)

        # Otherwise generate on-demand
        cfg    = _foundry_cfg(db_name, line_id)
        engine = _get_engine_for(cfg)
        from .config_store import load_foundry_config as _lfc
        _fl_cfg      = _lfc(_reg_engine, f"{db_name}_L{line_id}") if _reg_engine else {}
        chart_limits = _fl_cfg.get("param_chart_limits", {})

        from .production_report_generator import generate_component_pptx
        pptx_bytes = generate_component_pptx(
            engine, line_id, component_id, date_str, shift or "",
            chart_limits=chart_limits, foundry_cfg=_fl_cfg
        )

        if fmt == "pptx":
            file_bytes = pptx_bytes
        else:
            # Convert PPTX -> PDF via PowerPoint COM (same output, no separate generator)
            file_bytes = _pptx_to_pdf_via_com(pptx_bytes)

        return send_file(io.BytesIO(file_bytes), mimetype=mime,
                         as_attachment=True, download_name=filename)
    except Exception as exc:
        logger.error("component_report_download error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


def _pptx_to_pdf_via_com(pptx_bytes: bytes) -> bytes:
    """
    Convert PPTX bytes -> PDF bytes using PowerPoint COM automation (Windows only).
    PowerPoint must be Visible=True for SaveAs to work reliably on Windows.
    """
    import tempfile, os, pythoncom
    import win32com.client as wc

    tmp = tempfile.mkdtemp()
    pptx_path = os.path.abspath(os.path.join(tmp, "report.pptx"))
    pdf_path  = os.path.abspath(os.path.join(tmp, "report.pdf"))

    with open(pptx_path, "wb") as f:
        f.write(pptx_bytes)

    pythoncom.CoInitialize()
    ppt = None
    prs = None
    try:
        ppt = wc.Dispatch("PowerPoint.Application")
        ppt.Visible = True   # must be True for SaveAs to work
        prs = ppt.Presentations.Open(pptx_path, ReadOnly=False, WithWindow=False)
        prs.SaveAs(pdf_path, 32)  # 32 = ppSaveAsPDF
        prs.Close()
        ppt.Quit()
        with open(pdf_path, "rb") as f:
            return f.read()
    except Exception:
        if prs:
            try: prs.Close()
            except Exception: pass
        if ppt:
            try: ppt.Quit()
            except Exception: pass
        raise
    finally:
        pythoncom.CoUninitialize()
        # Clean up temp files
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def _fetch_badbatch_thresholds(engine, line_id: int) -> dict:
    """
    Load severity thresholds from smc_badbatch_config for the given foundry line.

    Returns:
        {
          "upper": {"watch": 3.0, "alert": 4.0, "critical": 5.0},
          "lower": {"watch": 1.0, "alert": 3.0, "critical": 5.0},
        }
    Upper thresholds apply when SMC > COSP (positive deviation).
    Lower thresholds apply when SMC < COSP (negative deviation, as absolute values).
    Falls back to defaults (1/2/3) when the table is missing or has no row.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""SELECT critical_config_json, moderate_config_json, low_config_json
                        FROM smc_badbatch_config
                        WHERE foundry_line_pkey = :fl AND deleted = 0
                        ORDER BY pkey DESC LIMIT 1"""),
                {"fl": int(line_id)},
            ).mappings().first()
        if not row:
            logger.warning("_fetch_badbatch_thresholds: no config for line=%d — severity will be 'ok'", line_id)
            return None

        import json as _json

        def _j(col):
            raw = row[col]
            return _json.loads(raw) if isinstance(raw, str) else (raw or {})

        crit = _j("critical_config_json")
        mod  = _j("moderate_config_json")
        low  = _j("low_config_json")

        # Fetch SMC valid range from properties table using the configured smc_col.
        # The SandMan UI lets operators set LCL/UCL for each additive parameter —
        # those values are stored in properties.cpk_min / cpk_max.
        # We convert the snake_case column name to camelCase for the java_name lookup.
        smc_min, smc_max = None, None
        try:
            # Derive all candidate java_names from the smc_col
            # e.g. compactability_smc_pct -> compactabilitySmcPct
            import re as _re
            smc_col_name = row_cfg.get("smc_col", "compactability_smc_pct") \
                           if (row_cfg := {}) else "compactability_smc_pct"
            # Build camelCase variant
            parts = smc_col_name.split("_")
            camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
            candidates = list({smc_col_name, camel, camel[0].upper() + camel[1:]})

            with engine.connect() as conn:
                smc_row = conn.execute(text("""
                    SELECT p.cpk_min, p.cpk_max
                    FROM   properties p
                    JOIN   measures   m ON p.measure_pkey = m.pkey
                    WHERE  m.foundry_line_id = :fl
                      AND  p.java_name IN :names
                      AND  p.deleted = 0 AND p.is_active = 1
                      AND  m.isActive = 1
                      AND  (p.cpk_min IS NOT NULL OR p.cpk_max IS NOT NULL)
                    ORDER BY p.cpk_min IS NULL ASC, p.pkey DESC
                    LIMIT 1
                """), {"fl": int(line_id), "names": tuple(candidates)}).mappings().first()
                if smc_row:
                    smc_min = float(smc_row["cpk_min"]) if smc_row["cpk_min"] is not None else None
                    smc_max = float(smc_row["cpk_max"]) if smc_row["cpk_max"] is not None else None
        except Exception:
            pass

        # Fall back to smc_badbatch_config columns if properties has no limits
        if smc_min is None:
            smc_min = float(row.get("smc_min_valid") or 0.0)
        if smc_max is None:
            smc_max = float(row.get("smc_max_valid") or 100.0)

        return {
            "upper_raw": {
                "lowUpperMin"    : float(low.get("lowUpperMin",      3.0)),
                "modUpperMin"    : float(mod.get("modUpperMin",      4.0)),
                "criticUpperMin" : float(crit.get("criticUpperMin",  5.0)),
            },
            "lower_raw": {
                "lowLowerMin"    : float(low.get("lowLowerMin",      -3.0)),
                "lowLowerMax"    : float(low.get("lowLowerMax",      -1.0)),
                "modLowerMin"    : float(mod.get("modLowerMin",      -5.0)),
                "modLowerMax"    : float(mod.get("modLowerMax",      -3.0)),
                "criticLowerMax" : float(crit.get("criticLowerMax",  -5.0)),
            },
            "smc_min": smc_min,
            "smc_max": smc_max,
        }
    except Exception as exc:
        logger.warning("_fetch_badbatch_thresholds failed (line=%d): %s", line_id, exc)
        return None


def _batch_deviation_severity(dev: float, thr: dict) -> str:
    """
    Severity for a single signed deviation value (SMC − COSP) using DB thresholds.

    Positive side (SMC > COSP):
      Low/Watch   : lowUpperMin  ≤ dev < modUpperMin
      Alert       : modUpperMin  ≤ dev < criticUpperMin
      Critical    : dev ≥ criticUpperMin

    Negative side (SMC < COSP):
      Low/Watch   : lowLowerMin  < dev ≤ lowLowerMax   (both negative)
      Alert       : modLowerMin  < dev ≤ modLowerMax
      Critical    : dev ≤ criticLowerMax
    """
    u = thr["upper_raw"]
    l = thr["lower_raw"]

    if dev >= u["criticUpperMin"] or dev <= l["criticLowerMax"]:
        return "critical"
    if (u["modUpperMin"] <= dev < u["criticUpperMin"]) or \
       (l["modLowerMin"] < dev <= l["modLowerMax"]):
        return "alert"
    if (u["lowUpperMin"] <= dev < u["modUpperMin"]) or \
       (l["lowLowerMin"] < dev <= l["lowLowerMax"]):
        return "watch"
    return "ok"


def _badbatch_run_severity(signed_devs: list, thr: dict) -> str:
    """Return the worst severity across all batch deviations in a run."""
    _RANK = {"ok": 0, "watch": 1, "alert": 2, "critical": 3}
    worst = "ok"
    for d in signed_devs:
        s = _batch_deviation_severity(d, thr)
        if _RANK[s] > _RANK[worst]:
            worst = s
    return worst


@app.route("/api/components")
def list_components():
    """
    List components produced on a given date for a foundry line.

    Query params: db, line_id, date
    Returns: [{component_id, shift, n_batches, t_start, t_end, n_bad_batch}]
    """
    db_name   = request.args.get("db")
    line_id   = request.args.get("line_id", 1, type=int)
    date_str  = request.args.get("date", "")
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to",   "")
    threshold = request.args.get("threshold", 2.0, type=float)

    # date_from / date_to override single date param
    if date_from and date_to:
        date_str = date_to   # used for watermark / display

    if not db_name:
        return jsonify({"error": "db is required"}), 400

    try:
        cfg    = _foundry_cfg(db_name, line_id)
        engine = _get_engine_for(cfg)

        # If date='last' or date is missing, find the most recent date with data
        if not date_str or date_str == "last":
            with engine.connect() as conn:
                row = conn.execute(text("""
                    SELECT DATE(MAX(timestamp)) AS last_date FROM additive
                    WHERE foundry_line_id=:fl_id AND deleted=0
                      AND component_id IS NOT NULL AND component_id != ''
                """), {"fl_id": line_id}).mappings().first()
            last_date = str(row["last_date"]) if row and row["last_date"] else ""
            if not last_date:
                return jsonify({"components": [], "date": date_str or "", "last_date": ""})
            return jsonify({"components": [], "date": last_date, "last_date": last_date})

        with engine.connect() as conn:
            # Fetch every batch in time order — do NOT group by component.
            # We detect contiguous runs in Python so the same component appearing
            # again after a different component counts as a separate production run.
            if date_from and date_to:
                date_sql   = "DATE(timestamp) BETWEEN :dt_from AND :dt_to"
                date_params = {"fl_id": line_id, "dt_from": date_from, "dt_to": date_to}
            else:
                date_sql   = "DATE(timestamp) = :dt"
                date_params = {"fl_id": line_id, "dt": date_str}

            rows = conn.execute(text(f"""
                SELECT
                    component_id,
                    shift,
                    timestamp,
                    compactability_smc_pct AS smc,
                    cosp_percentage_pct    AS cosp,
                    ABS(compactability_smc_pct - cosp_percentage_pct) AS dev
                FROM additive
                WHERE foundry_line_id = :fl_id
                  AND deleted = 0
                  AND {date_sql}
                  AND component_id IS NOT NULL
                  AND component_id != ''
                  AND LOWER(TRIM(component_id)) != 'null'
                ORDER BY timestamp ASC
            """), date_params).mappings().fetchall()

            # Detect contiguous runs — skip null/empty component_id
            _runs, _prev = [], None
            for b in rows:
                comp = b["component_id"]
                if not comp or str(comp).strip().lower() == 'null':
                    continue
                if comp != _prev:
                    _runs.append({"component_id": comp, "shift": b["shift"],
                                  "t_start": b["timestamp"], "t_end": b["timestamp"],
                                  "batches": []})
                _runs[-1]["batches"].append(b)
                _runs[-1]["t_end"] = b["timestamp"]
                _prev = comp

            # Load per-foundry thresholds from smc_badbatch_config
            thr = _fetch_badbatch_thresholds(engine, line_id)

            # Aggregate each run
            rows = []
            for run in _runs:
                bs       = run["batches"]
                # Filter out sensor errors using valid range from properties config
                _smin = thr["smc_min"] if thr else 0.0
                _smax = thr["smc_max"] if thr else 100.0
                valid_batches = [b for b in bs
                                 if b["smc"] is not None and b["cosp"] is not None
                                 and _smin <= float(b["smc"]) <= _smax]
                signed   = [float(b["smc"]) - float(b["cosp"]) for b in valid_batches]
                smc_avg  = sum(float(b["smc"])  for b in valid_batches) / len(valid_batches) if valid_batches else None
                cosp_avg = sum(float(b["cosp"]) for b in valid_batches) / len(valid_batches) if valid_batches else None
                max_dev  = max((abs(d) for d in signed), default=0.0)
                n_bad    = sum(1 for d in signed if abs(d) > threshold)
                # Severity from worst individual batch using DB thresholds
                # If no DB config exists for this foundry line, severity is unknown
                severity = _badbatch_run_severity(signed, thr) if thr else None
                rows.append({"component_id": run["component_id"], "shift": run["shift"],
                             "t_start": run["t_start"], "t_end": run["t_end"],
                             "n_batches": len(bs),
                             "smc_avg":  round(smc_avg,  2) if smc_avg  else None,
                             "cosp_avg": round(cosp_avg, 2) if cosp_avg else None,
                             "max_dev":  round(max_dev,  2),
                             "n_bad": n_bad, "severity": severity})

        # ── Component name lookup (fresh connection — outside original with block) ──
        _comp_ids = list({r["component_id"] for r in rows if r["component_id"]})
        _name_map = {}
        if _comp_ids:
            try:
                _ph = ", ".join(f":c{i}" for i in range(len(_comp_ids)))
                _params = {f"c{i}": cid for i, cid in enumerate(_comp_ids)}
                with engine.connect() as _nc:
                    try:
                        _nr = _nc.execute(text(f"""
                            SELECT component_id, component_name AS name
                            FROM   components
                            WHERE  component_id IN ({_ph})
                        """), _params).mappings().fetchall()
                    except Exception:
                        _nr = _nc.execute(text(f"""
                            SELECT gc.component_id, g.name
                            FROM   foundry_line_group_component gc
                            JOIN   foundry_line_group g ON g.pkey = gc.foundry_line_group_pkey
                            WHERE  gc.component_id IN ({_ph})
                              AND  gc.deleted = 0 AND g.deleted = 0
                              AND  g.foundry_line_pkey = :fl
                        """), {**_params, "fl": line_id}).mappings().fetchall()
                    _name_map = {str(r["component_id"]): str(r["name"]) for r in _nr}
            except Exception:
                pass

        result = [
            {
                "component_id"  : r["component_id"],
                "component_name": _name_map.get(str(r["component_id"]), r["component_id"]),
                "shift"         : str(r["shift"] or ""),
                "n_batches"     : int(r["n_batches"]),
                "t_start"       : str(r["t_start"] or ""),
                "t_end"         : str(r["t_end"] or ""),
                "smc_avg"       : r["smc_avg"],
                "cosp_avg"      : r["cosp_avg"],
                "max_dev"       : r["max_dev"],
                "n_bad_batch"   : r["n_bad"],
                "severity"      : r["severity"],
            }
            for r in rows
        ]
        return jsonify({"components": result, "date": date_str})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/production-report/download")
def production_report_download():
    """
    Generate and download a full-day matplotlib production report PDF.

    Query params:
      db       -- foundry DB name (required)
      line_id  -- foundry line id (default 1)
      date     -- YYYY-MM-DD (required)
    """
    from flask import send_file
    db_name  = request.args.get("db")
    line_id  = request.args.get("line_id", 1, type=int)
    date_str = request.args.get("date", "")

    if not db_name or not date_str:
        return jsonify({"error": "db and date are required"}), 400

    try:
        cfg    = _foundry_cfg(db_name, line_id)
        engine = _get_engine_for(cfg)
        from .production_report_generator import generate_production_pdf
        from .config_store import load_foundry_config as _lfc
        _fl_cfg = _lfc(_reg_engine, f"{db_name}_L{line_id}") if _reg_engine else {}
        chart_limits = _fl_cfg.get("param_chart_limits", {})
        pdf_bytes = generate_production_pdf(engine, line_id, date_str, chart_limits=chart_limits, foundry_cfg=_fl_cfg)
        filename  = f"Production_Report_{date_str}.pdf"
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as exc:
        logger.error("production_report_download error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/reports/list")
def list_generated_reports():
    """
    List pre-generated component reports saved by ComponentChangeMonitor.
    Query params: db, date (YYYY-MM-DD)
    Returns: [{component_id, date, pptx, pdf}]
    """
    from pathlib import Path
    db_name  = request.args.get("db", "")
    date_str = request.args.get("date", "")
    if not db_name or not date_str:
        return jsonify({"reports": []})

    reports_dir = Path(__file__).parent.parent / "logs" / "reports" / db_name / date_str
    if not reports_dir.exists():
        return jsonify({"reports": []})

    seen = {}
    for f in sorted(reports_dir.iterdir()):
        stem = f.stem
        if stem not in seen:
            seen[stem] = {"component_id": stem.replace("_", " "), "date": date_str,
                          "pptx": False, "pdf": False}
        if f.suffix == ".pptx":
            seen[stem]["pptx"] = True
        elif f.suffix == ".pdf":
            seen[stem]["pdf"] = True

    return jsonify({"reports": list(seen.values()), "date": date_str})


@app.route("/api/reports/download")
def download_generated_report():
    """
    Serve a pre-generated component report file.
    Query params: db, date, component_id, fmt (pptx|pdf)
    """
    from flask import send_file
    from pathlib import Path
    db_name      = request.args.get("db", "")
    date_str     = request.args.get("date", "")
    component_id = request.args.get("component_id", "")
    fmt          = request.args.get("fmt", "pptx").lower()

    if not db_name or not date_str or not component_id:
        return jsonify({"error": "db, date and component_id required"}), 400

    safe = component_id.replace("/", "_").replace(" ", "_")
    path = Path(__file__).parent.parent / "logs" / "reports" / db_name / date_str / f"{safe}.{fmt}"

    if not path.exists():
        return jsonify({"error": "Report not yet generated — will be ready after component completes"}), 404

    mime = ("application/vnd.openxmlformats-officedocument.presentationml.presentation"
            if fmt == "pptx" else "application/pdf")
    return send_file(str(path), mimetype=mime, as_attachment=True,
                     download_name=f"{safe}_{date_str}.{fmt}")


@app.route("/api/cost-saving/baseline")
def cost_saving_baseline():
    """
    Return per-component baseline rejection % for the configured baseline period.
    Falls back to component-group average if a component has no data in baseline period.

    Query params:
      db          -- foundry DB name (required)
      line_id     -- foundry line id (default 1)
      component_id -- specific component to look up (optional; returns all if omitted)
    """
    import json as _json
    db_name      = request.args.get("db", "")
    line_id      = request.args.get("line_id", 1, type=int)
    component_id = request.args.get("component_id", "").strip()

    if not db_name:
        return jsonify({"error": "db is required"}), 400

    # Load cost_saving config for this foundry using the already-initialized registry engine
    label = f"{db_name}_L{line_id}"
    fc = {}
    try:
        from .config_store import load_foundry_config
        if _reg_engine is not None:
            fc = load_foundry_config(_reg_engine, label) or {}
    except Exception:
        pass

    cs_cfg = fc.get("cost_saving", {})
    baseline_start = cs_cfg.get("baseline_start", "")
    baseline_end   = cs_cfg.get("baseline_end", "")
    cost_per_mt    = float(cs_cfg.get("cost_per_mt", 7000))
    currency       = cs_cfg.get("currency", "INR")

    if not baseline_start or not baseline_end:
        return jsonify({"error": "Baseline period not configured. Set baseline_start and baseline_end in Config."}), 400

    try:
        engine = _alerts_engine(db_name)
        with engine.connect() as conn:
            # Per-component baseline rejection %
            sql = text("""
                SELECT
                    component_id,
                    SUM(rejection_quantity)     AS total_rej,
                    SUM(total_quantity_produced) AS total_prod,
                    SUM(rejection_quantity * nett_casting_wt) AS rej_kg,
                    SUM(total_quantity_produced * nett_casting_wt) AS prod_kg
                FROM rejections
                WHERE deleted = 0
                  AND foundry_line_id = :lid
                  AND DATE(date) BETWEEN :bs AND :be
                  AND total_quantity_produced > 0
                  AND component_id IS NOT NULL
                GROUP BY component_id
            """)
            rows = conn.execute(sql, {"lid": line_id, "bs": baseline_start, "be": baseline_end}).mappings().fetchall()

            comp_baseline = {}
            for r in rows:
                if r["total_prod"] and r["total_prod"] > 0:
                    comp_baseline[str(r["component_id"])] = {
                        "rej_pct"  : round(float(r["total_rej"] or 0) / float(r["total_prod"]) * 100, 4),
                        "total_rej": float(r["total_rej"] or 0),
                        "total_prod": float(r["total_prod"] or 0),
                    }

            # Per-group baseline (fallback for components with no baseline data)
            sql_grp = text("""
                SELECT
                    g.name AS group_name,
                    SUM(r.rejection_quantity)      AS total_rej,
                    SUM(r.total_quantity_produced)  AS total_prod
                FROM rejections r
                JOIN foundry_line_group_component gc ON gc.component_id = r.component_id AND gc.deleted = 0
                JOIN foundry_line_group g ON g.pkey = gc.foundry_line_group_pkey
                    AND g.deleted = 0 AND g.foundry_line_pkey = :lid
                WHERE r.deleted = 0
                  AND r.foundry_line_id = :lid
                  AND DATE(r.date) BETWEEN :bs AND :be
                  AND r.total_quantity_produced > 0
                GROUP BY g.name
            """)
            grp_rows = conn.execute(sql_grp, {"lid": line_id, "bs": baseline_start, "be": baseline_end}).mappings().fetchall()

            group_baseline = {}
            for r in grp_rows:
                if r["total_prod"] and r["total_prod"] > 0:
                    group_baseline[str(r["group_name"])] = round(
                        float(r["total_rej"] or 0) / float(r["total_prod"]) * 100, 4
                    )

            # Component → group mapping for fallback
            sql_map = text("""
                SELECT gc.component_id, g.name AS group_name
                FROM foundry_line_group_component gc
                JOIN foundry_line_group g ON g.pkey = gc.foundry_line_group_pkey
                    AND g.deleted = 0 AND g.foundry_line_pkey = :lid
                WHERE gc.deleted = 0
            """)
            map_rows = conn.execute(sql_map, {"lid": line_id}).mappings().fetchall()
            comp_to_group = {str(r["component_id"]): str(r["group_name"]) for r in map_rows}

        # Overall baseline (last-resort fallback)
        all_rej  = sum(v["total_rej"]  for v in comp_baseline.values())
        all_prod = sum(v["total_prod"] for v in comp_baseline.values())
        overall_baseline = round(all_rej / all_prod * 100, 4) if all_prod > 0 else 0.0

        result = {
            "baseline_start"  : baseline_start,
            "baseline_end"    : baseline_end,
            "cost_per_mt"     : cost_per_mt,
            "currency"        : currency,
            "overall_baseline": overall_baseline,
            "components"      : comp_baseline,
            "groups"          : group_baseline,
            "comp_to_group"   : comp_to_group,
        }

        if component_id:
            # Resolve baseline for a single component
            if component_id in comp_baseline:
                bl = comp_baseline[component_id]["rej_pct"]
                source = "component"
            else:
                grp = comp_to_group.get(component_id)
                if grp and grp in group_baseline:
                    bl = group_baseline[grp]
                    source = f"group:{grp}"
                else:
                    bl = overall_baseline
                    source = "overall"
            result["component_id"]       = component_id
            result["baseline_pct"]       = bl
            result["baseline_source"]    = source

        return jsonify(result)

    except Exception as exc:
        import traceback as _tb
        return jsonify({"error": str(exc), "detail": _tb.format_exc()}), 500


@app.route("/api/rejection-data")
def rejection_data():
    """
    Return daily rejection statistics for the given foundry line and date range.

    Query params:
      db           -- foundry DB name (required)
      line_id      -- foundry line id (default 1)
      date_from    -- YYYY-MM-DD start (required)
      date_to      -- YYYY-MM-DD end (required)
      component_id -- filter to a specific component (optional)
      defect       -- defect type key (optional; uses first available if omitted)
    """
    import pandas as pd
    import numpy as np

    db_name      = request.args.get("db")
    line_id      = request.args.get("line_id", 1, type=int)
    date_from    = request.args.get("date_from", "")
    date_to      = request.args.get("date_to", "")
    component_id = request.args.get("component_id", "").strip()
    defect       = request.args.get("defect", "")

    if not db_name or not date_from or not date_to:
        return jsonify({"error": "db, date_from and date_to are required"}), 400

    try:
        import pandas as pd
        import numpy as np

        cfg    = _foundry_cfg(db_name, line_id)
        engine = _get_engine_for(cfg)

        # ── Check prod_flow_change ────────────────────────────────────────────
        with engine.connect() as conn:
            flow_row = conn.execute(
                text("SELECT prod_flow_change FROM foundry_line WHERE pkey = :lid AND is_active = 1"),
                {"lid": line_id}
            ).fetchone()
        is_prod_flow = bool(flow_row[0]) if flow_row else False

        # ── Resolve component_id: watchdog may store full name; rejections uses short code ──
        # components.component_name = full name  (e.g. "RENAULT NISSAN DISC FR BRAKE RBC")
        # components.component_id   = short code (e.g. "DISC RR Brake _ RBC")
        # rejections.component_id   = short code
        rej_comp_id = component_id  # start with what was passed
        if component_id:
            try:
                with engine.connect() as conn:
                    # If the passed component_id matches a component_name, resolve to component_id
                    resolved = conn.execute(
                        text("SELECT component_id FROM components WHERE foundry_line_id = :lid AND component_name = :name LIMIT 1"),
                        {"lid": line_id, "name": component_id}
                    ).fetchone()
                    if resolved:
                        rej_comp_id = resolved[0]
                    # Also check if it matches component_id directly (already short code)
                    # -> keep rej_comp_id as-is in that case
            except Exception:
                pass  # keep original

        # ── Step 1: Rejection weight per date for this component ─────────────
        # Formula: SUM(nett_casting_wt * rejection_quantity) grouped by date
        comp_filter_rej = "AND component_id = :comp_id" if rej_comp_id else ""
        rej_sql = text(f"""
            SELECT DATE(date) AS date,
                   SUM(nett_casting_wt * rejection_quantity) AS rejection_weight_kg
            FROM rejections
            WHERE foundry_line_id = :lid
              AND date BETWEEN :d_from AND :d_to
              {comp_filter_rej}
              AND (pkey NOT IN (
                SELECT entity_pkey FROM data_upload_error
                WHERE entity_type = 'rejection' AND errorCode <> 'ORPHAN_ENTRY'
              ))
            GROUP BY DATE(date)
            ORDER BY DATE(date)
        """)

        # Shifts per date come from the frontend's already-loaded production run data
        # (no extra query needed — _prodAllComps already has component_id, date, shift)
        shifts_sql = None  # placeholder; shifts_by_date is built client-side
        rej_params = {
            "lid":    line_id,
            "d_from": f"{date_from} 00:00:00",
            "d_to":   f"{date_to} 23:59:59",
        }
        if rej_comp_id:
            rej_params["comp_id"] = rej_comp_id

        # Check if the rejections table exists in this foundry's database
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1 FROM rejections LIMIT 1"))
        except Exception:
            # rejections table not found in this foundry DB — return empty gracefully
            return jsonify({
                "dates": [], "rejection_pct": [], "total_rejection_kg": [],
                "production_kg": [], "defect": "Rejection", "available_defects": [],
                "is_prod_flow": is_prod_flow,
                "info": "Rejection data not available for this foundry.",
            })

        try:
            with engine.connect() as conn:
                rej_df = pd.read_sql(rej_sql, conn, params=rej_params)
        except Exception:
            rej_df = pd.DataFrame()

        shifts_by_date = {}  # populated client-side from _prodAllComps

        if rej_df.empty:
            return jsonify({"dates": [], "rejection_pct": [], "total_rejection_kg": [],
                            "production_kg": [], "defect": "Rejection", "available_defects": ["Rejection"],
                            "is_prod_flow": is_prod_flow})

        rej_df["date"] = pd.to_datetime(rej_df["date"])
        rej_df["rejection_weight_kg"] = rej_df["rejection_weight_kg"].fillna(0).astype(float)

        # ── Step 2: Production weight per date ───────────────────────────────
        # Mode determined by prod_flow_change flag on foundry_line table:
        #   is_prod_flow = True  -> consumption SQL (handles pattern expansions)
        #   is_prod_flow = False -> total_quantity_produced × nett_casting_wt from rejections
        #                          (reliable: tqp already = boxes × cavities, no JOIN needed)
        if is_prod_flow:
            _cp = f"AND comp2.component_id = :comp_id" if rej_comp_id else ""
            _cs = f"AND comp.component_id  = :comp_id" if rej_comp_id else ""
            prod_sql = text(f"""
                SELECT DATE(date) AS date, SUM(prod_wt) AS production_weight_kg
                FROM (
                    SELECT c.date,
                        (cc.qty * comp2.cavities * comp2.weight_of_component) AS prod_wt
                    FROM consumption_component cc
                    JOIN consumption c   ON c.pkey = cc.consumption_pkey
                    JOIN components comp ON comp.pkey = cc.component_pkey
                    JOIN pattern_component pc  ON comp.component_id = pc.hid
                    JOIN pattern_component pc2 ON pc.pattern_no = pc2.pattern_no
                    JOIN components comp2 ON pc2.hid = comp2.component_id
                    WHERE c.date BETWEEN :d_from AND :d_to
                      AND comp.foundry_line_id = :lid
                      {_cp}
                    UNION ALL
                    SELECT c.date,
                        (cc.qty * comp.cavities * comp.weight_of_component) AS prod_wt
                    FROM consumption_component cc
                    JOIN consumption c   ON c.pkey = cc.consumption_pkey
                    JOIN components comp ON comp.pkey = cc.component_pkey
                    WHERE c.date BETWEEN :d_from AND :d_to
                      AND comp.foundry_line_id = :lid
                      AND comp.component_id NOT IN (SELECT hid FROM pattern_component)
                      {_cs}
                ) AS sub
                GROUP BY DATE(date)
            """)
        else:
            # Standard mode: total_quantity_produced is already boxes × cavities
            prod_sql = text(f"""
                SELECT DATE(date) AS date,
                       SUM(total_quantity_produced * nett_casting_wt) AS production_weight_kg
                FROM rejections
                WHERE foundry_line_id = :lid
                  AND date BETWEEN :d_from AND :d_to
                  {comp_filter_rej}
                GROUP BY DATE(date)
                ORDER BY DATE(date)
            """)
        with engine.connect() as conn:
            prod_df = pd.read_sql(prod_sql, conn, params=rej_params)
        prod_df["date"] = pd.to_datetime(prod_df["date"])
        prod_df["production_weight_kg"] = prod_df["production_weight_kg"].fillna(0).astype(float)

        # ── Step 3: Merge, calculate Rejection %, drop zero-production dates ──
        daily = pd.merge(rej_df, prod_df, on="date", how="left").fillna(0)

        # Drop dates where this component had no production (nothing to plot)
        daily = daily[daily["production_weight_kg"] > 0].copy()

        if daily.empty:
            return jsonify({"dates": [], "rejection_pct": [], "total_rejection_kg": [],
                            "production_kg": [], "defect": "Rejection", "available_defects": ["Rejection"],
                            "is_prod_flow": is_prod_flow})

        daily["rejection_pct"] = (
            (daily["rejection_weight_kg"] * 100.0) / daily["production_weight_kg"]
        ).replace([np.inf, -np.inf], 0.0).fillna(0.0).round(2)

        daily["date"] = daily["date"].dt.strftime("%Y-%m-%d")

        # ── Step 4: Top defect by weighted rejection ────────────────────────
        # Use measures/properties to get defect column names and display labels
        top_defect = None
        try:
            import re as _re
            def _java_to_col(java_name, db_col_set=None):
                """Convert camelCase java name to snake_case DB column.
                Falls back to fuzzy match (strip underscores) when exact fails.
                e.g. sandDropInclusionFoundryStage -> sand_drop... BUT DB has sanddrop...
                """
                s = _re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', java_name).lower()
                if db_col_set is None or s in db_col_set:
                    return s
                # Fuzzy: strip all underscores from both sides and compare
                s_bare = s.replace('_', '')
                for col in db_col_set:
                    if col.replace('_', '') == s_bare:
                        return col   # found the correct DB column name
                return s  # fallback: return the camelCase-converted name

            with engine.connect() as conn:
                defect_props = conn.execute(text("""
                    SELECT DISTINCT
                        COALESCE(p.alias_name, p.ui_name) AS display_name,
                        p.java_name, p.defect_group
                    FROM measures m
                    JOIN properties p ON m.pkey = p.measure_pkey
                    WHERE m.name = 'rejection' AND m.isActive = 1
                      AND p.is_active = 1 AND p.defect_group IS NOT NULL
                      AND p.ui_name IS NOT NULL
                      AND p.java_name NOT IN ('nettCastingWt','noOfBoxesPoured',
                          'noOfCastingsPerBox','totalQuantityProduced','rejectionQuantity')
                """)).fetchall()

            # Get all available columns in rejections table
            with engine.connect() as conn:
                col_check = conn.execute(text(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_NAME='rejections' AND TABLE_SCHEMA=DATABASE()"
                )).fetchall()
            db_cols = {r[0].lower() for r in col_check}

            # Calculate total rejection per defect type over the period
            defect_totals = {}
            total_prod_kg = prod_df["production_weight_kg"].sum() if not prod_df.empty else 0

            # Fetch raw rejection data (not grouped) for defect breakdown
            raw_sql = text(f"""
                SELECT nett_casting_wt, {', '.join(
                    f'COALESCE(`{_java_to_col(p[1], db_cols)}`, 0) AS `{_java_to_col(p[1], db_cols)}`'
                    for p in defect_props
                    if _java_to_col(p[1], db_cols) in db_cols
                )}
                FROM rejections
                WHERE foundry_line_id = :lid
                  AND date BETWEEN :d_from AND :d_to
                  {comp_filter_rej}
                  AND (pkey NOT IN (
                    SELECT entity_pkey FROM data_upload_error
                    WHERE entity_type='rejection' AND errorCode<>'ORPHAN_ENTRY'
                  ))
            """)
            with engine.connect() as conn:
                raw_df = pd.read_sql(raw_sql, conn, params=rej_params)

            if not raw_df.empty:
                wt = raw_df["nett_casting_wt"].fillna(0)
                for p in defect_props:
                    col = _java_to_col(p[1], db_cols)  # fuzzy match
                    if col in db_cols and col in raw_df.columns:
                        defect_wt = (raw_df[col].fillna(0) * wt).sum()
                        if defect_wt > 0 and total_prod_kg > 0:
                            display = str(p[0]).replace(' (no)','').replace(' (Kg)','').strip()
                            pct = round(defect_wt / total_prod_kg * 100, 3)
                            defect_totals[display] = {
                                "kg": round(defect_wt, 2),
                                "pct": pct,
                                "group": str(p[2] or '')
                            }

                if defect_totals:
                    top_name = max(defect_totals, key=lambda k: defect_totals[k]["pct"])
                    top_defect = {"name": top_name, **defect_totals[top_name]}
                    # Also send top 3
                    top3 = sorted(defect_totals.items(), key=lambda x: -x[1]["pct"])[:3]
                    top_defect["top3"] = [{"name": k, **v} for k, v in top3]
        except Exception as _de:
            logger.debug("top_defect calc failed: %s", _de)

        return jsonify({
            "defect":             "Rejection",
            "available_defects":  ["Rejection"],
            "is_prod_flow":       is_prod_flow,
            "dates":              daily["date"].tolist(),
            "rejection_pct":      daily["rejection_pct"].tolist(),
            "total_rejection_kg": daily["rejection_weight_kg"].round(2).tolist(),
            "production_kg":      daily["production_weight_kg"].round(2).tolist(),
            "top_defect":         top_defect,
        })

    except Exception as exc:
        logger.exception("rejection_data error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ── Data Flow annotate endpoint (form page for Investigate / Snooze) ─────────

@app.route("/api/data-flow/annotate", methods=["GET", "POST"])
def data_flow_annotate():
    """
    GET  -> show a simple form asking the user for a reason / note.
    POST -> submit the form and redirect to /api/data-flow/confirm with the note.
    Used for: data_issue (Investigate Pipeline) and snooze (Snooze 1 Hour).
    """
    line_id     = request.args.get("line", type=int) or request.form.get("line", type=int)
    status      = (request.args.get("status") or request.form.get("status", "")).strip()
    source_name = (request.args.get("source") or request.form.get("source", "all")).strip()

    if not line_id or status not in ("data_issue", "snooze"):
        return "<h2>Invalid link</h2>", 400

    if request.method == "POST":
        reason = request.form.get("reason", "").strip()
        from urllib.parse import quote
        note_param   = f"&notes={quote(reason)}" if reason else ""
        source_param = f"&source={source_name}" if source_name != "all" else ""
        db_param     = f"&db={source_name}" if source_name not in ("all", "") else ""
        # carry db param through
        _db_from_form = request.form.get("db", request.args.get("db", "")).strip()
        db_param = f"&db={_db_from_form}" if _db_from_form else ""
        return redirect(f"/api/data-flow/confirm?line={line_id}&status={status}{source_param}{db_param}{note_param}")

    labels = {
        "data_issue": ("Line Running — Investigate Data Pipeline", "#D97706",
                       "Describe what you are investigating or what you believe the cause is:"),
        "snooze"    : ("Still Checking — Snooze 1 Hour", "#64748B",
                       "Add an optional note about what you are checking:"),
    }
    title, colour, prompt = labels[status]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SandMan — {title}</title>
  <style>
    body{{font-family:'Segoe UI',Arial,sans-serif;background:#F8FAFC;display:flex;
          align-items:center;justify-content:center;min-height:100vh;margin:0}}
    .card{{background:#fff;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.10);
           padding:36px 44px;max-width:500px;width:100%}}
    h2{{font-size:18px;font-weight:800;color:#0F172A;margin:0 0 6px}}
    .badge{{display:inline-block;padding:5px 16px;border-radius:20px;font-size:12px;
            font-weight:700;color:#fff;background:{colour};margin-bottom:18px}}
    label{{font-size:13px;font-weight:600;color:#374151;display:block;margin-bottom:6px}}
    textarea{{width:100%;border:1px solid #D1D5DB;border-radius:8px;padding:10px 12px;
              font-size:13px;line-height:1.6;resize:vertical;min-height:90px;
              font-family:inherit;box-sizing:border-box;color:#1a1a1a}}
    textarea:focus{{outline:none;border-color:{colour};box-shadow:0 0 0 3px {colour}33}}
    .hint{{font-size:11px;color:#9CA3AF;margin-top:4px;margin-bottom:20px}}
    .row{{display:flex;gap:10px;justify-content:flex-end;margin-top:4px}}
    .btn{{padding:10px 22px;border:none;border-radius:8px;font-size:13px;
          font-weight:700;cursor:pointer;font-family:inherit}}
    .btn-skip{{background:#F3F4F6;color:#6B7280}}
    .btn-confirm{{background:{colour};color:#fff}}
    .btn-skip:hover{{background:#E5E7EB}}
  </style>
</head>
<body>
  <div class="card">
    <h2>{title}</h2>
    <span class="badge">Line {line_id}</span>
    <form method="POST" action="/api/data-flow/annotate">
      <input type="hidden" name="line" value="{line_id}">
      <input type="hidden" name="status" value="{status}">
      <input type="hidden" name="source" value="{source_name}">
      <label for="reason">{prompt}</label>
      <textarea id="reason" name="reason" placeholder="e.g. Checking network switch in panel room..."></textarea>
      <div class="hint">This note will be saved with the annotation. You can leave it blank.</div>
      <div class="row">
        <a href="/api/data-flow/confirm?line={line_id}&status={status}"
           style="text-decoration:none">
          <button type="button" class="btn btn-skip"
            onclick="window.location='/api/data-flow/confirm?line={line_id}&status={status}'">
            Skip — no note
          </button>
        </a>
        <button type="submit" class="btn btn-confirm">Confirm</button>
      </div>
    </form>
  </div>
</body>
</html>"""


# ── Data Flow confirmation endpoint ──────────────────────────────────────────

@app.route("/api/data-flow/confirm")
def data_flow_confirm():
    """
    Called when customer clicks a confirmation link in the data gap email.

    Query params:
      line   -- foundry_line_id (pkey in local foundry DB)
      status -- planned_off | breakdown | data_issue | snooze
      db     -- optional DB name (for context)

    Writes annotation to watchdog_data_annotations and returns a human-readable page.
    No SQL query runs on the foundry DB for this — only the registry (annotation) table is written.
    """
    line_id = request.args.get("line", type=int)
    status  = request.args.get("status", "").strip()
    db_name = request.args.get("db", "")

    if not line_id or status not in ("planned_off", "breakdown", "data_issue", "snooze"):
        return (
            "<h2>Invalid confirmation link</h2>"
            "<p>Status must be: planned_off | breakdown | data_issue | snooze</p>",
            400
        )

    ANNOTATION_MAP = {
        "planned_off" : "planned_shutdown",
        "breakdown"   : "unplanned_shutdown",
        "data_issue"  : "data_pipeline_failure",
        "snooze"      : None,   # snooze = don't annotate, just acknowledge temporarily
    }
    STATUS_LABELS = {
        "planned_off" : "Planned Shutdown",
        "breakdown"   : "Breakdown / Unplanned Stop",
        "data_issue"  : "Data Pipeline Failure — Line is Running",
        "snooze"      : "Snoozed (checking — will re-alert in 1 hour)",
    }

    annotation_type = ANNOTATION_MAP.get(status)
    confirmed_by    = request.args.get("user", "dashboard")
    user_notes      = request.args.get("notes", "").strip()
    source_name     = request.args.get("source", "all").strip()
    db_name         = request.args.get("db", "").strip()
    now_str         = datetime.now().strftime("%d %b %Y %H:%M")

    # Connect to the foundry DB if provided, else fall back to registry
    _write_engine = None
    if db_name:
        try:
            from .foundry_registry import _make_engine
            _db_cfg = {**_config.get("database", {}), "name": db_name}
            _write_engine = _make_engine(_db_cfg)
        except Exception as _e:
            logger.warning("data-flow confirm: could not connect to %s: %s", db_name, _e)
    if not _write_engine:
        _write_engine = _reg_engine

    if _write_engine:
        try:
            from sqlalchemy import text as _t
            with _write_engine.begin() as conn:
                # Close any existing open annotation for this line first
                conn.execute(_t("""
                    UPDATE watchdog_data_annotations
                    SET    gap_end = NOW()
                    WHERE  foundry_line_id = :lid AND gap_end IS NULL
                """), {"lid": line_id})

                # Write new annotation (not for snooze — snooze is handled in monitor memory)
                if annotation_type:
                    import json as _json
                    # Store the specific source so the monitor suppresses only that source
                    _src_json = _json.dumps([source_name]) if source_name not in ("all", "") else '["all"]'
                    conn.execute(_t("""
                        INSERT INTO watchdog_data_annotations
                          (foundry_line_id, gap_start, annotation_type,
                           sources_affected, confirmed_by, confirmed_at, notes)
                        VALUES
                          (:lid, NOW() - INTERVAL 3 HOUR, :atype,
                           :src, :by, NOW(), :notes)
                    """), {
                        "lid"  : line_id,
                        "atype": annotation_type,
                        "src"  : _src_json,
                        "by"   : confirmed_by,
                        "notes": (
                        f"Source: {source_name}  |  {user_notes}  --  Confirmed at {now_str}"
                        if user_notes else
                        f"Source: {source_name}  --  Confirmed at {now_str}"
                    ),
                    })
                    # Leave gap_end NULL — monitor will close it when data resumes

            logger.info(
                "data-flow confirm: line=%d status=%s by=%s",
                line_id, status, confirmed_by
            )
        except Exception as exc:
            logger.warning("data-flow confirm DB write failed: %s", exc)

    label = STATUS_LABELS.get(status, status)

    # For snooze: register in the running DataFlowMonitor instance if available
    if status == "snooze":
        try:
            from watchdog.data_flow.monitor import _register_snooze
            _register_snooze(line_id, hours=1)
        except Exception:
            pass  # monitor may not be running in this process

    colour = {
        "planned_off": "#15803D",
        "breakdown"  : "#DC2626",
        "data_issue" : "#D97706",
        "snooze"     : "#6366F1",
    }.get(status, "#374151")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SandMan — Confirmed</title>
  <style>
    body{{font-family:'Segoe UI',sans-serif;background:#F8FAFC;display:flex;align-items:center;
          justify-content:center;min-height:100vh;margin:0;}}
    .card{{background:#fff;border-radius:14px;box-shadow:0 4px 24px rgba(0,0,0,.10);
           padding:40px 48px;max-width:480px;text-align:center;}}
    .icon{{font-size:48px;margin-bottom:16px;}}
    h1{{font-size:20px;font-weight:800;color:#0F172A;margin-bottom:8px;}}
    .badge{{display:inline-block;padding:6px 18px;border-radius:20px;font-size:13px;
            font-weight:700;color:#fff;background:{colour};margin-bottom:16px;}}
    p{{font-size:13px;color:#6B7280;line-height:1.6;}}
    .meta{{font-size:12px;color:#94A3B8;margin-top:16px;padding-top:16px;
           border-top:1px solid #E5E7EB;}}
    a{{color:#6366F1;text-decoration:none;font-weight:600;}}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{'✅' if status in ('planned_off','snooze') else '⚠️' if status == 'breakdown' else '🔧'}</div>
    <h1>Confirmation Received</h1>
    <div class="badge">{label}</div>
    <p>Thank you. This response has been recorded for Line {line_id}.</p>
    <p>{'SandMan will keep analysis suspended and re-alert if the data gap extends beyond the expected shutdown window.' if status == 'planned_off' else 'The engineering team should investigate the data pipeline — PLC connection, network gateway, or upload service.' if status == 'data_issue' else 'You will be re-alerted in 1 hour if data has not resumed.' if status == 'snooze' else 'Please arrange recovery — data analysis remains suspended until data flow resumes.'}</p>
    <div class="meta">
      Line {line_id} &nbsp;·&nbsp; {now_str}
      &nbsp;·&nbsp; <a href="http://{request.host}/?user={confirmed_by}">Open Dashboard -></a>
    </div>
  </div>
</body>
</html>"""
    return html


if __name__ == "__main__":
    main()
