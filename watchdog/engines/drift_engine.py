"""
engines/drift_engine.py
------------------------
Drift Detection Engine -- matches si_may2026_v2.xlsx Drift tab logic.

Algorithm
---------
  MEAN-SHIFT:
    deviation = |rolling_mean - baseline_mean| / baseline_std  (? units)
    score_ms  = clip(deviation / strong_sigma, 0, 1)
    Excel: strong_sigma = 2  ->  score = 1 when deviation = 2?

  SLOPE / TREND:
    slope     = OLS regression slope over rolling window N  (units / period)
    score_sl  = clip(|slope| / (slope_sigma x baseline_std), 0, 1)
    Excel: slope_sigma = 0.5  ->  score = 1 when |slope| = 0.5 x baseline_std

  COMBINED (feeds SI):
    drift_score = max(score_ms, score_sl)

  Labels (priority order):
    deviation ? strong_sigma              -> STRONG DRIFT
    deviation ? slight_sigma              -> SLIGHT DRIFT
    score_sl  ? 1.0  (no mean-shift yet)  -> STRONG TREND
    score_sl  ? 0.5  (no mean-shift yet)  -> SLIGHT TREND
    otherwise                             -> STABLE

  Thresholds (watchdog_config.json -> engines.drift): slight=1.0?, strong=2.0?, slope_sigma=0.5
"""

import logging
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

logger = logging.getLogger(__name__)


def compute_drift_scores(
    df:          pd.DataFrame,
    baseline_df: pd.DataFrame,
    param_cols:  list[str],
    config:      dict,
) -> pd.DataFrame:
    """
    Compute rolling drift scores for all parameters.

    Returns DataFrame with columns per parameter:
      drift_mean_{col}        rolling mean (window N)
      drift_dev_{col}         mean-shift deviation in ? units
      drift_slope_{col}       OLS slope (units/shift) over rolling window
      drift_slope_score_{col} normalised slope risk (0-1)
      drift_label_{col}       STABLE | SLIGHT TREND | STRONG TREND |
                              SLIGHT DRIFT | STRONG DRIFT
      drift_score_{col}       max(mean-shift score, slope score) -> feeds SI
    """
    window   = config["engines"]["window"]
    d_cfg    = config["engines"].get("drift", {})
    slight_k = float(d_cfg.get("slight_sigma", 1.0))
    strong_k = float(d_cfg.get("strong_sigma", 3.0))
    slope_k  = float(d_cfg.get("slope_sigma",  slight_k))  # separate multiplier for slope normalisation
    # Trend score thresholds (configurable)
    slight_trend_k = float(d_cfg.get("slight_trend_score", 0.5))
    strong_trend_k = float(d_cfg.get("strong_trend_score", 1.0))
    # Configurable label strings (per-foundry overrideable)
    _d_lbls            = d_cfg.get("labels", {})
    _lbl_d_stable      = _d_lbls.get("stable",       "STABLE")
    _lbl_slight_drift  = _d_lbls.get("slight_drift",  "SLIGHT DRIFT")
    _lbl_strong_drift  = _d_lbls.get("strong_drift",  "STRONG DRIFT")
    _lbl_slight_trend  = _d_lbls.get("slight_trend",  "SLIGHT TREND")
    _lbl_strong_trend  = _d_lbls.get("strong_trend",  "STRONG TREND")

    result = {}

    for col in param_cols:
        if col not in df.columns:
            continue

        # Track which rows have no actual data before imputation
        raw_series = pd.to_numeric(df[col], errors="coerce")
        no_data_mask = raw_series.isna().values  # True where original value is NULL

        x = _to_float(df[col])

        if baseline_df is not None and col in baseline_df.columns and len(baseline_df) > 0:
            b = _to_float(baseline_df[col])
        else:
            b = x

        b_mean = float(np.nanmean(b))
        b_std  = float(np.nanstd(b, ddof=0))
        if b_std < 1e-9:
            b_std = 1e-9

        # -- Mean-shift score --------------------------------------------------
        roll_mean = _rolling_mean(x, window)
        deviation = np.where(
            np.isnan(roll_mean),
            np.nan,
            np.abs(roll_mean - b_mean) / b_std,
        )
        score_ms = np.where(
            np.isnan(deviation),
            0.0,
            np.clip(deviation / strong_k, 0.0, 1.0),
        )

        # -- Slope score -------------------------------------------------------
        # Excel: score = clip(|slope| / (slope_sigma x baseline_std), 0, 1)
        slope            = _rolling_slope(x, window)
        concerning_slope = slope_k * b_std
        score_sl = np.where(
            np.isnan(slope),
            0.0,
            np.clip(np.abs(slope) / concerning_slope, 0.0, 1.0),
        )

        # -- Combined score ----------------------------------------------------
        combined = np.maximum(score_ms, score_sl)

        # -- Labels (mean-shift takes priority) --------------------------------
        labels = np.where(np.isnan(deviation), _lbl_d_stable,
                 np.where(deviation >= strong_k,      _lbl_strong_drift,
                 np.where(deviation >= slight_k,      _lbl_slight_drift,
                 np.where(score_sl  >= strong_trend_k, _lbl_strong_trend,
                 np.where(score_sl  >= slight_trend_k, _lbl_slight_trend,
                          _lbl_d_stable))))).astype(object)

        # Override rows where the original value was NULL -> NO DATA
        labels[no_data_mask]   = "NO DATA"
        combined[no_data_mask] = 0.0

        result[f"drift_mean_{col}"]        = np.round(roll_mean, 4)
        result[f"drift_dev_{col}"]         = np.round(deviation, 4)
        result[f"drift_slope_{col}"]       = np.round(slope, 6)
        result[f"drift_slope_score_{col}"] = np.round(score_sl, 4)
        result[f"drift_label_{col}"]       = labels
        result[f"drift_score_{col}"]       = np.round(combined, 4)

    return pd.DataFrame(result, index=df.index)


