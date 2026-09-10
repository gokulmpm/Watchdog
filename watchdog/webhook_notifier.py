"""
watchdog/webhook_notifier.py
-----------------------------
Sends alerts to the push-notification API.

Two endpoints
-------------
  POST /api/sandman/alert-type   Register an alert parameter type (idempotent)
  POST /api/sandman/alert        Send a fired alert instance

Config path: config["webhook"]
  enabled        : true / false  (default false)
  base_url       : API base, e.g. "https://push-notification-alerts.up.railway.app"
  timeout_sec    : HTTP timeout in seconds  (default 10)
  shift_names    : optional {shift_str: display_name} override
                   default: {"1": "Morning", "2": "Afternoon", "3": "Night"}

foundry_key  = config["customer_pkey"]   (set per foundry in watchdog_config.json)
line_pkey    = config["foundry_line_id"]

LCL/UCL alerts (window mode only)
----------------------------------
  send_lcl_ucl_alerts() fires automatically when new prepared sand data arrives
  (window trigger).  It scans result["deviations"] for ps_ parameters that have
  breached their LCL or UCL and posts one alert per breach.

  Alert naming:
    "High {param}"  — value exceeded UCL
    "Low {param}"   — value fell below LCL

  Severity is always "Critical" for a limit breach.
  Only Critical and Warning are ever sent; watch-level is suppressed.
"""

import logging
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Dedicated file logger for every API call (success + failure)
_api_log_path = Path(__file__).parent.parent / "logs" / "webhook_api.log"
_api_log_path.parent.mkdir(parents=True, exist_ok=True)
_api_file_handler = logging.FileHandler(_api_log_path, encoding="utf-8")
_api_file_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
_api_logger = logging.getLogger("sandman.webhook_api")
_api_logger.setLevel(logging.DEBUG)
if not _api_logger.handlers:
    _api_logger.addHandler(_api_file_handler)
_api_logger.propagate = False   # don't duplicate into root logger

_DEFAULT_SHIFT_NAMES = {"1": "Morning", "2": "Afternoon", "3": "Night", "4": "Night 2"}

# Track which alert-types have already been registered this session so we
# don't POST /alert-type on every single alert.
_registered_types: set = set()

# Separate registry for LCL/UCL property alerts
_registered_lcl_ucl_types: set = set()

# Unit lookup for prepared sand parameters
_PS_UNITS: dict = {
    "moisture":               "%",
    "active_clay":            "%",
    "compactibility":         "%",
    "compactability_smc_pct": "%",
    "loi":                    "%",
    "volatile_matter":        "%",
    "inert_fines":            "%",
    "gfn_afs":                "AFS",
    "permeability":           "",
    "gcs":                    "N/cm²",
    "shear_strength":         "N/cm²",
    "split_strength":         "N/cm²",
    "temp_of_sand_after_mix": "°C",
}

#  PUBLIC API

