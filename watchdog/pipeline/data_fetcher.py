"""
pipeline/data_fetcher.py
-------------------------
Fetches raw data from the four source tables via SQLAlchemy:
  1. preparedsand          -- lab-measured sand properties  (shift-level)
  2. consumption           -- shift-level additive totals
  3. additive              -- per-batch PLC/SCADA actuals   (batch-level)
  4. prepared_sand_extra   -- per-batch SMC discharge data  (batch-level)

Also provides:
  fetch_control_limits()   -- LCL/UCL live from properties + measures tables
  fetch_shift_config()     -- shift boundaries from customer_foundry_info

All queries use SQLAlchemy text() with named :param placeholders.
Connection is pooled and SSL-ready (configured in watchdog_config.json).
"""

import json
import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import text

from .db_connector import get_engine, load_config

logger = logging.getLogger(__name__)


def _col_select(cols: list[str]) -> str:
    """Build a backtick-quoted column list for MySQL."""
    return ", ".join(f"`{c}`" for c in cols)


def _date_filter(
    start: Optional[date],
    end:   Optional[date],
    date_col: str = "date",
) -> tuple[str, dict]:
    """Return (SQL fragment, params dict) for an optional date range.
    Qualified names (containing '.') are NOT backtick-wrapped."""
    col    = date_col if "." in date_col else f"`{date_col}`"
    parts, params = [], {}
    if start:
        parts.append(f"DATE({col}) >= :start_date")
        params["start_date"] = start
    if end:
        parts.append(f"DATE({col}) <= :end_date")
        params["end_date"] = end
    return (" AND ".join(parts) if parts else "1=1", params)


def _run_query(sql: str, params: dict, config: dict) -> pd.DataFrame:
    """Execute a SELECT and return a DataFrame via the SQLAlchemy engine."""
    engine = get_engine(config)
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def fetch_prepared_sand(
    config:     dict,
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
) -> pd.DataFrame:
    fl_id = config["foundry_line_id"]
    cols  = config["parameters"]["prepared_sand"]
    # Always include `time` (reading timestamp) and `component_id` for
    # production-window and direct component matching.
    meta_extra = ["time", "component_id"]
    select_cols = ["date", "shift"] + meta_extra + [
        c for c in cols if c not in ("date", "shift") + tuple(meta_extra)
    ]
    col_sql = _col_select(select_cols)

    date_frag, date_params = _date_filter(start_date, end_date)
    params = {"fl_id": fl_id, **date_params}

    sql = f"""
        SELECT {col_sql}
        FROM   `preparedsand`
        WHERE  `foundry_line_id` = :fl_id
          AND  `deleted` = 0
          AND  {date_frag}
        ORDER BY `date`, `shift`
    """

    df = _run_query(sql, params, config)
    df = _normalise_date_shift(df)
    logger.info("preparedsand        : %d rows fetched (date range %s - %s)",
                len(df), start_date, end_date)
    return df


def fetch_consumption_booking(
    config:     dict,
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
) -> pd.DataFrame:
    """
    Fetch consumption_booking rows: one row per (date, shift, component_id)
    production run with start_time and end_time.
    Used to determine the exact time window each component was produced,
    so prepared sand readings can be attributed to the correct component.
    """
    fl_id = config["foundry_line_id"]
    date_frag, date_params = _date_filter(start_date, end_date, date_col="date")
    params = {"fl_id": fl_id, **date_params}

    sql = f"""
        SELECT `component_id`, DATE(`date`) AS date, `shift`,
               `start_time`, `end_time`
        FROM   `consumption_booking`
        WHERE  `foundry_line_id` = :fl_id
          AND  `deleted` = 0
          AND  {date_frag}
        ORDER BY `date`, `shift`, `start_time`
    """
    try:
        df = _run_query(sql, params, config)
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        logger.info("consumption_booking : %d rows fetched", len(df))
        return df
    except Exception as exc:
        # Return empty so callers degrade to shift-level PS attribution.
        # Log at WARNING so operators can distinguish a missing table (expected
        # for foundries that don't use bookings) from a transient DB outage.
        logger.warning(
            "fetch_consumption_booking failed — PS will fall back to shift average: %s",
            exc,
        )
        return pd.DataFrame()


def fetch_consumption(
    config:     dict,
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
) -> pd.DataFrame:
    fl_id = config["foundry_line_id"]
    cols  = config["parameters"]["consumption"]
    select_cols = ["date", "shift"] + [c for c in cols if c not in ("date", "shift")]
    col_sql = _col_select(select_cols)

    date_frag, date_params = _date_filter(start_date, end_date)
    params = {"fl_id": fl_id, **date_params}

    sql = f"""
        SELECT {col_sql}
        FROM   `consumption`
        WHERE  `foundry_line_id` = :fl_id
          AND  `deleted` = 0
          AND  {date_frag}
        ORDER BY `date`, `shift`
    """

    df = _run_query(sql, params, config)
    df = _normalise_date_shift(df)
    logger.info("consumption         : %d rows fetched", len(df))
    return df


def fetch_additive(
    config:     dict,
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
) -> pd.DataFrame:
    fl_id     = config["foundry_line_id"]
    cols      = config["parameters"]["additive"]
    meta_cols = ["date", "shift", "batch_counter", "mixer_name", "component_id", "timestamp"]
    select_cols = meta_cols + [c for c in cols if c not in meta_cols]
    col_sql   = _col_select(select_cols)

    date_frag, date_params = _date_filter(start_date, end_date)
    params = {"fl_id": fl_id, **date_params}

    sql = f"""
        SELECT {col_sql}
        FROM   `additive`
        WHERE  `foundry_line_id` = :fl_id
          AND  `deleted` = 0
          AND  {date_frag}
        ORDER BY `date`, `shift`, `batch_counter`
    """

    df = _run_query(sql, params, config)
    df = _normalise_date_shift(df)
    logger.info("additive            : %d batch rows fetched", len(df))
    return df


def fetch_prepared_sand_extra(
    config:     dict,
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
) -> pd.DataFrame:
    fl_id     = config["foundry_line_id"]
    cols      = config["parameters"]["prepared_sand_extra"]
    meta_cols = ["date", "shift"]
    select_cols = meta_cols + [c for c in cols if c not in meta_cols]
    col_sql   = _col_select(select_cols)

    date_frag, date_params = _date_filter(start_date, end_date)
    params = {"fl_id": fl_id, **date_params}

    sql = f"""
        SELECT {col_sql}
        FROM   `prepared_sand_extra`
        WHERE  `foundry_line_id` = :fl_id
          AND  `deleted` = 0
          AND  {date_frag}
        ORDER BY `date`, `shift`
    """

    df = _run_query(sql, params, config)
    df = _normalise_date_shift(df)
    logger.info("prepared_sand_extra : %d batch rows fetched", len(df))
    return df


def fetch_all(
    config:     dict,
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
) -> dict[str, pd.DataFrame]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tasks = {
        "prepared_sand"       : lambda: fetch_prepared_sand(config, start_date, end_date),
        "consumption"         : lambda: fetch_consumption(config, start_date, end_date),
        "additive"            : lambda: fetch_additive(config, start_date, end_date),
        "prepared_sand_extra" : lambda: fetch_prepared_sand_extra(config, start_date, end_date),
        "sieve"               : lambda: _fetch_sieve_safe(config, start_date, end_date),
        "consumption_booking" : lambda: fetch_consumption_booking(config, start_date, end_date),
    }

    result = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                result[key] = future.result()
            except Exception as exc:
                logger.warning("fetch_all: %s failed: %s", key, exc)
                result[key] = pd.DataFrame()

    return result