def aggregate_drift_score(drift_df: pd.DataFrame, param_weights: dict = None) -> pd.Series:
    """Weighted mean of all drift_score_* columns -> single per-period drift risk (0-1).

    param_weights: bare-name -> weight dict. None -> equal-weight mean.
    """
    score_cols = [c for c in drift_df.columns if c.startswith("drift_score_")]
    if not score_cols:
        return pd.Series(0.0, index=drift_df.index)
    mat = np.where(np.isnan(drift_df[score_cols].values.astype(float)), 0.0,
                   drift_df[score_cols].values.astype(float))
    return pd.Series(np.round(_weighted_agg(mat, score_cols, len("drift_score_"), param_weights), 4),
                     index=drift_df.index)


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


# -- Rolling helpers -----------------------------------------------------------

def _rolling_mean(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        out[w - 1:] = np.nanmean(sliding_window_view(x, w), axis=1)
    return out


def _rolling_slope(x: np.ndarray, w: int) -> np.ndarray:
    """
    OLS slope over a rolling window of size w.

    For each window of w shifts:
      t   = [0, 1, 2, ... w-1]  -- shift index (X axis)
      x   = actual parameter values  (Y axis)
      tc  = t - mean(t)             -- centred index
      xc  = x - mean(x)
      slope = Sum(tc x xc) / Sum(tc?)

    Result unit: parameter-units per shift.
    First w-1 positions are NaN (not enough data yet).
    """
    out = np.full(len(x), np.nan)
    if len(x) < w:
        return out

    windows = sliding_window_view(x, w)           # (n-w+1, w)
    t       = np.arange(w, dtype=float)
    t_c     = t - t.mean()                         # centred shift index
    t_var   = float((t_c ** 2).sum())              # Sum(tc?)

    if t_var < 1e-12:
        return out

    x_means = windows.mean(axis=1, keepdims=True)  # (n-w+1, 1)
    x_c     = windows - x_means                    # (n-w+1, w)
    slopes  = (x_c * t_c).sum(axis=1) / t_var      # (n-w+1,)

    out[w - 1:] = slopes
    return out


def _to_float(series) -> np.ndarray:
    if isinstance(series, np.ndarray):
        arr = series.astype(float)
    else:
        arr = pd.to_numeric(series, errors="coerce").values.astype(float)
    mean = np.nanmean(arr)
    if np.isnan(mean):
        mean = 0.0
    return np.where(np.isnan(arr), mean, arr)