def send_si_alerts(
    result:        dict,
    config:        dict,
    db_limits:     dict,
    display_names: dict,
    label:         str = "",
) -> int:
    """
    Send one webhook POST per non-STABLE SI parameter.

    For each deviated parameter:
      1. POST /alert-type  (once per unique parameter per process lifetime)
      2. POST /alert       (every time an alert fires)

    Parameters
    ----------
    result        : SI result dict from _build_period_result
    config        : watchdog config (must include 'webhook' section)
    db_limits     : {java_name: {"lcl": float|None, "ucl": float|None}}
    display_names : {param_code: human_label}
    label         : log prefix (foundry label)

    Returns total number of alert POSTs sent.
    """
    cfg = _get_cfg(config)
    if not cfg.get("enabled", False):
        return 0

    param_labels = result.get("param_labels", {})
    raw_values   = result.get("raw_values", {})
    deviations   = result.get("deviations", {})
    shift        = _shift_name(cfg, result.get("shift", ""))
    period_key   = str(result.get("period_key") or "")
    triggered_at = _triggered_at(result)
    foundry_key  = str(config.get("customer_pkey", ""))
    line_pkey    = int(config.get("foundry_line_id", 1))

    _RANK = {"STABLE": 0, "WATCH": 1, "ELEVATED": 2, "HIGH VAR": 3,
             "ALERT": 4, "CRITICAL": 5, "!! WARNING": 6}

    # Only send prepared-sand (ps_) parameters — skip additive, consumption, sieve etc.
    deviated = [
        p for p, lbl in param_labels.items()
        if p.startswith("ps_") and _RANK.get(str(lbl), 0) > 0 and p in raw_values
    ]

    sent          = 0
    alert_batch   = []          # alert payloads
    new_types     = []          # type payloads not yet registered
    new_type_keys = []
    type_by_param = {}          # param -> type_payload (for 404 retry in _post_alert)

    optimal_vals   = config.get("optimal_values", {})
    baseline_means = result.get("baseline_means", {})

    for param in deviated:
        level     = str(param_labels.get(param, "WATCH"))
        raw_val   = raw_values.get(param)
        deviation = str(deviations.get(param, ""))
        process   = _process_for_param(param)
        bare      = _bare_name(param)
        plabel    = display_names.get(param) or display_names.get(bare) or bare
        severity  = "Critical" if _RANK.get(level, 0) >= 4 else "Warning"
        if _RANK.get(level, 0) < 1:
            continue
        if not _severity_passes(cfg, severity):
            continue
        unit      = _unit_for_param(param)
        lcl, ucl  = _get_limits(bare, db_limits)

        # Determine direction
        _rv = _safe_round(raw_val)
        if _rv is not None and ucl is not None and _rv > ucl:
            direction = "High"
        elif _rv is not None and lcl is not None and _rv < lcl:
            direction = "Low"
        else:
            direction = "High" if "HIGH" in str(deviation).upper() else "Low"

        # threshold_percentage = % deviation of actual from the reference mean
        # (baseline mean -> optimal value -> LCL/UCL midpoint, in priority order)
        _ref = (baseline_means.get(bare)
                or optimal_vals.get(bare)
                or ((float(lcl) + float(ucl)) / 2 if lcl is not None and ucl is not None else None))
        if _ref is not None and abs(float(_ref)) > 1e-12 and _rv is not None:
            thr_pct = round((float(_rv) - float(_ref)) / float(_ref) * 100, 2)
        else:
            thr_pct = None
        # Avoid double unit if display name already contains it e.g. "Active Clay (%)"
        _unit_suffix = f" ({unit})" if unit and f"({unit})" not in plabel else ""
        alert_name = f"{direction} {plabel}{_unit_suffix}"

        type_key = f"{process}::{bare}"
        type_payload = {
            "foundry_key"     : foundry_key,
            "name"            : alert_name,
            "parameter_label" : plabel,
            "category"        : process,
            "severity"        : severity,
            "threshold_min"   : lcl,
            "threshold_max"   : ucl,
            "threshold_unit"  : unit,
            "line_pkey"       : line_pkey,
        }
        if type_key not in _registered_types:
            new_types.append(type_payload)
            new_type_keys.append(type_key)
        type_by_param[param] = (type_key, type_payload)

        root_cause = result.get("root_cause") or (
            f"{plabel} — {level}"
            + (f" ({deviation})" if deviation and deviation != "OK" else "")
            + (f" deviation {thr_pct:+.2f}%" if thr_pct is not None else "")
        )

        alert_batch.append({
            "alert_id"            : f"ALT-SI-{period_key}-{bare}-{datetime.now().strftime('%H%M%S')}",
            "foundry_key"         : foundry_key,
            "process"             : process,
            "parameter"           : plabel,
            "parameter_label"     : alert_name,
            "actual_value"        : _safe_round(raw_val),
            "threshold_min"       : lcl,
            "threshold_max"       : ucl,
            "threshold_percentage": thr_pct,
            "unit"                : unit,
            "severity"            : severity,
            "shift"               : shift,
            "line_pkey"           : line_pkey,
            "root_cause"          : root_cause,
            "recommendation"      : result.get("recommendation"),
            "triggered_at"        : triggered_at,
            "_type_key"           : type_key,
            "_type_payload"       : type_payload,
        })

    # ── Send all new alert-types in one batch POST ────────────────────────────
    if new_types:
        if _post_alert_type_batch(cfg, new_types, label):
            for k in new_type_keys:
                _registered_types.add(k)

    for ap in alert_batch:
        _tk  = ap.pop("_type_key", None)
        _tp  = ap.pop("_type_payload", None)
        if _post_alert(cfg, ap, label, _type_payload=_tp, _type_key=_tk):
            sent += 1
            if sent > 0:
                time.sleep(1)

    return sent

#  MIXER (SMC / prepared_sand_extra) PER-BATCH ALERTS

# Unit lookup for SMC parameters
_SMC_UNITS: dict = {
    "co1"                 : "%",
    "co_final_percentage" : "%",
    "cosp_percent"        : "%",
    "moisture_percentage" : "%",
    "temp_st1c"           : "°C",
    "total_seconds"       : "s",
    "total_water"         : "ltr",
    "wd1"                 : "ltr",
    "current"             : "A",
}