def _fetch_sieve_safe(config, start_date, end_date) -> pd.DataFrame:
    try:
        return fetch_sieve_data(config, start_date, end_date, sand_types=[1, 2, 3])
    except Exception as exc:
        logger.debug("fetch_all: sieve data not available (%s)", exc)
        return pd.DataFrame()


def fetch_last_n_periods(config: dict, n_periods: int = 30) -> dict[str, pd.DataFrame]:
    end   = date.today()
    start = end - timedelta(days=n_periods + 5)
    return fetch_all(config, start_date=start, end_date=end)


def _camel_to_snake(name: str) -> str:
    """Convert javaName -> java_name."""
    import re
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s)
    return s.lower()


def fetch_control_limits(config: dict) -> dict:
    """
    Returns {column_name: {"lcl": float|None, "ucl": float|None}} from DB.

    Each config parameter group is mapped to the correct measure_name so that
    parameters which exist in multiple measures (e.g. moisture, loi, gcs) always
    use the limits for the table they are actually monitored from.

    Priority (applied in order -- later entries overwrite earlier ones):
        consumption / smc / consumptionbooking -> then preparedsand (highest priority)
    """
    fl_id = config["foundry_line_id"]

    # Map each config parameter to its authoritative measure_name
    param_measure: dict[str, str] = {}
    for p in config["parameters"].get("consumption", []):
        param_measure[p] = "consumption"
    for p in config["parameters"].get("additive", []):
        param_measure[p] = "smc"
    for p in config["parameters"].get("prepared_sand_extra", []):
        param_measure[p] = "smc"
    for p in config["parameters"].get("prepared_sand", []):
        param_measure[p] = "preparedsand"   # highest priority -- overwrites if duplicate

    SQL = text("""
        SELECT p.java_name, p.cpk_min, p.cpk_max, m.name AS measure_name
        FROM   `properties` p
        JOIN   `measures`   m ON p.measure_pkey = m.pkey
        WHERE  m.foundry_line_id = :fl_id
          AND  m.isActive  = 1
          AND  p.deleted   = 0
          AND  p.is_active = 1
        ORDER BY m.name, p.java_name
    """)

    engine = get_engine(config)
    try:
        with engine.connect() as conn:
            rows = conn.execute(SQL, {"fl_id": fl_id}).mappings().all()
    except Exception as exc:
        logger.warning("fetch_control_limits: query failed -- %s", exc)
        return {}

    # Build per-measure buckets first
    by_measure: dict[str, dict] = {}
    for row in rows:
        java_name    = (row.get("java_name")    or "").strip()
        measure_name = (row.get("measure_name") or "").strip()
        if not java_name:
            continue
        col_name = _camel_to_snake(java_name)
        cpk_min  = row.get("cpk_min")
        cpk_max  = row.get("cpk_max")
        by_measure.setdefault(measure_name, {})[col_name] = {
            "lcl": float(cpk_min) if cpk_min is not None else None,
            "ucl": float(cpk_max) if cpk_max is not None else None,
        }

    # Merge: for each monitored param, pick limits from its own measure
    limits: dict = {}
    for col_name, measure_name in param_measure.items():
        bucket = by_measure.get(measure_name, {})
        if col_name in bucket:
            limits[col_name] = bucket[col_name]
            logger.debug("  [%-14s] %-30s  LCL=%-10s UCL=%s",
                         measure_name, col_name,
                         limits[col_name]["lcl"], limits[col_name]["ucl"])

    # Also include non-monitored params from all measures (for reference / future use)
    monitored = set(param_measure.keys())
    for measure_name, bucket in by_measure.items():
        for col_name, v in bucket.items():
            if col_name not in limits and col_name not in monitored:
                limits[col_name] = v

    logger.info("fetch_control_limits: %d limits loaded from DB", len(limits))
    return limits


