"""
engines/oscillation_engine.py
------------------------------
Oscillation Detection Engine -- matches GPI Variance Sheet "SignOsc" tab logic.

Algorithm (count mode — default)
---------
  sc[i]    = 1 if SIGN(diff[i]) != SIGN(diff[i-1]) else 0  (sign reversal)
  count[i] = rolling SUM of sc over window N
  max_changes = N - 2
  Thresholds scale with window via ratios stored in config:
    mild_ratio (default 0.40) -> mild  = max(1, ceil(mild_ratio x max_changes))
    osc_ratio  (default 0.70) -> osc   = max(2, ceil(osc_ratio  x max_changes))
  Example N=10: max=8, mild=4, osc=6.   N=5: max=3, mild=2, osc=3.
  Labels   : STABLE < mild -> MILD < osc -> OSCILLATING
  Score    : count / max_changes  (0-1 normalised to true maximum)

Algorithm (amplitude_weighted mode — opt-in via config)
-----------
  Each sign reversal is weighted by the peak-to-trough amplitude of that
  reversal, normalised by the parameter's standard deviation over the window.
  This means 3.30↔3.31 (tiny swing) scores near 0 while 3.0↔3.9 (large swing)
  scores proportionally high, even with the same number of sign changes.

  sc_amp[j] = (|diff[j]| + |diff[j-1]|) / (2 * sigma)   when sign reversal
             = 0                                           otherwise
  score[i]  = clip( sum(sc_amp[window]) / max_changes, 0, 1 )
  Labels use mild_ratio / osc_ratio applied directly to the 0-1 score.

  Enable:  config["engines"]["oscillation"]["amplitude_weighted"] = true
"""

import math
import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _osc_thresholds(window: int, osc_cfg: dict) -> tuple[int, int, int]:
    """Return (max_changes, mild_threshold, osc_threshold) scaled to window."""
    max_changes = max(1, window - 2)
    mild_ratio  = float(osc_cfg.get("mild_ratio", 0.40))
    osc_ratio   = float(osc_cfg.get("osc_ratio",  0.70))
    mild = max(1, math.ceil(mild_ratio * max_changes))
    osc  = max(2, math.ceil(osc_ratio  * max_changes))
    return max_changes, mild, osc


def compute_oscillation_scores(
    df:         pd.DataFrame,
    param_cols: list[str],
    config:     dict,
) -> pd.DataFrame:
    """Compute rolling oscillation scores for all parameters.

    Returns DataFrame with osc_count / osc_label / osc_score columns.
    When config["engines"]["oscillation"]["amplitude_weighted"] is true, each
    sign reversal is weighted by its peak-to-trough amplitude (normalised by σ)
    so that tiny fluctuations score near 0 and large swings score proportionally.
    """
    window      = config["engines"]["window"]
    osc_cfg     = config["engines"].get("oscillation", {})
    amp_mode    = bool(osc_cfg.get("amplitude_weighted", False))
    max_chg, mild, osc = _osc_thresholds(window, osc_cfg)
    mild_ratio  = float(osc_cfg.get("mild_ratio", 0.40))
    osc_ratio   = float(osc_cfg.get("osc_ratio",  0.70))

    logger.debug(
        "Oscillation window=%d  max_changes=%d  mild>=%d  osc>=%d  amplitude_weighted=%s",
        window, max_chg, mild, osc, amp_mode,
    )

    _o_lbls       = osc_cfg.get("labels", {})
    _lbl_o_stable = _o_lbls.get("stable",      "STABLE")
    _lbl_o_mild   = _o_lbls.get("mild",        "MILD")
    _lbl_o_osc    = _o_lbls.get("oscillating", "OSCILLATING")

    result = {}

    for col in param_cols:
        if col not in df.columns:
            continue

        x = _to_float(df[col])

        if amp_mode:
            scores = np.round(_amplitude_weighted_osc(x, window, max_chg), 4)
            labels = np.where(scores >= osc_ratio,  _lbl_o_osc,
                     np.where(scores >= mild_ratio, _lbl_o_mild,
                              _lbl_o_stable)).astype(object)
            counts = np.zeros(len(x), dtype=int)
        else:
            counts = _sign_change_count(x, window)
            scores = np.round(counts / max_chg, 4)
            labels = np.where(counts >= osc,  _lbl_o_osc,
                     np.where(counts >= mild, _lbl_o_mild,
                              _lbl_o_stable)).astype(object)

        result[f"osc_count_{col}"] = counts.astype(int)
        result[f"osc_label_{col}"] = labels
        result[f"osc_score_{col}"] = scores

    return pd.DataFrame(result, index=df.index)


