"""
watchdog/llm_analysis.py
-------------------------
LLM-powered root cause analysis and recommendation engine.

Replaces the rule-based build_root_cause() / build_recommendation()
in alert_engine.py with a Claude API call that receives the full
cross-monitor context for the shift and returns richer, context-aware
explanations.

Config key (in watchdog_config.json or per-foundry DB config):
  llm_analysis:
    enabled   : true | false          (default false — opt-in)
    model     : claude-haiku-4-5      (default: fast + cheap for real-time)
    timeout   : 10                    (seconds, default 10)

Falls back silently to the existing rule-based functions on any error.
API key is read from the ANTHROPIC_API_KEY environment variable.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# In-memory cache so repeated polls for the same period don't re-call the API.
_ANALYSIS_CACHE: dict[str, dict] = {}


def build_llm_analysis(
    result: dict,
    config: dict,
    cross_alerts: Optional[dict] = None,
) -> dict:
    """
    Call Claude to produce root_cause and recommendation strings.

    Parameters
    ----------
    result       : the period result dict from _build_period_result()
    config       : watchdog config (used for display_names + llm_analysis settings)
    cross_alerts : optional dict of other active alerts this shift, e.g.
                   {"prescription_deviation": True, "sieve_change": "Fines -5.3%",
                    "bad_batch_count": 3, "component": "JD RA HSG RH"}

    Returns
    -------
    {"root_cause": str, "recommendation": str}
    Falls back to rule-based output on any error.
    """
    from .alert_engine import (build_root_cause, build_recommendation,
                               build_window_root_cause, build_window_recommendation)

    _is_window = result.get("trigger_mode") == "window"

    llm_cfg = config.get("llm_analysis", {})
    if not llm_cfg.get("enabled", False):
        if _is_window:
            return {
                "root_cause"    : build_window_root_cause(result, config),
                "recommendation": build_window_recommendation(result, config),
            }
        return {
            "root_cause"    : build_root_cause(result, config),
            "recommendation": build_recommendation(result),
        }

    period_key = str(result.get("period_key", ""))
    if period_key and period_key in _ANALYSIS_CACHE:
        return _ANALYSIS_CACHE[period_key]

    try:
        analysis = _call_claude(result, config, cross_alerts, llm_cfg)
    except Exception as exc:
        logger.warning("llm_analysis: Claude API call failed (%s) — using rule-based fallback", exc)
        if _is_window:
            analysis = {
                "root_cause"    : build_window_root_cause(result, config),
                "recommendation": build_window_recommendation(result, config),
            }
        else:
            analysis = {
                "root_cause"    : build_root_cause(result, config),
                "recommendation": build_recommendation(result),
            }

    if period_key:
        _ANALYSIS_CACHE[period_key] = analysis

    return analysis


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _call_claude(result: dict, config: dict, cross_alerts: Optional[dict], llm_cfg: dict) -> dict:
    """
    Call an LLM (Anthropic Claude or Groq) and return {root_cause, recommendation}.

    Provider selection (checked in order):
      1. config["groq_api_key"]     -> Groq  (free tier, llama-3.3-70b-versatile)
      2. GROQ_API_KEY env var       -> Groq
      3. config["anthropic_api_key"]-> Anthropic Claude
      4. ANTHROPIC_API_KEY env var  -> Anthropic Claude
    """
    model    = llm_cfg.get("model",   "claude-haiku-4-5")
    timeout  = float(llm_cfg.get("timeout", 10))
    _window  = result.get("trigger_mode") == "window"
    sys_prompt   = _WINDOW_SYSTEM_PROMPT if _window else _SYSTEM_PROMPT
    user_message = (_build_window_user_message(result, config)
                    if _window else _build_user_message(result, config, cross_alerts))

    # Provider priority: Ollama (local) -> Groq (free) -> Anthropic
    provider = llm_cfg.get("provider", "auto")

    if provider == "ollama" or (provider == "auto" and llm_cfg.get("ollama_model")):
        return _call_ollama(llm_cfg, user_message, timeout, result, config)

    groq_key = config.get("groq_api_key") or os.environ.get("GROQ_API_KEY")
    if groq_key:
        return _call_groq(groq_key, llm_cfg, user_message, timeout, result, config)

    import anthropic
    api_key = config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    client  = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    response = client.messages.create(
        model=model,
        max_tokens=300,
        system=sys_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    return _parse_response(text, result, config)


def _call_ollama(llm_cfg: dict, user_message: str,
                 timeout: float, result: dict, config: dict) -> dict:
    """Call a locally-running Ollama model (your fine-tuned foundry model)."""
    import urllib.request, urllib.error
    ollama_url   = llm_cfg.get("ollama_url",   "http://localhost:11434")
    ollama_model = llm_cfg.get("ollama_model", "foundry-alert")
    payload = json.dumps({
        "model" : ollama_model,
        "prompt": f"{_SYSTEM_PROMPT}\n\nUser: {user_message}\nAssistant:",
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 300},
    }).encode()
    req = urllib.request.Request(
        f"{ollama_url}/api/generate",
        data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    text = data.get("response", "")
    return _parse_response(text, result, config)


def _call_groq(groq_key: str, llm_cfg: dict, user_message: str,
               timeout: float, result: dict, config: dict) -> dict:
    """Call Groq's free API (OpenAI-compatible)."""
    from groq import Groq
    groq_model = llm_cfg.get("groq_model", "llama-3.3-70b-versatile")
    client     = Groq(api_key=groq_key, timeout=timeout)
    response   = client.chat.completions.create(
        model=groq_model,
        max_tokens=300,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
    )
    text = response.choices[0].message.content or ""
    return _parse_response(text, result, config)


