"""
engines/si_engine.py
---------------------
Stability Index (SI) Engine.

Formula  (weights in watchdog_config.json -> si_weights)
---------
  SI = w_var × variance_score
     + w_drift × drift_score
     + w_osc × oscillation_score
     + w_acc × acceleration_score      ← Phase 2 addition
     + w_rec × recovery_score          ← Phase 2 addition
  (scaled 0-100, weights normalised to sum=1)

Default weights:
  variance=0.25, drift=0.30, oscillation=0.15,
  acceleration=0.15, recovery=0.15

Alert bands
-----------
  SI ≤ 20 → STABLE  |  ≤ 49 → WATCH  |  ≤ 69 → ALERT  |  > 69 → CRITICAL
  Escalation to !! WARNING: any LCL/UCL deviation, or ALERT/CRITICAL + high % change
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# --- Band labels -- defaults (used when config has no overrides) ---------------
STABLE   = "STABLE"
WATCH    = "WATCH"
ALERT    = "ALERT"
CRITICAL = "CRITICAL"
WARNING  = "!! WARNING"


def _get_labels(config: dict) -> tuple[str, str, str, str]:
    """Return (stable, watch, alert, critical) label strings from config.
    Falls back to the module-level defaults when not configured."""
    lbls = config.get("alert_labels", {})
    return (
        lbls.get("stable",   STABLE),
        lbls.get("watch",    WATCH),
        lbls.get("alert",    ALERT),
        lbls.get("critical", CRITICAL),
    )


def _get_thresholds(config: dict) -> tuple[float, float, float]:
    """Return (stable_max, watch_max, alert_max) from config with defaults."""
    t = config.get("alert_thresholds", {})
    return (
        float(t.get("stable_max", 20)),
        float(t.get("watch_max",  49)),
        float(t.get("alert_max",  69)),
    )


def compute_si(
    var_risk:   pd.Series,
    drift_risk: pd.Series,
    osc_risk:   pd.Series,
    config:     dict,
    acc_risk:   pd.Series | None = None,
    rec_risk:   pd.Series | None = None,
) -> pd.DataFrame:
    """
    Compute aggregate per-period SI.

    acc_risk and rec_risk are optional (Phase 2 engines).
    When provided they are weighted into the composite score.

    Returns DataFrame with columns:
        si_variance_risk, si_drift_risk, si_oscillation_risk,
        si_acceleration_risk, si_recovery_risk,
        si_risk_score (0-1), si_score_100 (0-100), si_alert.
    """
    wv, wd, wo, wa, wr = _get_weights(config)

    idx = var_risk.index

    _acc = acc_risk.fillna(0).reindex(idx, fill_value=0.0) if acc_risk is not None \
           else pd.Series(0.0, index=idx)
    _rec = rec_risk.fillna(0).reindex(idx, fill_value=0.0) if rec_risk is not None \
           else pd.Series(0.0, index=idx)

    risk = (wv * var_risk.fillna(0)
          + wd * drift_risk.fillna(0)
          + wo * osc_risk.fillna(0)
          + wa * _acc
          + wr * _rec)

    risk   = np.clip(risk.values, 0.0, 1.0)
    si_100 = np.round(risk * 100, 2)

    stable_max, watch_max, alert_max             = _get_thresholds(config)
    lbl_stable, lbl_watch, lbl_alert, lbl_critical = _get_labels(config)

    alerts = np.where(si_100 <= stable_max, lbl_stable,
             np.where(si_100 <= watch_max,  lbl_watch,
             np.where(si_100 <= alert_max,  lbl_alert,
                      lbl_critical)))

    return pd.DataFrame({
        "si_variance_risk"     : np.round(var_risk.values, 4),
        "si_drift_risk"        : np.round(drift_risk.values, 4),
        "si_oscillation_risk"  : np.round(osc_risk.values, 4),
        "si_acceleration_risk" : np.round(_acc.values, 4),
        "si_recovery_risk"     : np.round(_rec.values, 4),
        "si_risk_score"        : np.round(risk, 4),
        "si_score_100"         : si_100,
        "si_alert"             : alerts,
    }, index=idx)


def compute_per_param_si(
    var_df:     pd.DataFrame,
    drift_df:   pd.DataFrame,
    osc_df:     pd.DataFrame,
    param_cols: list[str],
    config:     dict,
    acc_df:     pd.DataFrame | None = None,
    rec_df:     pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Compute SI score per parameter per period.

    acc_df and rec_df are optional (Phase 2 engines).
    Returns DataFrame with si_param_{col} (0-100) and si_label_{col}.
    """
    wv, wd, wo, wa, wr = _get_weights(config)

    stable_max, watch_max, alert_max              = _get_thresholds(config)
    lbl_stable, lbl_watch, lbl_alert, lbl_critical = _get_labels(config)

    idx    = var_df.index
    result = {}

    for col in param_cols:
        vs  = var_df.get(f"var_score_{col}",   pd.Series(0.0, index=idx))
        ds  = drift_df.get(f"drift_score_{col}", pd.Series(0.0, index=idx))
        os_ = osc_df.get(f"osc_score_{col}",   pd.Series(0.0, index=idx))
        as_ = acc_df.get(f"acc_score_{col}",   pd.Series(0.0, index=idx)) \
              if acc_df is not None else pd.Series(0.0, index=idx)
        rs  = rec_df.get(f"rec_score_{col}",   pd.Series(0.0, index=idx)) \
              if rec_df is not None else pd.Series(0.0, index=idx)

        score = np.round(
            np.clip(
                (  wv * vs.fillna(0)
                 + wd * ds.fillna(0)
                 + wo * os_.fillna(0)
                 + wa * as_.fillna(0)
                 + wr * rs.fillna(0)
                ).values * 100,
                0, 100,
            ), 2,
        )

        labels = np.where(score <= stable_max, lbl_stable,
                 np.where(score <= watch_max,  lbl_watch,
                 np.where(score <= alert_max,  lbl_alert,
                          lbl_critical)))

        result[f"si_param_{col}"] = score
        result[f"si_label_{col}"] = labels.astype(object)

    return pd.DataFrame(result, index=idx)


