"""
pipeline/aggregator.py
-----------------------
Merges the four raw DataFrames into one unified dataset and
aggregates it according to the configured mode:

  mode = 'day'       -> one row per production date
  mode = 'shift'     -> one row per (date, shift)
  mode = 'window'    -> same as 'shift', but engines use a rolling window
  mode = 'component' -> one row per (date, shift, component_id)
                        additive/consumption grouped by component;
                        prepared_sand uses shift average, falling back
                        to day average when shift data is unavailable.

The final DataFrame always contains:
  - 'period_key'  : str   -- "YYYY-MM-DD" or "YYYY-MM-DD_S1" or "YYYY-MM-DD_S1_C{id}"
  - 'date'        : date
  - 'shift'       : str   (NaN for day-mode rows)
  - 'component_id': str   (component mode only)
  - all parameter columns (prefixed with source table abbreviation to avoid clashes)

Column prefixes
---------------
  ps_   -> preparedsand
  con_  -> consumption
  add_  -> additive  (batch-level -> aggregated)
  pse_  -> prepared_sand_extra (batch-level -> aggregated)
"""

import logging
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# --- Public entry point -------------------------------------------------------

def build_dataset(
    raw: dict[str, pd.DataFrame],
    config: dict,
) -> pd.DataFrame:
    """
    Parameters
    ----------
    raw    : dict returned by data_fetcher.fetch_all()
    config : loaded watchdog_config

    Returns
    -------
    Sorted, merged DataFrame ready for analytics engines.
    """
    mode     = config["aggregation"]["mode"]
    agg_func = config["aggregation"].get("agg_func", "mean")
    sum_cols = set(config["aggregation"].get("sum_columns", []))

    ps_cols  = config["parameters"]["prepared_sand"]
    con_cols = config["parameters"]["consumption"]
    add_cols = config["parameters"]["additive"]
    pse_cols = config["parameters"]["prepared_sand_extra"]

    # -- Component mode has its own build path ---------------------------------
    if mode == "component":
        return _build_component_dataset(raw, config, ps_cols, con_cols, add_cols, pse_cols)

    # -- Window mode: last N non-null raw rows, no aggregation -----------------
    if mode == "window":
        return _build_window_dataset(raw, config, ps_cols, con_cols, add_cols, pse_cols)

    # -- Step 1: aggregate each table to period level --------------------------
    ps  = _aggregate_table(raw["prepared_sand"],       ps_cols,  mode, agg_func, sum_cols)
    con = _aggregate_table(raw["consumption"],          con_cols, mode, agg_func, sum_cols)
    add = _aggregate_table(raw["additive"],             add_cols, mode, agg_func, sum_cols)
    pse = _aggregate_table(raw["prepared_sand_extra"],  pse_cols, mode, agg_func, sum_cols)

    # -- Step 2: add table prefix to parameter columns -------------------------
    ps  = _prefix_cols(ps,  "ps_",  ps_cols)
    con = _prefix_cols(con, "con_", con_cols)
    add = _prefix_cols(add, "add_", add_cols)
    pse = _prefix_cols(pse, "pse_", pse_cols)

    # -- Step 3: determine merge key -------------------------------------------
    if mode == "day":
        merge_keys = ["date"]
    else:
        merge_keys = ["date", "shift"]

    # -- Step 4: outer-join all four tables on period key ---------------------
    df = ps
    for other in [con, add, pse]:
        if other.empty:
            continue
        df = pd.merge(df, other, on=merge_keys, how="outer")

    if df.empty:
        logger.warning("build_dataset: result DataFrame is empty")
        return df

    df = df.sort_values(["date"] if mode == "day" else ["date", "shift"])
    df = df.reset_index(drop=True)
    # Sieve is NOT merged here -- scores are computed in run_watchdog.py
    # using only actual measurement rows (last N available, no NaN).

    if mode in ("day", "shift"):
        for col in [c for c in df.columns if c.startswith("ps_")]:
            valid = df[col].dropna()
            if valid.empty:
                continue
            n_missing = int(df[col].isna().sum())
            if n_missing:
                df[col] = df[col].fillna(valid.iloc[-1])
                logger.info("Imputed %d missing '%s' with last available: %.4g",
                            n_missing, col, valid.iloc[-1])

    df.insert(0, "period_key", _make_period_key(df, mode))

    logger.info("build_dataset: %d periods, %d columns (mode=%s)",
                len(df), len(df.columns), mode)
    return df


# --- Component run dataset (N last shift-occurrences of one component) -------

