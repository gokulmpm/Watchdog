"""
run_alert_monitor.py
---------------------
Multi-foundry entry point for the AI Watchdog.

Usage
-----
    python -m watchdog.run_alert_monitor                          # daemon
    python -m watchdog.run_alert_monitor --once                   # one-shot SI
    python -m watchdog.run_alert_monitor --comp-change            # component-change check
    python -m watchdog.run_alert_monitor --bad-batch              # bad-batch check
    python -m watchdog.run_alert_monitor --sieve-change           # sieve % change check
    python -m watchdog.run_alert_monitor --single                 # force single-foundry
    python -m watchdog.run_alert_monitor --config /path/to/cfg.json
"""

import argparse
import copy
import json
import logging
import logging.handlers
import sys
import threading
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

_DEFAULT_CONFIG = Path(__file__).parent / "config" / "watchdog_config.json"
_LOG_FILE       = "watchdog.log"
_LOG_MAX_BYTES  = 10 * 1024 * 1024
_LOG_BACKUPS    = 5

# Full-history DataFrame cache keyed by (foundry_line_id, db_name).
# Avoids re-fetching all DB history on every component trigger.
_HIST_CACHE:    dict = {}
_HIST_CACHE_TTL      = 300  # seconds

_ALERT_RANK = {a: i for i, a in enumerate(
    ["STABLE", "WATCH", "ELEVATED", "HIGH VAR", "ALERT", "CRITICAL", "!! WARNING"]
)}


def _worst_alert(alerts):
    return max(alerts, key=lambda a: _ALERT_RANK.get(str(a), -1), default="STABLE")


def _fill_with_shift_mean(df_merged: pd.DataFrame, analysis_cols: list) -> pd.DataFrame:
    df = df_merged.copy()
    group_keys = ["date", "shift"] if "shift" in df.columns else ["date"]
    for col in analysis_cols:
        if col not in df.columns:
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce")
        shift_mean = df.groupby(group_keys)[col].transform("mean")
        df[col] = df[col].fillna(shift_mean).fillna(df[col].mean())
    return df


def _compute_component_baselines(
    df_merged: pd.DataFrame,
    df_rejection: pd.DataFrame,
    analysis_cols: list,
) -> dict:
    """
    Build a good-period baseline per component using rejection data.
    Good periods = rows where total_rejection <= median rejection for that component.
    Returns {str(component_id): good_period_df}, falls back to all periods when
    rejection data is absent for a component.
    """
    logger = logging.getLogger(__name__)
    if df_rejection is None or df_rejection.empty or "component_id" not in df_merged.columns:
        return {}

    pct_cols     = [c for c in df_rejection.columns if c.endswith("(%)")]
    has_comp_col = "Component ID" in df_rejection.columns
    result: dict = {}

    for comp_id, df_comp in df_merged.groupby("component_id"):
        comp_id_str = str(comp_id)
        rej_c = (
            df_rejection[df_rejection["Component ID"].astype(str) == comp_id_str].copy()
            if has_comp_col else df_rejection.copy()
        )

        if rej_c.empty or not pct_cols:
            result[comp_id_str] = df_comp
            continue

        if "Total Rejection (%)" in rej_c.columns:
            rej_c["_total_rej"] = pd.to_numeric(
                rej_c["Total Rejection (%)"], errors="coerce"
            ).fillna(0)
        else:
            rej_c["_total_rej"] = (
                rej_c[pct_cols].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)
            )

        rej_c["_date"] = pd.to_datetime(rej_c["Date"]).dt.date
        rej_lookup = rej_c.set_index("_date")["_total_rej"]

        df_c = df_comp.copy()
        df_c["_date_key"]  = pd.to_datetime(df_c["date"]).dt.date
        df_c["_total_rej"] = df_c["_date_key"].map(rej_lookup).fillna(0)

        threshold = float(df_c["_total_rej"].median())
        good_df   = df_comp.loc[df_c.index[df_c["_total_rej"] <= threshold]]
        if good_df.empty:
            good_df = df_comp

        logger.info("Component %s: rej_threshold=%.3f%%, good=%d/%d",
                    comp_id_str, threshold, len(good_df), len(df_comp))
        result[comp_id_str] = good_df

    return result


def _prepend_warmup(df_comp, df_history, comp_id, sort_keys, window):
    """
    Prepend up to (window-1) prior-history rows so rolling engines have a full
    window from the very first report row instead of scoring STABLE due to NaN fill.
    Returns (combined_df, n_warmup_rows, original_index_list).
    """
    n_need   = window - 1
    orig_idx = df_comp.index.tolist()

    if df_history is None or df_history.empty or "component_id" not in df_history.columns or n_need <= 0:
        return df_comp.reset_index(drop=True), 0, orig_idx

    report_start = pd.to_datetime(df_comp["date"].min())

    # Normalise to plain string to avoid int/float mismatch (e.g. "53015290010" vs "53015290010.0")
    _norm_id       = str(comp_id).strip().rstrip(".0") if "." in str(comp_id) else str(comp_id).strip()
    _hist_ids      = df_history["component_id"].astype(str).str.strip()
    _hist_ids_norm = _hist_ids.str.rstrip(".0").where(_hist_ids.str.contains("."), _hist_ids)
    hist_comp      = df_history[_hist_ids_norm == _norm_id].copy()
    hist_comp      = hist_comp[pd.to_datetime(hist_comp["date"]) < report_start]

    if hist_comp.empty:
        return df_comp.reset_index(drop=True), 0, orig_idx

    warmup   = hist_comp.sort_values(sort_keys).tail(n_need)
    combined = pd.concat([warmup, df_comp]).reset_index(drop=True)
    return combined, len(warmup), orig_idx