def _build_user_message(result: dict, config: dict, cross_alerts: Optional[dict]) -> str:
    """Serialize the alert context into a compact prompt message."""
    display = config.get("display_names", {})

    def label(col: str) -> str:
        bare = col
        for pfx in ("ps_", "con_", "add_", "pse_", "sv_"):
            if col.startswith(pfx):
                bare = col[len(pfx):]
        return display.get(bare) or display.get(col) or bare.replace("_", " ").title()

    alert_level = result.get("final_alert", "STABLE")
    si_score    = result.get("si_score_100", 0)
    component   = result.get("component_name") or result.get("component_id") or ""
    period      = result.get("period_key", "")

    # Top non-stable parameters
    param_labels = result.get("param_labels", {})
    drift_lbls   = result.get("drift_labels", {})
    var_lbls     = result.get("var_labels", {})
    deviations   = result.get("deviations", {})
    pct_changes  = result.get("pct_changes", {})
    raw_values   = result.get("raw_values", {})

    params_info = []
    for col, lv in param_labels.items():
        if lv == "STABLE":
            continue
        info = {
            "name"     : label(col),
            "alert"    : lv,
            "drift"    : drift_lbls.get(col, ""),
            "variance" : var_lbls.get(col, ""),
            "value"    : raw_values.get(col),
            "pct_chg"  : pct_changes.get(col),
            "deviation": deviations.get(col, ""),
        }
        params_info.append(info)

    # Sort by severity
    _rank = {"!! WARNING": 6, "CRITICAL": 5, "ALERT": 4, "HIGH VAR": 3,
             "ELEVATED": 2, "WATCH": 1}
    params_info.sort(key=lambda x: -_rank.get(x["alert"], 0))

    lines = [
        f"Period: {period}",
        f"Alert level: {alert_level}  |  SI score: {si_score:.1f}/100",
    ]
    if component:
        lines.append(f"Component: {component}")

    if params_info:
        lines.append("\nNon-stable parameters:")
        for p in params_info[:6]:  # top 6
            parts = [f"  {p['name']}: {p['alert']}"]
            if p["drift"] and p["drift"] != "STABLE":
                parts.append(f"drift={p['drift']}")
            if p["variance"] and p["variance"] != "STABLE":
                parts.append(f"var={p['variance']}")
            if p["value"] is not None:
                parts.append(f"value={p['value']:.3g}")
            if p["pct_chg"] is not None:
                parts.append(f"Δ={p['pct_chg']:+.1f}%")
            if p["deviation"] and p["deviation"] not in ("OK", ""):
                parts.append(f"({p['deviation']})")
            lines.append("  ".join(parts))

    if cross_alerts:
        lines.append("\nOther active alerts this shift:")
        for k, v in cross_alerts.items():
            lines.append(f"  {k}: {v}")

    return "\n".join(lines)


