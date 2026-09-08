"""
Acceleration Engine
===================
Measures the rate-of-change of the drift slope for each parameter.

A parameter drifting at a *constant* slope is concerning but predictable.
A parameter whose slope is *accelerating* — the drift is getting faster — is
a stronger early-warning signal that the process is destabilising.

Algorithm
---------
For each parameter column:
  1. Extract the rolling OLS slope series already computed by drift_engine
     (column ``drift_slope_{col}``).
  2. Compute period-over-period change:
         acc[t] = slope[t] - slope[t-1]
  3. Normalise against the parameter's baseline standard deviation:
         acc_score[t] = clip( |acc[t]| / (acc_sigma × baseline_std), 0, 1 )
     acc_sigma controls sensitivity (default 0.5 — half a std-dev shift per
     period is considered significant acceleration).
  4. Labels:
         score < 0.35  → STABLE
         score < 0.70  → ACCELERATING
         score >= 0.70 → RAPID CHANGE

Config keys (under ``engines.acceleration``):
  acc_sigma   float  0.5   normalisation divisor (in units of baseline_std/period)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_LABEL_STABLE = "STABLE"
_LABEL_ACCEL  = "ACCELERATING"
_LABEL_RAPID  = "RAPID CHANGE"
_LABEL_NODATA = "NO DATA"

_THRESH_STABLE = 0.35
_THRESH_ACCEL  = 0.70


def _get_cfg(config: dict) -> dict:
    return config.get("engines", {}).get("acceleration", {})


def compute_acceleration_scores(
    drift_df: pd.DataFrame,
    baseline_df: pd.DataFrame,
    param_cols: list[str],
    config: dict,
) -> pd.DataFrame:
    """
    Compute per-parameter acceleration (d²/dt²) scores.

    Parameters
    ----------
    drift_df    : DataFrame produced by drift_engine.compute_drift_scores()
                  Must contain ``drift_slope_{col}`` columns.
    baseline_df : Baseline DataFrame used to derive per-parameter std deviation.
    param_cols  : List of full parameter column names (e.g. ``ps_active_clay``).
    config      : Watchdog config dict.

    Returns
    -------
    DataFrame with columns per param:
        acc_delta_{col}  — raw slope change (units/period²)
        acc_score_{col}  — normalised score 0–1
        acc_label_{col}  — STABLE | ACCELERATING | RAPID CHANGE | NO DATA
    """
    cfg = _get_cfg(config)
    acc_sigma = float(cfg.get("acc_sigma", 0.5))

    result = pd.DataFrame(index=drift_df.index)

    for col in param_cols:
        slope_col = f"drift_slope_{col}"
        if slope_col not in drift_df.columns:
            result[f"acc_delta_{col}"] = np.nan
            result[f"acc_score_{col}"] = 0.0
            result[f"acc_label_{col}"] = _LABEL_NODATA
            continue

        slopes = drift_df[slope_col]

        # Baseline std for normalisation
        if col in baseline_df.columns:
            bstd = float(baseline_df[col].std(ddof=1))
        else:
            bstd = float(slopes.std(ddof=1)) if len(slopes) > 1 else 1.0
        if bstd == 0 or np.isnan(bstd):
            bstd = 1.0

        delta = slopes.diff()                            # slope change per period
        denom = acc_sigma * bstd
        score = (delta.abs() / denom).clip(0, 1)
        score = score.fillna(0.0)

        label = score.apply(
            lambda s: (
                _LABEL_RAPID  if s >= _THRESH_ACCEL else
                _LABEL_ACCEL  if s >= _THRESH_STABLE else
                _LABEL_STABLE
            )
        )

        result[f"acc_delta_{col}"] = delta.round(6)
        result[f"acc_score_{col}"] = score.round(4)
        result[f"acc_label_{col}"] = label

    return result


def aggregate_acceleration_score(
    acc_df: pd.DataFrame,
    param_weights: dict | None = None,
) -> pd.Series:
    """
    Weighted mean of all ``acc_score_*`` columns → single 0–1 risk series.
    """
    score_cols = [c for c in acc_df.columns if c.startswith("acc_score_")]
    if not score_cols:
        return pd.Series(0.0, index=acc_df.index)

    df = acc_df[score_cols].copy()

    if param_weights:
        weights = []
        for c in score_cols:
            bare = c[len("acc_score_"):]
            # strip table prefix (ps_, add_, etc.)
            bare_key = bare.split("_", 1)[-1] if "_" in bare else bare
            weights.append(param_weights.get(bare_key, param_weights.get(bare, 1.0)))
        total = sum(weights)
        if total > 0:
            weighted = sum(df[c] * (w / total) for c, w in zip(score_cols, weights))
            return weighted.clip(0, 1).rename("acc_risk")

    return df.mean(axis=1).clip(0, 1).rename("acc_risk")