def _get_cols_for_engine(analysis_cols: list, config: dict, engine: str) -> list:
    """Return the subset of analysis_cols with the given engine enabled.
    Columns absent from property_engine_flags default to enabled."""
    flags = config.get("property_engine_flags", {})
    if not flags:
        return analysis_cols
    return [c for c in analysis_cols if flags.get(c, {}).get(engine, True)]


def _run_component_mode_engines(df_merged, comp_baselines, analysis_cols, config, df_history=None):
    """Run variance/drift/oscillation per-component with per-component good-period baselines.
    Returns (var_df, drift_df, osc_df) indexed like df_merged.index."""
    from .engines.variance_engine    import compute_variance_scores
    from .engines.drift_engine       import compute_drift_scores
    from .engines.oscillation_engine import compute_oscillation_scores

    logger    = logging.getLogger(__name__)
    sort_keys = ["date", "shift"] if "shift" in df_merged.columns else ["date"]
    _window   = int(config.get("report_window") or config["engines"]["window"])

    var_parts, drift_parts, osc_parts = [], [], []

    for comp_id, df_comp in df_merged.groupby("component_id"):
        comp_id_str = str(comp_id)
        df_comp     = df_comp.sort_values(sort_keys)
        baseline    = comp_baselines.get(comp_id_str, df_comp)
        combined, n_w, orig_idx = _prepend_warmup(df_comp, df_history, comp_id, sort_keys, _window)

        try:
            var_c   = compute_variance_scores(combined, baseline, _get_cols_for_engine(analysis_cols, config, "variance"),    config)
            drift_c = compute_drift_scores(   combined, baseline, _get_cols_for_engine(analysis_cols, config, "drift"),       config)
            osc_c   = compute_oscillation_scores(combined,        _get_cols_for_engine(analysis_cols, config, "oscillation"), config)
        except Exception as exc:
            logger.warning("Component engines failed for %s: %s", comp_id_str, exc)
            continue

        var_parts.append(var_c.iloc[n_w:].set_index(pd.Index(orig_idx)))
        drift_parts.append(drift_c.iloc[n_w:].set_index(pd.Index(orig_idx)))
        osc_parts.append(osc_c.iloc[n_w:].set_index(pd.Index(orig_idx)))

    if not var_parts:
        empty = pd.DataFrame(index=df_merged.index)
        return empty, empty, empty

    return (
        pd.concat(var_parts).reindex(df_merged.index),
        pd.concat(drift_parts).reindex(df_merged.index),
        pd.concat(osc_parts).reindex(df_merged.index),
    )


def _build_agg_date_shift_comp(all_results: list) -> list:
    """Build a Date -> Shift -> Component three-level summary from per-period result dicts."""
    base = [
        {
            "date"          : str(r.get("date", "")),
            "shift"         : str(r["shift"])        if r.get("shift")        is not None else "--",
            "component"     : str(r["component_id"]) if r.get("component_id") is not None else "--",
            "si"            : float(r.get("si_score_100", 0) or 0),
            "alert"         : r.get("final_alert", "STABLE"),
            "root_cause"    : r.get("root_cause", ""),
            "recommendation": r.get("recommendation", ""),
        }
        for r in all_results
    ]
    if not base:
        return []

    df     = pd.DataFrame(base)
    output = []

    for date_val, dg in df.groupby("date", sort=True):
        output.append({
            "Level": "Date Total", "Date": date_val, "Shift": "", "Component": "",
            "Avg SI": round(float(dg["si"].mean()), 1),
            "Max SI": round(float(dg["si"].max()),  1),
            "Min SI": round(float(dg["si"].min()),  1),
            "Worst Alert": _worst_alert(dg["alert"]),
            "Count": len(dg), "Root Cause": "", "Recommendation": "",
        })
        for shift_val, sg in dg.groupby("shift", sort=True):
            output.append({
                "Level": "Shift Total", "Date": date_val, "Shift": shift_val, "Component": "",
                "Avg SI": round(float(sg["si"].mean()), 1),
                "Max SI": round(float(sg["si"].max()),  1),
                "Min SI": round(float(sg["si"].min()),  1),
                "Worst Alert": _worst_alert(sg["alert"]),
                "Count": len(sg), "Root Cause": "", "Recommendation": "",
            })
            for _, cr in sg.sort_values("component").iterrows():
                output.append({
                    "Level": "Component", "Date": date_val, "Shift": shift_val,
                    "Component": cr["component"],
                    "Avg SI": round(float(cr["si"]), 1),
                    "Max SI": round(float(cr["si"]), 1),
                    "Min SI": round(float(cr["si"]), 1),
                    "Worst Alert": cr["alert"], "Count": 1,
                    "Root Cause": cr["root_cause"], "Recommendation": cr["recommendation"],
                })
    return output