def _build_window_user_message(result: dict, config: dict) -> str:
    """Sigma-band context message for window mode LLM calls."""
    display        = config.get("display_names", {})
    param_labels   = result.get("param_labels", {})
    raw_values     = result.get("raw_values", {})
    baseline_means = result.get("baseline_means", {})
    pct_changes    = result.get("pct_changes", {})
    deviations     = result.get("deviations", {})
    final_alert    = result.get("final_alert", "STABLE")
    period         = result.get("period_key", "")

    _RANK = {"STABLE": 0, "ALERT": 1, "CRITICAL": 2, "!! WARNING": 3}

    deviating = sorted(
        [(col, lbl) for col, lbl in param_labels.items()
         if col.startswith("ps_") and _RANK.get(str(lbl), 0) > 0],
        key=lambda x: -_RANK.get(str(x[1]), 0),
    )

    def _lbl(col):
        bare = col[3:] if col.startswith("ps_") else col
        return display.get(bare) or display.get(col) or bare.replace("_", " ").title()

    lines = [
        f"Period: {period}",
        f"Alert level: {final_alert}",
        "Analysis mode: window (prepared sand only, sigma-band vs baseline)",
        "",
        "Non-stable parameters:",
    ]
    for col, lbl in deviating[:6]:
        bare   = col[3:]
        name   = _lbl(col)
        actual = raw_values.get(col)
        b_mean = baseline_means.get(bare)
        pct    = pct_changes.get(col)
        dev    = deviations.get(col, "")

        parts = [f"  {name}: {lbl}"]
        if actual is not None and b_mean is not None and abs(float(b_mean)) > 1e-12:
            pct_dev   = (float(actual) - float(b_mean)) / float(b_mean) * 100
            direction = "HIGH" if pct_dev > 0 else "LOW"
            parts.append(f"direction={direction}")
            parts.append(f"actual={actual:.3g}")
            parts.append(f"baseline_mean={b_mean:.3g}")
            parts.append(f"deviation_from_baseline={pct_dev:+.1f}%")
        elif actual is not None:
            parts.append(f"actual={actual:.3g}")
        if pct is not None:
            parts.append(f"recent_pct_change={pct:+.1f}%")
        if isinstance(dev, str) and dev.startswith("Deviated"):
            parts.append(f"({dev})")
        lines.append("  ".join(parts))

    return "\n".join(lines)


def _parse_response(text: str, result: dict, config: dict) -> dict:
    """
    Extract root_cause and recommendation from Claude's JSON response.
    Falls back gracefully if the JSON is malformed.
    """
    from .alert_engine import build_root_cause, build_recommendation

    # Try to parse JSON block
    try:
        # Strip markdown code fences if present
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        rc   = str(data.get("root_cause", "")).strip()
        rec  = str(data.get("recommendation", "")).strip()
        if rc and rec:
            return {"root_cause": rc, "recommendation": rec}
    except Exception:
        pass

    # Fallback: rule-based
    logger.debug("llm_analysis: could not parse Claude response as JSON — using rule-based fallback")
    return {
        "root_cause"    : build_root_cause(result, config),
        "recommendation": build_recommendation(result),
    }


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_property_llm_analysis(alert: dict, config: dict) -> dict:
    """
    Generate root_cause and recommendation for a single property deviation alert.

    Parameters
    ----------
    alert  : alert dict from property_alert_engine.detect_property_deviations()
    config : watchdog config (for llm_analysis settings + display_names)

    Returns
    -------
    {"root_cause": str, "recommendation": str}
    Falls back to template-based output on any error.
    """
    from .property_alert_engine import build_template_root_cause, build_template_recommendation

    llm_cfg = config.get("llm_analysis", {})
    if not llm_cfg.get("enabled", False):
        return {
            "root_cause"    : build_template_root_cause(alert),
            "recommendation": build_template_recommendation(alert, config),
        }

    cache_key = f"prop_{alert.get('period_key', '')}_{alert.get('parameter', '')}"
    if cache_key in _ANALYSIS_CACHE:
        return _ANALYSIS_CACHE[cache_key]

    try:
        analysis = _call_claude_property(alert, config, llm_cfg)
    except Exception as exc:
        logger.warning(
            "build_property_llm_analysis: API call failed (%s) — using template fallback", exc
        )
        analysis = {
            "root_cause"    : build_template_root_cause(alert),
            "recommendation": build_template_recommendation(alert, config),
        }

    _ANALYSIS_CACHE[cache_key] = analysis
    return analysis