def _table_columns(engine, table: str) -> set[str]:
    """Return the set of actual column names for a DB table."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"DESCRIBE `{table}`")).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def _resolve_columns(java_names: list[str], actual_cols: set[str]) -> list[str]:
    """
    Convert java_names to snake_case and validate against real table columns.
    Falls back to stripping all underscores when the snake_case name doesn't exist
    (handles legacy columns like freshsilicasand, returnsand).
    """
    result = []
    for jn in java_names:
        snake = _camel_to_snake(jn)
        if snake in actual_cols:
            result.append(snake)
        else:
            # try stripping underscores (e.g. fresh_silica_sand -> freshsilicasand)
            no_under = snake.replace("_", "")
            match = next((c for c in actual_cols if c.replace("_", "") == no_under), None)
            if match:
                result.append(match)
            else:
                logger.debug("fetch_monitored_parameters: column %r not found in table -- skipped", snake)
    return result


def fetch_monitored_parameters(config: dict) -> dict[str, list[str]]:
    """
    Returns all parameter lists to monitor, fully derived from the DB.

    prepared_sand  -- keys of analytics_report.predicted_sand_parameters_json
                     (the parameters that affect the prediction model)
    consumption    -- active properties of the consumption measure
    additive       -- active properties of the smc measure
    prepared_sand_extra -- active properties of the smc measure (same source)

    All column names are validated against the real table schema so legacy
    column names (e.g. freshsilicasand vs fresh_silica_sand) are handled.
    """
    fl_id  = config["foundry_line_id"]
    engine = get_engine(config)
    result: dict[str, list[str]] = {}

    # -- prepared_sand: from analytics_report prediction model ----------------
    SQL_AR = text("""
        SELECT predicted_sand_parameters_json
        FROM   `analytics_report`
        WHERE  foundry_line_pkey = :fl_id
          AND  deleted = 0
        ORDER  BY pkey DESC LIMIT 1
    """)
    try:
        with engine.connect() as conn:
            row = conn.execute(SQL_AR, {"fl_id": fl_id}).mappings().first()
        if row and row.get("predicted_sand_parameters_json"):
            data      = json.loads(row["predicted_sand_parameters_json"])
            ps_cols   = _table_columns(engine, "preparedsand")
            ps_params = _resolve_columns(list(data.keys()), ps_cols)
            result["prepared_sand"] = ps_params
            logger.info("fetch_monitored_parameters [prepared_sand]: %s", ps_params)
        else:
            logger.warning("fetch_monitored_parameters: no analytics_report for foundry_line_pkey=%s", fl_id)
    except Exception as exc:
        logger.warning("fetch_monitored_parameters [prepared_sand]: %s", exc)

    # -- consumption / additive / smc: from active measures + properties -------
    SQL_PROPS = text("""
        SELECT p.java_name
        FROM   `properties` p
        JOIN   `measures`   m ON p.measure_pkey = m.pkey
        WHERE  m.foundry_line_id = :fl_id
          AND  m.name      = :mname
          AND  m.isActive  = 1
          AND  p.deleted   = 0
          AND  p.is_active = 1
        ORDER  BY p.order_no, p.java_name
    """)
    for measure_name, (param_key, db_table) in {
        "consumption" : ("consumption", "consumption"),
        "additive"    : ("additive",    "additive"),
    }.items():
        try:
            with engine.connect() as conn:
                rows = conn.execute(SQL_PROPS, {"fl_id": fl_id, "mname": measure_name}).fetchall()
            java_names  = [r[0] for r in rows if r[0]]
            actual_cols = _table_columns(engine, db_table)
            params      = _resolve_columns(java_names, actual_cols)
            # Exclude set-point columns and columns with no analytical value.
            # wd1_ltr: internal water-dosing sub-channel, redundant with total_water_ltr.
            # inert_fines_actual: SMC sensor reading, not a controlled addition.
            _EXCLUDE = {"wd1_ltr", "inert_fines_actual"}
            params = [c for c in params
                      if not c.endswith("_set_point") and c not in _EXCLUDE]
            if params:
                result[param_key] = params
                logger.info("fetch_monitored_parameters [%s]: %s", param_key, params)
        except Exception as exc:
            logger.warning("fetch_monitored_parameters [%s]: %s", param_key, exc)

    return result


def fetch_display_names(config: dict) -> dict[str, str]:
    """
    Returns {column_name: ui_name} for all active properties of this foundry.

    Registers each java_name under THREE keys so lookups always succeed
    regardless of how legacy columns are named in the actual DB tables:
      1. snake_case:        fresh_silica_sand  -> "Fresh Silica Sand (MT)"
      2. no underscores:    freshsilicasand    -> "Fresh Silica Sand (MT)"
      3. original camelCase: freshSilicaSand   -> "Fresh Silica Sand (MT)"
    """
    fl_id = config["foundry_line_id"]
    SQL   = text("""
        SELECT p.java_name, p.ui_name
        FROM   `properties` p
        JOIN   `measures`   m ON p.measure_pkey = m.pkey
        WHERE  m.foundry_line_id = :fl_id
          AND  m.isActive  = 1
          AND  p.deleted   = 0
          AND  p.is_active = 1
          AND  p.ui_name   IS NOT NULL
          AND  p.java_name IS NOT NULL
    """)
    engine = get_engine(config)
    names: dict[str, str] = {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(SQL, {"fl_id": fl_id}).mappings().all()
        for row in rows:
            java_name = (row.get("java_name") or "").strip()
            ui_name   = (row.get("ui_name")   or "").strip()
            if not java_name or not ui_name:
                continue
            snake   = _camel_to_snake(java_name)
            no_und  = snake.replace("_", "")
            names[snake]     = ui_name   # fresh_silica_sand
            names[no_und]    = ui_name   # freshsilicasand
            names[java_name] = ui_name   # freshSilicaSand
        logger.info("fetch_display_names: %d ui_name mappings loaded from DB", len(names))
    except Exception as exc:
        logger.warning("fetch_display_names: query failed -- %s", exc)
    return names


# -----------------------------------------------------------------------------
# Sieve data
# -----------------------------------------------------------------------------

_SAND_TYPE_PREFIX = {
    0: "Return Sand",
    1: "Prepared Sand",
    2: "New Sand",
    3: "Core Sand",
}

_WASH_SUFFIX_MAP = {
    "after wash":  "aw",
    "before wash": "bw",
    "after":       "aw",
    "before":      "bw",
    "1":  "aw",   # DB stores wash_type as int: 1 = After Wash
    "0":  "bw",   # 0 = Before Wash
}

_BAND_SNAKE = {
    "Fines":     "fines",
    "Coarser":   "coarser",
    "gfnAfs":    "gfn_afs",
    "Middle":    "middle",
    "totalSand": "total_sand",
}


def _wash_suffix(wash_val) -> str:
    s = str(wash_val).strip().lower()
    return _WASH_SUFFIX_MAP.get(s, s.replace(" ", "_"))


def fetch_sieve_data(
    config:     dict,
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
    sand_types: list[int] | None = None,
) -> pd.DataFrame:
    """
    Fetch sieve band data per shift from sieve_band_data (which carries date + shift directly).
    LEFT JOINs sieves for wash_type.

    sand_types: list of sand_type ints to include.
                Defaults to [1, 2, 3] -- Prepared Sand, New Sand, Core Sand.
                Return Sand (0) is excluded by default.
    """
    if sand_types is None:
        sand_types = [1, 2, 3]   # Prepared, New, Core (no Return Sand)

    date_frag, date_params = _date_filter(start_date, end_date, date_col="s.date")
    # Build IN clause placeholders
    in_placeholders = ", ".join(f":st{i}" for i in range(len(sand_types)))
    st_params = {f"st{i}": v for i, v in enumerate(sand_types)}
    params = {**date_params, **st_params}

    sql = f"""
        SELECT s.date, s.shift, s.wash_type,
               b.sand_type, b.band_type, b.value
        FROM   sieves          s
        JOIN   sieve_band_data b ON s.pkey = b.sieve_id
        WHERE  b.sand_type IN ({in_placeholders})
          AND  {date_frag}
        ORDER  BY s.date, s.shift, b.sand_type, s.wash_type, b.band_type
    """
    try:
        df = _run_query(sql, params, config)
        df = _normalise_date_shift(df)
        logger.info("sieve_data (sand_types=%s): %d rows fetched", sand_types, len(df))
    except Exception as exc:
        logger.warning("fetch_sieve_data: query failed -- %s", exc)
        df = pd.DataFrame(columns=["date", "shift", "sand_type", "wash_type", "band_type", "value"])
    return df


_SAND_TYPE_SHORT = {0: "rs", 1: "ps", 2: "ns", 3: "cs"}  # rs=return, ps=prepared, ns=new, cs=core


def build_sieve_dataset(df_sieve: pd.DataFrame) -> pd.DataFrame:
    """
    Pivot raw sieve rows -> one row per (date, shift).
    Column name: sv_{sand_short}_{band}_{wash}
    e.g. sv_rs_fines_bw (Return Sand, Fines, Before Wash)
         sv_ps_gfn_afs_aw (Prepared Sand, GFN AFS, After Wash)
    Multiple readings per group are averaged.
    """
    if df_sieve.empty:
        return pd.DataFrame(columns=["date", "shift"])

    df = df_sieve.copy()
    df["value"] = pd.to_numeric(df["value"], errors="coerce")

    # Normalise wash_type (may be NaN/None if LEFT JOIN found no match)
    df["wash_suffix"] = df["wash_type"].apply(
        lambda x: _wash_suffix(x) if pd.notna(x) else "unk"
    )
    df["band_snake"] = df["band_type"].map(_BAND_SNAKE).fillna(
        df["band_type"].str.lower().str.replace(r"[^a-z0-9]+", "_", regex=True)
    )
    df["sand_short"] = df["sand_type"].map(_SAND_TYPE_SHORT).fillna(
        df["sand_type"].astype(str)
    )
    df["col"] = "sv_" + df["sand_short"] + "_" + df["band_snake"] + "_" + df["wash_suffix"]

    df_agg = (
        df.groupby(["date", "shift", "col"], as_index=False)["value"]
        .mean()
    )
    df_pivot = df_agg.pivot_table(
        index=["date", "shift"], columns="col", values="value", aggfunc="mean",
    ).reset_index()
    df_pivot.columns.name = None

    sv_cols = [c for c in df_pivot.columns if c.startswith("sv_")]
    logger.info("build_sieve_dataset: %d shift rows, cols: %s", len(df_pivot), sv_cols)
    return df_pivot


def build_sieve_shift_dataset(
    df_sieve:    pd.DataFrame,
    band_filter: list[str] | None = None,
) -> pd.DataFrame:
    """
    Pivot sieve data to one row per (date, shift).

    Unlike build_sieve_dataset, this version:
      - Averages across ALL wash types  -- one value per (shift, sand_type, band)
      - Omits the wash suffix from column names
      - Column format: sv_{sand_short}_{band_snake}
        e.g. sv_ps_fines, sv_ns_gfn_afs, sv_cs_fines

    band_filter  -- bare band names from config (e.g. ["Fines", "gfnAfs"]).
                   If None/empty, all available bands are included.

    If sieve data is not available for a shift those columns will be NaN
    in the merged dataset and are silently skipped by the engines.
    """
    if df_sieve is None or df_sieve.empty:
        return pd.DataFrame(columns=["date", "shift"])

    df = df_sieve.copy()
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["band_snake"] = df["band_type"].map(_BAND_SNAKE).fillna(
        df["band_type"].str.lower().str.replace(r"[^a-z0-9]+", "_", regex=True)
    )
    df["sand_short"] = df["sand_type"].map(_SAND_TYPE_SHORT).fillna(
        df["sand_type"].astype(str)
    )

    # Filter to configured bands when specified
    # e.g. ["Fines", "gfnAfs"] -> keep rows whose band_snake is "fines" or "gfn_afs"
    if band_filter:
        import re as _re
        norm = {
            _BAND_SNAKE.get(b, _re.sub(r"[^a-z0-9]+", "_", b.lower().strip()))
            for b in band_filter
        }
        df = df[df["band_snake"].isin(norm)]

    if df.empty:
        return pd.DataFrame(columns=["date", "shift"])

    # Average across all wash types -- one value per (date, shift, sand_type, band)
    df["col"] = "sv_" + df["sand_short"] + "_" + df["band_snake"]
    df_agg    = df.groupby(["date", "shift", "col"], as_index=False)["value"].mean()

    df_pivot = df_agg.pivot_table(
        index=["date", "shift"], columns="col", values="value", aggfunc="mean",
    ).reset_index()
    df_pivot.columns.name = None

    sv_cols = [c for c in df_pivot.columns if c.startswith("sv_")]
    logger.info("build_sieve_shift_dataset: %d rows, cols=%s", len(df_pivot), sv_cols)
    return df_pivot


def fetch_sieve_limits(config: dict, sand_types: list[int] | None = None) -> dict:
    """
    Parse sieve_band_values JSON from sieve_config for the requested sand types.
    Returns {col_name: {"lcl": float|None, "ucl": float|None}}.
    JSON key format : "Return Sand-After Wash-Fines": {"min": 4.7, "max": 5.1}
    Column name format: sv_{sand_short}_{band}_{wash}  e.g. sv_rs_fines_aw
    """
    if sand_types is None:
        sand_types = [0, 1, 2, 3]   # Return, Prepared, New, Core

    fl_id  = config.get("foundry_line_id")
    engine = get_engine(config)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT sieve_band_values FROM sieve_config WHERE foundry_line_id = :fl_id"),
                {"fl_id": fl_id},
            ).mappings().first()
    except Exception as exc:
        logger.warning("fetch_sieve_limits: query failed -- %s", exc)
        return {}

    if not row or not row.get("sieve_band_values"):
        return {}

    raw = row["sieve_band_values"]
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        logger.warning("fetch_sieve_limits: JSON parse error -- %s", exc)
        return {}

    prefix_to_short = {_SAND_TYPE_PREFIX[k]: _SAND_TYPE_SHORT[k]
                       for k in sand_types if k in _SAND_TYPE_PREFIX}

    limits = {}
    for key, val in data.items():
        sand_short = None
        sand_prefix = None
        for prefix, short in prefix_to_short.items():
            if key.startswith(prefix + "-"):
                sand_short  = short
                sand_prefix = prefix
                break
        if sand_short is None:
            continue

        rest  = key[len(sand_prefix) + 1:]
        parts = rest.split("-", 1)
        if len(parts) < 2:
            continue
        wash_sfx = _wash_suffix(parts[0].strip())
        band_sfx = _BAND_SNAKE.get(
            parts[1].strip(),
            parts[1].strip().lower().replace(" ", "_"),
        )
        col = f"sv_{sand_short}_{band_sfx}_{wash_sfx}"
        limits[col] = {
            "lcl": float(val["min"]) if val.get("min") is not None else None,
            "ucl": float(val["max"]) if val.get("max") is not None else None,
        }
        logger.debug("sieve limit: %s  LCL=%s  UCL=%s", col, limits[col]["lcl"], limits[col]["ucl"])

    logger.info("fetch_sieve_limits: %d limits parsed", len(limits))
    return limits


def sieve_display_names() -> dict:
    """
    Return human-readable display names for sv_ columns.
    Keys are WITHOUT the sv_ prefix because _label() strips it before lookup.
    """
    _band_labels = {
        "fines":      "Fines",
        "coarser":    "Coarser",
        "gfn_afs":    "GFN AFS",
        "middle":     "Middle",
        "total_sand": "Total Sand",
    }
    _sand_labels = {"rs": "RS", "ps": "PS", "ns": "NS", "cs": "CS"}
    names = {}
    for sand_short, sand_lbl in _sand_labels.items():
        for band, band_lbl in _band_labels.items():
            names[f"{sand_short}_{band}_bw"] = f"{sand_lbl} {band_lbl} (BW)"
            names[f"{sand_short}_{band}_aw"] = f"{sand_lbl} {band_lbl} (AW)"
    return names


def fetch_active_foundry_line_ids(config: dict) -> list[int]:
    """Return all active foundry_line pkeys from the DB."""
    SQL = text("SELECT `pkey` FROM `foundry_line` WHERE `is_active` = 1 ORDER BY `pkey`")
    engine = get_engine(config)
    with engine.connect() as conn:
        rows = conn.execute(SQL).fetchall()
    ids = [int(r[0]) for r in rows if r[0] is not None]
    if not ids:
        raise ValueError("foundry_line: no active rows found")
    logger.info("fetch_active_foundry_line_ids: %s", ids)
    return ids


def is_prescription_eligible(config: dict) -> bool:
    """
    Returns True if the foundry line has a SCADA DB connection configured
    (scada_db_properties_json IS NOT NULL in foundry_line).
    Lines without a SCADA connection have no additive batch data and should
    skip prescription monitoring.
    """
    fl_id = config.get("foundry_line_id")
    if not fl_id:
        return False
    engine = get_engine(config)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT `scada_db_properties_json` FROM `foundry_line` "
                    "WHERE `pkey` = :fl_id AND `deleted` = 0"
                ),
                {"fl_id": fl_id},
            ).mappings().first()
        if row is None:
            logger.warning("is_prescription_eligible: foundry_line pkey=%s not found", fl_id)
            return False
        scada_json = row["scada_db_properties_json"]
        eligible = scada_json is not None and str(scada_json).strip() not in ("", "{}")
        logger.info(
            "is_prescription_eligible: foundry_line=%s  scada_configured=%s",
            fl_id, eligible,
        )
        if not eligible:
            return False
        # Also verify the foundry DB actually has the 'additive' table.
        # Foundries that use a different schema (e.g. shreeraj) may have SCADA
        # configured but no 'additive' table.
        with engine.connect() as conn:
            row2 = conn.execute(text("SHOW TABLES LIKE 'additive'")).first()
        if row2 is None:
            logger.info(
                "is_prescription_eligible: foundry_line=%s  'additive' table not found -- skipping",
                fl_id,
            )
            return False
        return True
    except Exception as exc:
        logger.warning("is_prescription_eligible: query failed -- %s", exc)
        return False


def fetch_shift_config(config: dict) -> dict:
    """Return shift boundaries parsed from customer_foundry_info (shift + shift_timings columns)."""
    fl_id = config.get("foundry_line_id")

    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            row = None
            # Try with foundry_line_id filter first (multi-foundry correctness)
            if fl_id is not None:
                try:
                    SQL_filtered = text(
                        "SELECT `shift`, `shift_timings` FROM `customer_foundry_info` "
                        "WHERE `foundry_line_id` = :fl_id LIMIT 1"
                    )
                    row = conn.execute(SQL_filtered, {"fl_id": fl_id}).mappings().first()
                except Exception:
                    pass   # column may not exist -- fall through to unfiltered query
            if row is None:
                SQL_fallback = text(
                    "SELECT `shift`, `shift_timings` FROM `customer_foundry_info` LIMIT 1"
                )
                row = conn.execute(SQL_fallback).mappings().first()
    except Exception as exc:
        logger.warning("fetch_shift_config: query failed -- %s", exc)
        return {}

    if not row:
        logger.warning("fetch_shift_config: customer_foundry_info is empty")
        return {}

    raw_shifts  = (row.get("shift")         or "").strip()
    raw_timings = (row.get("shift_timings") or "").strip()

    if not raw_shifts or not raw_timings:
        logger.warning("fetch_shift_config: shift or shift_timings column is blank")
        return {}

    shift_names  = [s.strip() for s in raw_shifts.split(",")  if s.strip()]
    timing_parts = [t.strip() for t in raw_timings.split(",") if t.strip()]

    if len(shift_names) != len(timing_parts):
        logger.warning(
            "fetch_shift_config: mismatch -- %d shift names vs %d timings",
            len(shift_names), len(timing_parts)
        )
        return {}

    result_dict = {}
    for name, timing in zip(shift_names, timing_parts):
        if "-" not in timing:
            logger.warning("fetch_shift_config: unexpected timing format %r for shift %s",
                           timing, name)
            continue
        parts = timing.split("-", 1)
        result_dict[name] = {
            "start": _trim_to_hhmm(parts[0]),
            "end":   _trim_to_hhmm(parts[1]),
        }

    logger.info("fetch_shift_config: loaded %d shifts from DB: %s",
                len(result_dict), list(result_dict.keys()))
    return result_dict


def _trim_to_hhmm(time_str: str) -> str:
    """Convert 'HH:MM:SS' or 'HH:MM' to 'HH:MM'."""
    parts = time_str.strip().split(":")
    h = parts[0].zfill(2) if len(parts) > 0 else "00"
    m = parts[1].zfill(2) if len(parts) > 1 else "00"
    return f"{h}:{m}"


def assign_shift_from_time(time_val, shift_config: dict) -> str:
    """
    Return shift number string for a given time using shift_config from fetch_shift_config().

    shift_config format: {"1": {"start": "HH:MM", "end": "HH:MM"}, ...}

    Handles overnight shifts (e.g. 16:00–00:00) where end <= start.
    Returns "" when no shift matches or inputs are invalid.
    """
    if not shift_config or time_val is None:
        return ""

    # Normalise time_val to minutes-since-midnight
    try:
        if hasattr(time_val, "hour"):          # datetime.time or datetime.datetime
            minutes = time_val.hour * 60 + time_val.minute
        else:
            s = str(time_val).strip()
            parts = s.split(":")
            minutes = int(parts[0]) * 60 + int(parts[1]) if len(parts) >= 2 else -1
        if minutes < 0:
            return ""
    except Exception:
        return ""

    for shift_name, bounds in shift_config.items():
        try:
            sh, sm = map(int, bounds["start"].split(":"))
            eh, em = map(int, bounds["end"].split(":"))
        except Exception:
            continue

        start_min = sh * 60 + sm
        end_min   = eh * 60 + em

        if end_min == 0:               # "00:00" means midnight = end of day
            end_min = 24 * 60

        if start_min < end_min:        # normal shift: start < end
            if start_min <= minutes < end_min:
                return shift_name
        else:                          # overnight shift: wraps midnight
            if minutes >= start_min or minutes < end_min:
                return shift_name

    return ""


def _normalise_date_shift(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure date->Python date, shift->stripped string."""
    if df.empty:
        return df
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    if "shift" in df.columns:
        df["shift"] = df["shift"].astype(str).str.strip()
    return df