def build_component_run_dataset(
    raw:          dict,
    config:       dict,
    ps_cols:      list,
    con_cols:     list,
    add_cols:     list,
    pse_cols:     list,
    component_id: str,
    n_runs:       int  = 10,
    df_rejection: pd.DataFrame = None,
) -> tuple:
    """
    Build a time-series dataset for ONE component consisting of its last N
    shift-occurrences, with production-window-accurate prepared sand data.

    Each row = one shift where *component_id* was produced:
      (date, shift, component_id)

    Aggregation per row:
      additive          -> mean of all batches for this component in this shift
      preparedsand      -> mean of PS readings within the component's production
                          window (min->max additive timestamp ±30 min);
                          falls back to shift average, then day average
      consumption       -> shift average (no component_id in this table)
      prepared_sand_extra -> same window logic as preparedsand

    Baseline (good periods):
      From ALL historical occurrences of this component, find the median
      total rejection per occurrence.  Occurrences with rejection ≤ median
      are "good periods" whose mean per property = the optimal target used
      by the drift engine.

    Parameters
    ----------
    raw          : dict from data_fetcher.fetch_all()
    config       : watchdog config
    ps_cols …    : parameter column lists (same as build_dataset callers pass)
    component_id : the component to analyse
    n_runs       : how many most-recent shift-occurrences to include
    df_rejection : optional DataFrame with columns (date, shift, component_id,
                   total_rejection) — used to identify good periods

    Returns
    -------
    (df_runs, baseline_df)
      df_runs      : DataFrame with ≤ n_runs rows, oldest first
      baseline_df  : good-period subset of ALL historical occurrences
                     (may have more than n_runs rows — used for baseline stats)
    """
    agg_func = config["aggregation"].get("agg_func", "mean")
    sum_cols = set(config["aggregation"].get("sum_columns", []))

    add_raw      = raw.get("additive",             pd.DataFrame())
    ps_raw       = raw.get("prepared_sand",        pd.DataFrame())
    con_raw      = raw.get("consumption",          pd.DataFrame())
    pse_raw      = raw.get("prepared_sand_extra",  pd.DataFrame())
    booking_raw  = raw.get("consumption_booking",  pd.DataFrame())

    if add_raw.empty:
        return pd.DataFrame(), pd.DataFrame()

    # ── Step 1: aggregate additive by (date, shift, component_id) ─────────────
    all_add = _aggregate_table_component(add_raw, add_cols, agg_func, sum_cols)
    if all_add.empty:
        return pd.DataFrame(), pd.DataFrame()
    all_add = _prefix_cols(all_add, "add_", add_cols)

    # Keep only rows for this component
    if "component_id" not in all_add.columns:
        return pd.DataFrame(), pd.DataFrame()
    all_add = all_add[all_add["component_id"].astype(str).str.strip() == str(component_id).strip()].copy()
    if all_add.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Sort oldest -> newest
    sort_keys = [k for k in ["date", "shift"] if k in all_add.columns]
    all_add = all_add.sort_values(sort_keys).reset_index(drop=True)

    # ── Step 2: prepared sand — booking window -> component_id match -> shift -> day
    ps_shift = _aggregate_table(ps_raw, ps_cols, "shift", agg_func, sum_cols)
    ps_day   = _aggregate_table(ps_raw, ps_cols, "day",   agg_func, sum_cols)
    ps_shift = _prefix_cols(ps_shift, "ps_", ps_cols)
    ps_day   = _prefix_cols(ps_day,   "ps_", ps_cols)
    ps_window = _ps_by_component_window(ps_raw, booking_raw, ps_cols)

    # ── Step 3: consumption and PSE (shift-level) ──────────────────────────────
    con = _aggregate_table(con_raw, con_cols, "shift", agg_func, sum_cols)
    con = _prefix_cols(con, "con_", con_cols)

    pse_shift = _aggregate_table(pse_raw, pse_cols, "shift", agg_func, sum_cols)
    pse_day   = _aggregate_table(pse_raw, pse_cols, "day",   agg_func, sum_cols)
    pse_shift = _prefix_cols(pse_shift, "pse_", pse_cols)
    pse_day   = _prefix_cols(pse_day,   "pse_", pse_cols)

    # ── Step 4: merge all sources into one row per (date, shift, component_id) ─
    df = all_add.copy()

    if not con.empty:
        df = pd.merge(df, con, on=["date", "shift"], how="left")

    # PS: production-window first, then shift fallback, then day fallback
    if not ps_window.empty:
        ps_comp = ps_window[ps_window.get("component_id", pd.Series(dtype=str)).astype(str).str.strip() == str(component_id).strip()] \
                  if "component_id" in ps_window.columns else ps_window
        if not ps_comp.empty:
            df = pd.merge(df, ps_comp, on=["date", "shift", "component_id"], how="left")
            _fill_missing_from_shift(df, ps_shift, prefix="ps_")
        else:
            if not ps_shift.empty:
                df = pd.merge(df, ps_shift, on=["date", "shift"], how="left")
    elif not ps_shift.empty:
        df = pd.merge(df, ps_shift, on=["date", "shift"], how="left")

    if not pse_shift.empty:
        df = pd.merge(df, pse_shift, on=["date", "shift"], how="left")

    _fill_from_day_level(df, ps_day,  prefix="ps_",  date_col="date")
    _fill_from_day_level(df, pse_day, prefix="pse_", date_col="date")

    # Within-component batch variance
    bvar_df = _compute_batch_variance_component(add_raw, add_cols)
    if not bvar_df.empty:
        bvar_comp = bvar_df[bvar_df["component_id"].astype(str).str.strip() == str(component_id).strip()]
        if not bvar_comp.empty:
            bv_keys = [k for k in ["date", "shift", "component_id"] if k in bvar_comp.columns]
            df = pd.merge(df, bvar_comp, on=bv_keys, how="left")

    # ── Step 5: compute good-period baseline from ALL historical occurrences ───
    baseline_df = _good_period_baseline(df, df_rejection, component_id)

    # ── Step 6: limit to last N occurrences for the SI time series ─────────────
    df_runs = df.tail(n_runs).reset_index(drop=True)

    # Add period key: Run-01 | 2026-06-01 | Shift 2 | CompA
    def _pk(row):
        d = row.get("date", "?")
        s = row.get("shift", "?")
        c = row.get("component_id", "?")
        return f"{d} | Shift {s} | {c}"
    df_runs.insert(0, "period_key", [_pk(df_runs.iloc[i]) for i in range(len(df_runs))])

    logger.info(
        "build_component_run_dataset: component=%s  total_occurrences=%d  "
        "last_n=%d  good_period_rows=%d",
        component_id, len(df), len(df_runs), len(baseline_df),
    )
    return df_runs, baseline_df


