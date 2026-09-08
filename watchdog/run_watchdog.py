"""
run_watchdog.py
----------------
Pure computation module -- no CLI, no console output.
Called by scheduler.py on every trigger event.

Public functions
----------------
  run_full_history(config, db_limits)
      Fetches all history, runs all engines, writes Excel.

  run_for_period(config, trigger_mode, trigger_date, trigger_shift, db_limits)
      Fetches the required data window, runs all engines,
      builds the per-period result dict, writes Excel.
"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def run_full_history(config: dict, db_limits: dict | None = None) -> dict:
    """Pull all historical data, run every engine, write Excel."""
    from .pipeline.data_fetcher                 import fetch_all, fetch_control_limits
    from .pipeline.aggregator                   import build_dataset, get_baseline, get_param_columns
    from .engines.variance_engine               import compute_variance_scores, aggregate_variance_score
    from .engines.drift_engine                  import compute_drift_scores, aggregate_drift_score
    from .engines.oscillation_engine            import compute_oscillation_scores, aggregate_oscillation_score
    from .engines.acceleration_engine           import compute_acceleration_scores, aggregate_acceleration_score
    from .engines.recovery_engine               import compute_recovery_scores, aggregate_recovery_score
    from .engines.within_day_variance_engine    import compute_within_day_variance, aggregate_within_day_score
    from .engines.control_limits                import compute_deviation_status
    from .engines.pct_change_engine             import compute_pct_change
    from .engines.si_engine                     import compute_si, compute_per_param_si
    from .alert_engine                          import write_excel

    logger.info("run_full_history  starting")

    if db_limits is None:
        try:
            db_limits = fetch_control_limits(config)
        except Exception as exc:
            logger.warning("fetch_control_limits failed: %s  (using config fallback)", exc)
            db_limits = {}

    raw = fetch_all(config)
    for tbl, df in raw.items():
        logger.info("  %-25s : %d rows", tbl, len(df))

    df_merged = build_dataset(raw, config)
    if df_merged.empty:
        logger.error("run_full_history: dataset is empty -- aborting")
        return {}

    logger.info("Merged: %d periods, %d columns", len(df_merged), len(df_merged.columns))

    get_baseline(df_merged, config)   # logs baseline row count for info only
    param_cols  = get_param_columns(df_merged)

    # Fetch baseline in isolation so July 2025 NaNs are imputed within that period only,
    # keeping b_std accurate for the drift engine.
    baseline_df      = _fetch_clean_baseline(config)
    si_param_cols    = _filter_si_param_cols(param_cols, config)

    # Dynamically compute optimal_variance from baseline data (25th-pct of rolling variances).
    # This replaces any hardcoded values in watchdog_config.json so every foundry
    # automatically gets the correct reference for its own baseline period.
    import copy as _copy_cfg
    config = _copy_cfg.deepcopy(config)
    _dyn_opt_var = _compute_optimal_variance(baseline_df, si_param_cols, config)
    if _dyn_opt_var:
        config["optimal_variance"] = _dyn_opt_var
        logger.info("optimal_variance computed from baseline: %d params", len(_dyn_opt_var))

    baseline_df_drft = _shift_baseline_to_optimal(baseline_df, si_param_cols, config)

    _param_w    = ({k: float(v) for k, v in config.get("si_param_weights", {}).items()
                    if not k.startswith("_") and isinstance(v, (int, float))} or None)
    var_df      = compute_variance_scores(df_merged, baseline_df, si_param_cols, config)
    var_risk    = aggregate_variance_score(var_df,   param_weights=_param_w)
    drift_df    = compute_drift_scores(df_merged, baseline_df_drft, si_param_cols, config)
    drift_risk  = aggregate_drift_score(drift_df,   param_weights=_param_w)
    osc_df      = compute_oscillation_scores(df_merged, si_param_cols, config)
    osc_risk    = aggregate_oscillation_score(osc_df, param_weights=_param_w)
    acc_df      = compute_acceleration_scores(drift_df, baseline_df, si_param_cols, config)
    acc_risk    = aggregate_acceleration_score(acc_df,  param_weights=_param_w)
    rec_df      = compute_recovery_scores(var_df, drift_df, si_param_cols, config)
    rec_risk    = aggregate_recovery_score(rec_df,      param_weights=_param_w)
    wdv_df = compute_within_day_variance(df_merged, si_param_cols, config, baseline_df)
    # Consumption (con_) excluded from deviation check -- only ps_ and add_ are checked.
    dev_cols  = [c for c in param_cols if not c.startswith("con_")]
    dev_df    = compute_deviation_status(df_merged, dev_cols, config, db_limits=db_limits)

    # In component mode, pct_change must be computed per-component so each
    # component's values are compared to ITS OWN previous shift occurrence.
    # Without this, comparing component A to component B (different components
    # on the same shift) gives 0% because prepared-sand columns are identical
    # across components in the same shift (they are shift-level averages).
    _agg_mode = config.get("aggregation", {}).get("mode", "shift")
    if _agg_mode == "component" and "component_id" in df_merged.columns:
        _sort_keys_pct = ["date", "shift"] if "shift" in df_merged.columns else ["date"]
        _pct_parts = []
        for _cid, _df_c in df_merged.groupby("component_id"):
            _pct_parts.append(
                compute_pct_change(_df_c.sort_values(_sort_keys_pct), param_cols, config)
            )
        pct_df = (
            pd.concat(_pct_parts).reindex(df_merged.index)
            if _pct_parts else pd.DataFrame(index=df_merged.index)
        )
    else:
        pct_df = compute_pct_change(df_merged, param_cols, config)
    si_df    = compute_si(var_risk, drift_risk, osc_risk, config, acc_risk=acc_risk, rec_risk=rec_risk)
    param_si_df = compute_per_param_si(var_df, drift_df, osc_df, si_param_cols, config, acc_df=acc_df, rec_df=rec_df)

    # Per-param SI for additive cols -- display only, NOT in overall aggregate
    import warnings as _warnings
    add_param_cols = _get_add_param_cols(param_cols, config)
    if add_param_cols:
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", RuntimeWarning)
            _var_add   = compute_variance_scores(df_merged, baseline_df, add_param_cols, config)
            _drift_add = compute_drift_scores(df_merged, baseline_df, add_param_cols, config)
            _osc_add   = compute_oscillation_scores(df_merged, add_param_cols, config)
            _psi_add   = compute_per_param_si(_var_add, _drift_add, _osc_add, add_param_cols, config)
        var_df      = pd.concat([var_df,      _var_add],  axis=1)
        drift_df    = pd.concat([drift_df,    _drift_add], axis=1)
        osc_df      = pd.concat([osc_df,      _osc_add],  axis=1)
        param_si_df = pd.concat([param_si_df, _psi_add],  axis=1)
    si_display_cols = si_param_cols + add_param_cols

    _pos_map = {idx: pos for pos, idx in enumerate(df_merged.index)}
    all_results = [
        _build_period_result(i, df_merged, var_df, drift_df, osc_df, wdv_df,
                             dev_df, pct_df, si_df, param_si_df, config,
                             param_cols, si_display_cols, pos_map=_pos_map)
        for i in df_merged.index
    ]

    excel_path = ""
    if config["output"].get("save_excel", True):
        excel_path = write_excel(
            all_results, df_merged, var_df, drift_df,
            osc_df, dev_df, pct_df, si_df, param_si_df, config,
            db_limits=db_limits,
        )
        logger.info("Excel saved: %s", excel_path)

    _log_summary(si_df, all_results)

    return {
        "df"      : df_merged,
        "si_df"   : si_df,
        "results" : all_results,
        "excel"   : excel_path,
    }


def run_for_component(
    config:               dict,
    completed_component:  str,
    n_runs:               int  = 5,
    db_limits:            dict | None = None,
    df_rejection:         "pd.DataFrame | None" = None,
) -> Optional[dict]:
    """
    Run SI analysis for a component that just completed production.

    Engine inputs
    -------------
    Variance / Drift / Oscillation:
        Last *n_runs* shift-occurrences of *completed_component*, each row
        being the mean of all batches for that component in that shift.
        Engines see the trend across these N occurrences.

    Within-component variance:
        add_bvar_* columns already embedded in each row — these capture the
        batch-to-batch spread WITHIN each individual occurrence.

    Baseline:
        Good-period subset of the component's full history (occurrences where
        total rejection ≤ median rejection for that component).

    Returns the SI result dict for the most recent (just-completed) occurrence,
    or None if not enough data.
    """
    from datetime import date as _date, timedelta
    from .pipeline.data_fetcher      import fetch_all, fetch_control_limits
    from .pipeline.aggregator        import build_component_run_dataset, get_param_columns
    from .engines.variance_engine       import compute_variance_scores, aggregate_variance_score
    from .engines.drift_engine          import compute_drift_scores, aggregate_drift_score
    from .engines.oscillation_engine    import compute_oscillation_scores, aggregate_oscillation_score
    from .engines.acceleration_engine   import compute_acceleration_scores, aggregate_acceleration_score
    from .engines.recovery_engine       import compute_recovery_scores, aggregate_recovery_score
    from .engines.within_component_variance_engine import (
        compute_within_component_variance, aggregate_within_component_score,
    )
    from .engines.control_limits        import compute_deviation_status
    from .engines.pct_change_engine     import compute_pct_change
    from .engines.si_engine             import compute_si, compute_per_param_si

    if db_limits is None:
        try:
            db_limits = fetch_control_limits(config)
        except Exception as exc:
            logger.warning("run_for_component: fetch_control_limits failed: %s", exc)
            db_limits = {}

    today = _date.today()
    try:
        baseline_start = pd.to_datetime(config["baseline"]["start_date"]).date()
    except (KeyError, TypeError, ValueError):
        baseline_start = today - pd.Timedelta(days=365)
        logger.debug("run_for_component: config missing baseline.start_date — using 1-year lookback")
    fetch_start = min(baseline_start, today - pd.Timedelta(days=365))

    logger.info(
        "run_for_component: component=%s  n_runs=%d  fetch=%s->%s",
        completed_component, n_runs, fetch_start, today,
    )

    raw = fetch_all(config, start_date=fetch_start, end_date=today)

    ps_cols  = config["parameters"].get("prepared_sand",       [])
    con_cols = config["parameters"].get("consumption",         [])
    add_cols = config["parameters"].get("additive",            [])
    pse_cols = config["parameters"].get("prepared_sand_extra", [])

    df_runs, baseline_df = build_component_run_dataset(
        raw          = raw,
        config       = config,
        ps_cols      = ps_cols,
        con_cols     = con_cols,
        add_cols     = add_cols,
        pse_cols     = pse_cols,
        component_id = completed_component,
        n_runs       = n_runs,
        df_rejection = df_rejection,
    )

    if df_runs.empty:
        logger.warning("run_for_component: no data for component=%s", completed_component)
        return None

    # Use good-period baseline for engine reference.
    # When no rejection data is available baseline_df == df_runs (same content),
    # which means engines score against themselves — scores will be near-zero and
    # unreliable. Log a warning so operators know the result has low confidence.
    if not baseline_df.empty and len(baseline_df) >= 3:
        baseline_ref = baseline_df
    else:
        baseline_ref = df_runs
        logger.warning(
            "run_for_component: component=%s has insufficient good-period history "
            "(%d baseline rows) — SI scores have low confidence",
            completed_component, len(baseline_df),
        )

    param_cols    = get_param_columns(df_runs)
    si_param_cols = _filter_si_param_cols(param_cols, config)
    _param_w      = ({k: float(v) for k, v in config.get("si_param_weights", {}).items()
                      if not k.startswith("_") and isinstance(v, (int, float))} or None)

    # ── Variance engine (last N occurrences vs good-period spread) ─────────
    var_df   = compute_variance_scores(df_runs, baseline_ref, si_param_cols, config)
    var_risk = aggregate_variance_score(var_df, param_weights=_param_w)

    # ── Drift engine (current means vs good-period means) ──────────────────
    baseline_drft = _shift_baseline_to_optimal(baseline_ref, si_param_cols, config)
    drift_df      = compute_drift_scores(df_runs, baseline_drft, si_param_cols, config)
    drift_risk    = aggregate_drift_score(drift_df, param_weights=_param_w)

    # ── Oscillation engine (sign changes across N occurrences) ─────────────
    osc_df   = compute_oscillation_scores(df_runs, si_param_cols, config)
    osc_risk = aggregate_oscillation_score(osc_df, param_weights=_param_w)

    # ── Acceleration engine (rate of change of slope) ───────────────────────
    acc_df   = compute_acceleration_scores(drift_df, baseline_ref, si_param_cols, config)
    acc_risk = aggregate_acceleration_score(acc_df, param_weights=_param_w)

    # ── Recovery engine (persistence of instability) ────────────────────────
    rec_df   = compute_recovery_scores(var_df, drift_df, si_param_cols, config)
    rec_risk = aggregate_recovery_score(rec_df, param_weights=_param_w)

    # ── % change engine (occurrence-to-occurrence) ──────────────────────────
    pct_df = compute_pct_change(df_runs.reset_index(drop=True), param_cols, config)
    pct_df.index = df_runs.index

    # ── Control limits deviation ─────────────────────────────────────────────
    dev_cols = [c for c in param_cols if not c.startswith("con_")]
    dev_df   = compute_deviation_status(df_runs, dev_cols, config, db_limits=db_limits)

    # ── Within-component variance (add_bvar_* columns already in df_runs) ──
    # The bvar columns were computed per-occurrence in build_component_run_dataset.
    # Feed them directly as the wdv proxy (no additional engine call needed here).
    wdv_df = pd.DataFrame(index=df_runs.index)   # placeholder; bvar cols are in df_runs
    try:
        wdv_df = compute_within_component_variance(df_runs, add_cols, config)
    except Exception as exc:
        logger.debug("run_for_component: within_component_variance skipped: %s", exc)

    # ── Per-param SI ────────────────────────────────────────────────────────
    si_df       = compute_si(var_risk, drift_risk, osc_risk, config, acc_risk=acc_risk, rec_risk=rec_risk)
    param_si_df = compute_per_param_si(var_df, drift_df, osc_df, si_param_cols, config, acc_df=acc_df, rec_df=rec_df)

    # ── Build result for the LAST row (just-completed occurrence) ────────────
    target_idx = df_runs.index[-1]
    _pos_map   = {idx: pos for pos, idx in enumerate(df_runs.index)}

    result = _build_period_result(
        target_idx, df_runs, var_df, drift_df, osc_df, wdv_df,
        dev_df, pct_df, si_df, param_si_df, config, param_cols, si_param_cols,
        pos_map=_pos_map,
    )

    # Stamp the component_id on the result so callers can write the right alert row
    if result:
        result["component_id"] = completed_component
        result["n_runs_used"]  = len(df_runs)
        result["trigger"]      = "component_change"

    return result


def run_for_period(
    config:        dict,
    trigger_mode:  str,
    trigger_date:  Optional[date] = None,
    trigger_shift: Optional[str]  = None,
    db_limits:     dict | None    = None,
) -> Optional[dict]:
    """Fetch data, run all engines, return alert result for the triggered period."""
    from .pipeline.data_fetcher                 import fetch_all, fetch_control_limits
    from .pipeline.aggregator                   import build_dataset, get_baseline, get_param_columns
    from .engines.variance_engine               import compute_variance_scores, aggregate_variance_score
    from .engines.drift_engine                  import compute_drift_scores, aggregate_drift_score
    from .engines.oscillation_engine            import compute_oscillation_scores, aggregate_oscillation_score
    from .engines.acceleration_engine           import compute_acceleration_scores, aggregate_acceleration_score
    from .engines.recovery_engine               import compute_recovery_scores, aggregate_recovery_score
    from .engines.within_day_variance_engine    import compute_within_day_variance, aggregate_within_day_score
    from .engines.control_limits                import compute_deviation_status
    from .engines.pct_change_engine             import compute_pct_change
    from .engines.si_engine                     import compute_si, compute_per_param_si
    from .alert_engine                          import write_excel

    if db_limits is None:
        try:
            db_limits = fetch_control_limits(config)
        except Exception as exc:
            logger.warning("fetch_control_limits failed: %s  (using config fallback)", exc)
            db_limits = {}

    # window_size: shift/component modes use report_window (N=5).
    # Window mode uses window_mode_n (N=1) — set via build_dataset separately.
    window_size    = int(config.get("report_window") or config["engines"]["window"])
    today          = trigger_date or date.today()
    baseline_start = pd.to_datetime(config["baseline"]["start_date"]).date()

    # For window mode: limit lookback so historical rows don't dilute the signal.
    if trigger_mode == "window" and config.get("window_fetch_days"):
        _days = int(config["window_fetch_days"])
        fetch_start = today - timedelta(days=_days)
    else:
        fetch_start = min(baseline_start, today - timedelta(days=window_size * 5))

    logger.info("Fetching %s to %s  (mode=%s, shift=%s)",
                fetch_start, today, trigger_mode, trigger_shift or "-")

    raw       = fetch_all(config, start_date=fetch_start, end_date=today)
    df_merged = build_dataset(raw, config)

    if df_merged.empty:
        logger.warning("run_for_period: empty dataset for %s", today)
        return None

    # -- Merge sieve raw values for display / deviation checking --------------
    # Left join keeps NaN for shifts without measurements.
    # Scores are computed separately (last N actual, see below) -- NOT from rolling
    # window over the NaN-filled merged dataset.
    _raw_sieve = raw.get("sieve")
    if _raw_sieve is not None and not _raw_sieve.empty:
        from .pipeline.data_fetcher import build_sieve_shift_dataset
        _sv_pivot = build_sieve_shift_dataset(
            _raw_sieve, band_filter=config.get("sv_params")
        )
        if not _sv_pivot.empty:
            _mk = ["date", "shift"] if "shift" in df_merged.columns else ["date"]
            # Normalise date column types — window mode produces Python date objects
            # while build_sieve_shift_dataset produces datetime64; align both to date.
            for _tdf in [df_merged, _sv_pivot]:
                if "date" in _tdf.columns:
                    try:
                        _tdf["date"] = pd.to_datetime(_tdf["date"]).dt.date
                    except Exception:
                        pass
            df_merged = pd.merge(df_merged, _sv_pivot, on=_mk, how="left")
            logger.info("Sieve raw values added to df_merged: %s",
                        [c for c in _sv_pivot.columns if c.startswith("sv_")])

    target_idx = _find_target_row(df_merged, trigger_mode, today, trigger_shift)
    if target_idx is None:
        logger.warning("run_for_period: no matching row for date=%s shift=%s", today, trigger_shift)
        return None

    get_baseline(df_merged, config)
    param_cols    = get_param_columns(df_merged)
    si_param_cols = _filter_si_param_cols(param_cols, config)

    # Window mode: prepared sand only — sieve and additive excluded entirely.
    if trigger_mode == "window":
        si_param_cols = [c for c in si_param_cols if c.startswith("ps_")]

    # Split: sv_ scored with last-N-actual; ps_/add_ scored with rolling window
    _sv_cols  = [] if trigger_mode == "window" else [c for c in si_param_cols if c.startswith("sv_")]
    _ps_cols  = [c for c in si_param_cols if not c.startswith("sv_")]

    baseline_df   = _fetch_clean_baseline(config)
    import copy as _copy_cfg
    config = _copy_cfg.deepcopy(config)
    _dyn_opt_var = _compute_optimal_variance(baseline_df, _ps_cols, config)
    if _dyn_opt_var:
        config["optimal_variance"] = _dyn_opt_var
        logger.info("optimal_variance computed from baseline: %d params", len(_dyn_opt_var))

    baseline_df_drft = _shift_baseline_to_optimal(baseline_df, _ps_cols, config)

    _param_w = ({k: float(v) for k, v in config.get("si_param_weights", {}).items()
                 if not k.startswith("_") and isinstance(v, (int, float))} or None)

    # Main engines -- ps_ and add_ columns only (sieve handled separately below)
    var_df      = compute_variance_scores(df_merged, baseline_df,      _ps_cols, config)
    var_risk    = aggregate_variance_score(var_df,   param_weights=_param_w)
    drift_df    = compute_drift_scores(   df_merged, baseline_df_drft, _ps_cols, config)
    drift_risk  = aggregate_drift_score(drift_df,    param_weights=_param_w)
    osc_df      = compute_oscillation_scores(df_merged, _ps_cols, config)
    osc_risk    = aggregate_oscillation_score(osc_df,  param_weights=_param_w)
    acc_df      = compute_acceleration_scores(drift_df, baseline_df, _ps_cols, config)
    acc_risk    = aggregate_acceleration_score(acc_df,  param_weights=_param_w)
    rec_df      = compute_recovery_scores(var_df, drift_df, _ps_cols, config)
    rec_risk    = aggregate_recovery_score(rec_df,      param_weights=_param_w)
    wdv_df      = compute_within_day_variance(df_merged, _ps_cols, config, baseline_df)
    dev_cols_full = [c for c in param_cols if not c.startswith("con_")]
    dev_df        = compute_deviation_status(df_merged, dev_cols_full, config, db_limits=db_limits)
    _mode_full    = config.get("aggregation", {}).get("mode", "shift")
    if _mode_full == "component" and "component_id" in df_merged.columns:
        _sort_k = ["date", "shift"] if "shift" in df_merged.columns else ["date"]
        _pf     = [compute_pct_change(_df_c.sort_values(_sort_k), param_cols, config)
                   for _, _df_c in df_merged.groupby("component_id")]
        pct_df  = pd.concat(_pf).reindex(df_merged.index) if _pf else pd.DataFrame(index=df_merged.index)
    elif _mode_full == "window":
        # Window mode: rows are already ordered oldest->newest by slot position.
        # Compute % change across the slot sequence directly (no sort or grouping needed).
        pct_df = compute_pct_change(df_merged.reset_index(drop=True), param_cols, config)
        pct_df.index = df_merged.index
    else:
        pct_df  = compute_pct_change(df_merged, param_cols, config)
    si_df       = compute_si(var_risk, drift_risk, osc_risk, config, acc_risk=acc_risk, rec_risk=rec_risk)
    # Use _ps_cols here (not si_param_cols) — sv_ columns are not yet in var_df/drift_df/osc_df.
    # They are scored separately below and appended to avoid duplicate zero-filled sv_ columns.
    param_si_df = compute_per_param_si(var_df, drift_df, osc_df, _ps_cols, config, acc_df=acc_df, rec_df=rec_df)

    # Window mode: replace SI-based alerting with sigma-band alerting.
    # Mean/std are anchored to the baseline period (known-good reference).
    # Bands: |z|<=1 STABLE, |z|<=2 ALERT, |z|>2 CRITICAL.
    # !! WARNING is still applied downstream by _build_period_result()
    # when % change threshold or LCL/UCL deviation is detected.
    _sigma_baseline_means: dict = {}
    if trigger_mode == "window":
        from .engines.si_engine import compute_sigma_alert
        sigma_df = compute_sigma_alert(df_merged, baseline_df, si_param_cols, config)
        si_df["si_score_100"] = sigma_df["sigma_score_100"]
        si_df["si_alert"]     = sigma_df["sigma_alert"]
        for col in si_param_cols:
            if f"sigma_label_{col}" in sigma_df.columns:
                param_si_df[f"si_label_{col}"] = sigma_df[f"sigma_label_{col}"].values
            if f"sigma_{col}" in sigma_df.columns:
                param_si_df[f"si_param_{col}"] = (
                    sigma_df[f"sigma_{col}"].abs().fillna(0.0)
                    .clip(upper=3.0) / 3.0 * 100.0
                ).round(2).values
            mean_col = f"sigma_mean_{col}"
            if mean_col in sigma_df.columns:
                bare = col[3:] if col.startswith("ps_") else col
                _sigma_baseline_means[bare] = float(sigma_df[mean_col].iloc[0])
        logger.info(
            "Window sigma alert: score=%.1f  alert=%s",
            float(sigma_df["sigma_score_100"].iloc[-1]),
            sigma_df["sigma_alert"].iloc[-1],
        )

    # Per-param SI for additive cols -- display only, NOT in overall aggregate.
    # Skipped in window mode (prepared sand only).
    import warnings as _warnings
    add_param_cols = [] if trigger_mode == "window" else _get_add_param_cols(param_cols, config)
    if add_param_cols:
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", RuntimeWarning)
            _var_add   = compute_variance_scores(df_merged, baseline_df, add_param_cols, config)
            _drift_add = compute_drift_scores(df_merged, baseline_df, add_param_cols, config)
            _osc_add   = compute_oscillation_scores(df_merged, add_param_cols, config)
            _psi_add   = compute_per_param_si(_var_add, _drift_add, _osc_add, add_param_cols, config)
        var_df      = pd.concat([var_df,      _var_add],  axis=1)
        drift_df    = pd.concat([drift_df,    _drift_add], axis=1)
        osc_df      = pd.concat([osc_df,      _osc_add],  axis=1)
        param_si_df = pd.concat([param_si_df, _psi_add],  axis=1)
    # -- Sieve -- last N actual measurements (no NaN, no rolling-window gaps) --
    if _sv_cols and _raw_sieve is not None and not _raw_sieve.empty:
        _sv_var, _sv_drift, _sv_osc = _compute_sieve_scores_last_n(
            _raw_sieve, _sv_cols, config, df_merged.index
        )
        _sv_psi = compute_per_param_si(_sv_var, _sv_drift, _sv_osc, _sv_cols, config)
        var_df      = pd.concat([var_df,      _sv_var],   axis=1)
        drift_df    = pd.concat([drift_df,    _sv_drift], axis=1)
        osc_df      = pd.concat([osc_df,      _sv_osc],   axis=1)
        param_si_df = pd.concat([param_si_df, _sv_psi],   axis=1)

    si_display_cols = si_param_cols + add_param_cols

    _pos_map = {idx: pos for pos, idx in enumerate(df_merged.index)}
    result = _build_period_result(
        target_idx, df_merged, var_df, drift_df, osc_df, wdv_df,
        dev_df, pct_df, si_df, param_si_df, config, param_cols, si_display_cols,
        pos_map=_pos_map,
    )

    if _sigma_baseline_means:
        result["baseline_means"] = _sigma_baseline_means
    if result:
        result["trigger_mode"] = trigger_mode

    if config["output"].get("save_excel", True):
        all_results = [
            _build_period_result(i, df_merged, var_df, drift_df, osc_df, wdv_df,
                                 dev_df, pct_df, si_df, param_si_df, config,
                                 param_cols, si_display_cols, pos_map=_pos_map)
            for i in df_merged.index
        ]
        try:
            path = write_excel(
                all_results, df_merged, var_df, drift_df,
                osc_df, dev_df, pct_df, si_df, param_si_df, config,
                db_limits=db_limits,
            )
            logger.info("Excel updated: %s", path)
        except Exception as exc:
            logger.warning("Excel write failed: %s", exc)

    return result


def _build_period_result(
    idx:           int,
    df_merged:     pd.DataFrame,
    var_df:        pd.DataFrame,
    drift_df:      pd.DataFrame,
    osc_df:        pd.DataFrame,
    wdv_df:        pd.DataFrame,
    dev_df:        pd.DataFrame,
    pct_df:        pd.DataFrame,
    si_df:         pd.DataFrame,
    param_si_df:   pd.DataFrame,
    config:        dict,
    param_cols:    list[str],
    si_param_cols: list[str],
    wcv_df:        pd.DataFrame = None,
    pos_map:       dict = None,
) -> dict:
    from .alert_engine  import build_root_cause, build_recommendation
    from .llm_analysis  import build_llm_analysis

    raw_vals     = {c: _safe_float(df_merged.at[idx, c])
                    for c in param_cols if c in df_merged.columns}
    var_labels   = {c: var_df.at[idx, f"var_label_{c}"]
                    for c in si_param_cols if f"var_label_{c}" in var_df.columns}
    var_scores   = {c: _safe_float(var_df.at[idx, f"var_score_{c}"])
                    for c in si_param_cols if f"var_score_{c}" in var_df.columns}
    drift_labels = {c: drift_df.at[idx, f"drift_label_{c}"]
                    for c in si_param_cols if f"drift_label_{c}" in drift_df.columns}
    drift_scores = {c: _safe_float(drift_df.at[idx, f"drift_score_{c}"])
                    for c in si_param_cols if f"drift_score_{c}" in drift_df.columns}
    osc_labels   = {c: osc_df.at[idx, f"osc_label_{c}"]
                    for c in si_param_cols if f"osc_label_{c}" in osc_df.columns}
    osc_scores   = {c: _safe_float(osc_df.at[idx, f"osc_score_{c}"])
                    for c in si_param_cols if f"osc_score_{c}" in osc_df.columns}
    wdv_labels   = {c: wdv_df.at[idx, f"wdv_label_{c}"]
                    for c in si_param_cols if f"wdv_label_{c}" in wdv_df.columns}
    wdv_scores   = {c: _safe_float(wdv_df.at[idx, f"wdv_score_{c}"])
                    for c in si_param_cols if f"wdv_score_{c}" in wdv_df.columns}
    # Within-component batch variance (component mode only; empty df in other modes)
    _add_cols    = [c for c in param_cols if c.startswith("add_") and not c.startswith("add_bvar_")]
    _wcv         = wcv_df if (wcv_df is not None and not wcv_df.empty) else pd.DataFrame()
    wcv_labels   = {c: _wcv.at[idx, f"wcv_label_{c}"]
                    for c in _add_cols if f"wcv_label_{c}" in _wcv.columns}
    wcv_scores   = {c: _safe_float(_wcv.at[idx, f"wcv_score_{c}"])
                    for c in _add_cols if f"wcv_score_{c}" in _wcv.columns}
    # Consumption excluded -- deviations only checked for ps_ and add_ columns
    deviations   = {c: dev_df.at[idx, f"dev_status_{c}"]
                    for c in param_cols
                    if not c.startswith("con_") and f"dev_status_{c}" in dev_df.columns}
    pct_changes  = {c: _safe_float(pct_df.at[idx, f"pct_{c}"])
                    for c in param_cols if f"pct_{c}" in pct_df.columns}
    param_si     = {c: _safe_float(param_si_df.at[idx, f"si_param_{c}"])
                    for c in si_param_cols if f"si_param_{c}" in param_si_df.columns}
    param_labels = {c: param_si_df.at[idx, f"si_label_{c}"]
                    for c in si_param_cols if f"si_label_{c}" in param_si_df.columns}

    # For prepared-sand (ps_) null columns: fill raw_vals with last available
    # value from history so the UI can display it with a "prev" indicator.
    prev_params: set = set()
    pos = pos_map[idx] if pos_map and idx in pos_map else list(df_merged.index).index(idx)
    for c in param_cols:
        if c.startswith("ps_") and raw_vals.get(c) is None and c in df_merged.columns:
            history = df_merged[c].iloc[:pos]          # all rows strictly before target
            valid   = history.dropna()
            if not valid.empty:
                raw_vals[c] = round(float(valid.iloc[-1]), 4)
                prev_params.add(c)

    # Override engine labels/scores to NO DATA for non-ps_ null parameters only.
    # ps_ columns use last-available data so their engine labels remain meaningful.
    for c in list(drift_labels):
        if raw_vals.get(c) is None and not c.startswith("ps_"):
            drift_labels[c]  = "NO DATA"
            drift_scores[c]  = 0.0
    for c in list(var_labels):
        if raw_vals.get(c) is None and not c.startswith("ps_"):
            var_labels[c]    = "NO DATA"
            var_scores[c]    = 0.0
    for c in list(osc_labels):
        if raw_vals.get(c) is None and not c.startswith("ps_"):
            osc_labels[c]    = "NO DATA"
            osc_scores[c]    = 0.0

    si_row   = si_df.loc[idx]
    alert    = si_row["si_alert"]
    pct_warn = float(config.get("pct_change_warning", 5.0))
    has_dev  = any(str(v).startswith("Deviated") for v in deviations.values())
    # Escalate to !! WARNING if ANY parameter (ps_ or add_) exceeds the % change
    # threshold -- high % change signals sudden instability regardless of SI level.
    has_pct  = any(
        isinstance(pv, float) and not (pv != pv) and abs(pv) > pct_warn
        for pv in pct_changes.values()
    )
    if has_dev or has_pct:
        alert = "!! WARNING"

    # -- Fix 1: floor composite alert when any single parameter is CRITICAL -----
    # Mean aggregation across parameters can hide a critically deviated parameter
    # (e.g. Moisture SI=100 while 4 others are 5 -> composite ≈ 24, WATCH).
    # Rule: if ≥1 parameter has a CRITICAL-level SI score, the composite alert
    # cannot stay below ALERT.  !! WARNING is already higher — never downgraded.
    _thr        = config.get("alert_thresholds", {})
    _alert_max  = float(_thr.get("alert_max", 69))
    _watch_max  = float(_thr.get("watch_max",  49))
    _param_si_vals    = [v for v in param_si.values() if v is not None]
    max_param_si      = max(_param_si_vals) if _param_si_vals else 0.0
    n_critical_params = sum(1 for v in _param_si_vals if v > _alert_max)
    n_alert_params    = sum(1 for v in _param_si_vals if v > _watch_max)
    _RANK = {"STABLE": 0, "WATCH": 1, "ALERT": 2, "CRITICAL": 3, "!! WARNING": 4}
    if n_critical_params > 0 and _RANK.get(alert, 0) < _RANK["ALERT"]:
        alert = "ALERT"

    r = {
        "period_key"        : df_merged.at[idx, "period_key"],
        "date"              : df_merged.at[idx, "date"],
        "shift"             : df_merged.at[idx, "shift"]        if "shift"        in df_merged.columns else None,
        "component_id"      : df_merged.at[idx, "component_id"] if "component_id" in df_merged.columns else None,
        "si_score_100"      : float(si_row["si_score_100"]),
        "si_alert"          : si_row["si_alert"],
        "final_alert"       : alert,
        "max_param_si"      : round(max_param_si, 2),
        "n_critical_params" : n_critical_params,
        "n_alert_params"    : n_alert_params,
        "raw_values"   : raw_vals,
        "prev_params"  : prev_params,
        "var_labels"   : var_labels,
        "var_scores"   : var_scores,
        "drift_labels" : drift_labels,
        "drift_scores" : drift_scores,
        "osc_labels"   : osc_labels,
        "osc_scores"   : osc_scores,
        "wdv_labels"   : wdv_labels,
        "wdv_scores"   : wdv_scores,
        "wcv_labels"   : wcv_labels,
        "wcv_scores"   : wcv_scores,
        "deviations"   : deviations,
        "pct_changes"  : pct_changes,
        "param_si"     : param_si,
        "param_labels" : param_labels,
    }

    analysis            = build_llm_analysis(r, config)
    r["root_cause"]     = analysis["root_cause"]
    r["recommendation"] = analysis["recommendation"]
    return r


def _find_target_row(
    df: pd.DataFrame,
    mode: str,
    today: date,
    shift: Optional[str],
) -> Optional[int]:
    if mode == "day":
        mask = df["date"] == today
    elif mode in ("shift", "component") and shift:
        mask = (df["date"] == today) & (df["shift"] == shift.strip())
    elif mode == "window":
        # Window dataset is already restricted to last N slots — always target the newest slot
        return df.index[-1] if len(df) > 0 else None
    else:
        # continuous / fallback — use the last available row
        return df.index[-1] if len(df) > 0 else None

    matches = df[mask]
    if matches.empty:
        logger.warning(
            "_find_target_row: no row found for date=%s shift=%s mode=%s -- skipping period",
            today, shift, mode,
        )
        return None
    # For component mode, return the last component row for that (date, shift)
    return matches.index[-1]


def _log_summary(si_df: pd.DataFrame, all_results: list[dict]) -> None:
    counts = {}
    for r in all_results:
        lbl = r.get("final_alert", "STABLE")
        counts[lbl] = counts.get(lbl, 0) + 1

    scores = si_df["si_score_100"].values
    n      = len(all_results)

    logger.info("-" * 55)
    logger.info("  HISTORY SUMMARY   %d periods", n)
    logger.info("  SI  min=%.1f  max=%.1f  mean=%.1f",
                scores.min(), scores.max(), scores.mean())
    for lbl in ("STABLE", "WATCH", "ALERT", "CRITICAL", "!! WARNING"):
        cnt = counts.get(lbl, 0)
        if cnt:
            logger.info("  %-12s : %d  (%.1f%%)", lbl, cnt, cnt / n * 100)
    logger.info("-" * 55)


def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        return None if f != f else round(f, 4)
    except (TypeError, ValueError):
        return None


def _filter_si_param_cols(param_cols: list[str], config: dict) -> list[str]:
    """
    Return the subset of param_cols that should feed the SI score.

    Includes:
      - ps_  columns listed in config.si_params.params (or all ps_ if unconfigured)
      - sv_  columns whose band matches config.sv_params (e.g. ["Fines", "gfnAfs"])
             All sand-type variants (ps, ns, cs) for each selected band are included.

    Consumption (con_) and additive (add_) are always excluded from SI aggregate.
    """
    import re as _re

    # -- Prepared sand ---------------------------------------------------------
    allowed_bare = config.get("si_params", {}).get("params", [])
    if allowed_bare:
        allowed_prefixed = {f"ps_{p}" for p in allowed_bare}
        ps_cols = [c for c in param_cols if c in allowed_prefixed]
    else:
        ps_cols = [c for c in param_cols if c.startswith("ps_")]

    # -- Sieve -----------------------------------------------------------------
    sv_params = config.get("sv_params", [])   # e.g. ["Fines", "gfnAfs"]
    sv_cols: list[str] = []
    if sv_params:
        # Normalise configured band names to snake_case for matching
        # "Fines" -> "fines",  "gfnAfs" -> "gfn_afs"
        from .pipeline.data_fetcher import _BAND_SNAKE as _BS
        norm_bands = {
            _BS.get(b, _re.sub(r"[^a-z0-9]+", "_", b.lower().strip()))
            for b in sv_params
        }
        for col in param_cols:
            if not col.startswith("sv_"):
                continue
            # col format: sv_{sand_short}_{band_snake}
            # Extract band part (everything after sv_{2chars}_)
            parts = col.split("_", 2)   # ["sv", "ps", "fines"] or ["sv", "ps", "gfn_afs"]
            if len(parts) >= 3:
                band_part = parts[2]
                if band_part in norm_bands:
                    sv_cols.append(col)

    return ps_cols + sv_cols


def _get_add_param_cols(param_cols: list[str], config: dict) -> list[str]:
    """
    Return add_-prefixed param cols, minus any listed in config.additive_exclude.
    These get their own per-param SI (display only) but do NOT feed the aggregate SI.
    Excludes add_bvar_* columns (batch-variance internals injected by the aggregator).
    """
    excluded = {f"add_{p}" for p in config.get("additive_exclude", [])}
    return [
        c for c in param_cols
        if c.startswith("add_")
        and not c.startswith("add_bvar_")
        and c not in excluded
    ]


def _fetch_clean_baseline(config: dict) -> pd.DataFrame:
    """Fetch the July baseline period from DB in isolation so imputation stays within that period.

    When baseline rows are extracted from a df_merged that spans Jul 2025-Jun 2026, missing
    values get filled with the latest June 2026 value, corrupting b_std used by the drift engine.
    Fetching the baseline standalone ensures NaNs are filled with Jul 2025 values only.
    """
    from .pipeline.data_fetcher import fetch_all
    from .pipeline.aggregator   import build_dataset, get_baseline

    b_start = config["baseline"]["start_date"]
    b_end   = config["baseline"]["end_date"]
    try:
        raw_b      = fetch_all(config, start_date=b_start, end_date=b_end)
        df_b       = build_dataset(raw_b, config)
        baseline   = get_baseline(df_b, config)
        logger.info("Baseline fetched separately: %d rows (%s - %s)", len(baseline), b_start, b_end)
        return baseline
    except Exception as exc:
        logger.warning("Separate baseline fetch failed (%s) -- falling back to in-window baseline", exc)
        return pd.DataFrame()


def _compute_sieve_scores_last_n(
    raw_sieve:  pd.DataFrame,
    sv_cols:    list[str],
    config:     dict,
    df_index,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Compute variance / drift / oscillation scores for sieve columns using
    the last N actual measurements -- ignoring NaN rows regardless of date,
    shift, or component_id.

    raw_sieve : raw rows from fetch_sieve_data  (date, shift, sand_type, band_type, value)
    sv_cols   : sieve column names already filtered to config sv_params
                e.g. ["sv_ps_fines", "sv_ns_gfn_afs", ...]
    df_index  : index of the main df_merged (scores are broadcast to the last row)

    Returns (var_df, drift_df, osc_df) indexed by df_index.
    All rows carry the latest sieve score so the target period picks it up.
    """
    from .pipeline.data_fetcher import build_sieve_shift_dataset, _BAND_SNAKE
    from .engines.variance_engine    import compute_variance_scores
    from .engines.drift_engine       import compute_drift_scores
    from .engines.oscillation_engine import compute_oscillation_scores

    empty = pd.DataFrame(index=df_index)
    if raw_sieve is None or raw_sieve.empty or not sv_cols:
        return empty, empty, empty

    window = int(config["engines"]["window"])

    # Band filter from sv_cols names -- extract band_snake from sv_{sand}_{band}
    _bands = set()
    for col in sv_cols:
        parts = col.split("_", 2)   # ["sv", "ps", "fines"] or ["sv", "ps", "gfn_afs"]
        if len(parts) >= 3:
            # Reverse-map band_snake to original band name for filtering
            _bands.add(parts[2])
    # Use snake names directly as filter (build_sieve_shift_dataset matches band_snake)
    sv_pivot = build_sieve_shift_dataset(raw_sieve, band_filter=list(_bands) if _bands else None)
    if sv_pivot.empty:
        return empty, empty, empty

    # Keep only the requested sv_cols
    avail = [c for c in sv_cols if c in sv_pivot.columns]
    if not avail:
        return empty, empty, empty

    # Dense dataset: all rows are actual measurements (no NaN from pivot)
    sv_dense = sv_pivot[avail].copy().reset_index(drop=True)

    # Last N actual measurements -- no NaN, no date/shift filtering
    n_avail  = len(sv_dense)
    sv_window = sv_dense.tail(window).reset_index(drop=True)
    sv_base   = sv_dense                  # full history as baseline

    try:
        cfg_w  = dict(config)
        cfg_w["engines"] = dict(config["engines"])
        cfg_w["engines"]["window"] = min(window, len(sv_window))

        var_sv   = compute_variance_scores(sv_window, sv_base, avail, cfg_w)
        drift_sv = compute_drift_scores(   sv_window, sv_base, avail, cfg_w)
        osc_sv   = compute_oscillation_scores(sv_window, avail, cfg_w)
    except Exception as exc:
        logger.warning("_compute_sieve_scores_last_n: engine error -- %s", exc)
        return empty, empty, empty

    # Last row of the window = score for the current (latest) sieve measurement
    # Broadcast to ALL rows in df_index so _build_period_result can pick it up
    def _broadcast(score_df):
        out = pd.DataFrame(
            np.nan, index=df_index, columns=score_df.columns, dtype=object
        )
        last_row = score_df.iloc[-1]
        for col in score_df.columns:
            out[col] = last_row[col]
        return out

    logger.info(
        "Sieve (last %d of %d actual measurements): %d cols",
        min(window, n_avail), n_avail, len(avail),
    )
    return _broadcast(var_sv), _broadcast(drift_sv), _broadcast(osc_sv)


