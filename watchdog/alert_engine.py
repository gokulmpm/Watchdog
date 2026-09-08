"""
alert_engine.py
----------------
Formats alert reports, determines root cause, generates recommendations,
and writes all results to a multi-sheet Excel workbook.

Excel sheets
------------
  Alerts             -- per-parameter summary for the latest period
  SI_Alerts          -- per-period SI scores, alerts, root cause, recommendation
  Dashboard          -- per-period raw values + SI
  Deviation_Alerts   -- per-period LCL/UCL status per parameter
  Pct_Change         -- per-period % change per parameter
  Variance_Detail    -- per-period rolling variance and labels
  Drift_Detail       -- per-period rolling mean deviation and labels
  Oscillation_Detail -- per-period sign-change counts and labels
"""

import logging
import re
from typing import Optional

import numpy as np
import pandas as pd

_DEV_RE = re.compile(
    r"Deviated\s+(LOW|HIGH)\s+([+-]?\d+\.?\d*)\s+\((?:LCL|UCL)=(\d+\.?\d*)\)"
)

logger = logging.getLogger(__name__)

ALERT_COLORS = {
    "STABLE"     : "C6EFCE",
    "WATCH"      : "FFEB9C",
    "ALERT"      : "FFC7CE",
    "CRITICAL"   : "FF4444",
    "!! WARNING" : "FF0000",
}

ALERT_FONT_COLORS = {
    "STABLE"     : "276221",
    "WATCH"      : "9C5700",
    "ALERT"      : "9C0006",
    "CRITICAL"   : "FFFFFF",
    "!! WARNING" : "FFFFFF",
}


def log_alert_report(result: dict, config: dict) -> None:
    """Write a formatted per-period alert report to the logger."""
    period_key = result["period_key"]
    si_score   = result["si_score_100"]
    alert      = result["final_alert"]
    root_cause = result["root_cause"]
    rec        = result["recommendation"]
    raw        = result.get("raw_values", {})
    pct        = result.get("pct_changes", {})
    dev        = result.get("deviations", {})
    display    = config.get("display_names", {})

    divider = "=" * 90
    logger.info(divider)
    logger.info("  AI WATCHDOG ALERT REPORT  --  %s", period_key)
    logger.info("  SI Risk Score : %.1f / 100   |   Status : %s", si_score, alert)
    logger.info(divider)
    logger.info("  %-36s %10s %10s  %-33s %s",
                "Parameter", "Value", "% Change", "Dev Alert", "Flag")
    logger.info("-" * 90)

    for col, val in raw.items():
        label   = display.get(_strip_prefix(col), col)
        val_str = f"{val:.4g}" if isinstance(val, (int, float)) and val == val else "N/A"
        pct_val = pct.get(col)
        pct_str = f"{pct_val:+.1f}%" if isinstance(pct_val, float) and pct_val == pct_val else "N/A"
        dev_str = dev.get(col, "") or ""
        if len(dev_str) > 33:
            dev_str = dev_str[:30] + "..."
        flag = "!!" if dev_str.startswith("Deviated") else ""
        logger.info("  %-36s %10s %10s  %-33s %s",
                    label, val_str, pct_str, dev_str, flag)

    logger.info("-" * 90)
    logger.info("  Root Cause     : %s", root_cause)
    logger.info("  Recommendation : %s", rec)
    logger.info(divider)


# Direction-specific recommendations: (LOW, HIGH) tuples
_REC = {
    "active_clay":            (
        "Active Clay too low -- increase Bentonite addition rate; check Bentonite reactivity",
        "Active Clay too high -- reduce Bentonite addition; risk of over-activation and brittleness",
    ),
    "moisture":               (
        "Moisture too low -- increase water dosing; inspect spray nozzles for blockage",
        "Moisture too high -- reduce water addition; check cooler efficiency and return sand temperature",
    ),
    "loi":                    (
        "LOI too low -- increase Magnacoal/Coal Dust addition; verify hopper feed rate",
        "LOI too high -- reduce Coal Dust addition; check combustion completeness",
    ),
    "compactibility":         (
        "Compactability too low -- increase water addition; check mixing cycle duration",
        "Compactability too high -- reduce water dosing; inspect muller programme",
    ),
    "gcs":                    (
        "GCS too low -- extend mixing cycle time or increase muller speed; check Bentonite quality",
        "GCS too high -- review mixing energy; check if sand is over-activated",
    ),
    "shear_strength":         (
        "Shear Strength low -- increase Bentonite addition or mixing energy; check sand temperature",
        "Shear Strength high -- monitor for over-bonded sand; check muller cycle",
    ),
    "inert_fines":            (
        "Inert Fines low -- normal; monitor return sand fines level",
        "Inert Fines too high -- increase sand dump/washout rate; inspect reclamation system for fines build-up",
    ),
    "gfn_afs":                (
        "GFN/AFS too fine -- check new sand supplier; risk of reduced permeability",
        "GFN/AFS too coarse -- inspect new sand quality; review grain size distribution; check blending ratio",
    ),
    "volatile_matter":        (
        "Volatile Matter too low -- increase Coal Dust/Magnacoal addition; check old sand return rate",
        "Volatile Matter too high -- reduce Coal Dust addition; check combustion gas emissions",
    ),
    "permeability":           (
        "Permeability too low -- check inert fines level; reduce over-activation of clay",
        "Permeability too high -- check for loss of fines or clay; verify sand quality",
    ),
    "split_strength":         (
        "Split Strength low -- check Bentonite quality and addition rate",
        "Split Strength high -- monitor for over-bonded sand",
    ),
    "temp_of_sand_after_mix": (
        "Sand temperature low -- check cooler operation; verify return sand quality",
        "Sand temperature too high -- inspect cooler efficiency; reduce hot return sand",
    ),
    "compactability_smc_pct": (
        "SMC Compactability reading low -- check SMC sensor calibration; verify COSP setpoint",
        "SMC Compactability reading high -- reduce water addition; inspect SMC sensor",
    ),
    "moisture_smc_pct":       (
        "SMC Moisture reading low -- inspect moisture sensor; check water dosing system",
        "SMC Moisture reading high -- reduce water addition; verify sensor accuracy",
    ),
    "temperature_c":          (
        "Mixer temperature low -- check sensor; verify return sand pre-heating",
        "Mixer temperature too high -- improve cooler efficiency; reduce hot return sand intake",
    ),
    "bentonite_actual":       (
        "Bentonite addition below target -- check weigh scale; inspect feeder for blockage",
        "Bentonite addition above target -- review dosing setpoint; check weigh scale calibration",
    ),
    "coal_dust_actual":       (
        "Coal Dust addition below target -- check hopper level; inspect feeder for blockage",
        "Coal Dust addition above target -- review dosing setpoint; check hopper level",
    ),
    "total_seconds":          (
        "Mixing time short -- check muller programme; inspect cycle settings",
        "Mixing time long -- review muller programme; check for mechanical delays",
    ),
}