# Track registered SMC alert-types to avoid re-registering each poll
_registered_smc_types: set = set()

def send_smc_batch_webhook(
    batch_breaches: list,
    shift_breaches: list,
    config:         dict,
    label:          str = "",
) -> int:
    """
    POST per-batch and shift-average SMC/Mixer LCL/UCL breaches to the push API.

    category = "Mixer"  (appears in the app under the Mixer section)
    process  = "Mixer"

    Each breach dict from SMCBatchMonitor contains:
      col, display, value, lcl, ucl, status, z_score, sigma_breach,
      pkey, date, shift, is_shift_avg, batch_count (shift avg only)

    Returns total number of /alert POSTs successfully sent.
    """
    cfg = _get_cfg(config)
    if not cfg.get("enabled", False):
        return 0

    foundry_key = str(config.get("customer_pkey", ""))
    line_pkey   = int(config.get("foundry_line_id", 1))

    all_breaches = batch_breaches + shift_breaches
    if not all_breaches:
        return 0

    new_types     : list = []
    new_type_keys : list = []
    batch_payload : list = []

    for b in all_breaches:
        col      = b.get("col", "")
        display  = b.get("display", col)
        val      = b.get("value")
        lcl      = b.get("lcl")
        ucl      = b.get("ucl")
        status   = b.get("status", "OK")
        z_score  = b.get("z_score")
        shift    = _shift_name(cfg, b.get("shift", ""))
        date_str = str(b.get("date") or "")
        is_avg   = bool(b.get("is_shift_avg", False))
        unit     = _SMC_UNITS.get(col, "")

        # Direction from status string
        direction = "HIGH" if "ABOVE" in str(status) else "LOW"
        alert_name = f"{'High' if direction == 'HIGH' else 'Low'} {display}"
        if is_avg:
            alert_name += " (Shift Avg)"

        severity = "Critical"
        if not _severity_passes(cfg, severity):
            continue

        # Reference: midpoint of LCL/UCL
        ref = None
        if lcl is not None and ucl is not None:
            ref = (lcl + ucl) / 2
        thr_pct = None
        if ref is not None and ref != 0 and val is not None:
            thr_pct = round((float(val) - ref) / abs(ref) * 100, 2)
        else:
            thr_pct = _safe_round(val, 4) if val is not None else None

        triggered_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        pkey_str = str(b.get("pkey") or "")
        alert_id = f"ALT-SMC-{date_str}-{col}-{pkey_str or triggered_at[-6:]}"

        root_cause = (
            f"{'Shift average' if is_avg else 'Batch'} {display} "
            f"{'below LCL' if direction == 'LOW' else 'above UCL'} "
            f"(val={val}, LCL={lcl}, UCL={ucl})"
        )
        if z_score is not None:
            root_cause += f"  |z|={z_score:.2f}σ"

        # Register alert-type (idempotent)
        type_key = f"smc::{col}::{'avg' if is_avg else 'batch'}::{direction}"
        type_payload = {
            "foundry_key"     : foundry_key,
            "name"            : alert_name,
            "parameter_label" : display,
            "category"        : "Mixer",
            "severity"        : severity,
            "threshold_min"   : _safe_round(lcl, 4) if lcl is not None else None,
            "threshold_max"   : _safe_round(ucl, 4) if ucl is not None else None,
            "threshold_unit"  : unit,
            "line_pkey"       : line_pkey,
        }
        if type_key not in _registered_smc_types:
            new_types.append(type_payload)
            new_type_keys.append(type_key)

        batch_payload.append({
            "alert_id"            : alert_id,
            "foundry_key"         : foundry_key,
            "process"             : "Mixer",
            "parameter"           : display,
            "parameter_label"     : alert_name,
            "actual_value"        : _safe_round(val, 3) if val is not None else None,
            "threshold_min"       : _safe_round(lcl, 4) if lcl is not None else None,
            "threshold_max"       : _safe_round(ucl, 4) if ucl is not None else None,
            "threshold_percentage": thr_pct,
            "unit"                : unit,
            "severity"            : severity,
            "shift"               : shift,
            "line_pkey"           : line_pkey,
            "root_cause"          : root_cause,
            "recommendation"      : f"Check mixer settings for {display}. Verify sensor calibration and additive dosing.",
            "triggered_at"        : triggered_at,
        })

    # Register new types
    if new_types:
        if _post_alert_type_batch(cfg, new_types, label):
            for k in new_type_keys:
                _registered_smc_types.add(k)

    sent = 0
    if batch_payload:
        sent = _post_alert_batch(cfg, batch_payload, label)
        logger.info("[%s]  SMC webhook: %d/%d alert(s) sent", label, sent, len(batch_payload))

    return sent

