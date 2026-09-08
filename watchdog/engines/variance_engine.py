"""
engines/variance_engine.py
---------------------------
Rolling Variance Engine -- matches si_may2026_v2.xlsx Variance tab logic.

Algorithm
---------
  baseline_var = optimal_variance[param]   (25th-pct of rolling 5-shift variances,
                                            pre-computed from baseline period, stored in config)
  ratio  = (rolling_var - baseline_var) / baseline_var
           Measures relative excess variance above baseline.
           0 = exactly at baseline; 7 = 8x baseline (maximum instability).
  score  = clip(ratio / 7.0, 0, 1)
  Labels : STABLE < 0.35 | WATCH < 0.60 | ELEVATED < 0.85 | HIGH VAR >= 0.85
"""

import logging
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

logger = logging.getLogger(__name__)


_TPFX = ("ps_", "con_", "add_", "pse_", "sv_")


def _bare_name(col: str) -> str:
    for p in _TPFX:
        if col.startswith(p):
            return col[len(p):]
    return col


def compute_variance_scores(
    df:          pd.DataFrame,
    baseline_df: pd.DataFrame,
    param_cols:  list[str],
    config:      dict,
) -> pd.DataFrame:
    """Compute rolling variance scores for all parameters.

    baseline_var comes from config['optimal_variance'][bare_param] -- the pre-computed
    25th-percentile of rolling 5-shift variances from the baseline period (matches Excel).
    Falls back to computing from baseline_df if not found in config.
    """
    window       = config["engines"]["window"]
    v_cfg        = config["engines"].get("variance", {})
    # score_divisor: ratio / divisor -> score. Default 7.0 keeps saturation at 8x baseline
    # because with formula (x-a)/a the ratio at 8x is (8a-a)/a = 7.
    hv_r         = float(v_cfg.get("high_var_ratio", 7.0))
    sc_r         = float(v_cfg.get("score_divisor", hv_r))
    lbl_watch    = float(v_cfg.get("score_watch_max",    0.35))
    lbl_elevated = float(v_cfg.get("score_elevated_max", 0.60))
    lbl_high_var = float(v_cfg.get("score_high_var_min", 0.85))
    opt_var      = config.get("optimal_variance", {})
    # Configurable label strings (per-foundry overrideable)
    _v_lbls          = v_cfg.get("labels", {})
    _lbl_v_stable    = _v_lbls.get("stable",   "STABLE")
    _lbl_v_watch     = _v_lbls.get("watch",    "WATCH")
    _lbl_v_elevated  = _v_lbls.get("elevated", "ELEVATED")
    _lbl_v_high_var  = _v_lbls.get("high_var", "HIGH VAR")

    result = {}

    for col in param_cols:
        if col not in df.columns:
            continue

        no_data_mask = pd.to_numeric(df[col], errors="coerce").isna().values
        x            = _to_float(df[col])
        roll_var     = _rolling_var(x, window)

        # Baseline variance: prefer pre-computed optimal_variance from config (Excel method)
        bare = _bare_name(col)
        if bare in opt_var and float(opt_var[bare]) > 1e-12:
            baseline_var = float(opt_var[bare])
        else:
            # Fallback: mean of rolling variances over baseline_df
            b = _to_float(baseline_df[col]) \
                if (baseline_df is not None
                    and col in baseline_df.columns
                    and len(baseline_df) > 0) \
                else x
            b_var_series = _rolling_var(b, window)
            valid_b      = b_var_series[~np.isnan(b_var_series)]
            baseline_var = float(np.nanmean(valid_b)) if len(valid_b) > 0 else 1e-12
            if baseline_var < 1e-12:
                baseline_var = 1e-12

        ratio  = np.where(np.isnan(roll_var), np.nan,
                          (roll_var - baseline_var) / baseline_var)
        scores = np.where(np.isnan(ratio), 0.0, np.clip(ratio / sc_r, 0.0, 1.0))
        labels = np.where(np.isnan(ratio), _lbl_v_stable,
                 np.where(scores >= lbl_high_var, _lbl_v_high_var,
                 np.where(scores >= lbl_elevated, _lbl_v_elevated,
                 np.where(scores >= lbl_watch,    _lbl_v_watch,
                          _lbl_v_stable)))).astype(object)

        labels[no_data_mask] = "NO DATA"
        scores[no_data_mask] = 0.0

        result[f"var_rolling_var_{col}"] = np.round(roll_var, 6)
        result[f"var_ratio_{col}"]       = np.round(ratio, 4)
        result[f"var_label_{col}"]       = labels
        result[f"var_score_{col}"]       = np.round(scores, 4)

    return pd.DataFrame(result, index=df.index)


def aggregate_variance_score(var_df: pd.DataFrame, param_weights: dict = None) -> pd.Series:
    """Weighted mean of all var_score_* columns -> single per-period variance risk (0-1).

    param_weights: bare-name -> weight dict (e.g. {"active_clay": 0.15}).
    If None or empty, falls back to equal-weight mean (original behaviour).
    """
    score_cols = [c for c in var_df.columns if c.startswith("var_score_")]
    if not score_cols:
        return pd.Series(0.0, index=var_df.index)
    mat = np.where(np.isnan(var_df[score_cols].values.astype(float)), 0.0,
                   var_df[score_cols].values.astype(float))
    return pd.Series(np.round(_weighted_agg(mat, score_cols, len("var_score_"), param_weights), 4),
                     index=var_df.index)


def _weighted_agg(mat: np.ndarray, cols: list, prefix_len: int, param_weights: dict = None) -> np.ndarray:
    """Weighted row-mean using bare param name -> weight lookup. Falls back to equal weights."""
    if not param_weights:
        return mat.mean(axis=1)

    def _bare(col):
        return _bare_name(col[prefix_len:])

    w = np.array([float(param_weights.get(_bare(c), 1.0)) for c in cols])
    total = w.sum()
    if total < 1e-10:
        return mat.mean(axis=1)
    w = w / total
    return mat @ w


def _rolling_var(x: np.ndarray, w: int) -> np.ndarray:
    """Rolling population variance (VAR.P, ddof=0) over window w."""
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        out[w - 1:] = np.nanvar(sliding_window_view(x, w), axis=1, ddof=0)
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