_REC_FALLBACK = (
    "Check process additions and raw material quality",
    "Reduce addition rate and monitor closely",
)


def _rec_for(base: str, direction: str) -> str:
    pair = _REC.get(base, _REC_FALLBACK)
    return pair[0] if direction == "LOW" else pair[1]


def build_root_cause(result: dict, config: dict) -> str:
    """Build a clear, direction-aware root cause summary."""
    display  = config.get("display_names", {})
    pct_chg  = result.get("pct_changes", {})

    deviations = result.get("deviations", {})
    drift_lbls = result.get("drift_labels", {})
    var_lbls   = result.get("var_labels", {})
    osc_lbls   = result.get("osc_labels", {})
    wcv_lbls   = result.get("wcv_labels", {})

    parts = []

    # -- 1. Out-of-control parameters (highest priority) -----------------------
    dev_msgs = []
    for col, status in deviations.items():
        if not isinstance(status, str) or not status.startswith("Deviated"):
            continue
        name = display.get(_strip_prefix(col), _strip_prefix(col))
        m = _DEV_RE.search(status)
        if m:
            direction, magnitude, limit = m.group(1), abs(float(m.group(2))), float(m.group(3))
            pct_off = round(magnitude / limit * 100, 1) if limit else 0
            boundary = "below LCL" if direction == "LOW" else "above UCL"
            dev_msgs.append(f"{name} {pct_off}% {boundary}")
        else:
            dev_msgs.append(f"{name} outside control limit")
    if dev_msgs:
        parts.append("Limit breach: " + "; ".join(dev_msgs[:3]))

    # -- 2. Sustained trend / drift (include direction) ------------------------
    strong, slight = [], []
    for col, lbl in drift_lbls.items():
        if lbl not in ("STRONG DRIFT", "STRONG TREND", "SLIGHT DRIFT", "SLIGHT TREND"):
            continue
        name = display.get(_strip_prefix(col), _strip_prefix(col))
        pct  = pct_chg.get(col)
        arrow = ""
        if pct is not None and pct == pct:  # not NaN
            arrow = " (rising)" if pct > 0 else " (falling)"
        entry = f"{name}{arrow}"
        if lbl in ("STRONG DRIFT", "STRONG TREND"):
            strong.append(entry)
        else:
            slight.append(entry)
    if strong:
        parts.append("Sustained trend: " + ", ".join(strong[:3]))
    if slight and len(parts) < 3:
        parts.append("Gradual shift: " + ", ".join(slight[:3]))

    # -- 3. High variability ---------------------------------------------------
    hi_var = [display.get(_strip_prefix(c), _strip_prefix(c))
              for c, l in var_lbls.items() if l == "HIGH VAR"]
    if hi_var and len(parts) < 3:
        parts.append("Unstable (high variability): " + ", ".join(hi_var[:2]))

    # -- 4. Oscillation --------------------------------------------------------
    osc = [display.get(_strip_prefix(c), _strip_prefix(c))
           for c, l in osc_lbls.items() if l == "OSCILLATING"]
    if osc and len(parts) < 3:
        parts.append("Cycling / oscillating: " + ", ".join(osc[:2]))

    # -- 5. Within-component batch variance (component mode only) -------------
    wcv_hi = [display.get(_strip_prefix(c), _strip_prefix(c))
              for c, l in wcv_lbls.items() if l in ("HIGH VAR", "ELEVATED")]
    if wcv_hi and len(parts) < 3:
        parts.append("Batch instability: " + ", ".join(wcv_hi[:2]))

    return "  |  ".join(parts[:3]) if parts else "Process operating within normal range"


def build_recommendation(result: dict) -> str:
    """Build direction-specific, parameter-level recommendations."""
    drift_lbls  = result.get("drift_labels",  {})
    var_lbls    = result.get("var_labels",     {})
    osc_lbls    = result.get("osc_labels",     {})
    deviations  = result.get("deviations",     {})
    pct_chg     = result.get("pct_changes",    {})
    final_alert = result.get("final_alert",    "STABLE")

    recs = []

    # -- 1. Recommendations for out-of-control parameters ---------------------
    priority_dev = [
        "gcs", "active_clay", "moisture", "compactibility",
        "loi", "volatile_matter", "gfn_afs", "inert_fines",
        "shear_strength", "permeability", "split_strength",
        "temp_of_sand_after_mix",
    ]
    for base in priority_dev:
        for col, status in deviations.items():
            if _strip_prefix(col) != base:
                continue
            if not isinstance(status, str) or not status.startswith("Deviated"):
                continue
            direction = "LOW" if "LOW" in status else "HIGH"
            recs.append(_rec_for(base, direction))
            break
        if len(recs) >= 2:
            break

    # -- 2. Recommendations for drifting parameters (direction from pct_change) -
    if len(recs) < 2:
        priority_drift = [
            "active_clay", "moisture", "compactibility", "gcs",
            "loi", "volatile_matter", "inert_fines", "gfn_afs",
            "shear_strength", "bentonite_actual", "coal_dust_actual",
            "temp_of_sand_after_mix", "compactability_smc_pct",
            "moisture_smc_pct", "temperature_c", "total_seconds",
        ]
        for base in priority_drift:
            for col, lbl in drift_lbls.items():
                if _strip_prefix(col) != base:
                    continue
                if lbl not in ("STRONG DRIFT", "STRONG TREND", "SLIGHT DRIFT", "SLIGHT TREND"):
                    continue
                pct = pct_chg.get(col)
                direction = "HIGH" if (pct is not None and pct == pct and pct > 0) else "LOW"
                rec = _rec_for(base, direction)
                if rec not in recs:
                    recs.append(rec)
                break
            if len(recs) >= 2:
                break

    # -- 3. High variability fallback ------------------------------------------
    if len(recs) < 2:
        hi_var_bases = [_strip_prefix(c) for c, l in var_lbls.items() if l == "HIGH VAR"]
        for base in hi_var_bases[:1]:
            rec = f"Unstable {base.replace('_', ' ')} -- inspect sand reclamation and mixing consistency"
            if rec not in recs:
                recs.append(rec)

    # -- 4. Oscillation fallback -----------------------------------------------
    if len(recs) < 2:
        osc_bases = [_strip_prefix(c) for c, l in osc_lbls.items() if l == "OSCILLATING"]
        for base in osc_bases[:1]:
            rec = f"{base.replace('_', ' ').title()} cycling -- check addition control loops and feeder consistency"
            if rec not in recs:
                recs.append(rec)

    if not recs:
        if final_alert in ("STABLE", "WATCH"):
            return "Process within acceptable range -- continue monitoring"
        return "Review all sand additions and process parameters closely"

    return "  .  ".join(recs[:2])