def _call_claude_property(alert: dict, config: dict, llm_cfg: dict) -> dict:
    """Call LLM for a single property deviation and return {root_cause, recommendation}."""
    from .property_alert_engine import build_template_root_cause, build_template_recommendation

    model   = llm_cfg.get("model", "claude-haiku-4-5")
    timeout = float(llm_cfg.get("timeout", 10))
    message = _build_property_message(alert)

    provider = llm_cfg.get("provider", "auto")

    if provider == "ollama" or (provider == "auto" and llm_cfg.get("ollama_model")):
        return _call_ollama(llm_cfg, message, timeout, {}, config)

    groq_key = config.get("groq_api_key") or os.environ.get("GROQ_API_KEY")
    if groq_key:
        return _call_groq_property(groq_key, llm_cfg, message, timeout, alert, config)

    import anthropic
    api_key  = config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    client   = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    response = client.messages.create(
        model=model,
        max_tokens=300,
        system=_PROPERTY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    return _parse_property_response(text, alert, config)


def _call_groq_property(groq_key, llm_cfg, message, timeout, alert, config):
    from .property_alert_engine import build_template_root_cause, build_template_recommendation
    try:
        from groq import Groq
        groq_model = llm_cfg.get("groq_model", "llama-3.3-70b-versatile")
        client     = Groq(api_key=groq_key, timeout=timeout)
        response   = client.chat.completions.create(
            model=groq_model,
            max_tokens=300,
            messages=[
                {"role": "system", "content": _PROPERTY_SYSTEM_PROMPT},
                {"role": "user",   "content": message},
            ],
        )
        text = response.choices[0].message.content or ""
        return _parse_property_response(text, alert, config)
    except Exception as exc:
        logger.warning("_call_groq_property failed: %s", exc)
        return {
            "root_cause"    : build_template_root_cause(alert),
            "recommendation": build_template_recommendation(alert, config),
        }


def _build_property_message(alert: dict) -> str:
    """Build a compact prompt for a single prepared sand property deviation.
    Includes additive/consumption/sieve context so the LLM can reason about causes.
    """
    param    = alert.get("parameter_display", alert.get("parameter", ""))
    category = alert.get("category", "Prepared Sand")
    title    = alert.get("alert_title", "")
    atype    = alert.get("alert_type", "watch").upper()
    cur_val  = alert.get("current_value")
    lcl      = alert.get("lcl")
    ucl      = alert.get("ucl")
    var_lbl  = alert.get("variance_label", "STABLE")
    dft_lbl  = alert.get("drift_label", "STABLE")
    osc_lbl  = alert.get("oscillation_label", "STABLE")
    breach   = alert.get("lcl_ucl_breach", False)
    direction= alert.get("breach_direction", "")
    pct      = alert.get("pct_change")

    lines = [
        f"Category: {category}",
        f"Parameter: {param}",
        f"Alert: {title}",
        f"Alert type: {atype}",
    ]
    if cur_val is not None:
        lines.append(f"Current value: {cur_val:.4g}")
    if lcl is not None:
        lines.append(f"LCL: {lcl}")
    if ucl is not None:
        lines.append(f"UCL: {ucl}")
    if breach:
        lines.append(f"LCL/UCL breach: YES ({direction})")
    lines.append(f"Variance label: {var_lbl}")
    lines.append(f"Drift label: {dft_lbl}")
    lines.append(f"Oscillation label: {osc_lbl}")
    if pct is not None and pct == pct:
        lines.append(f"% Change from previous period: {pct:+.2f}%")

    # --- Additive context (potential causes) ---
    add_vals = alert.get("add_values", {})
    add_eng  = alert.get("add_engine", {})
    if add_vals:
        lines.append("\nAdditive values this period (potential causes):")
        for bare, val in list(add_vals.items())[:8]:
            eng    = add_eng.get(bare, {})
            d_lbl  = eng.get("drift", "STABLE")
            v_lbl  = eng.get("var",   "STABLE")
            p      = eng.get("pct_chg")
            name   = bare.replace("_actual", "").replace("_", " ").title()
            status = []
            if d_lbl not in ("STABLE", "NO DATA"):
                arrow = f" ({'rising' if p and p > 0 else 'falling'})" if p is not None else ""
                status.append(f"drift={d_lbl.lower()}{arrow}")
            if v_lbl not in ("STABLE", "NO DATA"):
                status.append(f"var={v_lbl.lower()}")
            if p is not None:
                status.append(f"{p:+.1f}%")
            flag = "  [" + ", ".join(status) + "]" if status else ""
            lines.append(f"  {name}: {val:.3g}{flag}")

    # --- Consumption context ---
    con_vals = alert.get("con_values", {})
    if con_vals:
        lines.append("\nShift consumption totals:")
        for bare, val in list(con_vals.items())[:6]:
            name = bare.replace("_", " ").title()
            lines.append(f"  {name}: {val:.3g}")

    # --- Sieve context ---
    sv_vals = alert.get("sv_values", {})
    if sv_vals:
        lines.append("\nSieve measurements:")
        for col, val in list(sv_vals.items())[:4]:
            lines.append(f"  {col}: {val:.3g}")

    return "\n".join(lines)


def _parse_property_response(text: str, alert: dict, config: dict) -> dict:
    """Extract root_cause and recommendation from LLM JSON response."""
    from .property_alert_engine import build_template_root_cause, build_template_recommendation

    try:
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        rc   = str(data.get("root_cause", "")).strip()
        rec  = str(data.get("recommendation", "")).strip()
        if rc and rec:
            return {"root_cause": rc, "recommendation": rec}
    except Exception:
        pass

    return {
        "root_cause"    : build_template_root_cause(alert),
        "recommendation": build_template_recommendation(alert, config),
    }


_PROPERTY_SYSTEM_PROMPT = """\
You are an AI assistant embedded in a foundry sand preparation monitoring system.
You receive deviation data for ONE prepared sand quality parameter plus the current
additive, consumption, and sieve values as supporting evidence.

Your job:
1. Identify what is wrong with the prepared sand parameter (direction, severity).
2. Use the additive/consumption/sieve context to explain WHY it is deviating.
3. Give a concrete corrective action targeting the most likely additive cause.

Domain context:
- Prepared sand properties: active clay, compactibility, GCS, GFN/AFS, moisture,
  permeability, LOI, volatile matter, inert fines, shear/split strength, temperature.
- Cause mapping:
    active_clay high/drift    -> bentonite over-dosed or high reactivity
    active_clay low           -> bentonite under-dosed or poor quality
    moisture high             -> water over-addition or cooler issue
    moisture low              -> water under-addition
    compactibility high       -> excess water or over-mulling
    compactibility low        -> insufficient water
    LOI / volatile matter     -> coal dust / LCA addition rate
    GFN/AFS drift             -> fresh silica sand quality or ratio change
    permeability low          -> excess fines or over-activated clay
    temperature high          -> return sand too hot, cooler underperforming
- Variance = batch-to-batch instability; Drift = sustained multi-shift trend;
  Oscillation = rapid up-down cycling; LCL/UCL breach = outside control limits.

Your output must be a JSON object with exactly two keys:
  "root_cause"     — 1-2 sentences naming the parameter deviation AND the likely
                     additive/process cause evidenced by the context data.
  "recommendation" — 1-2 sentences: specific corrective action on the additive
                     or process (quantities if possible).

Be concrete. Name the additive. State direction (rising/falling/oscillating).
Do not use vague language like "monitor closely".
Respond with ONLY the JSON object, no explanation before or after it.

Example:
{"root_cause": "Active clay has a strong upward drift (UCL breach at 10.2% vs 9.5%) driven by bentonite addition running 8% above target over the last 3 shifts.", "recommendation": "Reduce bentonite prescription by 2-3 kg/batch immediately and recheck the weigh scale calibration on the muller."}
"""


_WINDOW_SYSTEM_PROMPT = """\
You are an AI assistant embedded in a foundry sand preparation monitoring system.
You receive sigma-band alert data for prepared sand parameters only.

Each parameter is compared against its mean from a known-good baseline production period.
Sigma bands:
  STABLE   = within ±1σ of baseline mean (normal variation)
  ALERT    = ±1σ to ±2σ (moderate deviation, monitor)
  CRITICAL = beyond ±2σ (significant deviation — action likely needed)
  !! WARNING = CRITICAL plus % change threshold or LCL/UCL breach exceeded

Prepared sand parameters and their additive causes:
  active_clay   HIGH -> bentonite over-dosed or high-reactivity batch
  active_clay   LOW  -> bentonite under-dosed or poor quality
  moisture      HIGH -> water over-addition; cooler underperforming
  moisture      LOW  -> water under-addition; high return sand temperature
  compactibility HIGH -> excess water or over-mulling
  compactibility LOW  -> insufficient water or low active clay
  loi/volatile_matter -> coal dust / LCA addition rate change
  gfn_afs             -> fresh silica sand quality or blending ratio change
  permeability  LOW   -> excess fines or over-activated clay
  gcs           LOW   -> insufficient bentonite or short mixing cycle
  temperature   HIGH  -> return sand too hot; cooler underperforming

Use the "actual" vs "baseline_mean" and "deviation_from_baseline" % to determine direction and severity.

Your output must be a JSON object with exactly two keys:
  "root_cause"     — 1-2 sentences: which parameter(s) deviated, direction (above/below
                     baseline mean), and the most likely additive or process cause.
  "recommendation" — 1-2 sentences: specific corrective action naming the additive and
                     adjustment direction (increase/reduce), with a quantity if possible.

Be concrete. Do not use vague language like "monitor closely".
Respond with ONLY the JSON object.

Example:
{"root_cause": "Moisture is 14.2% above baseline mean (current 3.8%, baseline 3.33%) indicating water over-addition, likely driven by higher return sand temperature increasing evaporative demand.", "recommendation": "Reduce water addition by 4-6 litres per batch and verify cooler outlet temperature is below 45°C."}
"""


_SYSTEM_PROMPT = """\
You are an AI assistant embedded in a foundry sand preparation monitoring system.
You receive real-time alert data about prepared sand quality and must explain
what is happening and what the operator should do.

Domain context:
- Prepared sand properties: active clay, compactibility, GCS, GFN/AFS, moisture,
  permeability, LOI, volatile matter, inert fines, shear/split strength, temperature.
- Additives: bentonite (adds active clay), coal dust/LCA (LOI/volatile matter),
  fresh silica sand (GFN control), water (moisture/compactibility).
- SI score 0-100: STABLE <25, WATCH <50, ALERT <70, CRITICAL 70+.
- Drift = sustained multi-shift trend. Variance = batch-to-batch instability.
- Deviation = parameter outside LCL/UCL control limits.

Your output must be a JSON object with exactly two keys:
  "root_cause"     — 1-2 sentences: what is wrong and why (be specific about parameters).
  "recommendation" — 1-2 sentences: what the operator should do right now.

Be concrete. Name the specific parameters. Mention direction (rising/falling).
Do not use vague language like "monitor closely" — give an actionable step.
Respond with ONLY the JSON object, no explanation before or after it.

Example output format:
{"root_cause": "Active clay is drifting downward across 4 shifts and is now below the LCL, while bentonite has been under-dispensed by 8% for the last 3 batches.", "recommendation": "Increase bentonite prescription by 2-3 kg/batch immediately and verify the weighing system on the mixer."}
"""