def aggregate_oscillation_score(osc_df: pd.DataFrame, param_weights: dict = None) -> pd.Series:
    """Weighted mean of all osc_score_* columns -> single per-period oscillation risk (0-1).

    param_weights: bare-name -> weight dict. None -> equal-weight mean.
    """
    score_cols = [c for c in osc_df.columns if c.startswith("osc_score_")]
    if not score_cols:
        return pd.Series(0.0, index=osc_df.index)
    mat = np.where(np.isnan(osc_df[score_cols].values.astype(float)), 0.0,
                   osc_df[score_cols].values.astype(float))
    return pd.Series(np.round(_weighted_agg(mat, score_cols, len("osc_score_"), param_weights), 4),
                     index=osc_df.index)


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


def _amplitude_weighted_osc(x: np.ndarray, window: int, max_chg: int) -> np.ndarray:
    """
    Amplitude-weighted oscillation score (0-1).

    Each sign reversal contributes (|diff[j]| + |diff[j-1]|) / (2 * sigma)
    instead of 1.  This makes a 3.30↔3.31 oscillation score near 0 while a
    3.0↔3.9 oscillation scores proportionally high.

    Normalised by max_chg so the result is directly comparable to the count
    mode score and the same mild_ratio / osc_ratio thresholds apply.
    """
    n = len(x)
    if n <= 1:
        return np.zeros(n)

    diff  = np.diff(x)
    sigma = float(np.nanstd(x))
    if sigma < 1e-9:
        sigma = 1.0

    sc_amp = np.zeros(len(diff))
    for j in range(1, len(diff)):
        if diff[j - 1] != 0.0 and diff[j] != 0.0 and np.sign(diff[j]) != np.sign(diff[j - 1]):
            sc_amp[j] = (abs(diff[j]) + abs(diff[j - 1])) / (2.0 * sigma)

    scores = np.zeros(n)
    denom  = float(max_chg) if max_chg > 0 else 1.0
    for i in range(1, n):
        start      = max(0, i - window + 1)
        scores[i]  = sc_amp[start:i].sum() / denom

    return np.clip(scores, 0.0, 1.0)


def _sign_change_count(x: np.ndarray, window: int) -> np.ndarray:
    """Rolling sign-change count -- matches Excel SignOsc formula."""
    n    = len(x)
    diff = np.diff(x)

    sc = np.zeros(len(diff), dtype=int)
    for j in range(1, len(diff)):
        # Both diffs must be non-zero -- flat steps (repeated values) are not direction reversals
        if diff[j - 1] != 0.0 and diff[j] != 0.0 and np.sign(diff[j]) != np.sign(diff[j - 1]):
            sc[j] = 1

    counts = np.zeros(n, dtype=int)
    for i in range(1, n):
        sc_start   = max(0, i - window + 1)
        counts[i]  = int(sc[sc_start:i].sum())

    return counts



def _to_float(series) -> np.ndarray:
    if isinstance(series, np.ndarray):
        arr = series.astype(float)
    else:
        arr = pd.to_numeric(series, errors="coerce").values.astype(float)
    mean = np.nanmean(arr)
    if np.isnan(mean):
        mean = 0.0
    return np.where(np.isnan(arr), mean, arr)