def _print_aggregation(agg: list) -> None:
    if not agg:
        return
    SEP = "-" * 65
    print(f"\n{SEP}")
    print("  AGGREGATION SUMMARY  (Date -> Shift -> Component)")
    print(SEP)
    for row in agg:
        level, si, alert, count = row["Level"], row["Avg SI"], row["Worst Alert"], row["Count"]
        if level == "Date Total":
            print(f"\n  DATE  {row['Date']}   |  Avg SI: {si:5.1f}  |  Worst Alert: {alert:<12s}  |  {count} shift(s)")
            print(f"  {'-' * 60}")
        elif level == "Shift Total":
            print(f"    SHIFT {row['Shift']:<6}  |  Avg SI: {si:5.1f}  |  Worst Alert: {alert:<12s}  |  {count} record(s)")
        else:
            print(f"      [{row['Component']}]  SI: {si:.1f}  Alert: {alert}")
            if row.get("Root Cause"):
                print(f"        Root Cause    : {row['Root Cause']}")
            if row.get("Recommendation"):
                print(f"        Recommendation: {row['Recommendation']}")
    print(f"\n{SEP}\n")


def _run_component_pipeline(config: dict, db_limits: dict, target_date, target_shift=None):
    """
    Full component-mode analysis pipeline.

    1. Fetch full DB history (cached 5 min) for per-component good-period baselines.
    2. Fetch report-window data for scoring.
    3. Build per-component baselines from rejection data.
    4. Fill NaN with shift means, run variance/drift/oscillation per component.
    5. Run deviation, pct-change, SI engines.
    6. Return (target_result, day_results) for target_date.
    """
    from datetime import date as _date, timedelta
    from .pipeline.data_fetcher import fetch_all, fetch_rejection_data
    from .pipeline.aggregator   import build_dataset, get_param_columns
    from .engines.variance_engine    import aggregate_variance_score
    from .engines.drift_engine       import aggregate_drift_score
    from .engines.oscillation_engine import aggregate_oscillation_score
    from .engines.control_limits     import compute_deviation_status
    from .engines.pct_change_engine  import compute_pct_change
    from .engines.si_engine          import compute_si, compute_per_param_si
    from .engines.within_component_variance_engine import compute_within_component_variance
    from .run_watchdog import (
        _fetch_clean_baseline, _shift_baseline_to_optimal,
        _filter_si_param_cols, _get_add_param_cols,
        _build_period_result,
    )

    logger = logging.getLogger(__name__)

    report_window = config.get("report_window")
    report_config = copy.deepcopy(config)
    if report_window and int(report_window) != int(config["engines"]["window"]):
        report_config["engines"]["window"] = int(report_window)
        logger.info("Component pipeline: window override %d -> %d",
                    config["engines"]["window"], report_window)

    _window        = int(report_config["engines"]["window"])
    baseline_start = pd.to_datetime(config["baseline"]["start_date"]).date()
    fetch_start    = min(baseline_start, target_date - timedelta(days=_window * 5))
    _hist_start    = _date(2000, 1, 1)
    _hist_end      = _date.today()

    _cache_key = (config.get("foundry_line_id"), config.get("database", {}).get("name"))
    _cached    = _HIST_CACHE.get(_cache_key)
    if _cached and (time.monotonic() - _cached[0]) < _HIST_CACHE_TTL:
        df_all = _cached[1]
        logger.info("Component pipeline: using cached full DB history (%d rows)", len(df_all))
    else:
        logger.info("Component pipeline: fetching full DB history for baselines ...")
        try:
            df_all = build_dataset(fetch_all(config, start_date=_hist_start, end_date=_hist_end), config)
            _HIST_CACHE[_cache_key] = (time.monotonic(), df_all)
            logger.info("Full-history rows: %d (cached)", len(df_all))
        except Exception as exc:
            logger.warning("Full history fetch failed -- using report window only: %s", exc)
            df_all = pd.DataFrame()

    df_merged = build_dataset(fetch_all(config, start_date=fetch_start, end_date=target_date), config)
    if df_merged.empty:
        logger.warning("Component pipeline: empty dataset for %s", target_date)
        return None, []

    def _drop_null_comp(df):
        if "component_id" not in df.columns or df.empty:
            return df
        mask = (
            df["component_id"].notna() &
            (df["component_id"].astype(str).str.strip() != "") &
            ~df["component_id"].astype(str).str.lower().isin(["nan", "none", "null"])
        )
        return df[mask].copy()

    n_before  = len(df_merged)
    df_merged = _drop_null_comp(df_merged)
    df_all    = _drop_null_comp(df_all)
    logger.info("Component drop: %d -> %d rows (null component_id excluded)", n_before, len(df_merged))

    if df_merged.empty:
        logger.warning("Component pipeline: all rows had null component_id")
        return None, []

    param_cols     = get_param_columns(df_merged)
    si_param_cols  = _filter_si_param_cols(param_cols, report_config)
    add_param_cols = _get_add_param_cols(param_cols, report_config)
    # Exclude consumption; sieve (sv_) stays — shift-level value is broadcast to all components
    analysis_cols  = [c for c in si_param_cols if not c.startswith("con_")]

    baseline_df      = _fetch_clean_baseline(config)
    baseline_df_drft = _shift_baseline_to_optimal(baseline_df, analysis_cols, report_config)

    try:
        df_rejection = fetch_rejection_data(
            config, start_date=_hist_start, end_date=_hist_end, group_by="component",
        )
        logger.info("Rejection data for component baselines: %d rows", len(df_rejection))
    except Exception as exc:
        logger.warning("Rejection fetch failed -- using all-period baselines: %s", exc)
        df_rejection = pd.DataFrame()

    df_src_filled  = _fill_with_shift_mean(df_all if not df_all.empty else df_merged, analysis_cols)
    df_for_engines = _fill_with_shift_mean(df_merged, analysis_cols)

    try:
        comp_baselines = _compute_component_baselines(df_src_filled, df_rejection, analysis_cols)
        logger.info("Component baselines ready: %d components", len(comp_baselines))
    except Exception as exc:
        logger.warning("Component baseline build failed -- using global baseline: %s", exc)
        comp_baselines = {}

    if comp_baselines:
        var_df, drift_df, osc_df = _run_component_mode_engines(
            df_for_engines, comp_baselines, analysis_cols, report_config, df_history=df_src_filled,
        )
    else:
        from .engines.variance_engine    import compute_variance_scores
        from .engines.drift_engine       import compute_drift_scores
        from .engines.oscillation_engine import compute_oscillation_scores
        var_df   = compute_variance_scores(   df_for_engines, baseline_df,      _get_cols_for_engine(analysis_cols, report_config, "variance"),    report_config)
        drift_df = compute_drift_scores(      df_for_engines, baseline_df_drft, _get_cols_for_engine(analysis_cols, report_config, "drift"),       report_config)
        osc_df   = compute_oscillation_scores(df_for_engines,                   _get_cols_for_engine(analysis_cols, report_config, "oscillation"), report_config)

    # WDV requires multiple shifts per day — not available at component granularity
    wdv_df = pd.DataFrame(index=df_for_engines.index)

    add_cols_wcv = [c for c in param_cols if c.startswith("add_") and not c.startswith("add_bvar_")]
    wcv_df = compute_within_component_variance(df_for_engines, add_cols_wcv, report_config, baseline_df)

    # pct_change per-component to avoid cross-component shift comparisons
    _sort_keys = ["date", "shift"] if "shift" in df_for_engines.columns else ["date"]
    _pct_cols  = [c for c in analysis_cols + add_param_cols if c in df_for_engines.columns]
    _pct_parts = [
        compute_pct_change(_df_c.sort_values(_sort_keys), _pct_cols, report_config)
        for _, _df_c in df_for_engines.groupby("component_id")
    ]
    pct_df = (
        pd.concat(_pct_parts).reindex(df_for_engines.index)
        if _pct_parts else pd.DataFrame(index=df_for_engines.index)
    )

    _param_w = ({k: float(v) for k, v in config.get("si_param_weights", {}).items()
                 if not k.startswith("_") and isinstance(v, (int, float))} or None)

    var_risk   = aggregate_variance_score(var_df,    param_weights=_param_w)
    drift_risk = aggregate_drift_score(drift_df,     param_weights=_param_w)
    osc_risk   = aggregate_oscillation_score(osc_df, param_weights=_param_w)

    # Consumption excluded from deviation check
    dev_cols    = [c for c in param_cols if not c.startswith("con_")]
    dev_df      = compute_deviation_status(df_merged, dev_cols, report_config, db_limits=db_limits)
    si_df       = compute_si(var_risk, drift_risk, osc_risk, report_config)
    param_si_df = compute_per_param_si(var_df, drift_df, osc_df, analysis_cols, report_config)

    # Additive params scored for display only — not aggregated into SI
    if add_param_cols:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            from .engines.variance_engine    import compute_variance_scores as _cvs
            from .engines.drift_engine       import compute_drift_scores    as _cds
            from .engines.oscillation_engine import compute_oscillation_scores as _cos
            _var_add   = _cvs(df_merged, baseline_df, add_param_cols, report_config)
            _drift_add = _cds(df_merged, baseline_df, add_param_cols, report_config)
            _osc_add   = _cos(df_merged, add_param_cols, report_config)
            _psi_add   = compute_per_param_si(_var_add, _drift_add, _osc_add, add_param_cols, report_config)
        var_df      = pd.concat([var_df,      _var_add],  axis=1)
        drift_df    = pd.concat([drift_df,    _drift_add], axis=1)
        osc_df      = pd.concat([osc_df,      _osc_add],  axis=1)
        param_si_df = pd.concat([param_si_df, _psi_add],  axis=1)

    si_display_cols = analysis_cols + add_param_cols
    _pos_map        = {idx: pos for pos, idx in enumerate(df_merged.index)}

    all_results = [
        _build_period_result(
            i, df_merged, var_df, drift_df, osc_df, wdv_df,
            dev_df, pct_df, si_df, param_si_df,
            report_config, param_cols, si_display_cols,
            wcv_df=wcv_df, pos_map=_pos_map,
        )
        for i in df_for_engines.index
    ]

    try:
        from .pipeline.data_fetcher import fetch_component_names
        _comp_names = fetch_component_names(config)
        for r in all_results:
            cid = str(r.get("component_id") or "").strip()
            r["component_name"] = _comp_names.get(cid, "")
    except Exception as exc:
        logger.warning("Component name lookup failed: %s", exc)

    target_result = None
    day_results   = []
    for r in all_results:
        r_date  = str(r.get("date", ""))
        r_shift = str(r.get("shift", "")) if r.get("shift") is not None else ""
        if r_date == str(target_date):
            day_results.append(r)
            if target_shift is None or r_shift == str(target_shift).strip():
                target_result = r

    if target_result is None:
        logger.warning(
            "Component pipeline: no rows matched date=%s shift=%s -- available dates: %s",
            target_date, target_shift,
            sorted({str(r.get("date", "")) for r in all_results})[:5],
        )
        return None, []
    if not day_results:
        day_results = [target_result]

    if report_config.get("output", {}).get("save_excel", True):
        try:
            from .alert_engine import write_excel
            write_excel(
                all_results, df_merged, var_df, drift_df, osc_df,
                dev_df, pct_df, si_df, param_si_df, report_config,
                db_limits=db_limits, wcv_df=wcv_df,
            )
        except Exception as exc:
            logger.warning("Component pipeline Excel write failed: %s", exc)

    logger.info("Component pipeline done -- %d components for %s", len(day_results), target_date)
    return target_result, day_results