def build_window_root_cause(result: dict, config: dict) -> str:
    """
    Root cause summary for window mode (sigma-band alerting).
    Describes deviation direction and magnitude relative to the baseline mean.
    """
    display        = config.get("display_names", {})
    param_labels   = result.get("param_labels", {})
    raw_values     = result.get("raw_values", {})
    baseline_means = result.get("baseline_means", {})
    pct_changes    = result.get("pct_changes", {})
    deviations     = result.get("deviations", {})

    _RANK = {"STABLE": 0, "ALERT": 1, "CRITICAL": 2, "!! WARNING": 3}

    deviating = sorted(
        [(col, lbl) for col, lbl in param_labels.items()
         if col.startswith("ps_") and _RANK.get(str(lbl), 0) > 0],
        key=lambda x: -_RANK.get(str(x[1]), 0),
    )

    parts = []
    for col, lbl in deviating[:3]:
        bare   = col[3:]
        name   = display.get(bare) or display.get(col) or bare.replace("_", " ").title()
        actual = raw_values.get(col)
        b_mean = baseline_means.get(bare)

        if actual is None:
            continue

        if b_mean is not None and abs(float(b_mean)) > 1e-12:
            pct_dev   = (float(actual) - float(b_mean)) / float(b_mean) * 100
            direction = "above" if pct_dev > 0 else "below"
            dev_str   = deviations.get(col, "")
            suffix    = ""
            if isinstance(dev_str, str) and dev_str.startswith("Deviated"):
                suffix = " — LCL/UCL breach"
            parts.append(
                f"{name} {abs(pct_dev):.1f}% {direction} baseline mean "
                f"(current {actual:.3g}, baseline {b_mean:.3g}){suffix}"
            )
        else:
            pct = pct_changes.get(col)
            if pct is not None:
                parts.append(f"{name} {pct:+.1f}% change from previous  [{lbl}]")
            else:
                parts.append(f"{name} outside stable sigma range  [{lbl}]")

    return "  |  ".join(parts) if parts else "Prepared sand within normal sigma range"


def build_window_recommendation(result: dict, config: dict = None) -> str:
    """
    Recommendation for window mode: uses baseline mean direction to look up
    the same parameter-specific advice table as the SI-mode recommendation.
    """
    param_labels   = result.get("param_labels", {})
    raw_values     = result.get("raw_values", {})
    baseline_means = result.get("baseline_means", {})
    pct_changes    = result.get("pct_changes", {})

    _RANK = {"STABLE": 0, "ALERT": 1, "CRITICAL": 2, "!! WARNING": 3}

    deviating = sorted(
        [(col, lbl) for col, lbl in param_labels.items()
         if col.startswith("ps_") and _RANK.get(str(lbl), 0) > 0],
        key=lambda x: -_RANK.get(str(x[1]), 0),
    )

    recs = []
    for col, _ in deviating[:2]:
        bare   = col[3:]
        actual = raw_values.get(col)
        b_mean = baseline_means.get(bare)

        if actual is not None and b_mean is not None and abs(float(b_mean)) > 1e-12:
            direction = "HIGH" if float(actual) > float(b_mean) else "LOW"
        else:
            pct = pct_changes.get(col)
            direction = "HIGH" if (pct is not None and pct > 0) else "LOW"

        rec = _rec_for(bare, direction)
        if rec not in recs:
            recs.append(rec)

    return "  .  ".join(recs) if recs else \
        "Verify recent prepared sand additions and compare against prescription targets"