def _good_period_baseline(
    df_all:       pd.DataFrame,
    df_rejection: pd.DataFrame,
    component_id: str,
) -> pd.DataFrame:
    """
    From all historical occurrences of *component_id*, identify good periods:
    occurrences where total_rejection ≤ median total_rejection for this component.

    If df_rejection is None or empty, falls back to returning all rows
    (no rejection data available to filter).

    Returns the good-period subset of df_all.
    """
    if df_rejection is None or df_rejection.empty:
        return df_all.copy()

    # Normalise rejection df
    rej = df_rejection.copy()
    if "component_id" in rej.columns:
        rej = rej[rej["component_id"].astype(str).str.strip() == str(component_id).strip()]

    if rej.empty or "total_rejection" not in rej.columns:
        return df_all.copy()

    median_rej = float(rej["total_rejection"].median())

    # Require both date AND shift so we don't cross-contaminate shifts when
    # rejection varies intra-day.  A date-only join would tag a good shift as
    # bad whenever any other shift on the same date had high rejection.
    join_keys = [k for k in ["date", "shift"] if k in rej.columns and k in df_all.columns]
    if "date" not in join_keys:
        return df_all.copy()
    if "shift" not in join_keys:
        logger.debug(
            "_good_period_baseline: 'shift' absent from rejection data — "
            "falling back to all rows (baseline not filtered)"
        )
        return df_all.copy()

    merged = pd.merge(df_all, rej[join_keys + ["total_rejection"]], on=join_keys, how="left")
    # Rows with no rejection data are treated as good (rejection = 0)
    merged["total_rejection"] = merged["total_rejection"].fillna(0)
    good = merged[merged["total_rejection"] <= median_rej].drop(columns=["total_rejection"])

    return good.reset_index(drop=True) if not good.empty else df_all.copy()


# --- Baseline subset ----------------------------------------------------------

