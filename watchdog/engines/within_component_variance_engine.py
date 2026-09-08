"""
engines/within_component_variance_engine.py
--------------------------------------------
Within-Component Batch Variance Engine.

For each (date, shift, component_id) row, uses the pre-computed VAR.P of all
additive batch values logged for that component in that shift (stored as
add_bvar_{col} columns by the aggregator).  This captures intra-component
batch instability -- how much additive quantities swing between individual
batches poured for the same component in one shift.

Algorithm
---------
  batch_var = VAR.P( all batch values for same date+shift+component_id )
              pre-computed in aggregator._compute_batch_variance_component()
  ratio     = (batch_var - baseline_var) / baseline_var
              Relative excess above baseline. 0 = at baseline; 5 = 6x baseline.
              baseline_var = mean of rolling-window VAR.P over the baseline
              add_ column (mirrors within_day_variance_engine logic)
  score     = clamp( ratio / 5.0, 0, 1 )
  Labels    : STABLE < watch_max | WATCH < elevated_max | ELEVATED < high_var_min | HIGH VAR

Single-batch rows (variance == 0) are forced to STABLE / score 0.
"""

import logging

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

logger = logging.getLogger(__name__)

_TPFX = ("ps_", "con_", "add_", "pse_", "sv_")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_within_component_variance(
    df: pd.DataFrame,
    add_param_cols: list,
    config: dict,
    baseline_df: pd.DataFrame = None,
) -> pd.DataFrame:
    """
    Returns a DataFrame with wcv_var / wcv_ratio / wcv_score / wcv_label columns
    for every add_ column in add_param_cols.

    Expects df to contain add_bvar_{bare} columns produced by
    aggregator._compute_batch_variance_component().

    Parameters
    ----------
    df             : the merged component-mode DataFrame (df_for_engines)
    add_param_cols : list of prefixed add_ column names, e.g. ["add_bentonite"]
    config         : watchdog config dict
    baseline_df    : optional baseline DataFrame for deriving baseline variance
    """
    v_cfg        = config["engines"].get("variance", {})
    hv_r         = float(v_cfg.get("high_var_ratio",    5.0))  # (6x-1)=5 with (x-a)/a formula
    sc_r         = float(v_cfg.get("score_divisor",     hv_r))
    lbl_watch    = float(v_cfg.get("score_watch_max",   0.35))
    lbl_elevated = float(v_cfg.get("score_elevated_max", 0.60))
    lbl_high_var = float(v_cfg.get("score_high_var_min", 0.85))
    window       = int(config["engines"]["window"])

    result = {}

    for col in add_param_cols:
        if not col.startswith("add_"):
            continue

        bare     = col[4:]                      # "add_bentonite" -> "bentonite"
        bvar_col = f"add_bvar_{bare}"           # column written by aggregator

        if bvar_col not in df.columns:
            n = len(df)
            result[f"wcv_var_{col}"]   = np.full(n, np.nan)
            result[f"wcv_ratio_{col}"] = np.full(n, np.nan)
            result[f"wcv_score_{col}"] = np.zeros(n)
            result[f"wcv_label_{col}"] = np.full(n, "NO DATA", dtype=object)
            continue

        bvar_vals    = pd.to_numeric(df[bvar_col], errors="coerce").values.astype(float)
        baseline_var = _baseline_var(baseline_df, col, window)

        ratio  = np.where(np.isnan(bvar_vals), np.nan,
                          (bvar_vals - baseline_var) / baseline_var)
        scores = np.where(np.isnan(ratio), 0.0,
                          np.clip(ratio / sc_r, 0.0, 1.0))
        labels = np.where(np.isnan(ratio),         "STABLE",
                 np.where(scores >= lbl_high_var,  "HIGH VAR",
                 np.where(scores >= lbl_elevated,  "ELEVATED",
                 np.where(scores >= lbl_watch,     "WATCH",
                                                   "STABLE")))).astype(object)

        # Single-batch rows: variance is 0, force STABLE
        single_batch = bvar_vals == 0.0
        labels[single_batch] = "STABLE"
        scores[single_batch] = 0.0

        result[f"wcv_var_{col}"]   = np.round(bvar_vals, 6)
        result[f"wcv_ratio_{col}"] = np.round(ratio,     4)
        result[f"wcv_score_{col}"] = np.round(scores,    4)
        result[f"wcv_label_{col}"] = labels

    return pd.DataFrame(result, index=df.index)


def aggregate_within_component_score(
    wcv_df: pd.DataFrame,
    param_weights: dict = None,
) -> pd.Series:
    """Weighted mean of all wcv_score_* columns -> single per-period WCV risk (0-1)."""
    score_cols = [c for c in wcv_df.columns if c.startswith("wcv_score_")]
    if not score_cols:
        return pd.Series(0.0, index=wcv_df.index)

    mat = np.where(
        np.isnan(wcv_df[score_cols].values.astype(float)), 0.0,
        wcv_df[score_cols].values.astype(float),
    )
    return pd.Series(
        np.round(_weighted_agg(mat, score_cols, len("wcv_score_"), param_weights), 4),
        index=wcv_df.index,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _weighted_agg(mat: np.ndarray, cols: list, prefix_len: int, param_weights: dict = None) -> np.ndarray:
    if not param_weights:
        return mat.mean(axis=1)

    def _bare(col):
        name = col[prefix_len:]
        for p in _TPFX:
            if name.startswith(p):
                return name[len(p):]
        return name

    w     = np.array([float(param_weights.get(_bare(c), 1.0)) for c in cols])
    total = w.sum()
    if total < 1e-10:
        return mat.mean(axis=1)
    return mat @ (w / total)


def _baseline_var(baseline_df: pd.DataFrame, col: str, window: int) -> float:
    """
    Mean of rolling-window VAR.P over baseline add_ column.
    Mirrors the logic in within_day_variance_engine._baseline_var().
    Falls back to 1e-12 when baseline data is absent or trivially small.
    """
    if (baseline_df is not None
            and col in baseline_df.columns
            and len(baseline_df) > 1):
        b = pd.to_numeric(baseline_df[col], errors="coerce").fillna(0).values.astype(float)
        if len(b) >= window:
            wins  = sliding_window_view(b, window)
            rvars = np.nanvar(wins, axis=1, ddof=0)
            valid = rvars[~np.isnan(rvars)]
            bv    = float(np.nanmean(valid)) if len(valid) > 0 else 0.0
        else:
            bv = float(np.nanvar(b, ddof=0))
    else:
        bv = 0.0
    return max(bv, 1e-12)