def write_excel(
    all_results: list[dict],
    df_merged:   pd.DataFrame,
    var_df:      pd.DataFrame,
    drift_df:    pd.DataFrame,
    osc_df:      pd.DataFrame,
    dev_df:      pd.DataFrame,
    pct_df:      pd.DataFrame,
    si_df:       pd.DataFrame,
    param_si_df: pd.DataFrame,
    config:      dict,
    output_path: Optional[str] = None,
    db_limits:   Optional[dict] = None,
    wcv_df:      Optional[pd.DataFrame] = None,
) -> str:
    """Write the full watchdog analysis to a multi-sheet Excel workbook."""
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font
    except ImportError:
        logger.error("openpyxl not installed. Run: pip install openpyxl")
        return ""

    path       = output_path or config["output"].get("excel_path", "watchdog_output.xlsx")
    key_cols   = (
        ["period_key", "date"]
        + (["shift"]        if "shift"        in df_merged.columns else [])
        + (["component_id"] if "component_id" in df_merged.columns else [])
    )

    # Only include monitored parameters that also have at least one real value
    _params_cfg = config.get("parameters", {})
    _monitored  = set()
    for _grp in ("prepared_sand", "consumption", "additive", "prepared_sand_extra"):
        _monitored.update(_params_cfg.get(_grp, []))

    def _keep_col(col: str) -> bool:
        if _monitored and _strip_prefix(col) not in _monitored:
            return False
        return pd.to_numeric(df_merged[col], errors="coerce").notna().any()

    param_cols = [c for c in df_merged.columns
                  if c not in set(key_cols) and _keep_col(c)]
    window     = config["engines"]["window"]

    dash = df_merged[key_cols].copy()
    for c in param_cols:
        dash[c] = pd.to_numeric(df_merged[c], errors="coerce").round(4)
    dash["SI Score"]    = si_df["si_score_100"].values
    dash["SI Alert"]    = si_df["si_alert"].values
    dash["Final Alert"] = (
        pd.Series([r["final_alert"] for r in all_results], index=si_df.index)
        if all_results else si_df["si_alert"]
    )

    si_sheet = df_merged[key_cols].copy()
    si_sheet["SI Score (0-100)"] = si_df["si_score_100"]
    si_sheet["Variance Risk"]    = si_df["si_variance_risk"].round(4)
    si_sheet["Drift Risk"]       = si_df["si_drift_risk"].round(4)
    si_sheet["Oscillation Risk"] = si_df["si_oscillation_risk"].round(4)
    si_sheet["SI Alert"]         = si_df["si_alert"]
    si_sheet["Final Alert"]      = dash["Final Alert"]
    if all_results:
        si_sheet["Root Cause"]     = [r["root_cause"]     for r in all_results]
        si_sheet["Recommendation"] = [r["recommendation"] for r in all_results]

    _wcv = wcv_df if (wcv_df is not None and not wcv_df.empty) else pd.DataFrame()

    alerts_sheet = _build_alerts_sheet(
        all_results, df_merged, var_df, drift_df, osc_df,
        dev_df, pct_df, param_si_df, param_cols, config, window,
        wcv_df=_wcv,
    )

    trend_sheet    = _build_param_status_trend(
        all_results, df_merged, param_si_df, si_df, param_cols, config
    )
    baseline_sheet = _build_baseline_stats_sheet(
        df_merged, param_cols, config, db_limits or {}
    )

    # Explainer for the latest period
    explainer_sheet = build_shift_explainer(
        df_merged, var_df, drift_df, osc_df, dev_df, pct_df,
        si_df, param_si_df, param_cols, config,
        db_limits=db_limits or {},
        target_idx=df_merged.index[-1],
        wcv_df=_wcv,
    )

    sheets = {
        "Alerts"             : alerts_sheet,
        "SI_Alerts"          : si_sheet,
        "SI_Parameter_Trend" : trend_sheet,
        "Baseline_Stats"     : baseline_sheet,
        "Latest_Shift_Expl"  : explainer_sheet,
        "Dashboard"          : dash,
        "Deviation_Alerts"   : pd.concat([df_merged[key_cols], dev_df],   axis=1),
        "Pct_Change"         : pd.concat([df_merged[key_cols], pct_df],   axis=1),
        "Variance_Detail"    : pd.concat([df_merged[key_cols], var_df],   axis=1),
        "Drift_Detail"       : pd.concat([df_merged[key_cols], drift_df], axis=1),
        "Oscillation_Detail" : pd.concat([df_merged[key_cols], osc_df],   axis=1),
    }

    # Within-component batch variance detail (component mode only)
    if not _wcv.empty:
        sheets["WCV_Detail"] = pd.concat([df_merged[key_cols], _wcv], axis=1)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, sheet_df in sheets.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

    wb = openpyxl.load_workbook(path)
    _format_alert_sheet(wb["SI_Alerts"],          si_sheet)
    _format_alert_sheet(wb["Alerts"],             alerts_sheet)
    _format_trend_sheet(wb["SI_Parameter_Trend"], trend_sheet)
    _format_baseline_sheet(wb["Baseline_Stats"],  baseline_sheet)
    _format_explainer_sheet(wb["Latest_Shift_Expl"])
    wb.save(path)

    logger.info("Excel written: %s", path)
    return path


def _build_alerts_sheet(
    all_results:  list[dict],
    df_merged:    pd.DataFrame,
    var_df:       pd.DataFrame,
    drift_df:     pd.DataFrame,
    osc_df:       pd.DataFrame,
    dev_df:       pd.DataFrame,
    pct_df:       pd.DataFrame,
    param_si_df:  pd.DataFrame,
    param_cols:   list[str],
    config:       dict,
    window:       int,
    wcv_df:       pd.DataFrame = None,
) -> pd.DataFrame:
    """Build the Alerts sheet -- one row per parameter for the latest period."""
    if not all_results or df_merged.empty:
        return pd.DataFrame()

    display  = config.get("display_names", {})
    last_idx = df_merged.index[-1]
    prev_idx = df_merged.index[max(0, len(df_merged) - window - 1)]

    rows = []
    for col in param_cols:
        if col not in df_merged.columns:
            continue

        base  = _strip_prefix(col)
        label = display.get(base, base)

        try:
            latest_val = round(float(pd.to_numeric(df_merged.at[last_idx, col], errors="coerce")), 4)
        except Exception:
            latest_val = None

        try:
            prev_val = float(pd.to_numeric(df_merged.at[prev_idx, col], errors="coerce"))
            if latest_val is not None and not np.isnan(prev_val) and abs(prev_val) > 1e-12:
                if   latest_val > prev_val * 1.005: trend = "up"
                elif latest_val < prev_val * 0.995: trend = "down"
                else:                               trend = "flat"
            else:
                trend = "flat"
        except Exception:
            trend = "flat"

        si_score  = _safe_val(param_si_df, last_idx, f"si_param_{col}")
        si_lbl    = _safe_str(param_si_df, last_idx, f"si_label_{col}")
        drift_lbl = _safe_str(drift_df,    last_idx, f"drift_label_{col}")
        osc_lbl   = _safe_str(osc_df,      last_idx, f"osc_label_{col}")
        var_lbl   = _safe_str(var_df,      last_idx, f"var_label_{col}")
        dev_str   = _safe_str(dev_df,      last_idx, f"dev_status_{col}") or "OK"
        pct_val   = _safe_val(pct_df,      last_idx, f"pct_{col}")
        pct_str   = f"{pct_val:+.2f}%" if pct_val is not None else "N/A"

        # Within-component batch variance label (add_ columns only)
        _wcv_df = wcv_df if wcv_df is not None else pd.DataFrame()
        wcv_lbl = (
            _safe_str(_wcv_df, last_idx, f"wcv_label_{col}")
            if col.startswith("add_") and not _wcv_df.empty
            else ""
        )

        pct_warn = float(config.get("pct_change_warning", 5.0))
        is_dev   = isinstance(dev_str, str) and dev_str.startswith("Deviated")
        is_pct   = pct_val is not None and abs(pct_val) > pct_warn
        is_alert = si_lbl in ("ALERT", "CRITICAL")
        final    = "!! WARNING" if (is_dev or (is_alert and is_pct)) else (si_lbl or "STABLE")

        rows.append({
            "Parameter"       : label,
            "Latest Value"    : latest_val,
            "SI Score"        : round(si_score, 1) if si_score is not None else None,
            "SI Status"       : si_lbl,
            "Trend"           : trend,
            "Drift Component" : drift_lbl,
            "Osc Component"   : osc_lbl,
            "Var Component"   : var_lbl,
            "Batch Var"       : wcv_lbl,
            "Deviation Alert" : dev_str,
            "% Change"        : pct_str,
            "FINAL STATUS"    : final,
        })

    return pd.DataFrame(rows)