_PRESC_PARAM_MAP = {
    "bentonite"      : "bentonite_actual",
    "freshSilicaSand": "fss_actual",
    "lca"            : "coal_dust_actual",
    "water"          : "water_actual",
}
_PRESC_PARAM_LABELS = {
    "bentonite"      : "Bentonite (kg/batch)",
    "freshSilicaSand": "Fresh Silica Sand (kg/batch)",
    "lca"            : "LCA / Coal Dust (kg/batch)",
    "water"          : "Water (ltr/batch)",
}


def fetch_prescription_data(
    config:     dict,
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
) -> pd.DataFrame:
    """
    Fetch additive batch actuals joined with AI prescriptions from analytics_report.
    Returns a flat DataFrame with columns:
      date, shift, group, batch_time, component_id, pkey,
      {param}_prescribed, {param}_actual, {param}_diff, {param}_pct_diff, {param}_status
      overall_status  (OK / DEVIATION)
    for params: bentonite, freshSilicaSand, lca, water
    """
    fl_id   = int(config.get("foundry_line_id", 1))
    tol_pct = float(config.get("prescription_watchdog", {}).get("tolerance_pct", 3.0))

    date_frag, params = _date_filter(start_date, end_date, date_col="a.date")

    sql = f"""
        SELECT a.pkey, a.component_id, DATE(a.date) AS date, a.shift,
               a.timestamp AS batch_time,
               a.bentonite_actual, a.coal_dust_actual,
               a.fss_actual,       a.water_actual,
               g.name AS group_name
        FROM   additive a
        LEFT JOIN foundry_line_group_component gc
               ON gc.component_id = a.component_id AND gc.deleted = 0
        LEFT JOIN foundry_line_group g
               ON g.pkey = gc.foundry_line_group_pkey
              AND g.deleted = 0 AND g.foundry_line_pkey = :fl_id
        WHERE  a.foundry_line_id = :fl_id
          AND  a.deleted = 0
          AND  {date_frag}
        ORDER  BY a.date, a.shift, a.pkey
    """
    params["fl_id"] = fl_id

    engine = get_engine(config)
    with engine.connect() as conn:
        df_batches = pd.read_sql(text(sql), conn, params=params)

    if df_batches.empty:
        logger.info("fetch_prescription_data: no additive batches found for date range")
        return pd.DataFrame()

    df_batches["date"]  = pd.to_datetime(df_batches["date"]).dt.date
    df_batches["shift"] = df_batches["shift"].astype(str).str.strip()

    # Fetch prescriptions once per (group, date, shift) combination
    combos = (
        df_batches[["group_name", "date", "shift"]]
        .dropna(subset=["group_name"])
        .drop_duplicates()
    )
    presc_cache: dict[tuple, dict | None] = {}

    presc_sql = text("""
        SELECT predicted_additives_json
        FROM   analytics_report
        WHERE  foundry_line_pkey       = :fl_id
          AND  foundry_line_group_name = :grp
          AND  DATE(date)              = :dt
          AND  shift                   = :sh
          AND  deleted                 = 0
        ORDER  BY pkey DESC
        LIMIT  1
    """)
    with engine.connect() as conn:
        for _, row in combos.iterrows():
            key = (str(row["group_name"]), str(row["date"]), str(row["shift"]))
            r   = conn.execute(presc_sql, {
                "fl_id": fl_id,
                "grp"  : row["group_name"],
                "dt"   : str(row["date"]),
                "sh"   : str(row["shift"]),
            }).mappings().first()
            if r and r["predicted_additives_json"]:
                try:
                    presc_cache[key] = json.loads(r["predicted_additives_json"])
                except Exception:
                    presc_cache[key] = None
            else:
                presc_cache[key] = None

    rows_out = []
    for _, b in df_batches.iterrows():
        grp = b.get("group_name")
        key = (str(grp), str(b["date"]), str(b["shift"])) if grp else None
        presc = presc_cache.get(key) if key else None

        row_out = {
            "Date"        : b["date"],
            "Shift"       : b["shift"],
            "Group"       : grp or "--",
            "Batch Time"  : b.get("batch_time"),
            "Component ID": b.get("component_id"),
            "Batch pkey"  : b["pkey"],
        }

        any_deviation = False
        for pkey_name, actual_col in _PRESC_PARAM_MAP.items():
            label    = _PRESC_PARAM_LABELS[pkey_name]
            actual   = b.get(actual_col)
            prescribed = float(presc[pkey_name]) if (presc and pkey_name in presc and presc[pkey_name] is not None) else None

            actual_f     = float(actual) if actual is not None else None
            prescribed_f = prescribed

            # Skip zero/null actuals -- nothing was dispensed
            if actual_f is not None and prescribed_f is not None and prescribed_f != 0 and actual_f != 0:
                diff     = round(actual_f - prescribed_f, 3)
                pct_diff = round(diff / prescribed_f * 100, 2)
                within   = abs(pct_diff) <= tol_pct   # percentage-based tolerance
                status   = "OK" if within else "DEVIATION"
                if not within:
                    any_deviation = True
            elif actual_f == 0.0 or actual_f is None:
                diff = pct_diff = None
                status = "NO DATA"
            else:
                diff = pct_diff = None
                status = "NO PRESCRIPTION"

            short = pkey_name
            row_out[f"{short} Prescribed"] = prescribed_f
            row_out[f"{short} Actual"]     = round(actual_f, 3) if actual_f is not None else None
            row_out[f"{short} Diff"]       = diff
            row_out[f"{short} % Diff"]     = pct_diff
            row_out[f"{short} Status"]     = status

        row_out["Overall Status"] = "DEVIATION" if any_deviation else (
            "OK" if presc else "NO PRESCRIPTION"
        )
        rows_out.append(row_out)

    df_out = pd.DataFrame(rows_out)
    logger.info("fetch_prescription_data: %d batches, tolerance=%.1f%%", len(df_out), tol_pct)
    return df_out