#  LCL / UCL BREACH ALERTS  (prepared sand, window mode only)

def send_lcl_ucl_alerts(
    result:        dict,
    config:        dict,
    db_limits:     dict,
    display_names: dict,
    label:         str = "",
) -> int:
    """
    Send prepared-sand sigma CRITICAL alerts to the push API (window mode only).

    Fires only when a ps_ parameter reaches the CRITICAL sigma band (|z| > 2,
    the 3rd sigma level). Parameters in the STABLE (1-sigma) or ALERT (2-sigma)
    bands are suppressed — no alert is sent for those.

    threshold_percentage = the actual current value that is not in the stable range.
    threshold_min / threshold_max = LCL / UCL from db_limits (unchanged).

    The baseline mean (from the good/reference period) is used as the direction
    reference: actual > optimal_target -> "High", actual < optimal_target -> "Low".

    Returns the number of /alert POSTs successfully sent.
    """
    cfg = _get_cfg(config)
    if not cfg.get("enabled", False):
        logger.debug("[%s]  sigma-alert webhook disabled (webhook.enabled=false)", label)
        return 0

    deviations   = result.get("deviations", {})
    param_labels = result.get("param_labels", {})
    raw_values   = result.get("raw_values", {})
    period_key   = str(result.get("period_key") or "")
    shift        = _shift_name(cfg, result.get("shift", ""))
    triggered_at = _triggered_at(result)
    foundry_key  = str(config.get("customer_pkey", ""))
    line_pkey    = int(config.get("foundry_line_id", 1))
    optimal_vals    = config.get("optimal_values", {})
    baseline_means  = result.get("baseline_means", {})

    CRITICAL = "!! CRITICAL"
    WARNING  = "!! WARNING"

    sent        = 0
    batch       = []        # alert payloads
    new_types   = []        # type payloads not yet registered
    new_type_keys = []

    for col, dev_str in deviations.items():
        # Prepared sand only
        if not col.startswith("ps_"):
            continue
        # Only process actual LCL/UCL breaches
        if not isinstance(dev_str, str) or not dev_str.startswith("Deviated"):
            continue
        lbl  = param_labels.get(col, WARNING)
        bare = col[3:]   # strip "ps_"
        actual = raw_values.get(col)
        if actual is None:
            continue

        plabel = (display_names.get(bare)
                  or display_names.get(col)
                  or bare.replace("_", " ").title())

        # Direction: compare actual vs baseline mean (falls back to optimal target,
        # then db LCL/UCL midpoint when neither is available).
        b_mean = baseline_means.get(bare)
        ref    = b_mean if b_mean is not None else optimal_vals.get(bare)
        if ref is None:
            lim    = db_limits.get(bare, {})
            ref    = (float(lim.get("lcl") or 0) + float(lim.get("ucl") or 0)) / 2
        direction = "HIGH" if float(actual) > float(ref) else "LOW"

        alert_name = f"{'High' if direction == 'HIGH' else 'Low'} {plabel}"

        lim  = db_limits.get(bare, {})
        lcl  = _safe_round(lim.get("lcl"), 4) if lim.get("lcl") is not None else None
        ucl  = _safe_round(lim.get("ucl"), 4) if lim.get("ucl") is not None else None
        unit = _PS_UNITS.get(bare, "%")

        # threshold_percentage = % deviation of actual from the baseline mean.
        b_mean_f = float(b_mean) if b_mean is not None else None
        if b_mean_f is not None and abs(b_mean_f) > 1e-12:
            thr_pct = round((float(actual) - b_mean_f) / b_mean_f * 100, 2)
        else:
            thr_pct = _safe_round(actual, 4)

        severity = "Critical" if str(lbl) == CRITICAL else "Warning"
        if not _severity_passes(cfg, severity):
            continue

        dev_str    = result.get("deviations", {}).get(col, f"Deviated {direction}")
        root_cause = _build_breach_root_cause(
            plabel, direction, _safe_round(actual), lcl, ucl, str(dev_str),
            result=result, col=col,
        )
        recommendation = _build_breach_recommendation(bare, direction)

        # ── 1. Collect new alert-types — sent as one batch POST below ─────────
        type_key = f"ps::{bare}::{alert_name}"
        type_payload = {
            "foundry_key"        : foundry_key,
            "name"               : alert_name,
            "parameter_label"    : plabel,
            "category"           : "Prepared Sand",
            "severity"           : severity,
            "threshold_min"      : lcl,
            "threshold_max"      : ucl,
            "threshold_unit"     : unit,
            "line_pkey"          : line_pkey,
        }
        if type_key not in _registered_lcl_ucl_types:
            new_types.append(type_payload)
            new_type_keys.append(type_key)

        batch.append({
            "alert_id"            : f"ALT-PS-{period_key}-{bare}-{datetime.now().strftime('%H%M%S')}",
            "foundry_key"         : foundry_key,
            "process"             : "Prepared Sand",
            "parameter"           : plabel,
            "parameter_label"     : alert_name,
            "actual_value"        : _safe_round(actual),
            "threshold_min"       : lcl,
            "threshold_max"       : ucl,
            "threshold_percentage": thr_pct,
            "unit"                : unit,
            "severity"            : severity,
            "shift"               : shift,
            "line_pkey"           : line_pkey,
            "root_cause"          : root_cause,
            "recommendation"      : recommendation,
            "triggered_at"        : triggered_at,
        })

    # ── Send all new alert-types in one batch POST ───────────────────────────
    if new_types:
        if _post_alert_type_batch(cfg, new_types, label):
            for k in new_type_keys:
                _registered_lcl_ucl_types.add(k)

    if batch:
        sent = _post_alert_batch(cfg, batch, label)

    if sent == 0 and any(
        col.startswith("ps_") and str(lbl) in (CRITICAL, WARNING)
        for col, lbl in param_labels.items()
    ):
        logger.warning(
            "[%s]  CRITICAL/WARNING sigma params found but 0 alerts sent — check webhook config", label
        )

    return sent

