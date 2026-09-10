"""
watchdog/report_generator.py
-----------------------------
Generates PPT and PDF component analysis reports in the style of the
GPI Scab analysis format.

Report structure (per component):
  Slide 1  — Component overview (production window, moulds, weights)
  Slide 2  — SMC Discharge vs COSP table (bad batches highlighted red)
  Slide 3  — Additives data table (deviations highlighted)
  Slide 4  — Prepared sand properties table
  Slide 5  — Charts (SMC trend, additives trend)

Usage:
    from watchdog.report_generator import generate_component_report
    pptx_bytes = generate_component_report(engine, config, component_id, date_str, shift)
"""

import io
import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── Colour palette ─────────────────────────────────────────────────────────────
_RED      = "FF4444"
_ORANGE   = "FF9944"
_YELLOW   = "FFE14D"
_GREEN    = "2d7a4f"
_GREEN_LT = "D4F0E0"
_INK      = "111111"
_WHITE    = "FFFFFF"
_BG       = "F5F0E8"
_HEADER   = "1A1A1A"
_SAGE     = "2D7A4F"
_GRAY     = "F0ECE2"

# ── Threshold for bad batch detection ──────────────────────────────────────────
BB_THRESHOLD_DEFAULT = 2.0
BB_WARN_THRESHOLD    = 1.5

# ── Prepared sand properties to show ──────────────────────────────────────────
_PS_COLS = [
    ("active_clay",            "Active Clay (%)"),
    ("compactibility",         "Compactability (%)"),
    ("gcs",                    "GCS (gm/cm²)"),
    ("gfn_afs",                "GFN/AFS (no)"),
    ("inert_fines",            "Inert Fines (%)"),
    ("loi",                    "LOI (%)"),
    ("moisture",               "Moisture (%)"),
    ("permeability",           "Permeability (no)"),
    ("shear_strength",         "Shear Strength (gm/cm²)"),
    ("split_strength",         "Split Strength (gm/cm²)"),
    ("temp_of_sand_after_mix", "Sand Temp (°C)"),
    ("volatile_matter",        "Volatile Matter (%)"),
]