def compute_trends(
    df:         pd.DataFrame,
    param_cols: list[str],
    window:     int = 10,
    threshold:  float = 0.005,
) -> dict[str, str]:
    """Return {col: 'up'|'down'|'flat'} for the latest period."""
    if df.empty or len(df) < 2:
        return {col: "flat" for col in param_cols}

    latest_idx = df.index[-1]
    prev_idx   = df.index[max(0, len(df) - window - 1)]

    trends = {}
    for col in param_cols:
        if col not in df.columns:
            trends[col] = "flat"
            continue
        try:
            latest = float(pd.to_numeric(df.at[latest_idx, col], errors="coerce"))
            prev   = float(pd.to_numeric(df.at[prev_idx,   col], errors="coerce"))
            if np.isnan(latest) or np.isnan(prev) or abs(prev) < 1e-12:
                trends[col] = "flat"
            elif latest > prev * (1 + threshold):
                trends[col] = "up"
            elif latest < prev * (1 - threshold):
                trends[col] = "down"
            else:
                trends[col] = "flat"
        except Exception:
            trends[col] = "flat"

    return trends


def _get_weights(config: dict) -> tuple[float, float, float, float, float]:
    """Return (w_variance, w_drift, w_oscillation, w_acceleration, w_recovery) normalised to sum=1."""
    weights = config.get("si_weights", {})
    wv = float(weights.get("variance",     0.25))
    wd = float(weights.get("drift",        0.30))
    wo = float(weights.get("oscillation",  0.15))
    wa = float(weights.get("acceleration", 0.15))
    wr = float(weights.get("recovery",     0.15))
    total = wv + wd + wo + wa + wr
    if total < 1e-9:
        total = 1.0
    return wv / total, wd / total, wo / total, wa / total, wr / total


def si_label_to_int(label: str) -> int:
    return {STABLE: 0, WATCH: 1, ALERT: 2, CRITICAL: 3, WARNING: 4}.get(label, 0)


def get_worst_period(si_df: pd.DataFrame) -> int:
    return int(si_df["si_score_100"].idxmax())


def compute_sigma_alert(
    df: pd.DataFrame,
    baseline_df: pd.DataFrame,
    param_cols: list[str],
    config: dict,
) -> pd.DataFrame:
    """
    Sigma-band alerting for window mode (SPC-style).

    Mean and std are derived from baseline_df (the known-good reference period)
    so that control limits are anchored to stable process behaviour, not the
    current (potentially drifted) window.

    Classification per parameter per row:
      |z| <= 1  ->  STABLE
      |z| <= 2  ->  ALERT
      |z|  > 2  ->  CRITICAL

    !! WARNING is NOT emitted here — it is applied downstream in
    _build_period_result() when % change or LCL/UCL deviation is detected.

    Composite per-row alert = worst band across all parameters.
    Score (0-100) = min(max_|z|, 3) / 3 * 100.

    Returns DataFrame indexed like df with columns:
      sigma_{col}        -- z-score for each param
      sigma_label_{col}  -- per-param band label
      sigma_score_100    -- 0-100 score (max |z| capped at 3)
      sigma_alert        -- composite band label
    """
    result: dict = {}
    z_max_abs = pd.Series(0.0, index=df.index)

    for col in param_cols:
        if col not in df.columns:
            continue

        # Baseline stats (from the reference good-period data)
        base_series = pd.to_numeric(
            baseline_df[col] if (baseline_df is not None and col in baseline_df.columns)
            else pd.Series(dtype=float),
            errors="coerce",
        ).dropna()

        if len(base_series) < 2:
            # Fall back to current window when baseline is insufficient
            base_series = pd.to_numeric(df[col], errors="coerce").dropna()

        if len(base_series) < 2:
            result[f"sigma_{col}"]       = pd.Series(np.nan, index=df.index)
            result[f"sigma_label_{col}"] = pd.Series(STABLE, index=df.index, dtype=object)
            continue

        mu  = float(base_series.mean())
        std = float(base_series.std(ddof=0))

        current = pd.to_numeric(df[col], errors="coerce")
        z = (current - mu) / std if std > 1e-12 else pd.Series(0.0, index=df.index)

        labels = np.where(z.abs() <= 1.0, STABLE,
                 np.where(z.abs() <= 2.0, ALERT,
                          CRITICAL))

        result[f"sigma_{col}"]        = z.round(4)
        result[f"sigma_label_{col}"]  = pd.Series(labels, index=df.index, dtype=object)
        result[f"sigma_mean_{col}"]   = mu   # baseline mean — used for % deviation in webhook

        z_max_abs = z_max_abs.combine(z.abs().fillna(0.0), max)

    # Score: clip |z| at 3 then scale to 0-100
    score_100 = (z_max_abs.clip(upper=3.0) / 3.0 * 100.0).round(2)

    composite = np.where(z_max_abs <= 1.0, STABLE,
                np.where(z_max_abs <= 2.0, ALERT,
                         CRITICAL))

    result["sigma_score_100"] = score_100.values
    result["sigma_alert"]     = composite

    return pd.DataFrame(result, index=df.index)