def build_shift_explainer(
    df_merged:   pd.DataFrame,
    var_df:      pd.DataFrame,
    drift_df:    pd.DataFrame,
    osc_df:      pd.DataFrame,
    dev_df:      pd.DataFrame,
    pct_df:      pd.DataFrame,
    si_df:       pd.DataFrame,
    param_si_df: pd.DataFrame,
    param_cols:  list[str],
    config:      dict,
    db_limits:   Optional[dict] = None,
    target_idx:  Optional[int]  = None,
    wcv_df:      Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Full step-by-step explainer for one shift period.

    One row per parameter. Columns:
      Parameter | Final Status | SI Score
      Current Value | LCL | UCL | Deviation Status | % Change
      -- Baseline --
      Baseline Mean | Baseline Std | Baseline Variance
      -- Variance Engine (weight W%) --
      Window Variance | Var Ratio | Var Label | Var Score
      -- Drift Engine (weight W%) --
      Window Mean | Deviation (sigma) | OLS Slope | Slope Score | Drift Label | Drift Score
      -- Oscillation Engine (weight W%) --
      Sign Changes | Osc Label | Osc Score
      -- Window Values (last N shifts) --
      W-N ... W-1 (oldest -> current)

    A two-row header block above the parameter table shows overall shift
    info and the SI formula so the reader can verify the calculation.
    """
    if df_merged.empty:
        return pd.DataFrame()

    db_limits = db_limits or {}
    display   = config.get("display_names", {})
    window    = int(config["engines"]["window"])

    wv_pct = round(config.get("si_weights", {}).get("variance",    0.35) * 100)
    wd_pct = round(config.get("si_weights", {}).get("drift",       0.45) * 100)
    wo_pct = round(config.get("si_weights", {}).get("oscillation", 0.20) * 100)

    if target_idx is None:
        target_idx = df_merged.index[-1]

    # Baseline stats
    b_cfg   = config.get("baseline", {})
    b_start = b_cfg.get("start_date", "")
    b_end   = b_cfg.get("end_date",   "")
    if b_start and b_end and "date" in df_merged.columns:
        start = pd.to_datetime(b_start).date()
        end   = pd.to_datetime(b_end).date()
        base_df = df_merged[(df_merged["date"] >= start) & (df_merged["date"] <= end)]
    else:
        base_df = df_merged

    # Period identity
    period_key = df_merged.at[target_idx, "period_key"] if "period_key" in df_merged.columns else str(target_idx)
    shift_date = df_merged.at[target_idx, "date"]       if "date"       in df_merged.columns else ""
    shift_no   = df_merged.at[target_idx, "shift"]      if "shift"      in df_merged.columns else ""

    # Overall SI for this period
    overall_si    = float(si_df.at[target_idx, "si_score_100"])
    overall_alert = str(si_df.at[target_idx, "si_alert"])
    var_risk      = float(si_df.at[target_idx, "si_variance_risk"])
    drift_risk    = float(si_df.at[target_idx, "si_drift_risk"])
    osc_risk      = float(si_df.at[target_idx, "si_oscillation_risk"])

    # Positional index of target row (for window slice)
    all_idx = list(df_merged.index)
    pos     = all_idx.index(target_idx)
    win_start = max(0, pos - window + 1)
    window_rows = df_merged.iloc[win_start : pos + 1]

    rows = []

    # -- Metadata header rows (blank parameter) -------------------------------
    _wcv = wcv_df if (wcv_df is not None and not wcv_df.empty) else pd.DataFrame()

    rows.append({
        "Parameter"                        : f"PERIOD : {period_key}",
        "Final Status"                     : overall_alert,
        "SI Score (0-100)"                 : overall_si,
        "Current Value"                    : f"Date: {shift_date}  Shift: {shift_no}",
        "LCL"                              : "",
        "UCL"                              : "",
        "Deviation Status"                 : "",
        "% Change"                         : "",
        "Baseline Mean"                    : f"Baseline: {b_start} to {b_end}",
        "Baseline Std Dev"                 : f"Window N: {window}",
        "Baseline Variance (avg)"          : f"Formula: SI = {wd_pct}%xDrift + {wv_pct}%xVariance + {wo_pct}%xOscillation",
        f"Window Variance"                 : f"Var risk={var_risk:.4f}",
        "Var Ratio (window/baseline)"      : f"Drift risk={drift_risk:.4f}",
        f"Var Label"                       : f"Osc risk={osc_risk:.4f}",
        "Var Score (0-1)"                  : "",
        "Window Mean"                      : "",
        "Deviation from Baseline (?)"      : "",
        "OLS Slope (units/shift)"          : "",
        "Slope Score (0-1)"                : "",
        "Drift Label"                      : "",
        "Drift Score (0-1)"                : "",
        "Sign Changes in Window"           : "",
        "Oscillation Label"                : "",
        "Oscillation Score (0-1)"          : "",
        "Batch VAR.P"                      : "Within-component batch spread" if not _wcv.empty else "",
        "Batch Var Ratio"                  : "",
        "Batch Var Label"                  : "",
        "Batch Var Score (0-1)"            : "",
        **{f"W{i+1}" : "" for i in range(window)},
    })

    # -- Per-parameter rows ----------------------------------------------------
    for col in param_cols:
        if col not in df_merged.columns:
            continue

        base  = _strip_prefix(col)
        label = display.get(base, base)

        # Current value
        cur_val = _safe_val(df_merged, target_idx, col)

        # Control limits
        lim = db_limits.get(base, {})
        lcl = round(float(lim["lcl"]), 4) if lim.get("lcl") is not None else None
        ucl = round(float(lim["ucl"]), 4) if lim.get("ucl") is not None else None

        dev_status = _safe_str(dev_df,      target_idx, f"dev_status_{col}") or "OK"
        pct_val    = _safe_val(pct_df,      target_idx, f"pct_{col}")
        pct_str    = f"{pct_val:+.2f}%" if pct_val is not None else "N/A"

        # Baseline stats for this column
        if col in base_df.columns:
            b_series = pd.to_numeric(base_df[col], errors="coerce").dropna()
            b_mean   = round(float(b_series.mean()), 4)   if not b_series.empty else None
            b_std    = round(float(b_series.std()),  4)   if not b_series.empty else None
            b_var    = round(float(b_series.var()),  6)   if not b_series.empty else None
        else:
            b_mean = b_std = b_var = None

        # Variance engine
        w_var   = _safe_val(var_df,   target_idx, f"var_rolling_var_{col}")
        v_ratio = _safe_val(var_df,   target_idx, f"var_ratio_{col}")
        v_label = _safe_str(var_df,   target_idx, f"var_label_{col}")
        v_score = _safe_val(var_df,   target_idx, f"var_score_{col}")

        # Drift engine
        w_mean   = _safe_val(drift_df, target_idx, f"drift_mean_{col}")
        d_dev    = _safe_val(drift_df, target_idx, f"drift_dev_{col}")
        d_slope  = _safe_val(drift_df, target_idx, f"drift_slope_{col}")
        d_slsco  = _safe_val(drift_df, target_idx, f"drift_slope_score_{col}")
        d_label  = _safe_str(drift_df, target_idx, f"drift_label_{col}")
        d_score  = _safe_val(drift_df, target_idx, f"drift_score_{col}")

        # Oscillation engine
        osc_cnt   = _safe_val(osc_df,  target_idx, f"osc_count_{col}")
        osc_label = _safe_str(osc_df,  target_idx, f"osc_label_{col}")
        osc_score = _safe_val(osc_df,  target_idx, f"osc_score_{col}")

        # Per-param SI
        p_si    = _safe_val(param_si_df, target_idx, f"si_param_{col}")
        p_label = _safe_str(param_si_df, target_idx, f"si_label_{col}") or "STABLE"

        # Window values (W1 = oldest, WN = current)
        w_vals = {}
        w_series = pd.to_numeric(window_rows[col], errors="coerce") if col in window_rows.columns else pd.Series([], dtype=float)
        vals = w_series.tolist()
        # Left-pad with None if fewer than window rows available
        padded = [None] * (window - len(vals)) + vals
        for i, v in enumerate(padded):
            w_vals[f"W{i+1}"] = round(float(v), 4) if v is not None and v == v else None

        # Batch variance (within-component) -- add_ columns only
        if col.startswith("add_") and not _wcv.empty:
            wcv_var   = _safe_val(_wcv, target_idx, f"wcv_var_{col}")
            wcv_ratio = _safe_val(_wcv, target_idx, f"wcv_ratio_{col}")
            wcv_lbl   = _safe_str(_wcv, target_idx, f"wcv_label_{col}")
            wcv_score = _safe_val(_wcv, target_idx, f"wcv_score_{col}")
        else:
            wcv_var = wcv_ratio = wcv_lbl = wcv_score = None

        rows.append({
            "Parameter"                    : label,
            "Final Status"                 : p_label,
            "SI Score (0-100)"             : p_si,
            "Current Value"                : cur_val,
            "LCL"                          : lcl,
            "UCL"                          : ucl,
            "Deviation Status"             : dev_status,
            "% Change"                     : pct_str,
            "Baseline Mean"                : b_mean,
            "Baseline Std Dev"             : b_std,
            "Baseline Variance (avg)"      : b_var,
            "Window Variance"              : w_var,
            "Var Ratio (window/baseline)"  : v_ratio,
            "Var Label"                    : v_label,
            "Var Score (0-1)"              : v_score,
            "Window Mean"                  : w_mean,
            "Deviation from Baseline (?)"  : d_dev,
            "OLS Slope (units/shift)"      : d_slope,
            "Slope Score (0-1)"            : d_slsco,
            "Drift Label"                  : d_label,
            "Drift Score (0-1)"            : d_score,
            "Sign Changes in Window"       : int(osc_cnt) if osc_cnt is not None else None,
            "Oscillation Label"            : osc_label,
            "Oscillation Score (0-1)"      : osc_score,
            "Batch VAR.P"                  : wcv_var,
            "Batch Var Ratio"              : wcv_ratio,
            "Batch Var Label"              : wcv_lbl,
            "Batch Var Score (0-1)"        : wcv_score,
            **w_vals,
        })

    return pd.DataFrame(rows)


def _format_explainer_sheet(ws) -> None:
    """Style the explainer sheet: colour alert cells, header row, freeze panes."""
    try:
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    except ImportError:
        return

    if ws.max_row < 2:
        return

    header = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]

    # Header row
    hdr_fill = PatternFill("solid", fgColor="1F4E79")
    hdr_font = Font(color="FFFFFF", bold=True, size=9)
    for c in range(1, ws.max_column + 1):
        cell = ws.cell(1, c)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    # Colour-coded status columns
    status_cols = {"Final Status", "Var Label", "Drift Label", "Oscillation Label", "Deviation Status", "Batch Var Label"}

    # Column widths
    for c_idx, h in enumerate(header, start=1):
        letter = ws.cell(1, c_idx).column_letter
        if h == "Parameter":
            ws.column_dimensions[letter].width = 28
        elif h in ("Final Status", "Var Label", "Drift Label", "Oscillation Label"):
            ws.column_dimensions[letter].width = 15
        elif h == "Deviation Status":
            ws.column_dimensions[letter].width = 20
        elif h in ("Baseline Mean", "Baseline Std Dev", "Baseline Variance (avg)",
                   "Window Mean", "Window Variance"):
            ws.column_dimensions[letter].width = 16
        elif h and h.startswith("W"):
            ws.column_dimensions[letter].width = 10
        else:
            ws.column_dimensions[letter].width = 14

    ws.freeze_panes = ws.cell(3, 2)   # freeze header + metadata row, freeze Parameter col

    # Metadata row (row 2) -- light blue background
    meta_fill = PatternFill("solid", fgColor="BDD7EE")
    for c in range(1, ws.max_column + 1):
        cell = ws.cell(2, c)
        cell.fill = meta_fill
        cell.font = Font(bold=True, size=9)
        cell.alignment = Alignment(horizontal="left")

    # Data rows (row 3 onward)
    for row in range(3, ws.max_row + 1):
        for c_idx, h in enumerate(header, start=1):
            cell = ws.cell(row, c_idx)
            cell.alignment = Alignment(horizontal="center")

            if h == "Parameter":
                cell.alignment = Alignment(horizontal="left")
                cell.font = Font(bold=True, size=9)
                continue

            if h in status_cols:
                val = str(cell.value or "")
                bg  = ALERT_COLORS.get(val, "FFFFFF")
                fg  = ALERT_FONT_COLORS.get(val, "000000")
                cell.fill = PatternFill("solid", fgColor=bg)
                cell.font = Font(color=fg, bold=(val in ("CRITICAL", "!! WARNING", "ALERT")), size=9)

            # Highlight Deviation from Baseline (?): yellow >1, orange >2
            elif h == "Deviation from Baseline (?)":
                try:
                    v = float(cell.value)
                    if   v >= 2.0: cell.fill = PatternFill("solid", fgColor="FFC7CE")
                    elif v >= 1.0: cell.fill = PatternFill("solid", fgColor="FFEB9C")
                except (TypeError, ValueError):
                    pass

            # Highlight Var Ratio: yellow >2, orange >3.5, red >6
            elif h == "Var Ratio (window/baseline)":
                try:
                    v = float(cell.value)
                    if   v >= 6.0: cell.fill = PatternFill("solid", fgColor="FFC7CE")
                    elif v >= 3.5: cell.fill = PatternFill("solid", fgColor="FFEB9C")
                except (TypeError, ValueError):
                    pass

            # Window values: subtle alternating colour
            elif h and h.startswith("W") and len(h) <= 3:
                cell.fill = PatternFill("solid", fgColor="F2F2F2")
                cell.font = Font(size=8)


def _build_baseline_stats_sheet(
    df_merged:  pd.DataFrame,
    param_cols: list[str],
    config:     dict,
    db_limits:  dict,
) -> pd.DataFrame:
    """
    One row per parameter.  Columns:
      Parameter | Baseline Period | Shifts | Mean | Std Dev | Min | Max
      | LCL | UCL | Last Value | Diff from Baseline Mean | Diff %

    'Baseline Period' = config baseline.start_date - end_date (e.g. July 2025).
    'Last Value'      = most recent non-null value in the full dataset.
    This lets users see at a glance how the current process compares to the
    best-period reference used inside every SI calculation.
    """
    b_cfg  = config.get("baseline", {})
    b_start = b_cfg.get("start_date", "")
    b_end   = b_cfg.get("end_date",   "")
    display = config.get("display_names", {})

    start = pd.to_datetime(b_start).date() if b_start else None
    end   = pd.to_datetime(b_end).date()   if b_end   else None

    if start and end and "date" in df_merged.columns:
        mask     = (df_merged["date"] >= start) & (df_merged["date"] <= end)
        base_df  = df_merged[mask]
    else:
        base_df  = df_merged

    baseline_label = f"{b_start} to {b_end}" if b_start and b_end else "All data"

    rows = []
    for col in param_cols:
        if col not in df_merged.columns:
            continue

        base  = _strip_prefix(col)
        label = display.get(base, base)

        series     = pd.to_numeric(base_df[col],    errors="coerce").dropna()
        full_series = pd.to_numeric(df_merged[col], errors="coerce").dropna()

        if series.empty:
            b_mean = b_std = b_min = b_max = None
        else:
            b_mean = round(float(series.mean()), 4)
            b_std  = round(float(series.std()),  4)
            b_min  = round(float(series.min()),  4)
            b_max  = round(float(series.max()),  4)

        last_val = round(float(full_series.iloc[-1]), 4) if not full_series.empty else None

        # Control limits from db_limits
        lim   = db_limits.get(base, {})
        lcl   = lim.get("lcl")
        ucl   = lim.get("ucl")
        lcl   = round(float(lcl), 4) if lcl is not None else None
        ucl   = round(float(ucl), 4) if ucl is not None else None

        # Difference: last value vs baseline mean
        if last_val is not None and b_mean is not None:
            diff     = round(last_val - b_mean, 4)
            diff_pct = round((diff / b_mean) * 100, 2) if abs(b_mean) > 1e-12 else None
        else:
            diff = diff_pct = None

        # Status: is last value within baseline ? 2??
        if last_val is not None and b_mean is not None and b_std is not None and b_std > 0:
            z = abs(last_val - b_mean) / b_std
            if   z <= 1.0: status = "STABLE"
            elif z <= 2.0: status = "WATCH"
            elif z <= 3.0: status = "ALERT"
            else:          status = "CRITICAL"
        else:
            status = ""

        rows.append({
            "Parameter"            : label,
            "Baseline Period"      : baseline_label,
            "Baseline Shifts"      : len(series),
            "Baseline Mean"        : b_mean,
            "Baseline Std Dev"     : b_std,
            "Baseline Min"         : b_min,
            "Baseline Max"         : b_max,
            "LCL"                  : lcl,
            "UCL"                  : ucl,
            "Latest Value"         : last_val,
            "Diff from Baseline"   : diff,
            "Diff %"               : diff_pct,
            "Status vs Baseline"   : status,
        })

    return pd.DataFrame(rows)


def _format_baseline_sheet(ws, df: pd.DataFrame) -> None:
    """Colour the 'Status vs Baseline' column and style the header."""
    try:
        from openpyxl.styles import PatternFill, Font, Alignment
    except ImportError:
        return

    if df.empty:
        return

    header = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]

    # Header styling
    hdr_fill = PatternFill("solid", fgColor="1F4E79")
    hdr_font = Font(color="FFFFFF", bold=True, size=9)
    for c in range(1, ws.max_column + 1):
        cell = ws.cell(1, c)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    # Column widths
    col_widths = {
        "Parameter"          : 30,
        "Baseline Period"    : 24,
        "Baseline Shifts"    : 14,
        "Baseline Mean"      : 14,
        "Baseline Std Dev"   : 14,
        "Baseline Min"       : 12,
        "Baseline Max"       : 12,
        "LCL"                : 10,
        "UCL"                : 10,
        "Latest Value"       : 13,
        "Diff from Baseline" : 18,
        "Diff %"             : 10,
        "Status vs Baseline" : 18,
    }
    for c_idx, col_name in enumerate(header, start=1):
        ws.column_dimensions[ws.cell(1, c_idx).column_letter].width = col_widths.get(col_name, 14)

    ws.freeze_panes = ws.cell(2, 1)

    # Find status column
    status_col = next((i + 1 for i, h in enumerate(header) if h == "Status vs Baseline"), None)
    diff_col   = next((i + 1 for i, h in enumerate(header) if h == "Diff %"), None)

    for row in range(2, ws.max_row + 1):
        # Colour status cell
        if status_col:
            cell = ws.cell(row, status_col)
            val  = str(cell.value or "")
            bg   = ALERT_COLORS.get(val, "FFFFFF")
            fg   = ALERT_FONT_COLORS.get(val, "000000")
            cell.fill = PatternFill("solid", fgColor=bg)
            cell.font = Font(color=fg, bold=(val in ("CRITICAL", "ALERT")), size=9)
            cell.alignment = Alignment(horizontal="center")

        # Colour Diff % cell: red if > 10%, orange if > 5%
        if diff_col:
            dcell = ws.cell(row, diff_col)
            try:
                dval = float(dcell.value)
                if   abs(dval) > 10: dcell.fill = PatternFill("solid", fgColor="FFC7CE")
                elif abs(dval) > 5:  dcell.fill = PatternFill("solid", fgColor="FFEB9C")
            except (TypeError, ValueError):
                pass


def _build_param_status_trend(
    all_results:  list[dict],
    df_merged:    pd.DataFrame,
    param_si_df:  pd.DataFrame,
    si_df:        pd.DataFrame,
    param_cols:   list[str],
    config:       dict,
) -> pd.DataFrame:
    """
    One row per period.  Columns:
      Period Key | Date | Shift | SI Score | Final Alert
      | <Param 1> SI Score | <Param 1> Status | <Param 2> SI Score | ...

    Each parameter gets two columns: numeric SI score (0-100) and status label.
    Status label columns are colour-coded by alert level.
    """
    if df_merged.empty:
        return pd.DataFrame()

    display  = config.get("display_names", {})
    key_cols = (
        ["period_key", "date"]
        + (["shift"]        if "shift"        in df_merged.columns else [])
        + (["component_id"] if "component_id" in df_merged.columns else [])
    )

    rows = []
    for idx, result in zip(df_merged.index, all_results):
        row: dict = {}
        for kc in key_cols:
            row[kc] = df_merged.at[idx, kc]
        row["SI Score"]    = round(float(si_df.at[idx, "si_score_100"]), 1)
        row["Final Alert"] = result.get("final_alert", "")

        for col in param_cols:
            label = display.get(_strip_prefix(col), _strip_prefix(col))

            si_score = result.get("param_si", {}).get(col)
            if si_score is None:
                si_score = _safe_val(param_si_df, idx, f"si_param_{col}")
            si_score = round(float(si_score), 1) if si_score is not None else None

            si_status = result.get("param_labels", {}).get(col, "")
            if not si_status:
                si_status = _safe_str(param_si_df, idx, f"si_label_{col}") or "STABLE"

            row[f"{label} SI"]     = si_score
            row[f"{label} Status"] = si_status

        rows.append(row)

    return pd.DataFrame(rows)


def _format_trend_sheet(ws, df: pd.DataFrame) -> None:
    """
    Colour every *Status cell by alert level; leave *SI score cells plain.
    Fixed columns (period_key, date, shift, component_id, SI Score) are left-aligned.
    """
    try:
        from openpyxl.styles import PatternFill, Font, Alignment
    except ImportError:
        return

    if df.empty:
        return

    header = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]

    fixed_plain  = {"period_key", "date", "shift", "component_id", "SI Score"}
    fixed_colour = {"Final Alert"}

    # Style the header row
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True, size=9)
    for c in range(1, ws.max_column + 1):
        cell = ws.cell(1, c)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    # Column widths
    for c_idx, col_name in enumerate(header, start=1):
        letter = ws.cell(1, c_idx).column_letter
        if col_name == "period_key":
            ws.column_dimensions[letter].width = 22
        elif col_name == "date":
            ws.column_dimensions[letter].width = 12
        elif col_name in ("shift", "component_id"):
            ws.column_dimensions[letter].width = 10
        elif col_name == "SI Score":
            ws.column_dimensions[letter].width = 10
        elif col_name == "Final Alert":
            ws.column_dimensions[letter].width = 13
        elif col_name and col_name.endswith(" SI"):
            ws.column_dimensions[letter].width = 9     # numeric score — narrow
        elif col_name and col_name.endswith(" Status"):
            ws.column_dimensions[letter].width = 13    # label — slightly wider
        else:
            ws.column_dimensions[letter].width = 13

    # Freeze after the identity columns actually present in this sheet
    freeze_col = sum(1 for h in header if h in fixed_plain) + 1
    freeze_col = min(freeze_col, ws.max_column)
    ws.freeze_panes = ws.cell(2, freeze_col)

    # Colour data rows
    for row in range(2, ws.max_row + 1):
        for c_idx, col_name in enumerate(header, start=1):
            cell = ws.cell(row, c_idx)
            cell.alignment = Alignment(horizontal="center")

            if col_name in fixed_plain:
                cell.alignment = Alignment(horizontal="left")
                continue

            # Colour only Status columns (and Final Alert)
            if col_name in fixed_colour or (col_name and col_name.endswith(" Status")):
                val = str(cell.value or "")
                bg  = ALERT_COLORS.get(val, "FFFFFF")
                fg  = ALERT_FONT_COLORS.get(val, "000000")
                cell.fill = PatternFill("solid", fgColor=bg)
                cell.font = Font(
                    color=fg,
                    bold=(val in ("CRITICAL", "!! WARNING", "ALERT")),
                    size=9,
                )
            elif col_name and col_name.endswith(" SI"):
                # Score column: light grey background, small font
                cell.fill = PatternFill("solid", fgColor="F2F2F2")
                cell.font = Font(size=9)


def _format_alert_sheet(ws, df: pd.DataFrame) -> None:
    """Apply colour coding to alert status column."""
    try:
        from openpyxl.styles import PatternFill, Font
    except ImportError:
        return

    header    = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    alert_col = None
    for candidate in ("FINAL STATUS", "Final Alert", "SI Status", "SI Alert"):
        if candidate in header:
            alert_col = header.index(candidate) + 1
            break

    if alert_col is None:
        return

    for row in range(2, ws.max_row + 1):
        alert_val = ws.cell(row, alert_col).value or ""
        bg   = ALERT_COLORS.get(alert_val, "FFFFFF")
        fg   = ALERT_FONT_COLORS.get(alert_val, "000000")
        fill = PatternFill("solid", fgColor=bg)
        font = Font(color=fg, bold=(alert_val in ("CRITICAL", "!! WARNING")), size=9)
        ws.cell(row, alert_col).fill = fill
        ws.cell(row, alert_col).font = font


# --- Helpers ------------------------------------------------------------------

_PREFIXES = ("ps_", "con_", "add_", "pse_")

def _strip_prefix(col: str) -> str:
    for p in _PREFIXES:
        if col.startswith(p):
            return col[len(p):]
    return col


def _safe_val(df: pd.DataFrame, idx, col: str) -> Optional[float]:
    try:
        v = df.at[idx, col]
        f = float(v)
        return None if f != f else round(f, 4)
    except (KeyError, TypeError, ValueError):
        return None


def _safe_str(df: pd.DataFrame, idx, col: str) -> str:
    try:
        v = df.at[idx, col]
        return str(v) if v is not None else ""
    except (KeyError, TypeError):
        return ""
