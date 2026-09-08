"""
engines/within_day_variance_engine.py
---------------------------------------
Within-Day Variance Engine.

For each shift row, computes VAR.P of each parameter across ALL shifts of the
same calendar day.  This captures intra-day instability -- how much values swing
between shifts within a single day, independent of the rolling-window trend.

Algorithm
---------
  wday_var  = VAR.P( all shift values on the same date )
  ratio     = (wday_var - baseline_var) / baseline_var
              Relative excess above baseline. 0 = at baseline; 5 = 6x baseline.
  score     = clamp( ratio / 5.0, 0, 1 )
  Labels    : score-band based  (STABLE<0.35  WATCH<0.60  ELEVATED<0.85  HIGH VAR>=0.85)
"""

import logging
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

logger = logging.getLogger(__name__)


def compute_within_day_variance(
    df:             pd.DataFrame,
    param_cols:     list[str],
    config:         dict,
    baseline_df_var: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Returns DataFrame with wdv_var / wdv_ratio / wdv_score / wdv_label columns.
    Requires a 'date' column in df.
    """
    v_cfg = config["engines"].get("variance", {})
    hv_r  = float(v_cfg.get("high_var_ratio", 5.0))   # (6x-1) = 5 with new (x-a)/a formula
    sc_r  = float(v_cfg.get("score_divisor",  hv_r))
    lbl_watch    = float(v_cfg.get("score_watch_max",    0.35))
    lbl_elevated = float(v_cfg.get("score_elevated_max", 0.60))
    lbl_high_var = float(v_cfg.get("score_high_var_min", 0.85))
    window = int(config["engines"]["window"])

    result = {}

    if "date" not in df.columns:
        logger.warning("No 'date' column -- within-day variance skipped")
        for col in param_cols:
            result[f"wdv_var_{col}"]   = np.full(len(df), np.nan)
            result[f"wdv_ratio_{col}"] = np.full(len(df), np.nan)
            result[f"wdv_score_{col}"] = np.zeros(len(df))
            result[f"wdv_label_{col}"] = np.full(len(df), "NO DATA", dtype=object)
        return pd.DataFrame(result, index=df.index)

    dates = df["date"].values

    for col in param_cols:
        if col not in df.columns:
            continue

        vals = pd.to_numeric(df[col], errors="coerce").values.astype(float)

        # -- Within-day VAR.P per row ------------------------------------------
        wdv = np.full(len(df), np.nan)
        for d in pd.unique(dates):
            mask      = dates == d
            day_vals  = vals[mask]
            valid     = day_vals[~np.isnan(day_vals)]
            if len(valid) >= 2:
                wdv[mask] = float(np.var(valid, ddof=0))
            elif len(valid) == 1:
                wdv[mask] = 0.0
            # else: stays NaN (no data for this day)

        # -- Baseline var (same optimal-target as rolling engine) --------------
        baseline_var = _baseline_var(baseline_df_var, col, window)

        # -- Ratio / score / labels --------------------------------------------
        ratio  = np.where(np.isnan(wdv), np.nan,
                          (wdv - baseline_var) / baseline_var)
        scores = np.where(np.isnan(ratio), 0.0,
                          np.clip(ratio / sc_r, 0.0, 1.0))
        labels = np.where(np.isnan(ratio), "STABLE",
                 np.where(scores >= lbl_high_var, "HIGH VAR",
                 np.where(scores >= lbl_elevated, "ELEVATED",
                 np.where(scores >= lbl_watch,    "WATCH",
                          "STABLE")))).astype(object)

        result[f"wdv_var_{col}"]   = np.round(wdv,    6)
        result[f"wdv_ratio_{col}"] = np.round(ratio,  4)
        result[f"wdv_score_{col}"] = np.round(scores, 4)
        result[f"wdv_label_{col}"] = labels

    return pd.DataFrame(result, index=df.index)


def aggregate_within_day_score(wdv_df: pd.DataFrame, param_weights: dict = None) -> pd.Series:
    """Weighted mean of all wdv_score_* columns -> single per-period within-day variance risk.

    param_weights: bare-name -> weight dict. None -> equal-weight mean.
    """
    score_cols = [c for c in wdv_df.columns if c.startswith("wdv_score_")]
    if not score_cols:
        return pd.Series(0.0, index=wdv_df.index)
    mat = np.where(np.isnan(wdv_df[score_cols].values.astype(float)), 0.0,
                   wdv_df[score_cols].values.astype(float))
    return pd.Series(np.round(_weighted_agg(mat, score_cols, len("wdv_score_"), param_weights), 4),
                     index=wdv_df.index)


def _weighted_agg(mat: np.ndarray, cols: list, prefix_len: int, param_weights: dict = None) -> np.ndarray:
    _TPFX = ("ps_", "con_", "add_", "pse_", "sv_")
    if not param_weights:
        return mat.mean(axis=1)

    def _bare(col):
        name = col[prefix_len:]
        for p in _TPFX:
            if name.startswith(p):
                return name[len(p):]
        return name

    w = np.array([float(param_weights.get(_bare(c), 1.0)) for c in cols])
    total = w.sum()
    if total < 1e-10:
        return mat.mean(axis=1)
    w = w / total
    return mat @ w


def _baseline_var(baseline_df_var, col, window):
    """Mean of rolling window VAR.P over baseline (mirrors variance_engine logic)."""
    if (baseline_df_var is not None
            and col in baseline_df_var.columns
            and len(baseline_df_var) > 1):
        b = pd.to_numeric(baseline_df_var[col], errors="coerce").fillna(0).values.astype(float)
        if len(b) >= window:
            wins   = sliding_window_view(b, window)
            rvars  = np.nanvar(wins, axis=1, ddof=0)
            valid  = rvars[~np.isnan(rvars)]
            bv     = float(np.nanmean(valid)) if len(valid) > 0 else 0.0
        else:
            bv = float(np.nanvar(b, ddof=0))
    else:
        bv = 0.0
    return max(bv, 1e-12)
