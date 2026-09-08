"""
watchdog/property_alert_engine.py
-----------------------------------
Individual property-based deviation detection engine.

Monitors each PREPARED SAND (ps_) parameter individually.
Additive, consumption, and sieve data are used only as context
to reason about WHY a prepared sand property deviated — they
do NOT trigger alerts themselves.

TRIGGER CONDITIONS (any one is sufficient to raise an alert):
  1. 2 or more engines deviated   (variance + drift, drift + oscillation, etc.)
  2. LCL/UCL control limit breach  (always critical regardless of engine count)
  3. % change exceeds PCT_ALERT threshold (even if all engines are stable)

Single-engine deviations with no breach and small % change are suppressed —
they represent normal process noise and should not flood the alert feed.

SEVERITY:
  critical  — LCL/UCL breach  OR  3 engines triggered  OR  2 engines + STRONG labels
  warning   — 2 engines triggered (without strong labels)  OR  1 engine + breach
  watch     — % change alone exceeded threshold  OR  1 strong engine (HIGH VAR / STRONG DRIFT)

ROOT CAUSE FORMAT (human-readable narrative):
  § What:    Statistical description of the deviation with numbers
  § Why:     Likely causal link to additive/consumption/sieve data
  § Note:    Any additional context clues from related parameters

RECOMMENDATION FORMAT:
  § Action:  Specific corrective action with approximate magnitude
  § Monitor: What to watch after the correction
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Threshold constants ────────────────────────────────────────────────────────
PCT_ALERT  = 5.0   # % change that triggers an alert even without engine deviation
PCT_STRONG = 10.0  # % change considered a strong signal

# ── Engine deviation label sets ────────────────────────────────────────────────
_VAR_DEVIATED_STRONG   = {"HIGH VAR"}
_VAR_DEVIATED_MILD     = {"ELEVATED"}
_VAR_DEVIATED          = _VAR_DEVIATED_STRONG | _VAR_DEVIATED_MILD

_DRIFT_DEVIATED_STRONG = {"STRONG DRIFT", "STRONG TREND"}
_DRIFT_DEVIATED_MILD   = {"SLIGHT DRIFT", "SLIGHT TREND"}
_DRIFT_DEVIATED        = _DRIFT_DEVIATED_STRONG | _DRIFT_DEVIATED_MILD

_OSC_DEVIATED_STRONG   = {"OSCILLATING"}
_OSC_DEVIATED_MILD     = {"MILD"}
_OSC_DEVIATED          = _OSC_DEVIATED_STRONG | _OSC_DEVIATED_MILD

_PREFIXES = ("ps_", "con_", "add_", "pse_", "sv_")

# ── Causal relationship map ────────────────────────────────────────────────────
# Maps each prepared sand parameter to related additive/other parameters
# with a direction hint (+1 = direct, -1 = inverse, None = complex/varies)
# and a plain-English explanation of the relationship.
_CAUSE_MAP = {
    "active_clay": [
        ("bentonite",        +1, "Bentonite is the primary source of active clay in green sand"),
        ("bentonite_actual", +1, "Actual bentonite dosing directly sets the clay level"),
        ("lca",              +1, "LCA (Loss on Calcination at 500°C) tracks total clay content"),
        ("returnsand",       -1, "Higher return sand ratio dilutes active clay concentration"),
    ],
    "compactibility": [
        ("water_actual",     +1, "Water addition is the direct lever for compactibility"),
        ("total_water_ltr",  +1, "Total water volume in the mix"),
        ("active_clay",      +1, "Higher active clay amplifies the compactibility response to water"),
        ("temperature_c",    -1, "Elevated sand temperature causes faster moisture loss during mixing"),
    ],
    "moisture": [
        ("water_actual",     +1, "Water dosing directly sets moisture content"),
        ("total_water_ltr",  +1, "Total water added to the batch"),
        ("temperature_c",    -1, "High sand temperature drives off moisture — check post-mix temp"),
        ("active_clay",       0, "Clay fraction holds moisture — changes in clay affect moisture retention"),
    ],
    "loi": [
        ("coal_dust_actual", +1, "Coal dust is the primary contributor to Loss on Ignition"),
        ("lca",              +1, "LCA is a direct measure of total volatile / carbon content"),
        ("volatile_matter",  +1, "Volatile matter content of the coal dust batch"),
    ],
    "volatile_matter": [
        ("coal_dust_actual", +1, "Coal dust drives volatile matter in green sand"),
        ("lca",              +1, "LCA is a direct measure of volatile content alongside LOI"),
    ],
    "gfn_afs": [
        ("freshsilicasand",  None, "Fresh sand additions shift grain fineness — FSS AFS may differ from system AFS"),
        ("fss_actual",       None, "Fresh silica sand dosing rate"),
        ("returnsand",       None, "Return sand ratio affects overall AFS distribution"),
    ],
    "inert_fines": [
        ("freshsilicasand",  +1, "Fresh sand additions can introduce fines depending on sand grade"),
        ("loi",              None, "LOI changes indirectly relate to the inert fines fraction"),
        ("lca",              None, "LCA indirectly tracks non-clay non-volatile content"),
    ],
    "permeability": [
        ("moisture",         -1, "High moisture fills inter-grain voids, reducing permeability"),
        ("active_clay",      -1, "More active clay lowers permeability by closing the grain structure"),
        ("compactibility",   -1, "Very high compactibility is often correlated with reduced permeability"),
        ("bentonite_actual", -1, "Higher bentonite increases clay content which reduces permeability"),
    ],
    "gcs": [
        ("bentonite_actual", +1, "Bentonite is the primary binder that builds Green Compressive Strength"),
        ("active_clay",      +1, "Active clay is the binding agent — more clay means higher GCS"),
        ("moisture",         +1, "Moisture activates clay bonds — optimal moisture is critical for peak GCS"),
        ("compactibility",   +1, "Compactibility is directly related to GCS through clay-water bonding"),
    ],
    "shear_strength": [
        ("bentonite_actual", +1, "Bentonite builds shear resistance through clay bonding"),
        ("active_clay",      +1, "Active clay is the primary shear resistance agent"),
        ("moisture",         +1, "Moisture activates clay for shear resistance — underdosing weakens it"),
    ],
    "split_strength": [
        ("bentonite_actual", +1, "Bentonite provides the bond that resists splitting"),
        ("active_clay",      +1, "Active clay content is the structural binding agent for split strength"),
        ("moisture",         +1, "Moisture activates the clay bond"),
    ],
    "temp_of_sand_after_mix": [
        ("temperature_c",    +1, "Inlet or ambient sand temperature is the main driver of mix temperature"),
        ("water_actual",     -1, "Higher water addition provides evaporative cooling during mixing"),
    ],
    "compactability_smc_pct": [
        ("water_actual",     +1, "Water is the primary lever for SMC compactability"),
        ("moisture",         +1, "Moisture level in sand before SMC measurement"),
        ("active_clay",      +1, "Clay fraction responds to moisture to build compaction"),
    ],
}

# ── Recommendation library ─────────────────────────────────────────────────────
# (parameter, direction) -> (action, monitor)
_RECS = {
    ("active_clay",    "HIGH"): (
        "Reduce bentonite addition by 8–12% over the next 2–3 batches and re-measure active clay after each batch.",
        "Compactibility and GCS — they should drop proportionally if clay is being corrected."
    ),
    ("active_clay",    "LOW"): (
        "Increase bentonite dosing by 8–10%. Check if fresh sand rate increased recently, as dilution reduces active clay concentration.",
        "LCA, compactibility, and GCS — all should recover within 2–4 batches."
    ),
    ("compactibility", "HIGH"): (
        "Reduce water addition by 5–8%. If active clay is also elevated, reduce bentonite first as clay affects water demand.",
        "Moisture and permeability — over-compaction typically reduces permeability simultaneously."
    ),
    ("compactibility", "LOW"): (
        "Increase water addition by 3–5%. If active clay is low, address clay first — adding water to low-clay sand is ineffective.",
        "GCS and shear strength — they should improve alongside compactibility."
    ),
    ("moisture",       "HIGH"): (
        "Reduce water addition by 3–6 litres per batch. Check sand temperature — high temperature can cause the system to over-dose water to compensate.",
        "Compactibility and permeability — both are directly affected by moisture level."
    ),
    ("moisture",       "LOW"): (
        "Increase water addition by 3–5 litres. Check if sand temperature has risen, which would increase evaporation during mixing.",
        "Compactibility and GCS — they will confirm moisture correction within 1–2 batches."
    ),
    ("loi",            "HIGH"): (
        "Reduce coal dust addition by 10–15%. Excessive LOI increases rejection risk and affects permeability. Check LCA trend independently.",
        "Volatile matter and permeability — permeability typically decreases with very high LOI."
    ),
    ("loi",            "LOW"): (
        "Increase coal dust addition by 10–12%. Low LOI reduces lustrous carbon generation and increases metal penetration risk.",
        "Cast surface finish and rejection rate — verify over the next production run."
    ),
    ("volatile_matter","HIGH"): (
        "Reduce coal dust or switch to a lower VM coal blend. Excessively high VM can cause blow holes.",
        "Mould surface quality and blow-hole rejection rate."
    ),
    ("volatile_matter","LOW"): (
        "Increase coal dust dosing or check the coal batch quality — volatile content may have degraded.",
        "Cast surface finish and LOI — both should respond within 2–4 batches."
    ),
    ("gfn_afs",        "HIGH"): (
        "Reduce fresh sand addition or switch to a coarser FSS grade. Review recent FSS supplier batch AFS certificates.",
        "Permeability — finer AFS typically reduces permeability, which should also be monitored."
    ),
    ("gfn_afs",        "LOW"): (
        "Check fresh sand quality — supplier batch may be coarser than specification. Increase FSS addition rate temporarily.",
        "Permeability and casting surface quality — coarser AFS improves permeability but may affect surface finish."
    ),
    ("inert_fines",    "HIGH"): (
        "Review fresh sand addition rate and sand quality certificate. Increase return sand ratio slightly to dilute fines. Check for contamination sources.",
        "Permeability and compactibility — high inert fines reduce both properties."
    ),
    ("inert_fines",    "LOW"): (
        "No urgent corrective action typically needed for low inert fines. Monitor trend to ensure it stays within spec.",
        "Active clay — reduced inert fines is usually positive, but watch for clay concentration changes."
    ),
    ("permeability",   "HIGH"): (
        "Check moisture and active clay — high permeability with normal other properties may indicate under-dosed clay. Verify compactibility is in range.",
        "GCS and shear strength — both may be low if permeability is high due to clay deficiency."
    ),
    ("permeability",   "LOW"): (
        "Reduce moisture by 3–5 litres and check active clay. If clay is elevated, reduce bentonite first. High-clay, high-moisture sand consistently produces low permeability.",
        "Blow-hole rejection rate — low permeability is a direct casting defect risk."
    ),
    ("gcs",            "HIGH"): (
        "Review bentonite and water — both directly drive GCS. If compactibility is also elevated, reduce water first before touching bentonite.",
        "Compactibility and permeability — these typically shift with GCS."
    ),
    ("gcs",            "LOW"): (
        "Increase bentonite addition by 8–10%. If moisture is also low, correct water first — low moisture prevents clay from developing full strength.",
        "Shear strength and compactibility — both should recover alongside GCS."
    ),
    ("shear_strength", "HIGH"): (
        "Check bentonite and moisture — both drive shear strength. If GCS is also high, reduce bentonite gradually.",
        "Compactibility — excess shear strength often correlates with over-compacted sand."
    ),
    ("shear_strength", "LOW"): (
        "Increase bentonite by 5–8% or verify moisture is at target. Low shear strength significantly increases mould collapse risk.",
        "Mould rejection rate and compactibility — both are directly impacted."
    ),
    ("split_strength", "HIGH"): (
        "Review bentonite and active clay. High split strength with high GCS usually indicates over-bonded sand — reduce bentonite.",
        "Compactibility — over-bonded sand tends to have reduced flowability."
    ),
    ("split_strength", "LOW"): (
        "Increase bentonite addition by 8–10%. Verify moisture is at target — low moisture prevents clay from developing binding strength.",
        "GCS and mould rejection rate — both should improve with split strength."
    ),
    ("temp_of_sand_after_mix", "HIGH"): (
        "Check cooling system function. Increase water addition slightly for evaporative cooling effect. High sand temperature causes rapid moisture loss and inconsistent compactibility.",
        "Moisture and compactibility — both degrade with high sand temperature."
    ),
    ("temp_of_sand_after_mix", "LOW"): (
        "Verify heating/mixing system. Cold sand reduces the activation of bentonite and slows water absorption — mixing time may need to increase.",
        "Active clay efficiency and compactibility — both are temperature sensitive."
    ),
    ("compactability_smc_pct", "HIGH"): (
        "Reduce water addition. If active clay is also high, address clay first. Excess SMC compactability risks over-compaction in the mould.",
        "Moisture and permeability."
    ),
    ("compactability_smc_pct", "LOW"): (
        "Increase water addition by 3–5 litres. Verify active clay is at target — low clay limits the compactability response to water.",
        "GCS and shear strength — both will confirm whether clay-water balance has been restored."
    ),
}

_REC_FALLBACK = (
    "Review the parameter trend over the last 5–10 shifts with the process engineer. "
    "Cross-check additive dosing records against the target prescription before making any adjustment.",
    "Track the parameter in the next 2–3 batches after any correction to confirm the trend has reversed."
)


def _safe_float(v):
    try:
        f = float(v)
        return None if f != f else round(f, 4)
    except (TypeError, ValueError):
        return None


def _strip_prefix(col: str) -> str:
    for p in _PREFIXES:
        if col.startswith(p):
            return col[len(p):]
    return col


def _category(col: str) -> str:
    if col.startswith("ps_"):  return "Prepared Sand"
    if col.startswith("add_"): return "Additive"
    if col.startswith("sv_"):  return "Sieve"
    if col.startswith("con_"): return "Consumption"
    return "Other"


def _alert_title(param_display: str, direction: Optional[str], osc_deviated: bool) -> str:
    if direction == "HIGH":
        return f"High {param_display}"
    if direction == "LOW":
        return f"Low {param_display}"
    if osc_deviated:
        return f"Unstable {param_display}"
    return f"Deviation — {param_display}"


def detect_property_deviations(
    result:    dict,
    config:    dict,
    db_limits: Optional[dict] = None,
) -> list:
    """
    Inspect a period result dict and return one alert dict per deviating
    prepared sand property.

    TRIGGER LOGIC (requires at least one of):
      - 2 or more engines deviated
      - LCL/UCL breach
      - abs(pct_change) >= PCT_ALERT threshold

    Parameters
    ----------
    result    : dict from run_watchdog._build_period_result()
    config    : watchdog config dict
    db_limits : {bare_name: {lcl, ucl}} from fetch_control_limits()
    """
    db_limits  = db_limits or {}
    display    = config.get("display_names", {})

    var_labels   = result.get("var_labels",   {})
    var_scores   = result.get("var_scores",   {})
    drift_labels = result.get("drift_labels", {})
    drift_scores = result.get("drift_scores", {})
    osc_labels   = result.get("osc_labels",   {})
    osc_scores   = result.get("osc_scores",   {})
    deviations   = result.get("deviations",   {})
    raw_values   = result.get("raw_values",   {})
    pct_changes  = result.get("pct_changes",  {})

    # Only alert on prepared sand columns
    monitored_cols = [c for c in var_labels.keys() if c.startswith("ps_")]

    # Build additive / consumption / sieve context for root cause reasoning
    def _ctx(prefix):
        return {
            _strip_prefix(c): _safe_float(raw_values.get(c))
            for c in raw_values
            if c.startswith(prefix)
            and not c.startswith("add_bvar_")
            and _safe_float(raw_values.get(c)) is not None
        }

    add_values = _ctx("add_")
    con_values = _ctx("con_")
    sv_values  = {c: _safe_float(raw_values.get(c))
                  for c in raw_values if c.startswith("sv_")
                  and _safe_float(raw_values.get(c)) is not None}

    # Additive engine states (drift/variance labels + % change)
    add_engine = {}
    for c in list(drift_labels.keys()) + list(var_labels.keys()):
        if not c.startswith("add_") or c.startswith("add_bvar_"):
            continue
        bare = _strip_prefix(c)
        add_engine.setdefault(bare, {})
        if c in drift_labels:
            add_engine[bare]["drift"]   = drift_labels[c]
        if c in var_labels:
            add_engine[bare]["var"]     = var_labels[c]
        pct = _safe_float(pct_changes.get(c))
        if pct is not None:
            add_engine[bare]["pct_chg"] = pct

    period_key   = str(result.get("period_key", ""))
    alert_date   = result.get("date")
    shift        = result.get("shift")
    component_id = result.get("component_id")
    mode         = config.get("aggregation", {}).get("mode", "shift")

    alerts = []

    for col in monitored_cols:
        var_lbl   = var_labels.get(col,   "STABLE") or "STABLE"
        drift_lbl = drift_labels.get(col, "STABLE") or "STABLE"
        osc_lbl   = osc_labels.get(col,   "STABLE") or "STABLE"
        dev_str   = deviations.get(col, "") or ""

        if var_lbl == "NO DATA" or drift_lbl == "NO DATA":
            continue

        is_var_dev   = var_lbl   in _VAR_DEVIATED
        is_drift_dev = drift_lbl in _DRIFT_DEVIATED
        is_osc_dev   = osc_lbl   in _OSC_DEVIATED
        is_breach    = dev_str.startswith("Deviated")

        pct_chg      = _safe_float(pct_changes.get(col))
        pct_exceeded = pct_chg is not None and abs(pct_chg) >= PCT_ALERT

        engines_triggered = sum([is_var_dev, is_drift_dev, is_osc_dev])

        # ── TRIGGER GATE ──────────────────────────────────────────────────────
        # Require: 2+ engines  OR  breach  OR  significant % change
        # Single-engine with small % change = noise, skip.
        if not is_breach and not pct_exceeded and engines_triggered < 2:
            continue
        # ─────────────────────────────────────────────────────────────────────

        # ── Severity ──────────────────────────────────────────────────────────
        is_strong_var   = var_lbl   in _VAR_DEVIATED_STRONG
        is_strong_drift = drift_lbl in _DRIFT_DEVIATED_STRONG
        is_strong_osc   = osc_lbl   in _OSC_DEVIATED_STRONG
        strong_count    = sum([is_strong_var, is_strong_drift, is_strong_osc])

        if is_breach:
            alert_type = "critical"
        elif engines_triggered >= 3:
            alert_type = "critical"
        elif engines_triggered == 2 and strong_count >= 1:
            alert_type = "critical"
        elif engines_triggered == 2:
            alert_type = "warning"
        elif engines_triggered == 1 and is_breach:
            alert_type = "warning"
        elif pct_chg is not None and abs(pct_chg) >= PCT_STRONG:
            alert_type = "warning"
        else:
            alert_type = "watch"
        # ─────────────────────────────────────────────────────────────────────

        bare = _strip_prefix(col)
        lim  = db_limits.get(bare, {})
        lcl  = _safe_float(lim.get("lcl"))
        ucl  = _safe_float(lim.get("ucl"))

        breach_direction = None
        if is_breach:
            breach_direction = "HIGH" if "HIGH" in dev_str else "LOW"

        if breach_direction:
            direction = breach_direction
        elif pct_chg is not None:
            direction = "HIGH" if pct_chg > 0 else "LOW"
        else:
            direction = None

        param_display = display.get(bare, bare.replace("_", " ").title())
        title         = _alert_title(param_display, direction, is_osc_dev)

        cur_val = _safe_float(raw_values.get(col))

        alert = {
            "period_key"        : period_key,
            "date"              : alert_date,
            "shift"             : str(shift) if shift is not None else "",
            "mode"              : mode,
            "component_id"      : str(component_id) if component_id else "",

            "category"          : _category(col),
            "parameter"         : bare,
            "parameter_col"     : col,
            "parameter_display" : param_display,

            "alert_title"       : title,
            "alert_type"        : alert_type,

            "current_value"     : cur_val,
            "lcl"               : lcl,
            "ucl"               : ucl,

            "variance_label"    : var_lbl,
            "drift_label"       : drift_lbl,
            "oscillation_label" : osc_lbl,
            "lcl_ucl_breach"    : is_breach,
            "breach_direction"  : breach_direction,

            "variance_score"    : _safe_float(var_scores.get(col)),
            "drift_score"       : _safe_float(drift_scores.get(col)),
            "oscillation_score" : _safe_float(osc_scores.get(col)),

            "engines_triggered" : engines_triggered,
            "pct_change"        : pct_chg,

            # Context data for root cause reasoning
            "add_values"        : add_values,
            "con_values"        : con_values,
            "sv_values"         : sv_values,
            "add_engine"        : add_engine,

            "root_cause"        : "",
            "recommendation"    : "",
        }

        # Build root cause + recommendation immediately
        alert["root_cause"]     = build_root_cause_narrative(alert)
        alert["recommendation"] = build_recommendation(alert)

        alerts.append(alert)

    logger.debug(
        "detect_property_deviations: %d alerts  period=%s",
        len(alerts), period_key,
    )
    return alerts


# ═══════════════════════════════════════════════════════════════════════════════
#  ROOT CAUSE NARRATIVE
# ═══════════════════════════════════════════════════════════════════════════════

def build_root_cause_narrative(alert: dict) -> str:
    """
    Build a human-readable root cause paragraph for a single property alert.

    Structure:
      [What]  Statistical description — which engines fired, value vs limits
      [Why]   Causal link to additive/other data with actual values and trends
      [Note]  Additional context if relevant
    """
    param   = alert["parameter_display"]
    bare    = alert["parameter"]
    var_lbl = alert["variance_label"]
    dft_lbl = alert["drift_label"]
    osc_lbl = alert["oscillation_label"]
    breach  = alert["lcl_ucl_breach"]
    direct  = alert.get("breach_direction") or ""
    pct_chg = alert.get("pct_change")
    cur_val = alert.get("current_value")
    lcl     = alert.get("lcl")
    ucl     = alert.get("ucl")
    add_vals = alert.get("add_values", {})
    con_vals = alert.get("con_values", {})
    add_eng  = alert.get("add_engine", {})

    # ── § What: statistical description ───────────────────────────────────────
    what_parts = []

    if breach:
        limit = "upper control limit (UCL)" if direct == "HIGH" else "lower control limit (LCL)"
        limit_val = ucl if direct == "HIGH" else lcl
        val_str = f" (current: {cur_val:.3g}, limit: {limit_val:.3g})" if cur_val is not None and limit_val is not None else ""
        what_parts.append(f"{param} has breached the {limit}{val_str}")
    else:
        if cur_val is not None:
            range_str = ""
            if lcl is not None and ucl is not None:
                range_str = f" against a target range of {lcl:.3g}–{ucl:.3g}"
            what_parts.append(f"{param} is currently at {cur_val:.3g}{range_str}")

    if pct_chg is not None and abs(pct_chg) >= 2.0:
        direction_word = "up" if pct_chg > 0 else "down"
        what_parts.append(f"trending {direction_word} by {abs(pct_chg):.1f}% from the baseline")

    engine_desc = []
    if dft_lbl in _DRIFT_DEVIATED_STRONG:
        engine_desc.append(f"a strong {'upward' if pct_chg and pct_chg > 0 else 'downward'} drift is in progress")
    elif dft_lbl in _DRIFT_DEVIATED_MILD:
        engine_desc.append(f"a gradual {'upward' if pct_chg and pct_chg > 0 else 'downward'} drift is developing")
    if var_lbl == "HIGH VAR":
        engine_desc.append(f"variance is high — readings are inconsistent between batches")
    elif var_lbl == "ELEVATED":
        engine_desc.append(f"variance is elevated — some batch-to-batch inconsistency present")
    if osc_lbl == "OSCILLATING":
        engine_desc.append(f"the parameter is oscillating — alternating high and low readings")
    elif osc_lbl == "MILD":
        engine_desc.append(f"mild oscillation observed — minor instability in the trend")

    what_sentence = ". ".join(p.capitalize() for p in what_parts)
    if engine_desc:
        what_sentence += (". " if what_sentence else "") + "; ".join(e.capitalize() for e in engine_desc) + "."
    elif what_sentence and not what_sentence.endswith("."):
        what_sentence += "."

    # ── § Why: causal reasoning from related data ──────────────────────────────
    cause_parts = []
    cause_map_entries = _CAUSE_MAP.get(bare, [])

    for related_key, direction_rel, explanation in cause_map_entries:
        val = add_vals.get(related_key) or con_vals.get(related_key)
        eng = add_eng.get(related_key, {})
        add_drift = eng.get("drift", "STABLE")
        add_var   = eng.get("var",   "STABLE")
        add_pct   = eng.get("pct_chg")

        if val is None:
            continue

        name = related_key.replace("_actual", "").replace("_", " ").title()

        # Describe the additive state
        state_parts = []
        if add_drift in _DRIFT_DEVIATED_STRONG:
            arrow = "rising" if add_pct and add_pct > 0 else "falling"
            state_parts.append(f"strong {arrow} trend")
        elif add_drift in _DRIFT_DEVIATED_MILD:
            arrow = "rising" if add_pct and add_pct > 0 else "falling"
            state_parts.append(f"slight {arrow} trend")
        elif add_var in _VAR_DEVIATED:
            state_parts.append("variable dosing")

        if add_pct is not None and abs(add_pct) >= 3.0:
            state_parts.append(f"{add_pct:+.1f}% from baseline")

        state_str = f" ({', '.join(state_parts)})" if state_parts else ""
        cause_parts.append(f"{name}: {val:.2f}{state_str} — {explanation.lower()}")

        if len(cause_parts) >= 3:
            break

    why_sentence = ""
    if cause_parts:
        if len(cause_parts) == 1:
            why_sentence = f"Most likely contributing factor: {cause_parts[0]}."
        else:
            why_sentence = (
                "Contributing factors: "
                + "; ".join(cause_parts)
                + "."
            )

    # ── Assemble final narrative ───────────────────────────────────────────────
    sections = [s for s in [what_sentence, why_sentence] if s.strip()]
    return " ".join(sections) if sections else f"{param} deviation detected."


# ═══════════════════════════════════════════════════════════════════════════════
#  RECOMMENDATION
# ═══════════════════════════════════════════════════════════════════════════════

def build_recommendation(alert: dict) -> str:
    """
    Build a specific, actionable recommendation for a property alert.
    Returns a plain string: action sentence + monitor sentence.
    """
    bare    = alert["parameter"]
    direct  = alert.get("breach_direction")
    pct_chg = alert.get("pct_change")

    if not direct:
        if pct_chg is not None:
            direct = "HIGH" if pct_chg > 0 else "LOW"
        else:
            direct = "HIGH"  # safe fallback

    action, monitor = _RECS.get((bare, direct), _REC_FALLBACK)
    return f"{action} Monitor: {monitor}"


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKWARDS-COMPATIBLE ALIASES
# ═══════════════════════════════════════════════════════════════════════════════

def build_template_root_cause(alert: dict) -> str:
    """Alias for backwards compatibility."""
    return build_root_cause_narrative(alert)


def build_template_recommendation(alert: dict, config: dict = None) -> str:
    """Alias for backwards compatibility."""
    return build_recommendation(alert)