#  BAD BATCH DAILY SUMMARY WEBHOOK

def send_bad_batch_daily_webhook(
    config:   dict,
    date_str: str,
    rows:     list,
    label:    str = "",
) -> bool:
    """
    POST a bad-batch end-of-day shift-wise summary to /api/sandman/alert-summary.

    Uses a dedicated summary route — NOT /alert or /alert-type.
    Fires at day boundary regardless of severity filter (bypasses _severity_passes).
    Requires webhook.enabled=true AND webhook.send_bad_batch=true.

    rows: list of {shift, total, bad, pct} dicts for the completed date.
    """
    cfg = _get_cfg(config)
    if not cfg.get("enabled", False):
        logger.debug("[%s]  daily summary webhook skipped — webhook disabled", label)
        return False
    if not cfg.get("send_bad_batch", False):
        logger.debug("[%s]  daily summary webhook skipped — send_bad_batch=false", label)
        return False
    if not rows:
        return False

    try:
        foundry_key  = str(config.get("customer_pkey", ""))
        line_pkey    = int(config.get("foundry_line_id", 1))
        bb_cfg       = config.get("bad_batch_watchdog", {})
        ok_thr       = float(bb_cfg.get("pct_ok_thr", 1.0)) * 100
        triggered_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        grand_total = sum(r["total"] for r in rows)
        grand_bad   = sum(r["bad"]   for r in rows)
        grand_pct   = round(grand_bad / grand_total * 100, 1) if grand_total > 0 else 0.0
        status      = "OK" if grand_pct <= ok_thr else "BAD BATCH"

        # Build shift-wise payload rows with resolved shift names
        shift_data = []
        for r in rows:
            shift_data.append({
                "shift"      : _shift_name(cfg, r["shift"]),
                "total"      : int(r["total"]),
                "bad"        : int(r["bad"]),
                "bad_pct"    : float(r["pct"]),
            })

        payload = {
            "foundry_key"  : foundry_key,
            "line_pkey"    : line_pkey,
            "date"         : date_str,
            "triggered_at" : triggered_at,
            "grand_total"  : grand_total,
            "grand_bad"    : grand_bad,
            "grand_pct"    : grand_pct,
            "status"       : status,
            "shifts"       : shift_data,
        }

        base    = str(cfg.get("base_url", "")).rstrip("/")
        timeout = int(cfg.get("timeout_sec", 10))
        url     = f"{base}/api/sandman/alert-summary"

        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()

        logger.info(
            "[%s]  Daily summary webhook sent — %s  bad=%d/%d (%.1f%%)  status=%s  HTTP=%s",
            label, date_str, grand_bad, grand_total, grand_pct, status, resp.status_code,
        )
        _api_logger.info(
            "POST /alert-summary  STATUS=%s  date=%s  foundry=%s  line=%s  bad=%d/%d (%.1f%%)  status=%s  url=%s",
            resp.status_code, date_str, foundry_key, line_pkey,
            grand_bad, grand_total, grand_pct, status, url,
        )
        return True

    except Exception as exc:
        logger.warning("[%s]  send_bad_batch_daily_webhook FAILED: %s", label, exc)
        _api_logger.error(
            "POST /alert-summary  STATUS=FAILED  date=%s  foundry=%s  error=%s",
            date_str, config.get("customer_pkey", ""), exc,
        )
        return False