def _compute_optimal_variance(
    baseline_df: pd.DataFrame,
    param_cols:  list[str],
    config:      dict,
) -> dict:
    """
    Dynamically compute the optimal_variance target for each parameter from
    the baseline period data.

    Method (matches Excel / config comment):
      For each parameter column:
        1. Compute rolling VAR.P over the baseline rows using the engine window.
        2. Take the 25th percentile of those rolling variance values.
        3. That value becomes the reference variance for the Variance engine.

    Returns {bare_param_name: variance_value}.
    Returns an empty dict if baseline_df is empty (engine will use its own fallback).
    """
    from numpy.lib.stride_tricks import sliding_window_view as _swv

    if baseline_df is None or baseline_df.empty:
        return {}

    window = int(config["engines"]["window"])
    result: dict = {}

    for col in param_cols:
        if col not in baseline_df.columns:
            continue

        arr   = pd.to_numeric(baseline_df[col], errors="coerce").values.astype(float)
        valid = arr[~np.isnan(arr)]

        if len(valid) < window:
            val = float(np.nanvar(valid, ddof=0)) if len(valid) > 1 else None
        else:
            windows   = _swv(valid, window)
            roll_vars = np.nanvar(windows, axis=1, ddof=0)
            good      = roll_vars[~np.isnan(roll_vars)]
            val = float(np.percentile(good, 25)) if len(good) > 0 else None

        bare = _bare_name(col)
        if val is not None and val > 1e-12:
            result[bare] = round(val, 8)

    return result