# Sand-related defect columns — fallback when DB has no measures/properties config
_REJ_DEFECT_COLS = [
    "rejection_quantity",
    "blow_hole",
    "blow_hole_foundry_stage",
    "blow_hole_machining_stage",
    "sanddrop_inclusion_foundry_stage",
    "sanddrop_inclusion_machining_stage",
    "expansion_scab",
    "erosion_scab",
    "sand_fusion",
    "burn_on",
    "lustrous_carbon_defect",
    "shrinkage",
    "cold_shut_MISRUN",
]

_REJ_DEFECT_LABELS = {
    "rejection_quantity"                : "Total Rejection",
    "blow_hole"                         : "Blow Hole",
    "blow_hole_foundry_stage"           : "Blow Hole (Foundry)",
    "blow_hole_machining_stage"         : "Blow Hole (Machining)",
    "sanddrop_inclusion_foundry_stage"  : "Sand Drop Inclusion (Foundry)",
    "sanddrop_inclusion_machining_stage": "Sand Drop Inclusion (Machining)",
    "expansion_scab"                    : "Expansion Scab",
    "erosion_scab"                      : "Erosion Scab",
    "sand_fusion"                       : "Sand Fusion",
    "burn_on"                           : "Burn On",
    "lustrous_carbon_defect"            : "Lustrous Carbon",
    "shrinkage"                         : "Shrinkage",
    "cold_shut_MISRUN"                  : "Cold Shut / Misrun",
}


