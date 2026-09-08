"""
Recovery Engine
===============
Measures how long a parameter has remained in an unstable state after the
system (or operator) applied a correction.

A fast-recovering parameter is healthy. A parameter that stays unstable for
many consecutive periods signals either:
  - The correction was insufficient, or
  - There is a persistent root cause not yet addressed (raw material, equipment).

Algorithm
---------
For each parameter column:
  1. Determine if each period is *unstable*:
         unstable[t] = 1  if  var_label ∈ {ELEVATED, HIGH VAR}
                           OR drift_label ∈ {STRONG DRIFT, STRONG TREND}
                       0  otherwise
  2. Count consecutive unstable periods up to and including t:
         lag[t] = number of consecutive 1's ending at t
  3. Normalise:
         recovery_score[t] = clip( lag[t] / max_lag_periods, 0, 1 )
     max_lag_periods default = 5  (5 consecutive unstable shifts = fully stuck)
  4. Labels:
         score = 0          → RECOVERED   (currently stable)
         0 < score < 0.40   → RECOVERING  (1–2 shifts unstable)
         0.40 ≤ score < 0.80→ SLOW        (2–4 shifts, correction not working)
         score ≥ 0.80       → STUCK       (≥ 4 shifts, persistent instability)

Config keys (under ``engines.recovery``):
  max_lag_periods  int   5   shifts before score saturates at 1.0
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_UNSTABLE_VAR   = {"ELEVATED", "HIGH VAR"}
_UNSTABLE_DRIFT = {"STRONG DRIFT", "STRONG TREND"}

_LABEL_RECOVERED  = "RECOVERED"
_LABEL_RECOVERING = "RECOVERING"
_LABEL_SLOW       = "SLOW RECOVERY"
_LABEL_STUCK      = "STUCK"
_LABEL_NODATA     = "NO DATA"

_THRESH_RECOVERING = 0.40
_THRESH_SLOW       = 0.80


def _get_cfg(config: dict) -> dict:
    return config.get("engines", {}).get("recovery", {})


def _consecutive_run(series: pd.Series) -> pd.Series:
    """Return rolling count of consecutive True/1 values ending at each row."""
    result = []
    run = 0
    for v in series:
        if v:
            run += 1
        else:
            run = 0
        result.append(run)
    return pd.Series(result, index=series.index)


def compute_recovery_scores(
    var_df: pd.DataFrame,
    drift_df: pd.DataFrame,
    param_cols: list[str],
    config: dict,
) -> pd.DataFrame:
    """
    Compute per-parameter recovery / correction-lag scores.

    Parameters
    ----------
    var_df      : DataFrame produced by variance_engine — must contain
                  ``var_label_{col}`` columns.
    drift_df    : DataFrame produced by drift_engine — must contain
                  ``drift_label_{col}`` columns.
    param_cols  : List of full parameter column names.
    config      : Watchdog config dict.

    Returns
    -------
    DataFrame with columns per param:
        rec_lag_{col}    — consecutive unstable period count (int)
        rec_score_{col}  — normalised score 0–1
        rec_label_{col}  — RECOVERED | RECOVERING | SLOW RECOVERY | STUCK | NO DATA
    """
    cfg = _get_cfg(config)
    max_lag = int(cfg.get("max_lag_periods", 5))

    result = pd.DataFrame(index=var_df.index)

    for col in param_cols:
        var_lbl_col   = f"var_label_{col}"
        drift_lbl_col = f"drift_label_{col}"

        has_var   = var_lbl_col   in var_df.columns
        has_drift = drift_lbl_col in drift_df.columns

        if not has_var and not has_drift:
            result[f"rec_lag_{col}"]   = 0
            result[f"rec_score_{col}"] = 0.0
            result[f"rec_label_{col}"] = _LABEL_NODATA
            continue

        unstable = pd.Series(False, index=var_df.index)
        if has_var:
            unstable = unstable | var_df[var_lbl_col].isin(_UNSTABLE_VAR)
        if has_drift:
            drift_lbl = drift_df[drift_lbl_col].reindex(var_df.index)
            unstable  = unstable | drift_lbl.isin(_UNSTABLE_DRIFT)

        lag   = _consecutive_run(unstable)
        score = (lag / max_lag).clip(0, 1)

        label = score.apply(
            lambda s: (
                _LABEL_STUCK      if s >= _THRESH_SLOW       else
                _LABEL_SLOW       if s >= _THRESH_RECOVERING else
                _LABEL_RECOVERING if s > 0                   else
                _LABEL_RECOVERED
            )
        )

        result[f"rec_lag_{col}"]   = lag
        result[f"rec_score_{col}"] = score.round(4)
        result[f"rec_label_{col}"] = label

    return result


def aggregate_recovery_score(
    rec_df: pd.DataFrame,
    param_weights: dict | None = None,
) -> pd.Series:
    """
    Weighted mean of all ``rec_score_*`` columns → single 0–1 risk series.
    """
    score_cols = [c for c in rec_df.columns if c.startswith("rec_score_")]
    if not score_cols:
        return pd.Series(0.0, index=rec_df.index)

    df = rec_df[score_cols].copy()

    if param_weights:
        weights = []
        for c in score_cols:
            bare = c[len("rec_score_"):]
            bare_key = bare.split("_", 1)[-1] if "_" in bare else bare
            weights.append(param_weights.get(bare_key, param_weights.get(bare, 1.0)))
        total = sum(weights)
        if total > 0:
            weighted = sum(df[c] * (w / total) for c, w in zip(score_cols, weights))
            return weighted.clip(0, 1).rename("rec_risk")

    return df.mean(axis=1).clip(0, 1).rename("rec_risk")