def get_baseline(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """
    Return only the rows that fall within the configured baseline period.
    Used by variance_engine to compute reference thresholds.
    """
    b = config["baseline"]
    start = pd.to_datetime(b["start_date"]).date()
    end   = pd.to_datetime(b["end_date"]).date()
    mask  = (df["date"] >= start) & (df["date"] <= end)
    base  = df[mask].copy()
    logger.info("Baseline subset: %d rows (%s - %s)", len(base), start, end)
    return base


# --- Window slicing (for window mode) ----------------------------------------

def get_window(df: pd.DataFrame, config: dict, as_of_idx: int) -> pd.DataFrame:
    """
    Return the last window_size rows up to and including as_of_idx.
    Used when mode='window' to feed only the current window to engines.
    """
    w     = config["engines"]["window"]
    start = max(0, as_of_idx - w + 1)
    return df.iloc[start : as_of_idx + 1].copy()


# --- Internal aggregation logic -----------------------------------------------

def _aggregate_table(
    df: pd.DataFrame,
    param_cols: list[str],
    mode: str,
    agg_func: str,
    sum_cols: set,
) -> pd.DataFrame:
    """
    Aggregate one source DataFrame to period level.

    - For day mode: group by date, apply agg_func (mean/median) or sum
    - For shift / window mode: group by (date, shift)
    """
    if df.empty:
        logger.debug("_aggregate_table: empty input -- returning empty frame")
        return pd.DataFrame()

    # Keep only existing columns
    avail  = [c for c in param_cols if c in df.columns]
    if not avail:
        logger.debug("_aggregate_table: no matching columns in DataFrame")
        return pd.DataFrame()

    group_keys = ["date"] if mode == "day" else ["date", "shift"]
    # Ensure group keys exist
    group_keys = [k for k in group_keys if k in df.columns]

    work = df[group_keys + avail].copy()

    # Convert param cols to numeric; non-numeric become NaN
    for c in avail:
        work[c] = pd.to_numeric(work[c], errors="coerce")

    # Build per-column aggregation dict
    agg_dict = {}
    for c in avail:
        if c in sum_cols:
            agg_dict[c] = "sum"
        elif agg_func == "median":
            agg_dict[c] = "median"
        else:
            agg_dict[c] = "mean"

    result = work.groupby(group_keys, as_index=False).agg(agg_dict)
    result["date"] = pd.to_datetime(result["date"]).dt.date

    return result


def _prefix_cols(
    df: pd.DataFrame,
    prefix: str,
    param_cols: list[str],
) -> pd.DataFrame:
    """Rename parameter columns to prefix+name. Leave key columns (date, shift) unchanged."""
    rename = {c: f"{prefix}{c}" for c in param_cols if c in df.columns}
    return df.rename(columns=rename)


def _make_period_key(df: pd.DataFrame, mode: str) -> pd.Series:
    """Build a readable period identifier.

    Day   : 2026-05-31
    Shift : 2026-05-31 | Shift 2
    """
    if mode == "day":
        return df["date"].astype(str)
    else:
        shift_str = df["shift"].fillna("?").astype(str)
        return df["date"].astype(str) + " | Shift " + shift_str


# --- Utility: list all parameter column names in merged df -------------------

def get_param_columns(df: pd.DataFrame) -> list[str]:
    """Return all non-key columns from the merged dataset."""
    key_cols = {"period_key", "date", "shift", "component_id"}
    return [c for c in df.columns if c not in key_cols]


def get_param_columns_by_source(config: dict) -> dict[str, list[str]]:
    """
    Return a dict mapping source -> list of prefixed column names.

    e.g. {"prepared_sand": ["ps_active_clay", "ps_moisture", ...], ...}
    """
    return {
        "prepared_sand"      : [f"ps_{c}"  for c in config["parameters"]["prepared_sand"]],
        "consumption"        : [f"con_{c}" for c in config["parameters"]["consumption"]],
        "additive"           : [f"add_{c}" for c in config["parameters"]["additive"]],
        "prepared_sand_extra": [f"pse_{c}" for c in config["parameters"]["prepared_sand_extra"]],
    }


# --- Window mode --------------------------------------------------------------

def _build_window_dataset(
    raw:      dict,
    config:   dict,
    ps_cols:  list,
    con_cols: list,
    add_cols: list,
    pse_cols: list,
) -> pd.DataFrame:
    """
    Window mode: last N non-null values per parameter, collapsed to ONE row.

    Collects the last N non-null readings for every parameter independently,
    then mean-aggregates them into a single row representing the current
    process state over that window.

      - date / shift are taken from the most recent preparedsand spine entry
      - period_key = "Win-{N} | {date} | Shift {shift}"
      - parameters with no readings in the window are NaN

    The single row is compared against the historical baseline by the SI
    engines (deviation, drift), giving a clean one-point-in-time assessment
    rather than a multi-slot time series.
    """
    # window_mode_n (default 1): number of individual PS rows for window mode.
    # report_window (default 5): used by shift/component modes (stored separately).
    # Falls back to aggregation.window_size -> engines.window -> 10.
    n = int(
        config.get("window_mode_n")
        or config.get("report_window")
        or config["aggregation"].get("window_size")
        or config.get("engines", {}).get("window")
        or 10
    )

    agg_func = config["aggregation"].get("agg_func", "mean")
    sum_cols = set(config["aggregation"].get("sum_columns", []))

    # ── helpers ────────────────────────────────────────────────────────────────

    def _last_n_nonnull(df: pd.DataFrame, col: str, sort_keys: list, n: int) -> pd.Series:
        """Return the last N non-null values of *col* from *df*, oldest first."""
        if col not in df.columns or df.empty:
            return pd.Series([np.nan] * n, name=col)
        work = df[sort_keys + [col]].copy()
        work[col] = pd.to_numeric(work[col], errors="coerce")
        valid = work[work[col].notna()].sort_values(sort_keys)
        vals = valid[col].values
        if len(vals) >= n:
            return pd.Series(vals[-n:], name=col)
        # Pad left with NaN if fewer than N readings exist
        padded = np.full(n, np.nan)
        padded[n - len(vals):] = vals
        return pd.Series(padded, name=col)

    def _date_shift_for_col(df: pd.DataFrame, col: str, sort_keys: list, n: int):
        """Return (dates, shifts) arrays aligned to the last-N window of *col*."""
        if col not in df.columns or df.empty:
            return [None] * n, [None] * n
        work = df[sort_keys + [col]].copy()
        work[col] = pd.to_numeric(work[col], errors="coerce")
        valid = work[work[col].notna()].sort_values(sort_keys)
        dates  = list(valid["date"].values)  if "date"  in valid.columns else [None] * len(valid)
        shifts = list(valid["shift"].values) if "shift" in valid.columns else [None] * len(valid)
        # Pad left
        if len(dates) < n:
            pad = n - len(dates)
            dates  = [None] * pad + dates
            shifts = [None] * pad + shifts
        return dates[-n:], shifts[-n:]

    # ── Preparedsand: spine for date/shift and PS parameters ───────────────────
    ps_raw = raw.get("prepared_sand", pd.DataFrame())
    sort_k_ps = ["date", "shift"] if "shift" in (ps_raw.columns if not ps_raw.empty else []) else ["date"]
    if not ps_raw.empty:
        ps_raw = ps_raw.copy()
        ps_raw["date"] = pd.to_datetime(ps_raw["date"]).dt.date

    avail_ps = [c for c in ps_cols if not ps_raw.empty and c in ps_raw.columns]

    # Use first available PS col to determine the spine dates/shifts
    spine_dates, spine_shifts = ([None] * n, [None] * n)
    for _spine_col in avail_ps:
        _d, _s = _date_shift_for_col(ps_raw, _spine_col, sort_k_ps, n)
        if any(x is not None for x in _d):
            spine_dates, spine_shifts = _d, _s
            break

    # Build per-parameter series for PS columns.
    # Window mode: only include a parameter if the MOST RECENT preparedsand row
    # has a non-null value for it. This prevents stale historical readings from
    # triggering alerts when a user only enters partial data in the new row.
    ps_series = {}
    _latest_ps_row = ps_raw.sort_values(sort_k_ps).iloc[-1] if not ps_raw.empty else None
    for col in avail_ps:
        if _latest_ps_row is not None and col in _latest_ps_row.index:
            if pd.isna(_latest_ps_row[col]):
                continue  # skip — latest row has no value for this param
        ps_series[f"ps_{col}"] = _last_n_nonnull(ps_raw, col, sort_k_ps, n).values

    # ── Additive (batch-level) ─────────────────────────────────────────────────
    add_raw = raw.get("additive", pd.DataFrame())
    sort_k_add = ["date", "shift", "batch_counter"] if "batch_counter" in (
        add_raw.columns if not add_raw.empty else []) else (
        ["date", "shift"] if "shift" in (add_raw.columns if not add_raw.empty else []) else ["date"])
    if not add_raw.empty:
        add_raw = add_raw.copy()
        add_raw["date"] = pd.to_datetime(add_raw["date"]).dt.date

    avail_add = [c for c in add_cols if not add_raw.empty and c in add_raw.columns]
    add_series = {}
    for col in avail_add:
        add_series[f"add_{col}"] = _last_n_nonnull(add_raw, col, sort_k_add, n).values

    # ── Consumption ───────────────────────────────────────────────────────────
    con_raw = raw.get("consumption", pd.DataFrame())
    sort_k_con = ["date", "shift"] if "shift" in (con_raw.columns if not con_raw.empty else []) else ["date"]
    if not con_raw.empty:
        con_raw = con_raw.copy()
        con_raw["date"] = pd.to_datetime(con_raw["date"]).dt.date

    avail_con = [c for c in con_cols if not con_raw.empty and c in con_raw.columns]
    con_series = {}
    for col in avail_con:
        con_series[f"con_{col}"] = _last_n_nonnull(con_raw, col, sort_k_con, n).values

    # ── Prepared-sand extra ───────────────────────────────────────────────────
    pse_raw = raw.get("prepared_sand_extra", pd.DataFrame())
    sort_k_pse = ["date", "shift"] if "shift" in (pse_raw.columns if not pse_raw.empty else []) else ["date"]
    if not pse_raw.empty:
        pse_raw = pse_raw.copy()
        pse_raw["date"] = pd.to_datetime(pse_raw["date"]).dt.date

    avail_pse = [c for c in pse_cols if not pse_raw.empty and c in pse_raw.columns]
    pse_series = {}
    for col in avail_pse:
        pse_series[f"pse_{col}"] = _last_n_nonnull(pse_raw, col, sort_k_pse, n).values

    # ── Build N individual rows (one per reading slot) ────────────────────────
    # Keep each of the last-N readings as a separate row so the drift/variance/
    # oscillation engines can detect trends across them.
    # Date and shift are taken from the spine (preparedsand) timeline.
    all_series = {**ps_series, **con_series, **add_series, **pse_series}

    rows = []
    for i in range(n):
        d = spine_dates[i]  if i < len(spine_dates)  else None
        s = spine_shifts[i] if i < len(spine_shifts) else None
        row: dict = {
            "date" : d,
            "shift": str(s) if s is not None and str(s) not in ("nan", "None", "") else None,
        }
        for col, vals in all_series.items():
            row[col] = float(vals[i]) if i < len(vals) and not np.isnan(float(vals[i])) else np.nan
        rows.append(row)

    df = pd.DataFrame(rows)

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date

    df = df.reset_index(drop=True)

    # period_key: Win-N | latest_date | Shift latest_shift  (same as before for the last row)
    latest_date  = next((d for d in reversed(spine_dates)  if d is not None), None)
    latest_shift = next((s for s in reversed(spine_shifts) if s is not None and
                         str(s) not in ("nan", "None", "")), None)
    pk = f"Win-{n} | {latest_date if latest_date is not None else '?'} | Shift {latest_shift if latest_shift is not None else '?'}"
    df.insert(0, "period_key", [pk] * len(df))

    logger.info(
        "_build_window_dataset: %d rows (N=%d) | ps=%d  add=%d  con=%d  pse=%d cols",
        len(df), n, len(ps_series), len(add_series), len(con_series), len(pse_series),
    )
    return df


# --- Component mode -----------------------------------------------------------

def _build_component_dataset(
    raw:      dict[str, pd.DataFrame],
    config:   dict,
    ps_cols:  list[str],
    con_cols: list[str],
    add_cols: list[str],
    pse_cols: list[str],
) -> pd.DataFrame:
    """
    Build a dataset with one row per (date, shift, component_id).

    - additive   : grouped by (date, shift, component_id) -- component-specific
    - consumption: grouped by (date, shift) then broadcast to every component row
    - prepared_sand: grouped by (date, shift); rows with no shift data fall back
                     to the (date) day-level average
    - prepared_sand_extra: same fallback logic as prepared_sand
    """
    agg_func = config["aggregation"].get("agg_func", "mean")
    sum_cols = set(config["aggregation"].get("sum_columns", []))

    # -- Additive: aggregate by (date, shift, component_id) --------------------
    add_raw  = raw.get("additive", pd.DataFrame())
    bvar_df  = pd.DataFrame()   # within-component batch variance (add_bvar_* cols)

    if not add_raw.empty and "component_id" in add_raw.columns:
        add     = _aggregate_table_component(add_raw, add_cols, agg_func, sum_cols)
        bvar_df = _compute_batch_variance_component(add_raw, add_cols)
    else:
        logger.warning("_build_component_dataset: additive has no component_id -- falling back to shift mode")
        add = _aggregate_table(add_raw, add_cols, "shift", agg_func, sum_cols)
        if not add.empty:
            add["component_id"] = "unknown"
    if add.empty:
        logger.warning("_build_component_dataset: no additive data -- result will be empty")
        return pd.DataFrame()
    add = _prefix_cols(add, "add_", add_cols)

    # -- Consumption: aggregate by (date, shift) then broadcast ----------------
    con_raw = raw.get("consumption", pd.DataFrame())
    if not con_raw.empty:
        con = _aggregate_table(con_raw, con_cols, "shift", agg_func, sum_cols)
        con = _prefix_cols(con, "con_", con_cols)
    else:
        con = pd.DataFrame()

    # -- Prepared sand: booking window -> component_id -> shift -> day ──────────
    ps_raw      = raw.get("prepared_sand",       pd.DataFrame())
    booking_raw = raw.get("consumption_booking", pd.DataFrame())
    ps_shift = _aggregate_table(ps_raw, ps_cols, "shift", agg_func, sum_cols)
    ps_day   = _aggregate_table(ps_raw, ps_cols, "day",   agg_func, sum_cols)
    ps_shift = _prefix_cols(ps_shift, "ps_", ps_cols)
    ps_day   = _prefix_cols(ps_day,   "ps_", ps_cols)

    # Use consumption_booking start/end times to find PS readings within each
    # component's production window. Falls back to shift average, then day.
    ps_window = _ps_by_component_window(ps_raw, booking_raw, ps_cols)

    # -- Prepared sand extra: same fallback approach ----------------------------
    pse_raw   = raw.get("prepared_sand_extra", pd.DataFrame())
    pse_shift = _aggregate_table(pse_raw, pse_cols, "shift", agg_func, sum_cols)
    pse_day   = _aggregate_table(pse_raw, pse_cols, "day",   agg_func, sum_cols)
    pse_shift = _prefix_cols(pse_shift, "pse_", pse_cols)
    pse_day   = _prefix_cols(pse_day,   "pse_", pse_cols)

    # -- Merge: start from component-level additive rows -----------------------
    df = add.copy()

    if not con.empty:
        df = pd.merge(df, con, on=["date", "shift"], how="left")

    # PS priority: production-window average -> shift average -> day average
    if not ps_window.empty:
        df = pd.merge(df, ps_window, on=["date", "shift", "component_id"], how="left")
        _fill_missing_from_shift(df, ps_shift, prefix="ps_")
    elif not ps_shift.empty:
        df = pd.merge(df, ps_shift, on=["date", "shift"], how="left")

    if not pse_shift.empty:
        df = pd.merge(df, pse_shift, on=["date", "shift"], how="left")

    # Day-level fallback for any still-missing PS / PSE values
    _fill_from_day_level(df, ps_day,  prefix="ps_",  date_col="date")
    _fill_from_day_level(df, pse_day, prefix="pse_", date_col="date")

    # -- Within-component batch variance (add_bvar_* columns) -----------------
    if not bvar_df.empty:
        bv_keys = [k for k in ["date", "shift", "component_id"] if k in bvar_df.columns]
        df = pd.merge(df, bvar_df, on=bv_keys, how="left")

    df = df.sort_values(["date", "shift", "component_id"]).reset_index(drop=True)
    # Sieve is NOT merged here -- scores are computed separately in run_watchdog.py
    # using only actual measurement rows (last N available, no NaN).
    df.insert(0, "period_key", _make_period_key_component(df))

    logger.info(
        "_build_component_dataset: %d rows, %d columns (component mode)",
        len(df), len(df.columns),
    )
    return df


def _ps_by_component_window(
    ps_raw:      pd.DataFrame,
    booking_raw: pd.DataFrame,
    ps_cols:     list,
) -> pd.DataFrame:
    """
    Match prepared sand readings to each component using consumption_booking
    production windows (start_time -> end_time) with a 4-level priority:

      1. PS readings whose `time` falls within booking start_time -> end_time
         (most accurate — PS taken during that component's actual production)
      2. PS readings where ps.component_id = booking.component_id on same date
         (direct component label on the PS row)
      3. Callers fall back to shift average
      4. Callers fall back to day average

    Parameters
    ----------
    ps_raw      : preparedsand DataFrame (must include `time` and optionally `component_id`)
    booking_raw : consumption_booking DataFrame (must include component_id,
                  date, shift, start_time, end_time)
    ps_cols     : list of parameter column names (without prefix)

    Returns
    -------
    DataFrame with columns: date, shift, component_id, ps_{col}...
    Empty DataFrame when booking or PS lacks the needed columns.
    """
    if ps_raw.empty or booking_raw.empty:
        return pd.DataFrame()

    avail_ps = [c for c in ps_cols if c in ps_raw.columns]
    if not avail_ps or "time" not in ps_raw.columns:
        return pd.DataFrame()

    required_booking = {"component_id", "date", "start_time", "end_time"}
    if not required_booking.issubset(booking_raw.columns):
        return pd.DataFrame()

    ps_work = ps_raw.copy()
    book    = booking_raw.copy()

    ps_work["date"] = pd.to_datetime(ps_work["date"], errors="coerce").dt.date
    book["date"]    = pd.to_datetime(book["date"],    errors="coerce").dt.date

    ps_work["_ps_time_td"] = pd.to_timedelta(ps_work["time"],       errors="coerce")
    book["_start_td"]      = pd.to_timedelta(book["start_time"],    errors="coerce")
    book["_end_td"]        = pd.to_timedelta(book["end_time"],      errors="coerce")
    book["component_id"]   = book["component_id"].astype(str).str.strip()
    if "shift" in book.columns:
        book["shift"] = book["shift"].astype(str).str.strip()

    # ── Vectorized approach: merge PS onto booking by date(+shift), then filter ─
    merge_keys = ["date"] + (["shift"] if "shift" in book.columns and "shift" in ps_work.columns else [])
    merged = pd.merge(
        book[merge_keys + ["component_id", "_start_td", "_end_td"]],
        ps_work[merge_keys + ["_ps_time_td"] + (["component_id"] if "component_id" in ps_work.columns else []) + avail_ps],
        on=merge_keys,
        how="inner",
        suffixes=("_book", "_ps"),
    )
    if merged.empty:
        return pd.DataFrame()

    # Priority 1: time-window match (handles normal + overnight windows)
    normal_mask    = (merged["_end_td"] >= merged["_start_td"]) & \
                     (merged["_ps_time_td"] >= merged["_start_td"]) & \
                     (merged["_ps_time_td"] <= merged["_end_td"])
    overnight_mask = (merged["_end_td"] < merged["_start_td"]) & \
                     ((merged["_ps_time_td"] >= merged["_start_td"]) |
                      (merged["_ps_time_td"] <= merged["_end_td"]))
    window_rows = merged[normal_mask | overnight_mask].copy()
    window_rows["_ps_source"] = "window"

    # Priority 2: component_id match (for rows not matched by window)
    if "component_id_ps" in merged.columns:
        comp_col = "component_id_ps"
    elif "component_id" in ps_work.columns:
        comp_col = "component_id"
    else:
        comp_col = None

    if comp_col and comp_col in merged.columns:
        already_matched = window_rows[merge_keys + ["component_id_book" if "component_id_book" in window_rows.columns else "component_id"]].drop_duplicates()
        unmatched = merged[~(normal_mask | overnight_mask)]
        comp_rows = unmatched[unmatched[comp_col].astype(str).str.strip() == unmatched["component_id_book" if "component_id_book" in unmatched.columns else "component_id"]].copy()
        comp_rows["_ps_source"] = "component_id"
        combined = pd.concat([window_rows, comp_rows], ignore_index=True)
    else:
        combined = window_rows

    if combined.empty:
        return pd.DataFrame()

    # Aggregate: mean of matched PS values per (date, shift, component_id)
    comp_key = "component_id_book" if "component_id_book" in combined.columns else "component_id"
    group_keys = merge_keys + [comp_key]
    agg_dict = {f"ps_{col}": (col, "mean") for col in avail_ps if col in combined.columns}
    if not agg_dict:
        return pd.DataFrame()

    out = combined.groupby(group_keys, as_index=False).agg(**agg_dict)
    out = out.rename(columns={comp_key: "component_id"})

    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date

    window_count = (combined["_ps_source"] == "window").sum()
    comp_count   = (combined["_ps_source"] == "component_id").sum()
    logger.info(
        "_ps_by_component_window: %d matches (window=%d, component_id=%d)",
        len(out), window_count, comp_count,
    )
    return out


def _fill_missing_from_shift(
    df:       pd.DataFrame,
    ps_shift: pd.DataFrame,
    prefix:   str = "ps_",
) -> None:
    """
    In-place: for rows where ALL columns starting with *prefix* are NaN
    (window lookup found nothing), fill them from the shift-level aggregate.
    """
    if ps_shift.empty:
        return
    ps_cols_in_df = [c for c in df.columns if c.startswith(prefix)]
    if not ps_cols_in_df:
        return

    all_null = df[ps_cols_in_df].isnull().all(axis=1)
    if not all_null.any():
        return

    shift_keys = [k for k in ["date", "shift"] if k in ps_shift.columns and k in df.columns]
    shift_ps_cols = [c for c in ps_shift.columns if c.startswith(prefix)]
    if not shift_keys or not shift_ps_cols:
        return

    # Build a quick lookup keyed by (date, shift)
    lookup: dict = {}
    for _, row in ps_shift.iterrows():
        key = tuple(row[k] for k in shift_keys)
        lookup[key] = {col: row[col] for col in shift_ps_cols}

    for idx in df.index[all_null]:
        key = tuple(df.at[idx, k] for k in shift_keys)
        if key in lookup:
            for col, val in lookup[key].items():
                if col in df.columns:
                    df.at[idx, col] = val


def _compute_batch_variance_component(
    df: pd.DataFrame,
    param_cols: list,
) -> pd.DataFrame:
    """
    Compute VAR.P of raw additive batch values per (date, shift, component_id).

    Groups with only 1 batch get variance = 0 (single measurement, no spread).
    Returns a DataFrame with columns:
        date, shift, component_id, add_bvar_{col} for each col in param_cols.

    This variance is consumed by within_component_variance_engine to score
    intra-component batch instability without discarding batch-level spread
    during the mean-aggregation step.
    """
    if df.empty:
        return pd.DataFrame()

    avail = [c for c in param_cols if c in df.columns]
    if not avail:
        return pd.DataFrame()

    group_keys = [k for k in ["date", "shift", "component_id"] if k in df.columns]
    if "component_id" not in group_keys:
        return pd.DataFrame()

    work = df[group_keys + avail].copy()
    for c in avail:
        work[c] = pd.to_numeric(work[c], errors="coerce")

    # VAR.P (population variance, ddof=0) -- matches WDV engine convention.
    # Groups of 1 produce NaN from pandas; fill those with 0 (no spread).
    var_result = work.groupby(group_keys, as_index=False)[avail].var(ddof=0)
    for c in avail:
        if c in var_result.columns:
            var_result[c] = var_result[c].fillna(0.0)

    var_result["date"] = pd.to_datetime(var_result["date"]).dt.date
    if "component_id" in var_result.columns:
        var_result["component_id"] = var_result["component_id"].astype(str).str.strip()

    # Rename to add_bvar_ prefix so they live alongside add_ columns in the merged df
    rename = {c: f"add_bvar_{c}" for c in avail}
    var_result = var_result.rename(columns=rename)

    logger.info(
        "_compute_batch_variance_component: %d rows, %d bvar columns",
        len(var_result), len(avail),
    )
    return var_result


def _aggregate_table_component(
    df:       pd.DataFrame,
    param_cols: list[str],
    agg_func: str,
    sum_cols: set,
) -> pd.DataFrame:
    """Aggregate one source table by (date, shift, component_id)."""
    if df.empty:
        return pd.DataFrame()

    avail = [c for c in param_cols if c in df.columns]
    if not avail:
        return pd.DataFrame()

    group_keys = [k for k in ["date", "shift", "component_id"] if k in df.columns]
    work = df[group_keys + avail].copy()

    for c in avail:
        work[c] = pd.to_numeric(work[c], errors="coerce")

    agg_dict = {}
    for c in avail:
        if c in sum_cols:
            agg_dict[c] = "sum"
        elif agg_func == "median":
            agg_dict[c] = "median"
        else:
            agg_dict[c] = "mean"

    result = work.groupby(group_keys, as_index=False).agg(agg_dict)
    result["date"] = pd.to_datetime(result["date"]).dt.date
    if "component_id" in result.columns:
        result["component_id"] = result["component_id"].astype(str).str.strip()
    return result


def _fill_from_day_level(
    df:         pd.DataFrame,
    day_df:     pd.DataFrame,
    prefix:     str,
    date_col:   str = "date",
) -> None:
    """
    In-place: for rows where ALL columns starting with *prefix* are NaN,
    fill them using the day-level aggregate (keyed on date only).

    Falls back to shift average first (already merged into df); this function
    is only called when that merge produced NaN (i.e. no shift data exists).
    """
    prefixed = [c for c in df.columns if c.startswith(prefix)]
    if not prefixed or day_df.empty:
        return

    all_null = df[prefixed].isnull().all(axis=1)
    if not all_null.any():
        return

    # Build a date -> row mapping from the day aggregate
    day_lookup: dict = {}
    day_ps_cols = [c for c in day_df.columns if c.startswith(prefix)]
    for _, row in day_df.iterrows():
        d = row[date_col]
        day_lookup[d] = {col: row[col] for col in day_ps_cols if col in row.index}

    n_filled = 0
    for idx in df.index[all_null]:
        d = df.at[idx, date_col]
        if d in day_lookup:
            for col, val in day_lookup[d].items():
                if col in df.columns:
                    df.at[idx, col] = val
            n_filled += 1

    if n_filled:
        logger.info(
            "_fill_from_day_level: filled %d rows with day-level %s* values",
            n_filled, prefix,
        )


def _make_period_key_component(df: pd.DataFrame) -> pd.Series:
    """Build period key for component mode.

    Format: 2026-05-31 | Shift 2 | Component 53015290010
    """
    shift_str = df["shift"].fillna("?").astype(str)
    comp_str  = df["component_id"].fillna("?").astype(str)
    return (
        df["date"].astype(str)
        + " | Shift " + shift_str
        + " | Component " + comp_str
    )