def _setup_logging(log_file: str, level: int = logging.INFO) -> None:
    fmt  = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


def _load_config(config_path: Path) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _fetch_last_shift(config: dict):
    """Return (date, shift) of the most recent row in preparedsand."""
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    engine = get_engine(config)
    sql    = text("""
        SELECT DATE(`date`) AS date, `shift`
        FROM   `preparedsand`
        WHERE  `foundry_line_id` = :fl_id
          AND  `deleted` = 0
          AND  `date`    IS NOT NULL
        ORDER  BY `date` DESC, `shift` DESC
        LIMIT  1
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"fl_id": config["foundry_line_id"]}).mappings().first()
    if not row:
        return None, None
    return pd.to_datetime(row["date"]).date(), str(row["shift"]).strip()


def _fetch_last_shift_for_date(config: dict, target_date):
    """Return the latest shift available for target_date in preparedsand."""
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    engine = get_engine(config)
    sql    = text("""
        SELECT `shift`
        FROM   `preparedsand`
        WHERE  `foundry_line_id` = :fl_id
          AND  `deleted` = 0
          AND  DATE(`date`) = :d
        ORDER  BY `shift` DESC
        LIMIT  1
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"fl_id": config["foundry_line_id"], "d": str(target_date)}).mappings().first()
    return str(row["shift"]).strip() if row else None