#  INTERNAL HELPERS

def _get_cfg(config: dict) -> dict:
    return config.get("webhook", {})

def _severity_passes(cfg: dict, severity: str) -> bool:
    """Return True if severity meets the configured minimum threshold."""
    min_sev = str(cfg.get("min_severity", "warning")).lower()
    order   = {"warning": 0, "critical": 1}
    return order.get(severity.lower(), 0) >= order.get(min_sev, 0)

def _shift_name(cfg: dict, shift) -> str:
    custom  = cfg.get("shift_names", {})
    mapping = {**_DEFAULT_SHIFT_NAMES, **{str(k): v for k, v in custom.items()}}
    return mapping.get(str(shift).strip(), f"Shift {shift}" if shift else "")

def _process_for_param(param: str) -> str:
    if param.startswith(("ps_", "pse_")):
        return "Prepared Sand"
    if param.startswith("add_"):
        return "Additive"
    if param.startswith("con_"):
        return "Consumption"
    return "Sand"

def _bare_name(param: str) -> str:
    for pfx in ("ps_", "pse_", "add_", "con_", "sv_"):
        if param.startswith(pfx):
            return param[len(pfx):]
    return param

def _unit_for_param(param: str) -> str:
    lower = param.lower()
    if "water" in lower or "ltr" in lower:
        return "ltr"
    if param.startswith("add_"):
        return "kg"
    return "%"

def _get_limits(bare_name: str, db_limits: dict) -> tuple:
    entry = db_limits.get(bare_name, {})
    lcl   = entry.get("lcl")
    ucl   = entry.get("ucl")
    return (
        round(float(lcl), 4) if lcl is not None else None,
        round(float(ucl), 4) if ucl is not None else None,
    )

def _safe_round(v, ndigits: int = 4) -> Optional[float]:
    try:
        f = float(v)
        return round(f, ndigits) if f == f else None
    except (TypeError, ValueError):
        return None

def _triggered_at(result: dict) -> str:
    return datetime.now().isoformat(timespec="seconds")

def _post_alert_batch(cfg: dict, batch: list, label: str) -> int:
    """POST each alert individually to /api/sandman/alert with 1s spacing."""
    sent = 0
    for payload in batch:
        if _post_alert(cfg, payload, label):
            sent += 1
            if len(batch) > 1:
                time.sleep(1)
    return sent

def _post_alert_type(cfg: dict, payload: dict, label: str) -> bool:
    """Register a single alert-type (kept for prescription and bad-batch callers)."""
    return _post_alert_type_batch(cfg, [payload], label)

def _post_alert_type_batch(cfg: dict, payloads: list, label: str) -> bool:
    """
    Register multiple alert-types in one POST.
    Sends the list as a JSON array — the API accepts both a single object and an array.
    Returns True if all succeeded.
    """
    if not payloads:
        return True
    base    = str(cfg.get("base_url", "")).rstrip("/")
    timeout = int(cfg.get("timeout_sec", 10))
    url     = f"{base}/api/sandman/alert-type"
    all_ok  = True
    for payload in payloads:
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            logger.debug("[%s]  alert-type registered: %s", label, payload.get("name"))
            _api_logger.info(
                "POST /alert-type  STATUS=%s  name=%s  url=%s",
                resp.status_code, payload.get("name"), url,
            )
        except Exception as exc:
            logger.warning("[%s]  POST /alert-type failed [%s]: %s", label, payload.get("name"), exc)
            _api_logger.error(
                "POST /alert-type  STATUS=FAILED  name=%s  url=%s  error=%s",
                payload.get("name"), url, exc,
            )
            all_ok = False
    return all_ok