def _bare_name(col: str) -> str:
    for p in ("ps_", "con_", "add_", "pse_", "sv_"):
        if col.startswith(p):
            return col[len(p):]
    return col


def _shift_baseline_to_optimal(
    baseline_df: pd.DataFrame,
    param_cols:  list[str],
    config:      dict,
) -> pd.DataFrame:
    """Shift each column's baseline mean to its optimal_values target.

    Matches generate_si_report.py behaviour: drift deviations are measured
    relative to the optimal target, not the actual July baseline mean.
    Only applied to ps_ columns that have an entry in config.optimal_values.
    """
    opt_vals = config.get("optimal_values", {})
    if not opt_vals or baseline_df is None or baseline_df.empty:
        return baseline_df

    df = baseline_df.copy()
    _TPFX = ("ps_", "con_", "add_", "pse_", "sv_")

    for col in param_cols:
        if not col.startswith("ps_"):
            continue
        bare = col[3:]
        opt = opt_vals.get(bare)
        if opt is None or col not in df.columns:
            continue
        opt = float(opt)
        arr = pd.to_numeric(df[col], errors="coerce").values.astype(float)
        cur_mean = float(np.nanmean(arr))
        if np.isnan(cur_mean) or abs(cur_mean) < 1e-12:
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce") + (opt - cur_mean)

    return df