def _print_result(result: dict, config: dict) -> None:
    display = config.get("display_names", {})

    def _label(col: str) -> str:
        bare = col
        for pfx in ("ps_", "con_", "add_", "pse_"):
            if col.startswith(pfx):
                bare = col[len(pfx):]
        return display.get(bare) or display.get(col) or col

    SEP = "-" * 65
    print(f"\n{SEP}")
    print(f"  Period    : {result.get('period_key')}")
    print(f"  Alert     : {result.get('final_alert')}")
    print(f"  SI Score  : {result.get('si_score_100', 0):.1f} / 100")
    print(SEP)

    root = result.get("root_cause", "")
    rec  = result.get("recommendation", "")
    if root: print(f"  Root cause    : {root}")
    if rec:  print(f"  Recommendation: {rec}")
    print()

    param_labels = result.get("param_labels", {})
    non_stable   = {p: lv for p, lv in param_labels.items() if lv != "STABLE" and p.startswith("ps_")}

    if non_stable:
        print(f"  {'Parameter':<35}  {'SI':<6}  {'Alert':<10}  {'Drift':<14}  {'Variance':<12}  {'Deviation'}")
        print(f"  {'-'*35}  {'-'*6}  {'-'*10}  {'-'*14}  {'-'*12}  {'-'*20}")
        param_si   = result.get("param_si",     {})
        drift_lbl  = result.get("drift_labels", {})
        var_lbl    = result.get("var_labels",   {})
        deviations = result.get("deviations",   {})
        for p, lv in sorted(non_stable.items(), key=lambda x: -(param_si.get(x[0]) or 0)):
            si_val  = param_si.get(p)
            si_str  = f"{si_val:.1f}" if si_val is not None else "--"
            dev     = deviations.get(p, "")
            dev_str = dev if isinstance(dev, str) and dev != "OK" else ""
            print(f"  {_label(p):<35}  {si_str:<6}  {lv:<10}  {drift_lbl.get(p, ''):<14}  {var_lbl.get(p, ''):<12}  {dev_str}")
        print()
    else:
        print("  All parameters STABLE.\n")

    devs = [(p, v) for p, v in result.get("deviations", {}).items()
            if p.startswith("ps_") and isinstance(v, str) and v.startswith("Deviated")]
    if devs:
        print(f"  !!  LCL/UCL deviations ({len(devs)}):")
        for p, v in devs:
            print(f"      {_label(p):<35}  {v}")
        print()

    print(f"{SEP}\n")