def _post_alert(cfg: dict, payload: dict, label: str,
                _type_payload: dict = None, _type_key: str = None) -> bool:
    """
    Post an alert instance. On 404 (alert-type not found on server), automatically
    clears the cached registration and re-registers the type before retrying once.
    """
    base    = str(cfg.get("base_url", "")).rstrip("/")
    timeout = int(cfg.get("timeout_sec", 10))
    url     = f"{base}/api/sandman/alert"

    def _do_post():
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp

    try:
        resp = _do_post()
        logger.info(
            "[%s]  webhook alert sent  id=%s  param=%s  severity=%s",
            label, payload.get("alert_id"), payload.get("parameter"), payload.get("severity"),
        )
        _api_logger.info(
            "POST /alert  STATUS=%s  alert_id=%s  param=%s  process=%s  severity=%s"
            "  actual=%s  lcl=%s  ucl=%s  shift=%s  foundry=%s  line=%s  component=%s  url=%s",
            resp.status_code,
            payload.get("alert_id"), payload.get("parameter"), payload.get("process"),
            payload.get("severity"), payload.get("actual_value"),
            payload.get("threshold_min"), payload.get("threshold_max"),
            payload.get("shift"), payload.get("foundry_key"), payload.get("line_pkey"),
            payload.get("component", ""), url,
        )
        return True
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404 and _type_payload:
            # Alert-type no longer exists on server (server restarted) — re-register and retry
            logger.warning("[%s]  POST /alert 404 — re-registering alert-type and retrying: %s",
                           label, payload.get("alert_id"))
            # Clear stale cache entries
            if _type_key:
                _registered_types.discard(_type_key)
                _registered_lcl_ucl_types.discard(_type_key)
            _post_alert_type(cfg, _type_payload, label)
            try:
                resp = _do_post()
                _api_logger.info(
                    "POST /alert  STATUS=%s  alert_id=%s  param=%s  url=%s  (retry after 404)",
                    resp.status_code, payload.get("alert_id"), payload.get("parameter"), url,
                )
                return True
            except Exception as retry_exc:
                _api_logger.error(
                    "POST /alert  STATUS=FAILED  alert_id=%s  param=%s  url=%s  error=%s  (retry)",
                    payload.get("alert_id"), payload.get("parameter"), url, retry_exc,
                )
                return False
        logger.warning("[%s]  POST /alert failed  id=%s: %s",
                       label, payload.get("alert_id"), exc)
        _api_logger.error(
            "POST /alert  STATUS=FAILED  alert_id=%s  param=%s  url=%s  error=%s",
            payload.get("alert_id"), payload.get("parameter"), url, exc,
        )
        return False
    except Exception as exc:
        logger.warning("[%s]  POST /alert failed  id=%s: %s",
                       label, payload.get("alert_id"), exc)
        _api_logger.error(
            "POST /alert  STATUS=FAILED  alert_id=%s  param=%s  url=%s  error=%s",
            payload.get("alert_id"), payload.get("parameter"), url, exc,
        )
        return False

_BREACH_RECS = {
    ("moisture",               "HIGH"): "Reduce water addition by 3–6 litres per batch. Check sand temperature — high temperature can cause the system to over-dose water to compensate.",
    ("moisture",               "LOW"):  "Increase water addition by 3–5 litres. Check if sand temperature has risen, which would increase evaporation during mixing.",
    ("active_clay",            "HIGH"): "Reduce bentonite addition by 8–12% over the next 2–3 batches and re-measure active clay after each batch.",
    ("active_clay",            "LOW"):  "Increase bentonite dosing by 8–10%. Check if fresh sand rate increased recently, as dilution reduces active clay concentration.",
    ("compactibility",         "HIGH"): "Reduce water addition by 5–8%. If active clay is also elevated, reduce bentonite first.",
    ("compactibility",         "LOW"):  "Increase water addition by 3–5%. If active clay is low, address clay first before adding water.",
    ("loi",                    "HIGH"): "Reduce coal dust addition by 10–15%. Excessive LOI increases rejection risk and affects permeability.",
    ("loi",                    "LOW"):  "Increase coal dust addition by 10–12%. Low LOI reduces lustrous carbon generation and increases metal penetration risk.",
    ("volatile_matter",        "HIGH"): "Reduce coal dust or switch to a lower VM coal blend. Excessively high VM can cause blow holes.",
    ("volatile_matter",        "LOW"):  "Increase coal dust dosing or check the coal batch quality — volatile content may have degraded.",
    ("gfn_afs",                "HIGH"): "Reduce fresh sand addition or switch to a coarser FSS grade. Review recent FSS supplier batch AFS certificates.",
    ("gfn_afs",                "LOW"):  "Check fresh sand quality — supplier batch may be coarser than specification. Increase FSS addition rate temporarily.",
    ("inert_fines",            "HIGH"): "Review fresh sand addition rate and sand quality certificate. Increase return sand ratio slightly to dilute fines.",
    ("permeability",           "HIGH"): "Check moisture and active clay — high permeability may indicate under-dosed clay. Verify compactibility is in range.",
    ("permeability",           "LOW"):  "Reduce moisture by 3–5 litres and check active clay. High-clay, high-moisture sand consistently produces low permeability.",
    ("gcs",                    "HIGH"): "Review bentonite and water — both directly drive GCS. If compactibility is also elevated, reduce water first.",
    ("gcs",                    "LOW"):  "Increase bentonite addition by 8–10%. If moisture is also low, correct water first.",
    ("shear_strength",         "HIGH"): "Check bentonite and moisture — both drive shear strength. If GCS is also high, reduce bentonite gradually.",
    ("shear_strength",         "LOW"):  "Increase bentonite by 5–8% or verify moisture is at target. Low shear strength significantly increases mould collapse risk.",
    ("split_strength",         "HIGH"): "Review bentonite and active clay. High split strength with high GCS usually indicates over-bonded sand — reduce bentonite.",
    ("split_strength",         "LOW"):  "Increase bentonite addition by 8–10%. Verify moisture is at target — low moisture prevents clay from developing binding strength.",
    ("temp_of_sand_after_mix", "HIGH"): "Check cooling system function. Increase water addition slightly for evaporative cooling effect.",
    ("temp_of_sand_after_mix", "LOW"):  "Verify heating/mixing system. Cold sand reduces the activation of bentonite and slows water absorption.",
    ("compactability_smc_pct", "HIGH"): "Reduce water addition. If active clay is also high, address clay first.",
    ("compactability_smc_pct", "LOW"):  "Increase water addition by 3–5 litres. Verify active clay is at target.",
}