def fetch_prescription_data_scada(
    config:     dict,
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
) -> pd.DataFrame:
    """
    Fetch prescription vs actual data from scada_data for foundries without
    an additive table.

    - Actuals come from scada_data.additive_json  (values are totals in tonnes;
      divide by numberOfBatches and multiply by 1000 to get kg/batch).
    - Prescriptions come from analytics_report keyed on (group, date, shift).
    - Shift is derived from scada_data.time using fetch_shift_config().

    Returns a DataFrame in the same shape as fetch_prescription_data() so
    prescription_watchdog.py can use it without changes.
    """
    import json as _json
    fl_id       = int(config.get("foundry_line_id", 1))
    tol_pct     = float(config.get("prescription_watchdog", {}).get("tolerance_pct", 3.0))
    date_frag, params = _date_filter(start_date, end_date, date_col="s.date")
    params["fl_id"] = fl_id

    # Fetch shift config once
    shift_cfg = fetch_shift_config(config)

    sql = text(f"""
        SELECT s.pkey, s.component_id, DATE(s.date) AS date, s.time,
               s.additive_json,
               g.name AS group_name
        FROM   scada_data s
        LEFT JOIN foundry_line_group_component gc
               ON gc.component_id = s.component_id AND gc.deleted = 0
        LEFT JOIN foundry_line_group g
               ON g.pkey = gc.foundry_line_group_pkey
              AND g.deleted = 0 AND g.foundry_line_pkey = :fl_id
        WHERE  s.foundry_line_pkey = :fl_id
          AND  (s.deleted = 0 OR s.deleted IS NULL)
          AND  s.additive_json IS NOT NULL
          AND  s.additive_json NOT IN ('', '{{}}')
          AND  {date_frag}
        ORDER  BY s.pkey ASC
    """)

    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn, params=params)
    except Exception as exc:
        logger.warning("fetch_prescription_data_scada: query failed: %s", exc)
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"]).dt.date

    # Assign shift from time column
    df["shift"] = df["time"].apply(lambda t: assign_shift_from_time(t, shift_cfg))

    # Parse additive_json -> per-batch kg values
    _KEYS = ["bentonite", "freshSilicaSand", "lca", "water"]
    for key in _KEYS:
        df[f"{key}_actual_raw"] = df["additive_json"].apply(
            lambda js, k=key: _extract_scada_actual(js, k)
        )
    df["numberOfBatches"] = df["additive_json"].apply(
        lambda js: _extract_scada_actual(js, "numberOfBatches") or 1.0
    )
    # Convert total tonnes -> kg per batch
    for key in _KEYS:
        df[f"{key} Actual"] = (
            df[f"{key}_actual_raw"] / df["numberOfBatches"] * 1000
        ).round(3)

    # Fetch prescriptions per (group, date, shift) and join
    combos = (
        df[["group_name", "date", "shift"]]
        .dropna(subset=["group_name"])
        .drop_duplicates()
    )
    presc_sql = text("""
        SELECT predicted_additives_json
        FROM   analytics_report
        WHERE  foundry_line_pkey       = :fl_id
          AND  foundry_line_group_name = :grp
          AND  DATE(date)              = :dt
          AND  shift                   = :sh
          AND  deleted                 = 0
        ORDER  BY pkey DESC
        LIMIT  1
    """)
    presc_cache: dict = {}
    with engine.connect() as conn:
        for _, row in combos.iterrows():
            key = (str(row["group_name"]), str(row["date"]), str(row["shift"]))
            r   = conn.execute(presc_sql, {
                "fl_id": fl_id,
                "grp"  : row["group_name"],
                "dt"   : str(row["date"]),
                "sh"   : str(row["shift"]),
            }).mappings().first()
            if r and r["predicted_additives_json"]:
                try:
                    presc_cache[key] = (
                        _json.loads(r["predicted_additives_json"])
                        if isinstance(r["predicted_additives_json"], str)
                        else dict(r["predicted_additives_json"])
                    )
                except Exception:
                    presc_cache[key] = None
            else:
                presc_cache[key] = None

    # Attach prescribed values
    def _get_presc(row, param):
        key = (str(row.get("group_name") or ""), str(row.get("date") or ""), str(row.get("shift") or ""))
        p   = presc_cache.get(key) or {}
        return p.get(param)

    for key in _KEYS:
        df[f"{key} Prescribed"] = df.apply(lambda r, k=key: _get_presc(r, k), axis=1)

    # Rename to match fetch_prescription_data() output shape
    df = df.rename(columns={
        "pkey"        : "Batch pkey",
        "component_id": "Component ID",
        "date"        : "Date",
        "shift"       : "Shift",
        "group_name"  : "Group",
        "time"        : "Batch Time",
    })

    logger.info("fetch_prescription_data_scada: %d scada rows fetched", len(df))
    return df