# ══════════════════════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_component_data(engine, foundry_line_id: int, component_id: str,
                          date_str: str, shift: Optional[str] = None,
                          bb_threshold: float = BB_THRESHOLD_DEFAULT,
                          foundry_cfg: Optional[dict] = None) -> dict:
    """
    Fetch all data needed for the component report.
    Returns a structured dict consumed by the PPT/PDF generators.
    """
    from sqlalchemy import text

    fl_id = int(foundry_line_id)

    # ── Resolve component_id: foundries like Munjalkiriu pass the full name
    # but additive/consumption tables use the short component_id code.
    # Two-step: try direct match in additive, then resolve via components table.
    _comp_for_query = component_id
    try:
        with engine.connect() as _rc:
            # Does this component_id exist directly in additive?
            _exists = _rc.execute(
                text("SELECT 1 FROM additive WHERE foundry_line_id=:fl AND component_id=:c LIMIT 1"),
                {"fl": fl_id, "c": component_id}
            ).fetchone()
            if not _exists:
                # Resolve via components table: component_id may be the component_name
                _resolved = _rc.execute(
                    text("""SELECT component_id FROM components
                            WHERE foundry_line_id=:fl
                              AND (component_name=:c OR component_id=:c)
                            LIMIT 1"""),
                    {"fl": fl_id, "c": component_id}
                ).fetchone()
                if _resolved:
                    _comp_for_query = _resolved[0]
    except Exception:
        pass  # keep original component_id

    shift_filter    = "AND a.shift = :shift" if shift else ""
    shift_params    = {"shift": str(shift)} if shift else {}
    cb_shift_filter = "AND cb.shift = :shift" if shift else ""

    # ── Read additive delay from caller-supplied foundry config ───────────────
    # foundry_cfg is the dict from watchdog_si_config loaded by alert_server.py.
    # Falls back to 0 (no delay) when not provided.
    _additive_delay = int((foundry_cfg or {}).get("additive_delay_seconds") or 0)

    _SMC_COLS = """
            a.pkey, a.timestamp, a.shift,
            a.compactability_smc_pct  AS smc,
            a.cosp_percentage_pct     AS cosp,
            ROUND(a.compactability_smc_pct - a.cosp_percentage_pct, 3) AS diff,
            CASE WHEN a.cosp_percentage_pct != 0
                 THEN ROUND((a.compactability_smc_pct - a.cosp_percentage_pct)
                            / a.cosp_percentage_pct * 100, 2)
                 ELSE NULL END AS pct_deviation,
            a.bentonite_actual, a.bentonite_set_point,
            a.water_actual,    a.water_set_point,
            a.coal_dust_actual, a.coal_dust_set_point,
            a.fss_actual,      a.fss_set_point,
            a.total_water_ltr, a.temperature_c,
            a.total_seconds,   a.moisture_smc_pct,
            a.recycle_sand_actual"""

    # ── Additive / SMC rows ────────────────────────────────────────────────
    if _additive_delay > 0:
        # Delay mode: subtract delay from booking window and match additive by time.
        # Mirrors GPI Merging.py: START_DT_ADJ = START_DT - delay, END_DT_ADJ = END_DT - delay
        # Handles overnight bookings: cb.end_time < cb.start_time -> add 1 day to end_time.
        # consumption_booking.component_id may store the full name e.g. "51012670 JD CLUTCH HSG"
        # GPI Merging.py takes str.split()[0] to get just the numeric code.
        # We match using SUBSTRING_INDEX(cb.component_id,' ',1) which gives the first word.
        smc_sql = text(f"""
            SELECT DISTINCT {_SMC_COLS}
            FROM additive a
            INNER JOIN consumption_booking cb
                ON  cb.foundry_line_id = :fl_id
                AND (
                      cb.component_id = :comp
                   OR cb.component_id = :comp2
                   OR SUBSTRING_INDEX(cb.component_id, ' ', 1) = :comp
                   OR SUBSTRING_INDEX(cb.component_id, ' ', 1) = :comp2
                    )
                AND DATE(cb.date) = :dt
                {cb_shift_filter}
                AND a.timestamp BETWEEN
                    (TIMESTAMP(DATE(cb.date), cb.start_time) - INTERVAL :delay SECOND)
                    AND
                    (CASE WHEN cb.end_time < cb.start_time
                          THEN TIMESTAMP(DATE(cb.date) + INTERVAL 1 DAY, cb.end_time)
                          ELSE TIMESTAMP(DATE(cb.date), cb.end_time)
                     END - INTERVAL :delay SECOND)
            WHERE a.foundry_line_id = :fl_id
              AND a.deleted = 0
            ORDER BY a.pkey ASC
        """)
        smc_params = {
            "fl_id": fl_id,
            "comp":  _comp_for_query,
            "comp2": component_id,
            "dt":    date_str,
            "delay": _additive_delay,
            **shift_params,
        }
    else:
        # Standard mode: match by component_id directly (no delay)
        smc_sql = text(f"""
            SELECT {_SMC_COLS}
            FROM additive a
            WHERE a.foundry_line_id = :fl_id
              AND a.deleted = 0
              AND (a.component_id = :comp OR a.component_id = :comp2)
              AND DATE(a.date) = :dt
              {shift_filter}
            ORDER BY a.pkey ASC
        """)
        smc_params = {
            "fl_id": fl_id,
            "comp":  _comp_for_query,
            "comp2": component_id,
            "dt":    date_str,
            **shift_params,
        }

    with engine.connect() as conn:
        add_rows = conn.execute(smc_sql, smc_params).mappings().fetchall()

    add_rows = [dict(r) for r in add_rows]


    # ── Helper ────────────────────────────────────────────────────────────────
    def _avg(rows, col):
        vals = [r[col] for r in rows if r.get(col) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    # ── Prescription deviations from watchdog_alerts (exact date match) ─────
    presc_sql = text("""
        SELECT deviations_json, batch_pkey, batch_time, tolerance
        FROM watchdog_alerts
        WHERE foundry_line_id = :fl_id
          AND alert_type = 'PRESCRIPTION'
          AND component_id = :comp
          AND date = :dt
        ORDER BY id DESC LIMIT 5
    """)
    with engine.connect() as conn:
        presc_rows = conn.execute(presc_sql, {
            "fl_id": fl_id, "comp": component_id, "dt": date_str
        }).mappings().fetchall()

    import json as _json
    prescription_deviations = []
    for pr in presc_rows:
        if pr["deviations_json"]:
            devs = _json.loads(pr["deviations_json"])
            prescription_deviations.extend(devs)

    # ── Fetch AI prescription from analytics_report (most recent on or before date) ──
    # This is the "Prescribed vs Actual" mode — uses AI-predicted additive amounts
    # Keyed by (foundry_line_pkey, group_name, date, shift)
    _analytics_prescription = {}
    try:
        with engine.connect() as conn:
            # foundry_line_group_component stores component NAME in component_id column
            # Try direct match first, then resolve via components table
            _grp_row = conn.execute(text("""
                SELECT g.name
                FROM foundry_line_group_component gc
                JOIN foundry_line_group g ON g.pkey = gc.foundry_line_group_pkey
                WHERE gc.component_id = :comp AND gc.deleted = 0
                  AND g.foundry_line_pkey = :fl_id AND g.deleted = 0
                LIMIT 1
            """), {"comp": component_id, "fl_id": fl_id}).mappings().first()
            if not _grp_row:
                _grp_row = conn.execute(text("""
                    SELECT g.name
                    FROM foundry_line_group_component gc
                    JOIN foundry_line_group g ON g.pkey = gc.foundry_line_group_pkey
                    JOIN components c ON c.component_id = gc.component_id
                    WHERE c.component_name = :comp AND gc.deleted = 0
                      AND g.foundry_line_pkey = :fl_id AND g.deleted = 0
                    LIMIT 1
                """), {"comp": component_id, "fl_id": fl_id}).mappings().first()
        _grp = _grp_row["name"] if _grp_row else None

        if _grp:
            # Normalize group name for robust matching across all format variants:
            #   "Group High"  ->  "grouphigh"   (strip ALL separators, lowercase)
            #   "Group-high"  ->  "grouphigh"
            #   "Grouphigh"   ->  "grouphigh"
            # The SQL uses the same stripping on analytics_report.foundry_line_group_name
            import re as _re
            _grp_norm = _re.sub(r'[-\s]', '', _grp.strip().lower())

            # Exact date + shift match only — do NOT carry forward stale prescriptions
            _sh = str(shift) if shift else None
            if _sh:
                _ar_sql = text("""
                    SELECT predicted_additives_json, DATE(date) AS ar_date
                    FROM analytics_report
                    WHERE foundry_line_pkey = :fl_id
                      AND LOWER(REPLACE(REPLACE(foundry_line_group_name,'-',''),' ','')) = :grp
                      AND `shift`           = :sh
                      AND DATE(date)        = :dt
                      AND deleted           = 0
                    ORDER BY pkey DESC
                    LIMIT 1
                """)
            else:
                _ar_sql = text("""
                    SELECT predicted_additives_json, DATE(date) AS ar_date
                    FROM analytics_report
                    WHERE foundry_line_pkey = :fl_id
                      AND LOWER(REPLACE(REPLACE(foundry_line_group_name,'-',''),' ','')) = :grp
                      AND DATE(date)        = :dt
                      AND deleted           = 0
                    ORDER BY pkey DESC
                    LIMIT 1
                """)
            with engine.connect() as conn:
                _params = {"fl_id": fl_id, "grp": _grp_norm, "dt": date_str}
                if _sh:
                    _params["sh"] = _sh
                _ar_row = conn.execute(_ar_sql, _params).mappings().first()

            if _ar_row and _ar_row["predicted_additives_json"]:
                raw = _ar_row["predicted_additives_json"]
                _analytics_prescription = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception as _exc:
        pass  # analytics_report not available — fall back to set-point

    # ── Build prescription_deviations from analytics_report if not from alerts ──
    # Param labels matching prescription_watchdog.py convention
    _PRESC_PARAM_LABELS = {
        "bentonite":       "Bentonite",
        "water":           "Water (ltr)",
        "lca":             "Coal Dust / LCA",
        "freshSilicaSand": "Fresh Silica Sand",
    }
    _ACTUAL_COLS = {
        "bentonite":       "bentonite_actual",
        "water":           "water_actual",
        "lca":             "coal_dust_actual",
        "freshSilicaSand": "fss_actual",
    }

    def _avg(rows, col):
        vals = [r[col] for r in rows if r.get(col) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    if _analytics_prescription:  # Always fill missing params, not just when empty
        _existing = {d['param'] for d in prescription_deviations}
        for param, prescribed in _analytics_prescription.items():
            if param in _existing or prescribed is None:
                continue
            act_col = _ACTUAL_COLS.get(param)
            actual  = _avg(add_rows, act_col) if act_col else None
            pct_diff = round((actual - prescribed) / prescribed * 100, 2) \
                       if (actual is not None and prescribed > 0) else None
            prescription_deviations.append({
                "param":      param,
                "label":      _PRESC_PARAM_LABELS.get(param, param),
                "prescribed": round(float(prescribed), 3),
                "actual":     actual,
                "pct_diff":   pct_diff,
                "within":     abs(pct_diff) <= 3.0 if pct_diff is not None else True,
                "source":     "analytics_report",
            })

    # ── Prepared sand during production window ─────────────────────────────
    ps_data = {}
    ps_rows = []  # always initialized — ps_rows query only runs when add_rows is non-empty
    if add_rows:
        t_start = add_rows[0]["timestamp"]
        t_end   = add_rows[-1]["timestamp"]
        # Buffer = additive_delay_seconds for all foundries (0 = no padding)
        _buf = timedelta(seconds=_additive_delay)
        try:
            t_start_buf = t_start - _buf
            t_end_buf   = t_end   + _buf
        except Exception:
            t_start_buf, t_end_buf = t_start, t_end

        ps_cols_sql = ", ".join(f"`{c}`" for c, _ in _PS_COLS)
        ps_sql = text(f"""
            SELECT {ps_cols_sql}, `date`, `time`
            FROM preparedsand
            WHERE foundry_line_id = :fl_id
              AND deleted = 0
              AND TIMESTAMP(`date`, `time`) BETWEEN :ts_start AND :ts_end
            ORDER BY `date` ASC, `time` ASC
        """)
        with engine.connect() as conn:
            ps_rows = conn.execute(ps_sql, {
                "fl_id"    : fl_id,
                "ts_start" : t_start_buf,
                "ts_end"   : t_end_buf,
            }).mappings().fetchall()

        def _serial_ps(v):
            import datetime as _dt
            if isinstance(v, _dt.timedelta):
                total_s = int(v.total_seconds())
                return f'{total_s//3600:02d}:{(total_s%3600)//60:02d}:{total_s%60:02d}'
            if isinstance(v, (_dt.date, _dt.datetime)):
                return str(v)
            return v
        ps_rows = [{k: _serial_ps(v) for k,v in dict(r).items()} for r in ps_rows]

        for col, label in _PS_COLS:
            vals = [float(r[col]) for r in ps_rows if r.get(col) is not None]
            if vals:
                ps_data[col] = {
                    "label"  : label,
                    "value"  : round(sum(vals)/len(vals), 3),
                    "n"      : len(vals),
                    "source" : "window",
                }
            else:
                # Last available before production window
                try:
                    fb_sql = text(f"""
                        SELECT `{col}` FROM preparedsand
                        WHERE foundry_line_id = :fl_id AND deleted = 0
                          AND `{col}` IS NOT NULL
                          AND `date` < :dt
                        ORDER BY `date` DESC, `time` DESC LIMIT 1
                    """)
                    dt_start_d = t_start_buf.date() if hasattr(t_start_buf, 'date') else t_start_buf
                    with engine.connect() as conn:
                        fb = conn.execute(fb_sql, {"fl_id": fl_id, "dt": dt_start_d}).mappings().first()
                    if fb and fb[col] is not None:
                        ps_data[col] = {"label": label, "value": round(float(fb[col]), 3), "n": 0, "source": "last_available"}
                    else:
                        ps_data[col] = {"label": label, "value": None, "n": 0, "source": "unavailable"}
                except Exception:
                    ps_data[col] = {"label": label, "value": None, "n": 0, "source": "unavailable"}

    # ── Component group / name ─────────────────────────────────────────────
    # foundry_line_group_component.component_id stores component NAME (not numeric ID)
    # Try direct match first, then fall back via components table name lookup
    group_name = ""
    try:
        with engine.connect() as conn:
            g = conn.execute(text("""
                SELECT g.name, g.description
                FROM foundry_line_group_component gc
                JOIN foundry_line_group g ON g.pkey = gc.foundry_line_group_pkey
                WHERE gc.component_id = :comp AND gc.deleted = 0
                  AND g.foundry_line_pkey = :fl_id AND g.deleted = 0
                LIMIT 1
            """), {"comp": component_id, "fl_id": fl_id}).mappings().first()
            if g:
                group_name = g["name"] or ""
            else:
                # foundry_line_group_component stores component name — resolve via components table
                g2 = conn.execute(text("""
                    SELECT g.name, g.description
                    FROM foundry_line_group_component gc
                    JOIN foundry_line_group g ON g.pkey = gc.foundry_line_group_pkey
                    JOIN components c ON c.component_id = gc.component_id
                    WHERE c.component_name = :comp AND gc.deleted = 0
                      AND g.foundry_line_pkey = :fl_id AND g.deleted = 0
                    LIMIT 1
                """), {"comp": component_id, "fl_id": fl_id}).mappings().first()
                if g2:
                    group_name = g2["name"] or ""
    except Exception:
        pass

    # ── SMC LCL / UCL from properties table (operator-configured limits) ──
    smc_lcl, smc_ucl = None, None
    try:
        # Build candidate java_names for compactability_smc_pct
        _smc_col = "compactability_smc_pct"
        _parts   = _smc_col.split("_")
        _camel   = _parts[0] + "".join(p.capitalize() for p in _parts[1:])
        _names   = list({_smc_col, _camel, _camel[0].upper() + _camel[1:]})
        with engine.connect() as conn:
            _prop = conn.execute(text("""
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
            """), {"fl": fl_id, "names": tuple(_names)}).mappings().first()
        if _prop:
            smc_lcl = float(_prop["cpk_min"]) if _prop["cpk_min"] is not None else None
            smc_ucl = float(_prop["cpk_max"]) if _prop["cpk_max"] is not None else None
    except Exception:
        pass

    # ── Summarise additive averages ────────────────────────────────────────
    bb_threshold_used = bb_threshold
    n_bad = sum(1 for r in add_rows if r.get("diff") is not None and abs(r["diff"]) > bb_threshold_used)
    n_warn = sum(1 for r in add_rows if r.get("diff") is not None and BB_WARN_THRESHOLD < abs(r["diff"]) <= bb_threshold_used)

    production_start = add_rows[0]["timestamp"]  if add_rows else None
    production_end   = add_rows[-1]["timestamp"] if add_rows else None

    # ── Component details (name, group, weight, SMR, core) ──────────────
    _comp_detail = {"component_name": None, "group_name": None,
                    "component_weight": None, "smr": None, "core_weight": None}
    try:
        # Try with optional columns — different foundries may have different schemas
        _cd_sql = text("""
            SELECT component_name
            FROM components
            WHERE component_id = :comp
            LIMIT 1
        """)
        with engine.connect() as _cdconn:
            _cdr = _cdconn.execute(_cd_sql, {"comp": component_id}).mappings().first()
            if _cdr:
                _comp_detail["component_name"] = str(_cdr["component_name"]) if _cdr["component_name"] else None
                # Try extended fields (may not exist in all foundries)
                try:
                    _cd_ext = text("""
                        SELECT weight_of_component AS component_weight,
                               core_weight,
                               sand_metal_ratio AS smr
                        FROM components WHERE component_id = :comp LIMIT 1
                    """)
                    _ext = _cdconn.execute(_cd_ext, {"comp": component_id}).mappings().first()
                    if _ext:
                        _comp_detail["component_weight"] = float(_ext["component_weight"]) if _ext["component_weight"] else None
                        _comp_detail["core_weight"]      = float(_ext["core_weight"]) if _ext["core_weight"] else None
                        _comp_detail["smr"]              = _ext["smr"]
                except Exception:
                    pass
                _comp_detail["component_weight"] = float(_cdr["component_weight"]) if _cdr["component_weight"] else None
                _comp_detail["core_weight"]      = float(_cdr["core_weight"]) if _cdr["core_weight"] else None
                _comp_detail["smr"]              = _cdr["smr"]
    except Exception as _cde:
        logger.debug("component detail lookup failed: %s", _cde)
    # Group name from foundry_line_group
    # foundry_line_group_component stores component NAME in component_id column
    try:
        with engine.connect() as _gcconn:
            _gcr = _gcconn.execute(text("""
                SELECT g.name FROM foundry_line_group_component gc
                JOIN foundry_line_group g ON g.pkey=gc.foundry_line_group_pkey
                WHERE gc.component_id=:comp AND gc.deleted=0
                  AND g.foundry_line_pkey=:fl AND g.deleted=0 LIMIT 1
            """), {"comp": component_id, "fl": fl_id}).mappings().first()
            if not _gcr:
                _gcr = _gcconn.execute(text("""
                    SELECT g.name FROM foundry_line_group_component gc
                    JOIN foundry_line_group g ON g.pkey=gc.foundry_line_group_pkey
                    JOIN components c ON c.component_id = gc.component_id
                    WHERE c.component_name=:comp AND gc.deleted=0
                      AND g.foundry_line_pkey=:fl AND g.deleted=0 LIMIT 1
                """), {"comp": component_id, "fl": fl_id}).mappings().first()
        if _gcr:
            _comp_detail["group_name"] = str(_gcr["name"])
    except Exception as _gce:
        logger.debug("group name lookup failed: %s", _gce)

    # ── Pouring / metal data ─────────────────────────────────────────────
    # metal table has no alias, so can't reuse 'AND a.shift=:shift' from additive query
    _metal_sf = "AND shift = :shift" if shift else ""
    pour_rows = _fetch_pour_rows(engine, fl_id, component_id, date_str,
                                  _metal_sf, shift_params)

    return {
        "component_id"       : component_id,
        "group_name"         : group_name,
        "date"               : date_str,
        "shift"              : shift or (str(add_rows[0]["shift"]) if add_rows else ""),
        "production_start"   : production_start,
        "production_end"     : production_end,
        "n_batches"          : len(add_rows),
        "n_bad_batch"        : n_bad,
        "n_warn_batch"       : n_warn,
        "bb_threshold"       : bb_threshold_used,
        "smc_rows"           : add_rows,
        "additives_avg"      : {
            "bentonite_actual"  : _avg(add_rows, "bentonite_actual"),
            "bentonite_set"     : _avg(add_rows, "bentonite_set_point"),
            "water_actual"      : _avg(add_rows, "water_actual"),
            "water_set"         : _avg(add_rows, "water_set_point"),
            "coal_dust_actual"  : _avg(add_rows, "coal_dust_actual"),
            "coal_dust_set"     : _avg(add_rows, "coal_dust_set_point"),
            "fss_actual"        : _avg(add_rows, "fss_actual"),
            "fss_set"           : _avg(add_rows, "fss_set_point"),
            "temperature_avg"   : _avg(add_rows, "temperature_c"),
            "moisture_smc_avg"  : _avg(add_rows, "moisture_smc_pct"),
        },
        "prescription_deviations": prescription_deviations,
        "prepared_sand"          : ps_data,
        "smc_lcl"                : smc_lcl,   # configured lower control limit
        "smc_ucl"                : smc_ucl,   # configured upper control limit
        "pour_rows"              : pour_rows,  # metal/pouring data (may be empty)
        "ps_rows"               : ps_rows,    # prepared sand batch rows
        "ps_cols"               : [(c,l) for c,l in _PS_COLS],  # column definitions
        "component_name"         : _comp_detail.get("component_name"),
        "group_name"             : _comp_detail.get("group_name"),
        "component_weight"       : _comp_detail.get("component_weight"),
        "core_weight"            : _comp_detail.get("core_weight"),
        "smr"                    : _comp_detail.get("smr"),
    }


def _fetch_pour_rows(engine, fl_id, component_id, date_str, shift_filter, shift_params):
    """Fetch metal/pouring rows for a component. Returns [] if table absent."""
    from sqlalchemy import text
    try:
        sql = text("""
            SELECT TIMESTAMP(`date`, `time`) AS metal_ts,
                   heat_no, shift,
                   pouring_temp, pouring_time, tapping_temp,
                   mould_produced,
                   carbon, silicon, manganese, sulphur, phosphorous,
                   inoculation, inoculant_size
            FROM   metal
            WHERE  foundry_line_id = :fl_id
              AND  deleted         = 0
              AND  component_id    = :comp
              AND  `date`          = :dt
              {sf}
            ORDER  BY `time` ASC
        """.format(sf=shift_filter))
        with engine.connect() as conn:
            rows = conn.execute(sql,
                {"fl_id": fl_id, "comp": component_id,
                 "dt": date_str, **shift_params}).mappings().fetchall()
        return [dict(r) for r in rows]
    except Exception as _e:
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  PPT GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_pptx(data: dict) -> bytes:
    """Generate a PPTX report from component data dict. Returns bytes."""
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width  = Inches(13.33)
    prs.slide_height = Inches(7.5)

    blank_layout = prs.slide_layouts[6]  # blank

    comp      = data["component_id"]
    grp       = data["group_name"] or "—"
    dt        = data["date"]
    shift     = data["shift"]
    n_batches = data["n_batches"]
    n_bad     = data["n_bad_batch"]
    prod_s    = data["production_start"]
    prod_e    = data["production_end"]
    thr       = data["bb_threshold"]

    prod_time_str = ""
    if prod_s and prod_e:
        prod_time_str = f"{prod_s.strftime('%H:%M')} to {prod_e.strftime('%H:%M')}"

    # ─── Slide 1: Overview ───────────────────────────────────────────────────
    slide1 = prs.slides.add_slide(blank_layout)
    _slide1_overview(slide1, prs, data, comp, grp, dt, shift, prod_time_str, n_batches, n_bad)

    # ─── Slide 2: SMC / COSP Table ──────────────────────────────────────────
    if data["smc_rows"]:
        slide2 = prs.slides.add_slide(blank_layout)
        _slide_smc_table(slide2, prs, data)

    # ─── Slide 3: Additives Table ────────────────────────────────────────────
    slide3 = prs.slides.add_slide(blank_layout)
    _slide_additives_table(slide3, prs, data)

    # ─── Slide 4: Prepared Sand Table ────────────────────────────────────────
    if data["prepared_sand"]:
        slide4 = prs.slides.add_slide(blank_layout)
        _slide_prepared_sand(slide4, prs, data)

    # ─── Slide 5: Charts ────────────────────────────────────────────────────
    if data["smc_rows"]:
        slide5 = prs.slides.add_slide(blank_layout)
        _slide_charts(slide5, prs, data)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ── Slide builders ────────────────────────────────────────────────────────────

def _rgb(hex6: str):
    from pptx.dml.color import RGBColor
    return RGBColor(int(hex6[:2],16), int(hex6[2:4],16), int(hex6[4:],16))


def _set_cell_bg(cell, hex6: str):
    from pptx.oxml.ns import qn
    from lxml import etree
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    solidFill = etree.SubElement(tcPr, qn("a:solidFill"))
    srgb = etree.SubElement(solidFill, qn("a:srgbClr"))
    srgb.set("val", hex6)


def _add_title_bar(slide, prs, title: str, subtitle: str = ""):
    from pptx.util import Inches, Pt
    # Yellow title bar
    bar = slide.shapes.add_shape(
        1,  # MSO_SHAPE_TYPE.RECTANGLE
        Inches(0), Inches(0),
        prs.slide_width, Inches(1.0),
    )
    bar.fill.solid()
    bar.fill.fore_color.rgb = _rgb(_YELLOW)
    bar.line.color.rgb = _rgb(_INK)
    bar.line.width = Inches(0.02)

    tf = bar.text_frame
    tf.word_wrap = False
    p = tf.paragraphs[0]
    p.alignment = 1  # LEFT
    run = p.add_run()
    run.text = title
    run.font.bold  = True
    run.font.size  = Pt(20)
    run.font.color.rgb = _rgb(_INK)

    if subtitle:
        from pptx.util import Pt
        p2 = tf.add_paragraph()
        p2.alignment = 1
        r2 = p2.add_run()
        r2.text = subtitle
        r2.font.size = Pt(11)
        r2.font.color.rgb = _rgb(_HEADER)


def _slide1_overview(slide, prs, data, comp, grp, dt, shift, prod_time, n_batches, n_bad):
    from pptx.util import Inches, Pt

    _add_title_bar(slide, prs,
        f"Component Analysis  ·  {comp}",
        f"Date: {dt}  ·  Shift: {shift}  ·  Group: {grp}")

    # Info box
    info_box = slide.shapes.add_textbox(Inches(0.4), Inches(1.2), Inches(6), Inches(5.5))
    tf = info_box.text_frame
    tf.word_wrap = True

    lines = [
        ("Component ID",    comp),
        ("Group",           grp),
        ("Date",            dt),
        ("Shift",           shift),
        ("Production Time", prod_time),
        ("Total Batches",   str(n_batches)),
        ("Bad Batches",     f"{n_bad} / {n_batches}  (threshold ±{data['bb_threshold']})"),
    ]

    for i, (label, value) in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(6)
        rl = p.add_run()
        rl.text = f"{label}: "
        rl.font.bold = True
        rl.font.size = Pt(14)
        rl.font.color.rgb = _rgb(_SAGE)
        rv = p.add_run()
        rv.text = value
        rv.font.size = Pt(14)
        rv.font.color.rgb = _rgb(_INK)

    # Status badge
    sev_col = _RED if n_bad > 0 else _GREEN
    sev_txt = f"{n_bad} BAD BATCH{'ES' if n_bad>1 else ''}" if n_bad > 0 else "ALL BATCHES OK"
    badge = slide.shapes.add_shape(1, Inches(7), Inches(2), Inches(5.5), Inches(1.2))
    badge.fill.solid()
    badge.fill.fore_color.rgb = _rgb(sev_col)
    badge.line.color.rgb = _rgb(_INK)
    badge.line.width = Inches(0.03)
    tf2 = badge.text_frame
    tf2.word_wrap = False
    p2 = tf2.paragraphs[0]
    p2.alignment = 2  # CENTER
    r2 = p2.add_run()
    r2.text = sev_txt
    r2.font.bold = True
    r2.font.size = Pt(22)
    r2.font.color.rgb = _rgb(_WHITE)


def _slide_smc_table(slide, prs, data):
    from pptx.util import Inches, Pt

    rows_data = data["smc_rows"]
    thr       = data["bb_threshold"]

    _add_title_bar(slide, prs,
        f"SMC Discharge vs COSP  ·  {data['component_id']}",
        f"Date: {data['date']}  ·  Shift: {data['shift']}  ·  Bad Batch Threshold: ±{thr}")

    # Table headers
    headers = ["Time", "Water Added\n(ltr)", "Sand Temp\n(°C)", "Mixing Time\n(sec)",
               "SMC Discharge\n(%)", "COSP\n(%)", "Difference", "Status"]

    n_data = min(len(rows_data), 20)  # cap at 20 rows per slide
    n_rows = n_data + 1  # +1 for header

    col_widths = [Inches(1.4), Inches(1.0), Inches(1.0), Inches(1.0),
                  Inches(1.4), Inches(0.9), Inches(1.2), Inches(1.2)]
    total_w = sum(col_widths)

    table = slide.shapes.add_table(
        n_rows, len(headers),
        Inches(0.2), Inches(1.1),
        total_w, Inches(6.1),
    ).table

    # Set column widths
    for i, w in enumerate(col_widths):
        table.columns[i].width = w

    # Header row
    for ci, h in enumerate(headers):
        cell = table.cell(0, ci)
        cell.text = h
        cell.text_frame.paragraphs[0].alignment = 2
        _set_cell_bg(cell, _HEADER)
        for run in cell.text_frame.paragraphs[0].runs:
            run.font.color.rgb = _rgb(_YELLOW)
            run.font.bold = True
            run.font.size = Pt(9)

    # Data rows
    for ri, row in enumerate(rows_data[:20]):
        r_idx = ri + 1
        smc  = row.get("smc")
        cosp = row.get("cosp")
        diff = row.get("diff")
        ts   = row.get("timestamp")

        is_bad  = diff is not None and abs(diff) > thr
        is_warn = diff is not None and BB_WARN_THRESHOLD < abs(diff) <= thr
        row_bg  = _RED if is_bad else (_ORANGE if is_warn else _WHITE)
        txt_col = _WHITE if is_bad else (_WHITE if is_warn else _INK)

        cells_data = [
            ts.strftime("%H:%M") if ts else "—",
            f"{row.get('water_actual', '—')}" if row.get("water_actual") else "—",
            f"{row.get('temperature_c', '—'):.1f}" if row.get("temperature_c") else "—",
            f"{int(row.get('total_seconds', 0))}" if row.get("total_seconds") else "—",
            f"{smc:.2f}" if smc is not None else "—",
            f"{cosp:.2f}" if cosp is not None else "—",
            f"{diff:+.3f}" if diff is not None else "—",
            "BAD BATCH" if is_bad else ("WARNING" if is_warn else "OK"),
        ]

        for ci, val in enumerate(cells_data):
            cell = table.cell(r_idx, ci)
            cell.text = val
            p = cell.text_frame.paragraphs[0]
            p.alignment = 2  # CENTER
            _set_cell_bg(cell, row_bg)
            for run in p.runs:
                run.font.color.rgb = _rgb(txt_col)
                run.font.bold = is_bad or is_warn
                run.font.size = Pt(8.5)


def _slide_additives_table(slide, prs, data):
    from pptx.util import Inches, Pt

    _add_title_bar(slide, prs,
        f"Additives Data  ·  {data['component_id']}",
        f"Date: {data['date']}  ·  Shift: {data['shift']}")

    avg = data["additives_avg"]
    devs = {d["param"]: d for d in data.get("prescription_deviations", [])}

    # Build rows: parameter | avg actual | set point | deviation %
    additive_rows = [
        ("Bentonite",        avg.get("bentonite_actual"),  avg.get("bentonite_set"),   "bentonite"),
        ("Water (ltr)",      avg.get("water_actual"),      avg.get("water_set"),        "water"),
        ("Coal Dust / LCA",  avg.get("coal_dust_actual"),  avg.get("coal_dust_set"),   "lca"),
        ("Fresh Silica Sand",avg.get("fss_actual"),        avg.get("fss_set"),          "freshSilicaSand"),
        ("Sand Temp (°C)",   avg.get("temperature_avg"),   None,                       None),
        ("SMC Moisture (%)", avg.get("moisture_smc_avg"),  None,                       None),
    ]

    headers = ["Parameter", "Avg Actual", "Set Point / Prescribed", "Deviation %", "Status"]
    n_rows  = len(additive_rows) + 1

    col_widths = [Inches(2.8), Inches(2.0), Inches(2.8), Inches(2.0), Inches(2.0)]
    total_w    = sum(col_widths)

    table = slide.shapes.add_table(
        n_rows, 5,
        Inches(0.8), Inches(1.2),
        total_w, Inches(5.0),
    ).table

    for i, w in enumerate(col_widths):
        table.columns[i].width = w

    # Header
    for ci, h in enumerate(headers):
        cell = table.cell(0, ci)
        cell.text = h
        _set_cell_bg(cell, _HEADER)
        p = cell.text_frame.paragraphs[0]
        p.alignment = 2
        for run in p.runs:
            run.font.color.rgb = _rgb(_YELLOW)
            run.font.bold = True
            run.font.size = Pt(10)

    # Data rows
    for ri, (label, actual, setpt, param_key) in enumerate(additive_rows):
        r_idx = ri + 1

        # Get deviation from prescription alerts
        presc_dev = devs.get(param_key)
        pct_diff  = presc_dev.get("pct_diff") if presc_dev else None

        if pct_diff is None and actual is not None and setpt and setpt > 0:
            pct_diff = round((actual - setpt) / setpt * 100, 2)

        is_dev = pct_diff is not None and abs(pct_diff) > 5.0
        is_warn = pct_diff is not None and 2.0 < abs(pct_diff) <= 5.0
        row_bg = _RED if is_dev else (_ORANGE if is_warn else _WHITE)
        txt_col = _WHITE if (is_dev or is_warn) else _INK

        set_str = f"{setpt:.3f}" if setpt and setpt > 0 else (
            f"{presc_dev['prescribed']:.3f}" if presc_dev else "—"
        )
        pct_str  = f"{pct_diff:+.2f}%" if pct_diff is not None else "—"
        status   = "DEVIATION" if is_dev else ("WARNING" if is_warn else "OK")

        cells_data = [
            label,
            f"{actual:.3f}" if actual is not None else "—",
            set_str,
            pct_str,
            status,
        ]

        for ci, val in enumerate(cells_data):
            cell = table.cell(r_idx, ci)
            cell.text = val
            _set_cell_bg(cell, row_bg)
            p = cell.text_frame.paragraphs[0]
            p.alignment = 1 if ci == 0 else 2
            for run in p.runs:
                run.font.color.rgb = _rgb(txt_col)
                run.font.bold = is_dev or is_warn
                run.font.size = Pt(10)


def _slide_prepared_sand(slide, prs, data):
    from pptx.util import Inches, Pt

    _add_title_bar(slide, prs,
        f"Prepared Sand Properties  ·  {data['component_id']}",
        f"Date: {data['date']}  ·  Shift: {data['shift']}  ·  Readings during production window")

    ps = data["prepared_sand"]
    ps_items = [(v["label"], v["value"], v["n"], v["source"])
                for k, v in ps.items() if k != "_window"]

    headers = ["Property", "Value", "Readings", "Source"]
    n_rows  = len(ps_items) + 1

    col_widths = [Inches(3.5), Inches(2.0), Inches(1.5), Inches(2.5)]
    total_w    = sum(col_widths)

    table = slide.shapes.add_table(
        n_rows, 4,
        Inches(1.5), Inches(1.2),
        total_w, Inches(5.6),
    ).table

    for i, w in enumerate(col_widths):
        table.columns[i].width = w

    for ci, h in enumerate(headers):
        cell = table.cell(0, ci)
        cell.text = h
        _set_cell_bg(cell, _HEADER)
        p = cell.text_frame.paragraphs[0]
        p.alignment = 2
        for run in p.runs:
            run.font.color.rgb = _rgb(_YELLOW)
            run.font.bold = True
            run.font.size = Pt(10)

    for ri, (label, value, n, source) in enumerate(ps_items):
        r_idx = ri + 1
        src_col = _GRAY if source == "last_available" else _WHITE
        cells_data = [
            label,
            f"{value:.3f}" if value is not None else "—",
            str(n) if n > 0 else "—",
            source.replace("_", " ").title(),
        ]
        for ci, val in enumerate(cells_data):
            cell = table.cell(r_idx, ci)
            cell.text = val
            _set_cell_bg(cell, src_col if value is not None else "FFEEEE")
            p = cell.text_frame.paragraphs[0]
            p.alignment = 1 if ci == 0 else 2
            for run in p.runs:
                run.font.size = Pt(9.5)
                run.font.color.rgb = _rgb(_INK)


def _slide_charts(slide, prs, data):
    """Generate SMC + additive trend charts using matplotlib, embed as images."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from pptx.util import Inches

    _add_title_bar(slide, prs,
        f"Trend Charts  ·  {data['component_id']}",
        f"Date: {data['date']}  ·  Shift: {data['shift']}")

    rows = data["smc_rows"]
    if not rows:
        return

    times   = [r["timestamp"].strftime("%H:%M") if r.get("timestamp") else "" for r in rows]
    smc_vals = [r.get("smc") for r in rows]
    cosp_vals = [r.get("cosp") for r in rows]
    ben_vals  = [r.get("bentonite_actual") for r in rows]
    water_vals = [r.get("water_actual") for r in rows]
    thr = data["bb_threshold"]

    x = list(range(len(times)))
    xt = times[::max(1, len(times)//8)]
    xi = list(range(0, len(times), max(1, len(times)//8)))

    # ── Chart 1: SMC vs COSP ─────────────────────────────────────────────
    fig1, ax1 = plt.subplots(figsize=(7, 3.5))
    ax1.plot(x, smc_vals, 'o-', color='#c01616', linewidth=2, markersize=4, label='SMC Discharge')
    if any(v is not None for v in cosp_vals):
        ax1.plot(x, cosp_vals, '--', color='#2d7a4f', linewidth=1.5, label='COSP Setpoint')
        ax1.fill_between(x,
            [c - thr if c else None for c in cosp_vals],
            [c + thr if c else None for c in cosp_vals],
            alpha=0.12, color='#2d7a4f', label=f'±{thr} tolerance')
    # Highlight bad points
    bad_x = [i for i, r in enumerate(rows) if r.get("diff") is not None and abs(r["diff"]) > thr]
    bad_y = [smc_vals[i] for i in bad_x]
    if bad_x:
        ax1.scatter(bad_x, bad_y, color='#c01616', s=80, zorder=5, marker='X', label='Bad Batch')

    ax1.set_xticks(xi); ax1.set_xticklabels(xt, rotation=35, fontsize=7)
    ax1.set_ylabel("Compactability (%)", fontsize=8)
    ax1.set_title(f"SMC Discharge vs COSP — {data['component_id']}", fontsize=10, fontweight='bold')
    ax1.legend(fontsize=7); ax1.grid(True, alpha=0.3)
    ax1.set_facecolor('#fafaf8'); fig1.patch.set_facecolor('#f5f0e8')
    plt.tight_layout()

    buf1 = io.BytesIO()
    fig1.savefig(buf1, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig1)
    buf1.seek(0)
    slide.shapes.add_picture(buf1, Inches(0.2), Inches(1.1), Inches(6.8), Inches(3.0))

    # ── Chart 2: Additives ────────────────────────────────────────────────
    fig2, ax2 = plt.subplots(figsize=(5, 3.5))
    if any(v is not None for v in ben_vals):
        ax2.plot(x, ben_vals, 's-', color='#6d28d9', linewidth=1.5, markersize=4, label='Bentonite (kg)')
    if any(v is not None for v in water_vals):
        ax2_r = ax2.twinx()
        ax2_r.plot(x, water_vals, '^-', color='#0e7490', linewidth=1.5, markersize=4, label='Water (ltr)')
        ax2_r.set_ylabel("Water (ltr)", fontsize=8, color='#0e7490')
    ax2.set_xticks(xi); ax2.set_xticklabels(xt, rotation=35, fontsize=7)
    ax2.set_ylabel("Bentonite (kg)", fontsize=8, color='#6d28d9')
    ax2.set_title("Additives Trend", fontsize=10, fontweight='bold')
    ax2.grid(True, alpha=0.3)
    ax2.set_facecolor('#fafaf8'); fig2.patch.set_facecolor('#f5f0e8')
    plt.tight_layout()

    buf2 = io.BytesIO()
    fig2.savefig(buf2, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig2)
    buf2.seek(0)
    slide.shapes.add_picture(buf2, Inches(7.2), Inches(1.1), Inches(5.8), Inches(3.0))


# ══════════════════════════════════════════════════════════════════════════════
#  PDF GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_pdf(data: dict) -> bytes:
    """Generate a PDF report from component data dict. Returns bytes."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import inch, mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable, Image)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.colors import HexColor

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        rightMargin=15*mm, leftMargin=15*mm,
        topMargin=12*mm, bottomMargin=12*mm,
        title=f"Component Report — {data['component_id']}",
    )

    styles  = getSampleStyleSheet()
    W, H    = landscape(A4)
    usable_w = W - 30*mm

    C_YELLOW = HexColor("#FFE14D")
    C_INK    = HexColor("#111111")
    C_RED    = HexColor("#FF4444")
    C_ORANGE = HexColor("#FF9944")
    C_GREEN  = HexColor("#2D7A4F")
    C_GREEN_LT = HexColor("#D4F0E0")
    C_GRAY   = HexColor("#F0ECE2")
    C_HEADER = HexColor("#1A1A1A")
    C_WHITE  = colors.white
    C_SAGE   = HexColor("#2D7A4F")

    title_style = ParagraphStyle("Title",
        fontName="Helvetica-Bold", fontSize=16,
        textColor=C_INK, spaceAfter=4)
    sub_style   = ParagraphStyle("Sub",
        fontName="Helvetica", fontSize=10,
        textColor=HexColor("#333333"), spaceAfter=8)
    label_style = ParagraphStyle("Label",
        fontName="Helvetica-Bold", fontSize=9,
        textColor=C_SAGE)
    section_style = ParagraphStyle("Section",
        fontName="Helvetica-Bold", fontSize=12,
        textColor=C_INK, spaceBefore=12, spaceAfter=6,
        backColor=C_YELLOW, borderPad=4)

    comp   = data["component_id"]
    thr    = data["bb_threshold"]
    n_bad  = data["n_bad_batch"]
    n_warn = data["n_warn_batch"]

    story = []

    # ── Header ────────────────────────────────────────────────────────────
    story.append(Paragraph(f"Component Analysis Report — {comp}", title_style))
    story.append(Paragraph(
        f"Date: {data['date']}  ·  Shift: {data['shift']}  ·  Group: {data['group_name'] or '—'}  ·  "
        f"Production: {data['production_start'].strftime('%H:%M') if data.get('production_start') else '—'} "
        f"to {data['production_end'].strftime('%H:%M') if data.get('production_end') else '—'}",
        sub_style
    ))
    story.append(HRFlowable(width="100%", thickness=2, color=C_INK))
    story.append(Spacer(1, 8))

    # ── Summary row ───────────────────────────────────────────────────────
    sev_col = C_RED if n_bad > 0 else C_GREEN
    sev_txt = f"{n_bad} BAD BATCH(ES) DETECTED" if n_bad > 0 else "ALL BATCHES OK"
    summary_data = [[
        Paragraph(f"<b>Total Batches:</b> {data['n_batches']}", styles["Normal"]),
        Paragraph(f"<b>Bad Batches:</b> {n_bad}", styles["Normal"]),
        Paragraph(f"<b>Threshold:</b> ±{thr}", styles["Normal"]),
        Paragraph(f"<b>{sev_txt}</b>", ParagraphStyle("s", textColor=sev_col, fontName="Helvetica-Bold", fontSize=10)),
    ]]
    summary_tbl = Table(summary_data, colWidths=[usable_w/4]*4)
    summary_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), C_GRAY),
        ("BOX", (0,0), (-1,-1), 1, C_INK),
        ("INNERGRID", (0,0), (-1,-1), 0.5, HexColor("#cccccc")),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(summary_tbl)
    story.append(Spacer(1, 10))

    # ── SMC / COSP Table ─────────────────────────────────────────────────
    story.append(Paragraph("SMC Discharge vs COSP", section_style))
    smc_header = ["Time", "Water\n(ltr)", "Temp\n(°C)", "Mix\n(sec)",
                  "SMC\nDischarge", "COSP\n(%)", "Diff", "Status"]
    smc_data = [smc_header]
    for row in data["smc_rows"]:
        diff = row.get("diff")
        is_bad  = diff is not None and abs(diff) > thr
        is_warn = diff is not None and BB_WARN_THRESHOLD < abs(diff) <= thr
        ts   = row.get("timestamp")
        smc_data.append([
            ts.strftime("%H:%M") if ts else "—",
            f"{row.get('water_actual','—')}" if row.get("water_actual") else "—",
            f"{row.get('temperature_c','—'):.1f}" if row.get("temperature_c") else "—",
            f"{int(row.get('total_seconds',0))}" if row.get("total_seconds") else "—",
            f"{row.get('smc','—'):.2f}" if row.get("smc") is not None else "—",
            f"{row.get('cosp','—'):.2f}" if row.get("cosp") is not None else "—",
            f"{diff:+.3f}" if diff is not None else "—",
            "BAD" if is_bad else ("WARN" if is_warn else "OK"),
        ])

    col_w = [usable_w*f for f in [0.10, 0.09, 0.09, 0.09, 0.13, 0.09, 0.11, 0.10]]
    # normalise to sum=1
    total_f = sum(col_w)
    col_w   = [w * usable_w / total_f for w in col_w]

    smc_tbl = Table(smc_data, colWidths=col_w, repeatRows=1)
    smc_style = [
        ("BACKGROUND", (0,0), (-1,0), C_HEADER),
        ("TEXTCOLOR",  (0,0), (-1,0), C_YELLOW),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,-1), 7.5),
        ("ALIGN",      (0,0), (-1,-1), "CENTER"),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("BOX",        (0,0), (-1,-1), 1, C_INK),
        ("INNERGRID",  (0,0), (-1,-1), 0.3, HexColor("#cccccc")),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
    ]
    for ri, row in enumerate(data["smc_rows"]):
        diff = row.get("diff")
        if diff is not None and abs(diff) > thr:
            smc_style.append(("BACKGROUND", (0, ri+1), (-1, ri+1), C_RED))
            smc_style.append(("TEXTCOLOR",  (0, ri+1), (-1, ri+1), C_WHITE))
        elif diff is not None and abs(diff) > BB_WARN_THRESHOLD:
            smc_style.append(("BACKGROUND", (0, ri+1), (-1, ri+1), C_ORANGE))
            smc_style.append(("TEXTCOLOR",  (0, ri+1), (-1, ri+1), C_WHITE))

    smc_tbl.setStyle(TableStyle(smc_style))
    story.append(smc_tbl)
    story.append(Spacer(1, 12))

    # ── Additives Table ───────────────────────────────────────────────────
    story.append(Paragraph("Additives Data", section_style))
    avg  = data["additives_avg"]
    devs = {d["param"]: d for d in data.get("prescription_deviations", [])}

    add_header = ["Parameter", "Avg Actual", "Set Point / Prescribed", "Deviation %", "Status"]
    add_data   = [add_header]
    additive_rows_pdf = [
        ("Bentonite",         avg.get("bentonite_actual"),  avg.get("bentonite_set"),   "bentonite"),
        ("Water (ltr)",       avg.get("water_actual"),      avg.get("water_set"),        "water"),
        ("Coal Dust / LCA",   avg.get("coal_dust_actual"),  avg.get("coal_dust_set"),   "lca"),
        ("Fresh Silica Sand", avg.get("fss_actual"),        avg.get("fss_set"),          "freshSilicaSand"),
        ("Sand Temp (°C)",    avg.get("temperature_avg"),   None,                       None),
        ("SMC Moisture (%)",  avg.get("moisture_smc_avg"),  None,                       None),
    ]
    add_style_cmds = [
        ("BACKGROUND", (0,0), (-1,0), C_HEADER),
        ("TEXTCOLOR",  (0,0), (-1,0), C_YELLOW),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,-1), 8.5),
        ("ALIGN",      (0,1), (-1,-1), "CENTER"),
        ("ALIGN",      (0,0), (0,-1), "LEFT"),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("BOX",        (0,0), (-1,-1), 1, C_INK),
        ("INNERGRID",  (0,0), (-1,-1), 0.3, HexColor("#cccccc")),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
    ]
    for ri, (label, actual, setpt, pk) in enumerate(additive_rows_pdf):
        presc_dev = devs.get(pk)
        pct_diff  = presc_dev.get("pct_diff") if presc_dev else None
        if pct_diff is None and actual is not None and setpt and setpt > 0:
            pct_diff = round((actual - setpt) / setpt * 100, 2)
        set_str  = f"{setpt:.3f}" if setpt and setpt > 0 else (f"{presc_dev['prescribed']:.3f}" if presc_dev else "—")
        pct_str  = f"{pct_diff:+.2f}%" if pct_diff is not None else "—"
        is_dev   = pct_diff is not None and abs(pct_diff) > 5.0
        is_warn2 = pct_diff is not None and 2.0 < abs(pct_diff) <= 5.0
        add_data.append([label, f"{actual:.3f}" if actual else "—", set_str, pct_str,
                          "DEVIATION" if is_dev else ("WARNING" if is_warn2 else "OK")])
        if is_dev:
            add_style_cmds.append(("BACKGROUND", (0, ri+1), (-1, ri+1), C_RED))
            add_style_cmds.append(("TEXTCOLOR",  (0, ri+1), (-1, ri+1), C_WHITE))
        elif is_warn2:
            add_style_cmds.append(("BACKGROUND", (0, ri+1), (-1, ri+1), C_ORANGE))
            add_style_cmds.append(("TEXTCOLOR",  (0, ri+1), (-1, ri+1), C_WHITE))

    add_col_w = [usable_w*0.30, usable_w*0.17, usable_w*0.23, usable_w*0.17, usable_w*0.13]
    add_tbl   = Table(add_data, colWidths=add_col_w, repeatRows=1)
    add_tbl.setStyle(TableStyle(add_style_cmds))
    story.append(add_tbl)
    story.append(Spacer(1, 12))

    # ── Prepared Sand ─────────────────────────────────────────────────────
    if data["prepared_sand"]:
        story.append(Paragraph("Prepared Sand Properties (during production window)", section_style))
        ps_header = ["Property", "Value", "Readings", "Source"]
        ps_data_tbl = [ps_header]
        for col, label in _PS_COLS:
            info = data["prepared_sand"].get(col, {})
            ps_data_tbl.append([
                label,
                f"{info.get('value','—'):.3f}" if info.get("value") is not None else "—",
                str(info.get("n", "—")),
                info.get("source", "—").replace("_", " ").title(),
            ])
        ps_col_w = [usable_w*0.40, usable_w*0.20, usable_w*0.15, usable_w*0.25]
        ps_tbl   = Table(ps_data_tbl, colWidths=ps_col_w, repeatRows=1)
        ps_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), C_HEADER),
            ("TEXTCOLOR",  (0,0), (-1,0), C_YELLOW),
            ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",   (0,0), (-1,-1), 8.5),
            ("ALIGN",      (0,1), (-1,-1), "CENTER"),
            ("ALIGN",      (0,0), (0,-1), "LEFT"),
            ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
            ("BOX",        (0,0), (-1,-1), 1, C_INK),
            ("INNERGRID",  (0,0), (-1,-1), 0.3, HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_WHITE, C_GRAY]),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        story.append(ps_tbl)

    doc.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
#  CONVENIENCE: generate both formats
# ══════════════════════════════════════════════════════════════════════════════

def generate_component_report(engine, foundry_line_id: int, component_id: str,
                               date_str: str, shift: Optional[str] = None,
                               bb_threshold: float = BB_THRESHOLD_DEFAULT,
                               fmt: str = "pptx") -> bytes:
    """
    High-level entry point.
    fmt: "pptx" | "pdf"
    Returns bytes of the generated file.
    """
    data = fetch_component_data(engine, foundry_line_id, component_id,
                                 date_str, shift, bb_threshold)
    if fmt == "pdf":
        return generate_pdf(data)
    return generate_pptx(data)