_BREACH_REC_FALLBACK = (
    "Review the parameter trend over the last 5–10 shifts with the process engineer. "
    "Cross-check additive dosing records against the target prescription before making any adjustment."
)

def _build_breach_root_cause(
    plabel:    str,
    direction: str,
    actual:    Optional[float],
    lcl:       Optional[float],
    ucl:       Optional[float],
    dev_str:   str,
    result:    Optional[dict] = None,
    col:       Optional[str]  = None,
) -> str:
    import re as _re

    limit_label = "upper control limit (UCL)" if direction == "HIGH" else "lower control limit (LCL)"
    limit_val   = ucl if direction == "HIGH" else lcl

    # Extract deviation delta from dev_str e.g. "Deviated HIGH +0.35  (UCL=4.0)"
    delta_str = ""
    m = _re.search(r"([+-][\d.]+)", dev_str)
    if m:
        delta_str = f", deviation {m.group(1)}"

    val_str = ""
    if actual is not None and limit_val is not None:
        val_str = f" — current: {actual:.4g}, limit: {limit_val:.4g}{delta_str}"

    parts = [f"{plabel} has breached the {limit_label}{val_str}."]

    # Engine context (drift / variance / oscillation) if available
    if result and col:
        _DRIFT_DEV = {"SLIGHT DRIFT", "STRONG DRIFT", "SLIGHT TREND", "STRONG TREND"}
        _VAR_DEV   = {"ELEVATED", "HIGH VAR"}
        _OSC_DEV   = {"OSCILLATING", "MILD"}

        drift_lbl = str(result.get("drift_labels", {}).get(col, "STABLE"))
        var_lbl   = str(result.get("var_labels",   {}).get(col, "STABLE"))
        osc_lbl   = str(result.get("osc_labels",   {}).get(col, "STABLE"))
        pct_chg   = result.get("pct_changes", {}).get(col)

        engine_parts = []
        if drift_lbl in _DRIFT_DEV:
            trend_dir = "upward" if direction == "HIGH" else "downward"
            strength  = "strong" if "STRONG" in drift_lbl else "gradual"
            engine_parts.append(f"a {strength} {trend_dir} drift is in progress ({drift_lbl})")
        if var_lbl in _VAR_DEV:
            engine_parts.append(f"variance is {var_lbl.lower()} — readings are inconsistent between batches")
        if osc_lbl in _OSC_DEV:
            engine_parts.append(f"oscillation detected ({osc_lbl.lower()}) — alternating high/low readings")
        if pct_chg is not None and abs(pct_chg) >= 2.0:
            pct_word = "up" if pct_chg > 0 else "down"
            engine_parts.append(f"trending {pct_word} {abs(pct_chg):.1f}% from baseline")

        if engine_parts:
            parts.append("Also: " + "; ".join(engine_parts) + ".")

    return " ".join(parts)

def _build_breach_recommendation(bare: str, direction: str) -> str:
    return _BREACH_RECS.get((bare, direction), _BREACH_REC_FALLBACK)