def _extract_scada_actual(additive_json_str, key: str):
    """Parse additive_json string and return the value for key, or None."""
    import json as _json
    try:
        d = _json.loads(additive_json_str) if isinstance(additive_json_str, str) else additive_json_str
        v = d.get(key)
        return float(v) if v is not None else None
    except Exception:
        return None


import re as _re

_JAVA_TO_DB_OVERRIDES = {
    # DB stores these without underscore between "sand" and "drop"
    "sandDropInclusionFoundryStage":   "sanddrop_inclusion_foundry_stage",
    "sandDropInclusionMachiningStage": "sanddrop_inclusion_machining_stage",
}

def _java_name_to_db_col(java_name: str) -> str:
    """
    Convert camelCase java_name to snake_case DB column name.
    e.g.  sandDefect10  ->  sand_defect_10
          blowHole      ->  blow_hole
    """
    if java_name in _JAVA_TO_DB_OVERRIDES:
        return _JAVA_TO_DB_OVERRIDES[java_name]
    s = _re.sub(r'([A-Z])', r'_\1', java_name)   # insert _ before each capital
    s = s.lower().lstrip('_')
    s = _re.sub(r'([a-z])(\d)',  r'\1_\2', s)     # insert _ between letter and digit
    s = _re.sub(r'_+', '_', s)                    # collapse double underscores
    return s


def fetch_production_flow_flag(config: dict) -> bool:
    """
    Read prod_flow_change from foundry_line to determine whether
    production-flow rejection calculation is enabled for this line.

    SELECT prod_flow_change FROM foundry_line WHERE pkey = :line_id

    Returns True if prod_flow_change is a non-zero / truthy value,
    False otherwise (including when the column or table is unavailable).
    """
    fl_id = int(config.get("foundry_line_id", 1))
    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT `prod_flow_change` FROM `foundry_line` WHERE `pkey` = :line_id"),
                {"line_id": fl_id},
            ).mappings().first()
        if row is None:
            return False
        val = row["prod_flow_change"]
        enabled = bool(val) and str(val) not in ("0", "False", "false", "")
        logger.info(
            "fetch_production_flow_flag: foundry_line_id=%d  prod_flow_change=%r  -> %s",
            fl_id, val, "WITH production flow" if enabled else "WITHOUT production flow",
        )
        return enabled
    except Exception as exc:
        logger.debug("fetch_production_flow_flag: could not read foundry_line: %s", exc)
        return False


def fetch_rejection_defect_config(config: dict) -> dict:
    """
    Fetch dynamic defect column mapping from measures / properties tables.

    Returns {db_col: alias_label} for all defect properties where:
      - measures.name = 'rejection'  AND  measures.isActive = 1
      - properties.is_active = 1
      - properties.defect_group IS NOT NULL
      - properties.alias_name IS NOT NULL AND != ''

    Falls back to empty dict when the measures/properties tables don't exist.
    """
    try:
        engine = get_engine(config)
        fl_id  = config.get("foundry_line_id")
        with engine.connect() as conn:
            sql_rej = text("""
                SELECT DISTINCT p.alias_name, p.java_name, p.defect_group
                FROM   measures m
                JOIN   properties p ON m.pkey = p.measure_pkey
                WHERE  m.name      = 'rejection'
                  AND  m.isActive  = 1
                  AND  (:fl IS NULL OR m.foundry_line_id = :fl)
                  AND  p.is_active = 1
                  AND  p.defect_group IS NOT NULL
                  AND  p.defect_group != ''
                  AND  p.alias_name   IS NOT NULL
                  AND  p.alias_name   != ''
                ORDER  BY p.defect_group, p.alias_name
            """)
            rows = conn.execute(sql_rej, {"fl": fl_id}).fetchall()

        mapping = {}
        for r in rows:
            alias     = str(r[0]).strip()
            java_name = str(r[1]).strip() if r[1] else ""
            if not java_name:
                continue
            db_col = _java_name_to_db_col(java_name)
            if db_col and alias:
                mapping[db_col] = alias   # last alias wins on collision

        if mapping:
            logger.info(
                "fetch_rejection_defect_config: %d dynamic defect cols from measures/properties",
                len(mapping),
            )
        return mapping

    except Exception as exc:
        logger.debug("fetch_rejection_defect_config not available: %s", exc)
        return {}