def run_once(config: dict, target_date=None, target_shift=None) -> None:
    from .pipeline.data_fetcher import fetch_control_limits, fetch_monitored_parameters, fetch_display_names
    from .alert_db_writer       import ensure_table, write_si_alert
    from .pipeline.db_connector import get_engine
    from .run_watchdog          import run_for_period

    logger = logging.getLogger(__name__)

    try:
        monitored = fetch_monitored_parameters(config)
        for key in ("prepared_sand", "consumption", "additive", "prepared_sand_extra"):
            if monitored.get(key):
                config["parameters"][key] = monitored[key]
    except Exception as exc:
        logger.warning("fetch_monitored_parameters failed: %s", exc)

    try:
        config["display_names"] = fetch_display_names(config)
    except Exception as exc:
        logger.warning("fetch_display_names failed: %s", exc)

    try:
        db_limits = fetch_control_limits(config)
    except Exception as exc:
        logger.warning("fetch_control_limits failed: %s", exc)
        db_limits = {}

    if target_date:
        last_date  = pd.to_datetime(target_date).date()
        last_shift = str(target_shift).strip() if target_shift else _fetch_last_shift_for_date(config, last_date)
        if last_shift is None:
            print(f"ERROR: No data found in preparedsand for date {last_date}.")
            sys.exit(1)
        logger.info("Target period  ->  date=%s  shift=%s", last_date, last_shift)
    else:
        last_date, last_shift = _fetch_last_shift(config)
        if last_date is None:
            print("ERROR: No data found in preparedsand for this foundry line.")
            sys.exit(1)

    mode      = config["trigger"]["mode"]
    agg_mode  = config.get("aggregation", {}).get("mode", "shift")
    dual_mode = config.get("dual_mode", False)
    secondary = config.get("dual_mode_secondary", "shift")

    logger.info(
        "Last available data  ->  date=%s  shift=%s  (trigger=%s  agg=%s  dual=%s)",
        last_date, last_shift, mode, agg_mode, dual_mode,
    )

    def _fire_mode(cfg, run_mode, lbl):
        r = run_for_period(
            config=cfg, trigger_mode=run_mode,
            trigger_date=last_date, trigger_shift=last_shift, db_limits=db_limits,
        )
        return lbl, r, ([r] if r else [])

    all_run_results: list[tuple[str, dict, list]] = []

    if dual_mode:
        logger.info("DUAL MODE  >  component  +  %s", secondary)
        comp_result, _ = _run_component_pipeline(config, db_limits, last_date, last_shift)
        all_run_results.append(("COMPONENT", comp_result, [comp_result] if comp_result else []))
        sec_cfg = copy.deepcopy(config)
        sec_cfg["aggregation"]["mode"] = secondary
        all_run_results.append(_fire_mode(sec_cfg, secondary, secondary.upper()))
    elif agg_mode == "component":
        comp_result, _ = _run_component_pipeline(config, db_limits, last_date, last_shift)
        all_run_results.append(("COMPONENT", comp_result, [comp_result] if comp_result else []))
    else:
        all_run_results.append(_fire_mode(config, mode, agg_mode.upper()))

    result = next((r for _, r, _ in all_run_results if r), None)
    if not result:
        print(f"ERROR: Could not build result for {last_date} shift {last_shift}.")
        sys.exit(1)

    SEP = "=" * 65
    for lbl, r, day_res in all_run_results:
        if not r:
            continue
        print(f"\n{SEP}\n  MODE: {lbl}\n{SEP}")
        _print_result(r, config)
        if day_res:
            _print_aggregation(_build_agg_date_shift_comp(day_res))

    try:
        from .pipeline.data_fetcher import fetch_component_names as _fcn
        _comp_names = _fcn(config)
        for _, r, _ in all_run_results:
            if r:
                cid = str(r.get("component_id") or "").strip()
                r["component_name"] = _comp_names.get(cid, "")
    except Exception as exc:
        logger.warning("Component name lookup failed: %s", exc)

    try:
        engine = get_engine(config)
        ensure_table(engine)
        fl_id   = config.get("foundry_line_id", 1)
        written = 0
        for lbl, r, _ in all_run_results:
            if not r:
                continue
            try:
                write_si_alert(engine, r, fl_id,
                               display_names=config.get("display_names", {}),
                               customer_pkey=config.get("customer_pkey", 0),
                               pct_warn=float(config.get("pct_change_warning", 5.0)))
                written += 1
            except Exception as exc:
                logger.warning("DB write failed for %s: %s", r.get("period_key", "?"), exc)
        logger.info("Alert(s) written to watchdog_alerts: %d record(s).", written)
    except Exception as exc:
        logger.warning("DB write failed: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Watchdog -- multi-foundry monitor")
    parser.add_argument("--config",     type=Path, default=_DEFAULT_CONFIG, help="Path to watchdog_config.json")
    parser.add_argument("--once",       action="store_true", help="Analyse the last available shift once and exit")
    parser.add_argument("--date",       metavar="YYYY-MM-DD", default=None, help="Date to analyse with --once")
    parser.add_argument("--shift",      metavar="SHIFT",      default=None, help="Shift to analyse with --once")
    parser.add_argument("--comp-change",  action="store_true", help="One-shot component-change check")
    parser.add_argument("--bad-batch",    action="store_true", help="One-shot bad-batch (SMC vs COSP) check")
    parser.add_argument("--sieve-change", action="store_true", help="One-shot sieve %% change check")
    parser.add_argument("--last-n",     type=int, default=10, metavar="N", help="Recent entries for --sieve-change / --bad-batch (default 10)")
    parser.add_argument("--write",      action="store_true", help="Write alerts to DB with --comp-change / --bad-batch / --sieve-change")
    parser.add_argument("--single",     action="store_true", help="Force single-foundry mode")
    parser.add_argument("--log-level",  default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    config_path = args.config.resolve()
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    base_config = _load_config(config_path)
    _setup_logging(base_config.get("output", {}).get("log_file", _LOG_FILE),
                   level=getattr(logging, args.log_level))

    try:
        from .config_store import (
            get_registry_engine, ensure_config_table,
            seed_foundry_configs, load_all_configs,
        )
        _reg = get_registry_engine(base_config)
        if _reg:
            ensure_config_table(_reg)
            seeded = seed_foundry_configs(_reg, base_config.get("foundry_configs", {}))
            if seeded:
                logging.getLogger(__name__).info(
                    "Seeded default config for %d new foundry line(s)", seeded)
            base_config["foundry_configs"] = load_all_configs(_reg)
            logging.getLogger(__name__).info(
                "Per-foundry configs loaded from DB: %d foundry line(s)",
                len(base_config["foundry_configs"]),
            )
        else:
            logging.getLogger(__name__).warning("Registry DB not reachable -- running with defaults only")
            base_config["foundry_configs"] = {}
    except Exception as exc:
        logging.getLogger(__name__).warning("DB config load failed -- running with defaults: %s", exc)
        base_config["foundry_configs"] = {}

    logger = logging.getLogger(__name__)
    logger.info("=" * 70)
    logger.info("  AI Watchdog  --  %s",
                "One-shot"          if args.once
                else "Comp-Change"  if args.comp_change
                else "Bad-Batch"    if args.bad_batch
                else "Sieve-Change" if args.sieve_change
                else "Multi-Foundry Monitor")
    logger.info("  Config : %s", config_path)
    logger.info("=" * 70)

    def _apply_foundry_config(cfg):
        """Overlay all registry-DB settings onto cfg for the resolved foundry label."""
        _label = f"{cfg['database']['name']}_L{cfg.get('foundry_line_id', 1)}"
        _fc    = cfg.get("foundry_configs", {}).get(_label, {})
        if not _fc:
            return cfg

        cfg = copy.deepcopy(cfg)

        for _k in ("dual_mode", "dual_mode_secondary", "report_window", "pct_change_warning"):
            if _k in _fc:
                cfg[_k] = _fc[_k]

        for _k in ("sv_params", "add_params", "smc_params", "metal_params",
                   "enable_sieve", "enable_additive", "enable_smc", "enable_metal"):
            if _k in _fc:
                cfg[_k] = _fc[_k]

        if "si_params"             in _fc: cfg["si_params"]             = _fc["si_params"]
        if "si_weights"            in _fc: cfg["si_weights"]            = _fc["si_weights"]
        if "alert_thresholds"      in _fc: cfg["alert_thresholds"]      = _fc["alert_thresholds"]
        if "alert_labels"          in _fc: cfg["alert_labels"]          = _fc["alert_labels"]
        if "optimal_values"        in _fc: cfg["optimal_values"]        = _fc["optimal_values"]
        if "property_engine_config" in _fc: cfg["property_engine_config"] = _fc["property_engine_config"]

        if "engines" in _fc:
            for _eng, _ov in _fc["engines"].items():
                cfg.setdefault("engines", {}).setdefault(_eng, {}).update(_ov)

        # Merge dict sections so JSON-file defaults survive for unset keys
        for _sect in ("component_change_watchdog", "bad_batch_watchdog",
                      "sieve_watchdog", "prescription_watchdog",
                      "aggregation", "baseline", "trigger"):
            if _sect in _fc:
                cfg.setdefault(_sect, {}).update(_fc[_sect])

        # Merge notifications (email config stored in DB via Config UI)
        if "notifications" in _fc:
            cfg["notifications"] = _fc["notifications"]

        logger.info("Applied per-foundry DB config for %s (dual_mode=%s  agg=%s)",
                    _label, cfg.get("dual_mode"), cfg.get("aggregation", {}).get("mode"))
        return cfg

    if args.comp_change:
        from .component_change_monitor import run_check as _cc_check
        _cc_check(_apply_foundry_config(base_config), write_db=args.write)
        return

    if args.bad_batch:
        from .bad_batch_monitor import run_check as _bb_check
        _bb_check(_apply_foundry_config(base_config), write_db=args.write, last_n=args.last_n)
        return

    if args.sieve_change:
        from .sieve_monitor import run_check as _sv_check
        _sv_check(_apply_foundry_config(base_config), write_db=args.write, last_n=args.last_n)
        return

    if args.once:
        run_once(_apply_foundry_config(base_config), target_date=args.date, target_shift=args.shift)
        return

    from .foundry_registry   import expand_all_foundries
    from .foundry_si_monitor import FoundrySIMonitor

    if args.single:
        label   = base_config["database"].get("name", "default")
        fl_id   = base_config.get("foundry_line_id", 1)
        entries = [(f"{label}_L{fl_id}", base_config)]
        logger.info("Single-foundry mode: %s  line=%d", label, fl_id)
    else:
        entries = expand_all_foundries(base_config)

    if not entries:
        logger.warning("No foundry instances enabled -- waiting for a foundry to be enabled via Config UI (retrying every 60s)")
        while True:
            time.sleep(60)
            entries = expand_all_foundries(base_config)
            if entries:
                logger.info("Foundry instance(s) now enabled: %d -- continuing startup", len(entries))
                break
            logger.info("Still no enabled foundry instances, sleeping...")

    logger.info("Starting %d monitor thread(s)...", len(entries))
    threads = []
    # label -> (thread, FoundrySIMonitor) — used for live config reload
    _running_monitors: dict = {}
    for label, cfg in entries:
        monitor = FoundrySIMonitor(config=cfg, label=label)
        t = threading.Thread(target=monitor.start, name=f"watchdog-{label}", daemon=True)
        t.start()
        threads.append(t)
        _running_monitors[label] = (t, monitor)
        logger.info("  Started: %s", label)

    # ── Data Flow Monitor — ONE instance per unique foundry DB ───────────────
    # Multiple lines (L1, L2) of same foundry share one monitor instance
    # because _get_all_foundry_lines() discovers ALL lines from that DB.
    try:
        from .data_flow.run import start_data_flow_monitor
        _seen_dbs = set()
        for _df_label, _df_cfg in entries:
            _db_name = (_df_cfg.get("database") or {}).get("name", "")
            if _db_name in _seen_dbs:
                continue   # already started a monitor for this DB
            _seen_dbs.add(_db_name)
            _df_t = threading.Thread(
                target = start_data_flow_monitor,
                args   = (_df_cfg,),
                name   = f"data-flow-{_db_name}",
                daemon = True,
            )
            _df_t.start()
            threads.append(_df_t)
            logger.info("  Started: data-flow-%s", _db_name)
    except Exception as _df_exc:
        logger.warning("  Data flow monitor failed to start: %s", _df_exc)

    # ── Prediction Monitor — one thread per foundry line ────────────────────────
    try:
        from .prediction_monitor import PredictionMonitor
        for _pm_label, _pm_cfg in entries:
            _pm_enabled = _pm_cfg.get("prediction_monitor", {}).get("enabled", False)
            if not _pm_enabled:
                continue
            _pm = PredictionMonitor(config=_pm_cfg, label=f"{_pm_label}/prediction")
            _pm_t = threading.Thread(
                target = _pm.start,
                name   = f"prediction-{_pm_label}",
                daemon = True,
            )
            _pm_t.start()
            threads.append(_pm_t)
            logger.info("  Started: prediction-%s", _pm_label)
    except Exception as _pm_exc:
        logger.warning("  Prediction monitor failed to start: %s", _pm_exc)

    # ── Auto-restart after 24 h to free accumulated memory ─────────────────────
    # os.execv replaces this process with a fresh copy — same PID, all memory freed.
    import os as _os
    _start_time = datetime.now()
    _cfg_reload_since = datetime.now()   # only reload configs changed after this
    _MAX_RUNTIME_SEC = 24 * 3600   # 24 hours

    try:
        while True:
            # Check if any thread is still alive
            alive = [t for t in threads if t.is_alive()]
            if not alive:
                logger.info("All monitor threads exited — shutting down")
                break

            # Restart after 24 h to prevent memory bloat
            uptime = (datetime.now() - _start_time).total_seconds()
            if uptime >= _MAX_RUNTIME_SEC:
                logger.info(
                    "Auto-restart after %.0f h — freeing accumulated memory",
                    uptime / 3600,
                )
                _module = (__spec__ and __spec__.name) or "watchdog.run_alert_monitor"
                _os.execv(sys.executable, [sys.executable, "-m", _module])

            # ── Live config reload — runs every 60 s ─────────────────────────
            # 1. Pull fresh per-foundry configs from DB and update running
            #    monitors in-place.  All sub-monitors (prescription, sieve,
            #    bad_batch, …) share the same dict reference, so they pick up
            #    the new thresholds on their next poll cycle automatically.
            # 2. Discover newly enabled foundries and start them without a
            #    full restart.
            if _reg:
                try:
                    from .config_store import load_all_configs
                    fresh_configs = load_all_configs(_reg, since=_cfg_reload_since)
                    _cfg_reload_since = datetime.now()

                    # Update existing monitor configs in-place
                    for _lbl, (_t, _mon) in list(_running_monitors.items()):
                        if _lbl in fresh_configs:
                            _mon._config.update(fresh_configs[_lbl])

                    # Discover any newly enabled foundries
                    from .foundry_registry import expand_all_foundries
                    base_config["foundry_configs"].update(fresh_configs)
                    all_entries = expand_all_foundries(base_config)
                    for _lbl, _cfg in all_entries:
                        if _lbl not in _running_monitors or \
                                not _running_monitors[_lbl][0].is_alive():
                            logger.info("Auto-starting newly enabled foundry: %s", _lbl)
                            _mon = FoundrySIMonitor(config=_cfg, label=_lbl)
                            _t = threading.Thread(
                                target=_mon.start,
                                name=f"watchdog-{_lbl}",
                                daemon=True,
                            )
                            _t.start()
                            threads.append(_t)
                            _running_monitors[_lbl] = (_t, _mon)
                except Exception as _reload_exc:
                    logger.warning("Live config reload failed: %s", _reload_exc)

            time.sleep(60)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt -- shutting down")


if __name__ == "__main__":
    main()