def fetch_rejection_data(
    config:          dict,
    start_date:      Optional[date] = None,
    end_date:        Optional[date] = None,
    group_by:        str = "date",
    production_flow: Optional[bool] = None,
) -> pd.DataFrame:
    """
    Fetch rejections and compute weight-based aggregates.

    group_by = "date"      -> one row per date  (default)
    group_by = "component" -> one row per (date, component_id)

    production_flow = False  (default / WITHOUT production flow)
    ─────────────────────────────────────────────────────────────
      weight_rejected(defect) = qty(defect) × nett_casting_wt
      total_produced_wt       = total_quantity_produced × nett_casting_wt   ← per-component
      % rejected(defect)      = weight_rejected / total_produced_wt × 100

    production_flow = True  (WITH production flow — matches DISA/plant-level calc)
    ─────────────────────────────────────────────────────────────────────────────
      weight_rejected(defect) = qty(defect) × nett_casting_wt               (same)
      Comp_Prod_Wt            = total_quantity_produced × nett_casting_wt   (per component)
      Day_Total_Prod_Wt       = SUM(Comp_Prod_Wt) across ALL components     ← plant-level
      % rejected(defect)      = weight_rejected / Day_Total_Prod_Wt × 100

      This mirrors the production-flow method where the denominator is the
      total plant production weight for the day, not just the individual
      component's production.  Use this when comparing to plant-level reports.

    Columns returned:
      Date, [Component ID if component mode],
      Total Produced (wt kg), Total Rejection (%),
      <defect> (%), ...   (column names from alias_name in measures/properties,
                           or hardcoded fallback — vary per foundry)
    """
    fl_id = int(config.get("foundry_line_id", 1))
    date_frag, params = _date_filter(start_date, end_date, date_col="date")
    params["fl_id"] = fl_id

    # ── Auto-detect production_flow from foundry_line.prod_flow_change ─────────
    # If caller did not explicitly pass production_flow, read it from the DB.
    if production_flow is None:
        production_flow = fetch_production_flow_flag(config)

    # ── Resolve defect columns ─────────────────────────────────────────────────
    # Try dynamic config from measures/properties first (alias_name + defect_group
    # IS NOT NULL). Fall back to static hardcoded list if not available.
    dynamic_cfg = fetch_rejection_defect_config(config)

    if dynamic_cfg:
        # Use only columns that actually exist in the rejections table
        engine_tmp = get_engine(config)
        with engine_tmp.connect() as _conn:
            _tbl_cols = {r[0] for r in _conn.execute(text("DESCRIBE rejections")).fetchall()}
        defect_cols_dynamic  = [c for c in dynamic_cfg if c in _tbl_cols]
        defect_labels_dynamic = {c: dynamic_cfg[c] for c in defect_cols_dynamic}

        # Always prepend Total Rejection so it appears as the first column
        _total_col = "rejection_quantity"
        if _total_col in _tbl_cols and _total_col not in defect_cols_dynamic:
            active_defect_cols   = [_total_col] + defect_cols_dynamic
            active_defect_labels = {_total_col: "Total Sand Rejection Quantity", **defect_labels_dynamic}
        else:
            active_defect_cols   = defect_cols_dynamic
            active_defect_labels = defect_labels_dynamic

        logger.info(
            "fetch_rejection_data: using %d dynamic defect cols: %s",
            len(active_defect_cols), active_defect_cols,
        )
    else:
        active_defect_cols   = _REJ_DEFECT_COLS
        active_defect_labels = _REJ_DEFECT_LABELS
    # ──────────────────────────────────────────────────────────────────────────

    defect_select = ", ".join(f"`{c}`" for c in active_defect_cols)
    sql = f"""
        SELECT DATE(`date`) AS date, component_id, nett_casting_wt,
               total_quantity_produced, {defect_select}
        FROM   rejections
        WHERE  foundry_line_id = :fl_id
          AND  deleted = 0
          AND  {date_frag}
        ORDER  BY date, component_id
    """
    engine = get_engine(config)
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params=params)

    if df.empty:
        logger.info("fetch_rejection_data: no rejection data found")
        return pd.DataFrame()

    df["date"]         = pd.to_datetime(df["date"]).dt.date
    df["component_id"] = df["component_id"].astype(str).str.strip()
    nett_wt   = pd.to_numeric(df["nett_casting_wt"],        errors="coerce").fillna(0)
    total_qty = pd.to_numeric(df["total_quantity_produced"], errors="coerce").fillna(0)

    # Per-component production weight: qty × nett_casting_wt
    df["_comp_prod_wt"] = total_qty * nett_wt

    # Defect weight columns: rejected_qty × nett_casting_wt
    wt_cols = []
    for col in active_defect_cols:
        wt_col = f"_wt_{col}"
        df[wt_col] = pd.to_numeric(df[col], errors="coerce").fillna(0) * nett_wt
        wt_cols.append((col, wt_col))

    # -- Group key depends on aggregation mode ---------------------------------
    agg_cols     = ["_comp_prod_wt"] + [wt for _, wt in wt_cols]
    by_component = (group_by == "component")
    group_keys   = ["date", "component_id"] if by_component else ["date"]

    grouped = df.groupby(group_keys)[agg_cols].sum().reset_index()

    # ── WITH production flow: denominator = plant-level production from SQL ──────
    # Uses the exact query from fishbone_common.py:
    #   Pattern components: JOIN via pattern_component to expand siblings
    #   Non-pattern components: direct calculation
    #   Production = cc.qty × cavities × weight_of_component
    # This matches the chart production figures exactly.
    if production_flow:
        try:
            _prod_sql = text("""
                SELECT DATE(date) AS dte, SUM(prod_wt) AS day_prod_wt
                FROM (
                    SELECT c.date,
                        (cc.qty * comp2.cavities * comp2.weight_of_component) AS prod_wt
                    FROM consumption_component cc
                    JOIN consumption  c     ON c.pkey  = cc.consumption_pkey
                    JOIN components   comp  ON comp.pkey  = cc.component_pkey
                    JOIN pattern_component pc   ON comp.component_id = pc.hid
                    JOIN pattern_component pc2  ON pc.pattern_no = pc2.pattern_no
                    JOIN components   comp2 ON pc2.hid = comp2.component_id
                    WHERE c.date BETWEEN :s AND :e
                      AND comp.foundry_line_id = :fl

                    UNION ALL

                    SELECT c.date,
                        (cc.qty * comp.cavities * comp.weight_of_component) AS prod_wt
                    FROM consumption_component cc
                    JOIN consumption c    ON c.pkey  = cc.consumption_pkey
                    JOIN components  comp ON comp.pkey = cc.component_pkey
                    WHERE c.date BETWEEN :s AND :e
                      AND comp.foundry_line_id = :fl
                      AND comp.component_id NOT IN (SELECT hid FROM pattern_component)
                ) AS _sub
                GROUP BY DATE(date)
            """)
            _s = start_date.isoformat() + " 00:00:00" if start_date else "2000-01-01 00:00:00"
            _e = end_date.isoformat()   + " 23:59:59" if end_date   else "2099-12-31 23:59:59"
            with get_engine(config).connect() as _conn:
                _prod_rows = _conn.execute(_prod_sql, {"fl": fl_id, "s": _s, "e": _e}).fetchall()
            import pandas as _pd2
            _prod_df = _pd2.DataFrame(_prod_rows, columns=["date", "day_prod_wt"])
            _prod_df["date"] = _pd2.to_datetime(_prod_df["date"]).dt.date
            _prod_map = _prod_df.set_index("date")["day_prod_wt"].to_dict()
            grouped["_denominator"] = grouped["date"].map(_prod_map).fillna(0)
            logger.info(
                "fetch_rejection_data: production_flow=True (SQL pattern expansion) — "
                "%d date denominator(s) loaded", len(_prod_map)
            )
        except Exception as _pf_exc:
            logger.warning(
                "fetch_rejection_data: production_flow SQL failed (%s) — "
                "falling back to per-component denominator", _pf_exc
            )
            day_totals = grouped.groupby("date")["_comp_prod_wt"].transform("sum")
            grouped["_denominator"] = day_totals
    else:
        # WITHOUT production flow: denominator = this component/row's own production
        grouped["_denominator"] = grouped["_comp_prod_wt"]

    out_rows = []
    for _, row in grouped.iterrows():
        denom   = row["_denominator"]
        prod_wt = row["_comp_prod_wt"]

        out: dict = {"Date": row["date"]}
        if by_component:
            out["Component ID"] = row["component_id"]
        # When production_flow=True, show plant-level production (from SQL) not per-component
        _display_prod = round(row.get("_denominator", prod_wt), 1) if production_flow else round(prod_wt, 1)
        out["Total Produced (wt kg)"] = _display_prod

        for col, wt_col in wt_cols:
            label  = active_defect_labels.get(col, col)
            wt_val = round(row[wt_col], 1)
            # % uses the selected denominator
            pct    = round(wt_val / denom * 100, 3) if denom > 0 else None
            out[f"{label} (wt kg)"] = wt_val
            out[f"{label} (%)"]     = pct
        out_rows.append(out)

    df_out = pd.DataFrame(out_rows)

    # Drop columns that are all-zero
    fixed = {"Date", "Component ID", "Total Produced (wt kg)"}
    zero_cols = [c for c in df_out.columns if c not in fixed
                 and df_out[c].fillna(0).sum() == 0]
    df_out.drop(columns=zero_cols, inplace=True, errors="ignore")

    mode_tag = "with_flow" if production_flow else "without_flow"
    logger.info(
        "fetch_rejection_data: %d rows  group_by=%s  mode=%s  defect_cols=%d",
        len(df_out), group_by, mode_tag,
        sum(1 for c in df_out.columns if c.endswith(" (%)") and c not in fixed),
    )
    return df_out


def fetch_component_names(config: dict) -> dict:
    """Return {component_id: component_name} from the components table."""
    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT `component_id`, `component_name` FROM `components` WHERE `deleted` = 0"
            )).fetchall()
        return {str(r[0]).strip(): str(r[1]).strip() for r in rows if r[0] and r[1]}
    except Exception as exc:
        logger.warning("fetch_component_names failed: %s", exc)
        return {}
