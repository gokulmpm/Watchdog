"""
prescription_watchdog.py
-------------------------
Prescription deviation monitor.

Polls for new additive batches since the last processed batch pkey, compares
each batch against the AI-predicted prescription, and writes deviations to
watchdog_alerts (alert_type='PRESCRIPTION') via alert_db_writer.

Config keys consumed (watchdog_config.json -> prescription_watchdog):
  tolerance          -- max allowed deviation in kg/ltr (default 1.0)
  poll_interval_sec  -- seconds between checks (default 30)
  idle_timeout_min   -- stop after this many minutes with no new batches (0 = never, default 60)
  skip_zero_batches  -- skip batches where all prescribed values are 0 (default true)

Usage
-----
  Standalone:
      python -m watchdog.prescription_watchdog

  Embedded (from run_alert_monitor.py or any other entry point):
      from watchdog.prescription_watchdog import PrescriptionWatchdog
      monitor = PrescriptionWatchdog(config)
      thread  = threading.Thread(target=monitor.start, daemon=True)
      thread.start()
"""

import logging
import time
import traceback
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_PRESC_PARAMS = ["bentonite", "freshSilicaSand", "lca", "water"]

class PrescriptionWatchdog:
    """Poll for new additive batches and write prescription alerts."""

    def __init__(self, config: dict, label: str = "prescription") -> None:
        self._config             = config
        self._label              = label
        self._last_batch_pkey    = 0
        self._current_component  = ""   # currently running component_id

        # Deduplication — one alert per (component_id, date, shift, param) per shift.
        # The key includes component_id so A -> B -> A within the same shift
        # correctly alerts B but does NOT re-alert A.
        # The set is cleared automatically because the shift/date in the key
        # changes each new shift — old keys never match new shift keys.
        self._alerted: set = set()   # {(component_id, date_str, shift_str, param)}

        # Per-instance trend history: {(foundry_line_id, component_id, param): [pct_diff, ...]}
        self._trend_history: dict = {}

    def start(self) -> None:
        """Run forever (call from a daemon thread)."""
        pw_cfg       = self._config.get("prescription_watchdog", {})
        poll_sec     = int(pw_cfg.get("poll_interval_sec", 30))
        idle_timeout = int(pw_cfg.get("idle_timeout_min",  60))

        logger.info(
            "[%s]  Prescription monitor starting  (poll=%ds  idle_timeout=%dmin)",
            self._label, poll_sec, idle_timeout,
        )

        # Check whether this foundry line has SCADA data
        from .pipeline.data_fetcher import is_prescription_eligible
        if not is_prescription_eligible(self._config):
            logger.info(
                "[%s]  Foundry line has no SCADA connection -- prescription monitoring skipped",
                self._label,
            )
            return

        while True:
            try:
                self._last_batch_pkey   = self._fetch_max_pkey()
                self._current_component = self._fetch_current_component()
                break  # init succeeded
            except Exception as _init_exc:
                logger.warning("[%s] startup init failed: %s — retrying in 30s", self._label, _init_exc)
                time.sleep(30)
        logger.info("[%s]  Starting from pkey=%d  component=%s",
                    self._label, self._last_batch_pkey, self._current_component or "(none)")

        last_new_batch_time: Optional[datetime] = datetime.now()

        import gc as _gc
        _gc_counter = 0

        while True:
            try:
                # Re-read all config values each cycle — picks up live DB updates
                pw_cfg             = self._config.get("prescription_watchdog", {})
                skip_zero          = bool(pw_cfg.get("skip_zero_batches", True))
                setpoint_monitor   = bool(pw_cfg.get("setpoint_monitoring",  True))
                setpoint_tolerance = float(pw_cfg.get("setpoint_tolerance",  0.5))
                trend_window       = int(pw_cfg.get("trend_window",          5))
                trend_min          = int(pw_cfg.get("trend_min_batches",     3))

                _missing = [k for k in ("tolerance_pct", "watch_thr", "critical_thr")
                            if pw_cfg.get(k) is None]
                if _missing:
                    logger.warning(
                        "[%s]  Skipping poll — missing thresholds in DB config: %s",
                        self._label, _missing,
                    )
                    time.sleep(poll_sec)
                    continue

                tolerance_pct    = float(pw_cfg["tolerance_pct"])
                watch_thr        = float(pw_cfg["watch_thr"])
                critical_thr     = float(pw_cfg["critical_thr"])
                param_thresholds = dict(pw_cfg.get("param_thresholds") or {})
                param_columns    = dict(pw_cfg.get("param_columns")    or {})

                new_rows = self._poll_new_batches(tolerance_pct, skip_zero,
                                                  watch_thr, critical_thr,
                                                  setpoint_monitor, setpoint_tolerance,
                                                  trend_window, trend_min,
                                                  param_thresholds=param_thresholds,
                                                  param_columns=param_columns)

                if new_rows > 0:
                    last_new_batch_time = datetime.now()
                    logger.info("[%s]  Processed %d new batch(es)", self._label, new_rows)
                else:
                    if idle_timeout > 0 and last_new_batch_time is not None:
                        idle_secs = (datetime.now() - last_new_batch_time).total_seconds()
                        if idle_secs >= idle_timeout * 60:
                            logger.info(
                                "[%s]  Idle for %.0f min -- stopping prescription monitor",
                                self._label, idle_secs / 60,
                            )
                            return

            except Exception:
                logger.error("[%s]  Error in poll loop:\n%s", self._label, traceback.format_exc())

            # Force garbage collection every 10 minutes to release pandas memory
            _gc_counter += poll_sec
            if _gc_counter >= 600:
                _gc.collect()
                _gc_counter = 0

            time.sleep(poll_sec)

    def _poll_new_batches(self, tolerance_pct: float, skip_zero: bool,
                          watch_thr: float = 1.0,
                          critical_thr: float = 3.0,
                          setpoint_monitor: bool = True,
                          setpoint_tolerance: float = 0.5,
                          trend_window: int = 5,
                          trend_min: int = 3,
                          param_thresholds: dict = None,
                          param_columns: dict = None) -> int:
        """Fetch batches newer than last_batch_pkey for the current component only."""
        from .pipeline.data_fetcher import fetch_prescription_data, fetch_prescription_data_scada
        from .pipeline.db_connector import get_engine
        from .alert_db_writer       import ensure_table, write_prescription_alert

        use_scada  = bool(self._config.get("prescription_watchdog", {}).get("use_scada", False))
        end_date   = date.today()
        start_date = end_date - timedelta(days=1)

        df = (fetch_prescription_data_scada if use_scada else fetch_prescription_data)(
            self._config, start_date=start_date, end_date=end_date
        )
        if df.empty:
            return 0

        # Filter to rows newer than watermark
        if "Batch pkey" in df.columns:
            df = df[df["Batch pkey"] > self._last_batch_pkey]
        if df.empty:
            return 0

        # Detect component change from the newest batch
        if "Component ID" in df.columns:
            latest_comp = (
                df.sort_values("Batch pkey")
                  .dropna(subset=["Component ID"])
                  ["Component ID"]
                  .astype(str).str.strip()
                  .iloc[-1]
            )
            if latest_comp and latest_comp not in ("", "nan", "None"):
                if latest_comp != self._current_component:
                    logger.info(
                        "[%s]  Component change: %s -> %s",
                        self._label,
                        self._current_component or "(none)",
                        latest_comp,
                    )
                    # Clear dedup keys for the incoming component so it gets
                    # fresh alerts on this new production run
                    self._alerted = {k for k in self._alerted if k[0] != latest_comp}
                    self._current_component = latest_comp

        # Only write prescription alerts for the currently running component
        if self._current_component:
            comp_df = df[
                df["Component ID"].astype(str).str.strip() == self._current_component
            ]
        else:
            comp_df = df

        engine = get_engine(self._config)
        ensure_table(engine)

        fl_id     = int(self._config.get("foundry_line_id", 1))
        monitored = (
            self._config.get("prescription_watchdog", {}).get("monitored_params")
            or _PRESC_PARAMS
        )
        rows_written = 0

        for _, row in comp_df.iterrows():
            if _all_actuals_zero(row, monitored):
                if skip_zero:
                    logger.debug(
                        "[%s]  Skipping batch pkey=%s -- all actuals zero/null",
                        self._label, row.get("Batch pkey"),
                    )
                    continue
                # skip_zero=False -> still process so per-param annotations are generated

            group_name = str(row.get("Group", "") or "")
            batch_date = row.get("Date")
            shift_val  = str(row.get("Shift", "") or "")
            prediction = _fetch_analytics_prediction(
                self._config, group_name, batch_date, shift_val
            )
            if not prediction:
                logger.debug(
                    "[%s]  No analytics_report prediction for group=%s date=%s shift=%s — skipping",
                    self._label, group_name, batch_date, shift_val,
                )
                continue

            # ── Build deviations using upper = pred*(1+tol/100), lower = pred*(1-tol/100)
            deviations = _build_deviations_list(
                row, tolerance_pct, monitored, prediction,
                watch_thr=watch_thr,
                critical_thr=critical_thr,
                fl_id=fl_id,
                component_id=str(row.get("Component ID", "") or ""),
                setpoint_monitoring=setpoint_monitor,
                setpoint_tolerance=setpoint_tolerance,
                trend_window=trend_window,
                param_thresholds=param_thresholds,
                param_columns=param_columns,
                trend_min_batches=trend_min,
                trend_history=self._trend_history,
            )
            result = {
                "pkey"        : row.get("Batch pkey"),
                "component_id": row.get("Component ID", ""),
                "group_name"  : group_name,
                "date"        : batch_date,
                "shift"       : shift_val,
                "timestamp"   : row.get("Batch Time"),
                "deviations"  : deviations,
            }
            n = write_prescription_alert(engine, result, fl_id, tolerance_pct,
                                         customer_pkey=self._config.get("customer_pkey", 0))
            rows_written += n
            if n > 0:
                try:
                    from .email_notifier import send_prescription_email, check_and_send_combined
                    alert_devs = [d for d in deviations
                                  if str(d.get("severity","")).lower() in ("warning","critical")]
                    if alert_devs:
                        # Deduplicate — only alert once per (component, date, shift, param)
                        comp_id   = str(result.get("component_id", ""))
                        date_str  = str(result.get("date", ""))
                        shift_str = str(result.get("shift", ""))
                        new_devs  = [
                            d for d in alert_devs
                            if (comp_id, date_str, shift_str, d.get("param",""))
                               not in self._alerted
                        ]
                        if new_devs:
                            # Mark these as alerted before sending
                            for d in new_devs:
                                self._alerted.add(
                                    (comp_id, date_str, shift_str, d.get("param",""))
                                )
                            send_prescription_email(result, deviations, self._config, label=self._label)
                            self._send_prescription_webhook(new_devs, result)
                    # Check if bad batch alert also exists -> combined alert
                    check_and_send_combined(
                        engine, self._config,
                        component_id    = str(result.get("component_id", "")),
                        date_str        = str(result.get("date", "")),
                        shift           = str(result.get("shift", "")),
                        foundry_line_id = fl_id,
                        label           = self._label,
                    )
                except Exception as _email_exc:
                    logger.warning("[%s]  Prescription email/combined failed: %s", self._label, _email_exc)

        # Advance watermark over ALL fetched rows (not just current component)
        if "Batch pkey" in df.columns:
            self._last_batch_pkey = int(df["Batch pkey"].max())

        return rows_written

    def _send_prescription_webhook(self, alert_devs: list, result: dict) -> None:
        """
        Send one webhook alert per WARNING/CRITICAL prescription deviation.
        Fires for severity == 'warning' or 'critical'.
        Payload matches the push-notification API format:
          { foundry_key, line_pkey, name, parameter_label, category,
            severity, threshold_min, threshold_max, threshold_unit }
        """
        from .webhook_notifier import _get_cfg, _post_alert_type, _post_alert, _shift_name, _severity_passes
        cfg = _get_cfg(self._config)
        if not cfg.get("enabled", False):
            return
        if not cfg.get("send_prescription", False):
            return

        # Filter devs by the "Minimum severity to send" dropdown in webhook config
        alert_devs = [d for d in alert_devs
                      if _severity_passes(cfg, str(d.get("severity", "warning")))]
        if not alert_devs:
            return

        foundry_key = str(self._config.get("customer_pkey", ""))
        line_pkey   = int(self._config.get("foundry_line_id", 1))

        _UNIT_MAP = {
            "bentonite"      : "Kg",
            "freshSilicaSand": "Kg",
            "lca"            : "Kg",
            "water"          : "Ltr",
        }

        from datetime import datetime as _dt
        shift       = _shift_name(cfg, result.get("shift", ""))
        triggered   = str(result.get("batch_time") or result.get("date") or
                          _dt.now().isoformat(timespec="seconds"))[:19]

        for d in alert_devs:
            param      = d.get("param", "")
            lbl        = d.get("label") or param.replace("_", " ").title()
            actual     = d.get("actual")
            pres       = d.get("prescribed")
            sp         = d.get("setpoint")
            pct        = d.get("pct_diff", 0)
            comp       = d.get("comparison", "actual_vs_predicted")
            ref        = pres if comp == "actual_vs_predicted" else sp
            unit       = _UNIT_MAP.get(param, "Kg")
            direction   = "High" if (pct or 0) > 0 else "Low"
            _unit_sfx   = f" ({unit})" if unit and f"({unit})" not in lbl else ""
            alert_name  = f"{direction} {lbl}{_unit_sfx}"
            thr_min    = round(float(ref) * 0.97, 4) if ref else None
            thr_max    = round(float(ref) * 1.03, 4) if ref else None
            thr_pct    = round(float(pct), 2) if pct is not None else None
            sev_raw    = str(d.get("severity", "warning")).lower()
            sev_label  = "Critical" if sev_raw == "critical" else "Warning"

            type_payload = {
                "foundry_key"    : foundry_key,
                "line_pkey"      : line_pkey,
                "name"           : alert_name,
                "parameter_label": lbl,
                "category"       : "Additive",
                "severity"       : sev_label,
                "threshold_min"  : thr_min,
                "threshold_max"  : thr_max,
                "threshold_unit" : unit,
            }

            # Build recommendation from direction and comparison type
            if comp == "setpoint_vs_prescribed":
                rec = (
                    f"Update the {lbl} setpoint on the mixer control panel from "
                    f"{d.get('setpoint', '?')} to {d.get('prescribed', '?')} "
                    f"to match the current prescription. "
                    "The setpoint must be aligned with the prescription before the next batch."
                )
            elif comp == "actual_vs_setpoint":
                if direction == "High":
                    rec = (f"{lbl} was dispensed above the machine setpoint. "
                           "Check the load cell calibration and dosing mechanism — "
                           "over-dispensing may indicate a weighing or valve fault.")
                else:
                    rec = (f"{lbl} was dispensed below the machine setpoint. "
                           "Check the load cell calibration and dosing mechanism — "
                           "under-dispensing may indicate a blocked feeder or sensor fault.")
            else:
                if direction == "High":
                    rec = (f"Reduce {lbl} dosage to bring it within the prescribed range. "
                           + (d.get("trend", "") or "Review the last few batches for a consistent over-dose trend."))
                else:
                    rec = (f"Increase {lbl} dosage to bring it within the prescribed range. "
                           + (d.get("trend", "") or "Review the last few batches for a consistent under-dose trend."))

            alert_payload = {
                "alert_id"            : f"ALT-PRESC-{result.get('pkey','?')}-{param}",
                "foundry_key"         : foundry_key,
                "process"             : "Additive",
                "parameter"           : lbl,
                "parameter_label"     : alert_name,
                "actual_value"        : round(float(actual), 4) if actual is not None else None,
                "threshold_min"       : thr_min,
                "threshold_max"       : thr_max,
                "threshold_percentage": thr_pct,
                "unit"                : unit,
                "severity"            : sev_label,
                "shift"               : shift,
                "line_pkey"           : line_pkey,
                "component"           : result.get("component_id", ""),
                "root_cause"          : f"Component: {result.get('component_id','')}  |  {d.get('message', '')}",
                "recommendation"      : rec,
                "triggered_at"        : triggered,
            }

            try:
                _post_alert_type(cfg, type_payload, self._label)
                _post_alert(cfg, alert_payload, self._label,
                            _type_payload=type_payload,
                            _type_key=f"presc::{param}")
            except Exception as _we:
                logger.warning("[%s]  Prescription webhook failed for %s: %s",
                               self._label, param, _we)

    def _fetch_max_pkey(self) -> int:
        """Return the current MAX(pkey) in the source table for this foundry line."""
        from .pipeline.db_connector import get_engine
        from sqlalchemy import text

        fl_id      = self._config.get("foundry_line_id", 1)
        use_scada  = bool(self._config.get("prescription_watchdog", {}).get("use_scada", False))
        if use_scada:
            sql = text("SELECT COALESCE(MAX(`pkey`), 0) AS max_id "
                       "FROM `scada_data` WHERE `foundry_line_pkey` = :fl_id "
                       "AND (deleted = 0 OR deleted IS NULL)")
        else:
            sql = text("SELECT COALESCE(MAX(`pkey`), 0) AS max_id "
                       "FROM `additive` WHERE `foundry_line_id` = :fl_id AND `deleted` = 0")
        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                row = conn.execute(sql, {"fl_id": fl_id}).mappings().first()
            return int(row["max_id"]) if row else 0
        except Exception as exc:
            logger.warning("[%s]  _fetch_max_pkey failed: %s", self._label, exc)
            return 0

    def _fetch_current_component(self) -> str:
        """Return the component_id of the most recent batch."""
        from .pipeline.db_connector import get_engine
        from sqlalchemy import text

        fl_id     = self._config.get("foundry_line_id", 1)
        use_scada = bool(self._config.get("prescription_watchdog", {}).get("use_scada", False))
        if use_scada:
            sql = text("SELECT `component_id` FROM `scada_data` "
                       "WHERE `foundry_line_pkey` = :fl_id "
                       "  AND (deleted = 0 OR deleted IS NULL) "
                       "  AND `component_id` IS NOT NULL AND `component_id` != '' "
                       "ORDER BY `pkey` DESC LIMIT 1")
        else:
            sql = text("SELECT `component_id` FROM `additive` "
                       "WHERE `foundry_line_id` = :fl_id AND `deleted` = 0 "
                       "  AND `component_id` IS NOT NULL AND `component_id` != '' "
                       "ORDER BY `pkey` DESC LIMIT 1")
        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                row = conn.execute(sql, {"fl_id": fl_id}).mappings().first()
            return str(row["component_id"]).strip() if row else ""
        except Exception as exc:
            logger.warning("[%s]  _fetch_current_component failed: %s", self._label, exc)
            return ""

# -- Module-level helpers ------------------------------------------------------

_PARAM_LABELS = {
    "bentonite"      : "Bentonite",
    "freshSilicaSand": "Fresh Silica Sand",
    "lca"            : "LCA / Coal Dust",
    "water"          : "Water",
}

# Mapping: prescription param name -> additive table column name
_PARAM_TO_ACTUAL_COL = {
    "bentonite"      : "bentonite_actual",
    "freshSilicaSand": "fss_actual",
    "lca"            : "coal_dust_actual",
    "water"          : "water_actual",
}

# Mapping: prescription param name -> additive table SETPOINT column name
_PARAM_TO_SETPOINT_COL = {
    "bentonite"      : "bentonite_set_point",
    "freshSilicaSand": "fss_set_point",
    "lca"            : "coal_dust_set_point",
    "water"          : "water_set_point",
}

# trend_history moved to PrescriptionWatchdog.__init__ as self._trend_history

# Cache: (fl_id, group_name, date_str, shift) -> prediction dict
# One DB hit per (group, date, shift) — all batches in the same shift reuse it.
_prediction_cache: dict = {}

def _fetch_analytics_prediction(config: dict, group_name: str,
                                 batch_date, shift: str) -> dict:
    """
    Fetch the AI-predicted additive values from analytics_report for the
    given group / date / shift.

    Results are cached per (foundry_line, group, date, shift) so that all
    batches within the same shift hit the DB only once.

    Returns a dict like: {"bentonite": 64.5, "water": 95.0, "lca": 8.2, ...}
    Returns {} if no prediction found.
    """
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text
    import json as _json

    fl_id    = int(config.get("foundry_line_id", 1))
    date_str = str(batch_date)
    if not group_name:
        return {}

    cache_key = (fl_id, group_name, date_str, str(shift))
    if cache_key in _prediction_cache:
        return _prediction_cache[cache_key]

    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT `predicted_additives_json`
                    FROM   `analytics_report`
                    WHERE  `foundry_line_pkey`       = :fl_id
                      AND  `foundry_line_group_name` = :grp
                      AND  `date`                    = :dt
                      AND  `shift`                   = :sh
                      AND  `deleted`                 = 0
                    ORDER  BY `pkey` DESC
                    LIMIT  1
                """),
                {"fl_id": fl_id, "grp": group_name,
                 "dt": date_str, "sh": str(shift)},
            ).mappings().first()
        result = {}
        if row and row["predicted_additives_json"]:
            raw = row["predicted_additives_json"]
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
            result = {k: float(v) for k, v in parsed.items() if v is not None}
        _prediction_cache[cache_key] = result
        # Evict old entries — keep only the last 200 (shift × group combinations)
        if len(_prediction_cache) > 200:
            oldest = next(iter(_prediction_cache))
            del _prediction_cache[oldest]
        return result
    except Exception as exc:
        logger.debug("_fetch_analytics_prediction failed: %s", exc)
    return {}

def _deviation_severity(abs_pct: float,
                        ok_thr: float,
                        warn_thr: float,
                        critical_thr: float) -> str:
    if abs_pct <= ok_thr:
        return "ok"
    if abs_pct <= warn_thr:
        return "warning"
    return "critical"

def _build_deviations_list(row: pd.Series, tolerance_pct: float,
                            monitored: list,
                            prediction: dict = None,
                            watch_thr: float = 1.0,
                            critical_thr: float = 6.0,
                            fl_id: int = 0,
                            component_id: str = "",
                            setpoint_monitoring: bool = True,
                            setpoint_tolerance: float = 0.5,
                            trend_window: int = 5,
                            trend_min_batches: int = 3,
                            trend_history: dict = None,
                            param_thresholds: dict = None,
                            param_columns: dict = None) -> list[dict]:
    """
    Build per-parameter deviation dicts with two comparison types:

    1. Actual vs AI Prediction  -> operator not following AI prescription
    2. Actual vs Setpoint       -> dosing/load cell mechanical issue

    Severity thresholds (abs % deviation):
        ≤ 1%  -> ok
        1–3%  -> warning
        3–6%  -> critical
        > critical_thr  -> critical
    """
    prediction = prediction or {}
    if trend_history is None:
        trend_history = {}
    param_thresholds = param_thresholds or {}
    deviations = []

    for param in monitored:
        label = _PARAM_LABELS.get(param, param)

        # Per-param thresholds — fall back to global values if not configured
        _pthr      = param_thresholds.get(param, {})
        _ok_thr    = float(_pthr.get("ok_thr",   tolerance_pct))
        _warn_thr  = float(_pthr.get("warn_thr",  watch_thr))
        _crit_thr  = float(_pthr.get("crit_thr",  critical_thr))

        _pc        = (param_columns or {}).get(param, {})
        actual_col = _pc.get("actual_col") or _PARAM_TO_ACTUAL_COL.get(param) or f"{param}_actual"
        actual_raw = row.get(actual_col) if (actual_col and actual_col in row.index) \
                     else row.get(f"{param} Actual")
        try:
            actual = float(actual_raw)
        except (TypeError, ValueError):
            # Null / unreadable value — annotate instead of skip
            deviations.append({
                "param"      : param,
                "label"      : _PARAM_LABELS.get(param, param),
                "comparison" : "null_batch",
                "actual"     : None,
                "prescribed" : None,
                "setpoint"   : None,
                "diff"       : None,
                "pct_diff"   : None,
                "within"     : False,
                "severity"   : "annotation",
                "trend"      : "",
                "message"    : "No value recorded for this batch — sensor or data entry issue.",
                "annotation" : "NULL",
            })
            continue

        # Zero or NaN actual value -> annotate instead of skip
        if actual != actual or actual == 0.0:
            deviations.append({
                "param"      : param,
                "label"      : _PARAM_LABELS.get(param, param),
                "comparison" : "null_batch",
                "actual"     : 0.0 if actual == 0.0 else None,
                "prescribed" : None,
                "setpoint"   : None,
                "diff"       : None,
                "pct_diff"   : None,
                "within"     : False,
                "severity"   : "annotation",
                "trend"      : "",
                "message"    : "Zero value recorded — additive may not have been dosed. Check hopper/dispenser.",
                "annotation" : "ZERO",
            })
            continue

        sp_col   = _pc.get("setpoint_col") or _PARAM_TO_SETPOINT_COL.get(param) or f"{param}_set_point"
        sp_raw   = row.get(sp_col) if (sp_col and sp_col in row.index) else None
        setpoint = None
        if sp_raw is not None:
            try:
                v = float(sp_raw)
                setpoint = v if (v == v and v != 0.0) else None
            except (TypeError, ValueError):
                pass

        if param in prediction and prediction[param]:
            pred = float(prediction[param])
        else:
            pred_raw = row.get(f"{param} Prescribed")
            try:
                pred = float(pred_raw)
            except (TypeError, ValueError):
                pred = None
            if pred is not None and (pred != pred or pred == 0.0):
                pred = None

        #  Comparison A: Actual vs AI Prediction
        if pred is not None:
            diff_pred     = round(actual - pred, 3)
            pct_pred      = round((actual - pred) / pred * 100, 2) if pred else 0.0
            abs_pct_pred  = abs(pct_pred)
            sev_pred      = _deviation_severity(abs_pct_pred, _ok_thr, _warn_thr, _crit_thr)
            within_pred   = sev_pred == "ok"

            # Trend tracking: update history (cap total entries to prevent memory growth)
            trend_key = (fl_id, component_id, param)
            if len(trend_history) > 2000:
                # Evict oldest half when dict grows too large
                keys = list(trend_history.keys())
                for k in keys[:len(keys) // 2]:
                    del trend_history[k]
            hist = trend_history.setdefault(trend_key, [])
            hist.append(pct_pred)
            if len(hist) > trend_window:
                hist.pop(0)
            trend_msg = _compute_trend(hist, min_batches=trend_min_batches, ok_thr=_ok_thr)

            if sev_pred != "ok":
                direction = "over-dosed" if pct_pred > 0 else "under-dosed"
                deviations.append({
                    "param"        : param,
                    "label"        : label,
                    "comparison"   : "actual_vs_predicted",
                    "prescribed"   : round(pred, 4),
                    "actual"       : round(actual, 4),
                    "setpoint"     : round(setpoint, 4) if setpoint else None,
                    "diff"         : diff_pred,
                    "pct_diff"     : pct_pred,
                    "within"       : within_pred,
                    "severity"     : sev_pred,
                    "trend"        : trend_msg,
                    "ok_thr"       : _ok_thr,
                    "warn_thr"     : _warn_thr,
                    "crit_thr"     : _crit_thr,
                    "message"      : (
                        f"Operator is not following Sandman "
                        f"({direction} by {abs_pct_pred:.1f}%). "
                        "If dosing continues to deviate, "
                        "prepared sand properties will be impacted."
                    ),
                })

        #  Comparison B: Actual vs Setpoint (only if enabled)
        if setpoint_monitoring and setpoint is not None:
            diff_sp    = round(actual - setpoint, 3)
            pct_sp     = round((actual - setpoint) / setpoint * 100, 2) if setpoint else 0.0
            abs_pct_sp = abs(pct_sp)
            sev_sp     = _deviation_severity(abs_pct_sp, _ok_thr, _warn_thr, _crit_thr)

            if sev_sp != "ok":
                direction_sp = "above" if pct_sp > 0 else "below"
                deviations.append({
                    "param"        : param,
                    "label"        : label,
                    "comparison"   : "actual_vs_setpoint",
                    "prescribed"   : round(pred, 4) if pred else None,
                    "actual"       : round(actual, 4),
                    "setpoint"     : round(setpoint, 4),
                    "diff"         : diff_sp,
                    "pct_diff"     : pct_sp,
                    "within"       : sev_sp == "ok",
                    "severity"     : sev_sp,
                    "trend"        : "",
                    "ok_thr"       : _ok_thr,
                    "warn_thr"     : _warn_thr,
                    "crit_thr"     : _crit_thr,
                    "message"      : (
                        f"Actual {label} ({actual:.2f}) is {abs_pct_sp:.1f}% "
                        f"{direction_sp} the machine setpoint ({setpoint:.2f}). "
                        "Check whether the load cell and dosing mechanism are "
                        "working correctly — this may indicate a weighing or "
                        "dispensing error."
                    ),
                })

        #  Comparison C: Setpoint vs AI Prescription
        #  Fires when the machine setpoint itself is outside the allowed
        #  tolerance of the prescribed value — regardless of what was
        #  actually dosed.  Indicates the operator has not updated the
        #  machine setpoint to match the latest prescription.
        if setpoint_monitoring and setpoint is not None and pred is not None:
            diff_sp_pred = round(setpoint - pred, 3)
            abs_diff     = abs(diff_sp_pred)
            if abs_diff > setpoint_tolerance:
                direction_c  = "above" if diff_sp_pred > 0 else "below"
                pct_c        = round(diff_sp_pred / pred * 100, 2) if pred else 0.0
                deviations.append({
                    "param"        : param,
                    "label"        : label,
                    "comparison"   : "setpoint_vs_prescribed",
                    "prescribed"   : round(pred, 4),
                    "actual"       : round(actual, 4),
                    "setpoint"     : round(setpoint, 4),
                    "diff"         : diff_sp_pred,
                    "pct_diff"     : pct_c,
                    "within"       : False,
                    "severity"     : "critical",
                    "trend"        : "",
                    "ok_thr"       : _ok_thr,
                    "warn_thr"     : _warn_thr,
                    "crit_thr"     : _crit_thr,
                    "message"      : (
                        f"Machine setpoint for {label} ({setpoint:.2f}) is {abs_diff:.2f} units "
                        f"{direction_c} the prescribed value ({pred:.2f}), exceeding the allowed "
                        f"tolerance of ±{setpoint_tolerance}. "
                        "The machine setpoint has not been updated to match the latest prescription. "
                        "Please update the setpoint on the mixer control panel."
                    ),
                })

    return deviations

def _compute_trend(history: list, min_batches: int = 3, ok_thr: float = 1.0) -> str:
    """
    Analyse the last N pct_diff values for a consistent pattern.
    Returns a human-readable trend string, or "" if no clear trend.
    """
    if len(history) < min_batches:
        return ""
    recent = history
    n_neg  = sum(1 for v in recent if v < -ok_thr)
    n_pos  = sum(1 for v in recent if v >  ok_thr)
    n      = len(recent)
    if n_neg >= min_batches:
        avg = round(sum(v for v in recent if v < 0) / max(n_neg, 1), 1)
        return f"Consistently under-dosed in {n_neg}/{n} recent batches (avg {avg:+.1f}%)"
    if n_pos >= min_batches:
        avg = round(sum(v for v in recent if v > 0) / max(n_pos, 1), 1)
        return f"Consistently over-dosed in {n_pos}/{n} recent batches (avg {avg:+.1f}%)"
    return ""

def _all_actuals_zero(row: pd.Series, monitored: list) -> bool:
    """Return True if every monitored parameter has an actual value of 0 or null."""
    for param in monitored:
        actual_col = _PARAM_TO_ACTUAL_COL.get(param) or f"{param}_actual"
        val = row.get(actual_col)
        if val is None:
            val = row.get(f"{param} Actual")
        try:
            if float(val) != 0.0:
                return False
        except (TypeError, ValueError):
            continue
    return True

# -- One-shot check ------------------------------------------------------------

def run_check(config: dict, days: int = 7, write_db: bool = False) -> None:
    """
    Fetch all batches for the LAST RUNNING COMPONENT from the additive table,
    evaluate prescription deviations for each batch, print a summary, and
    optionally write alerts to DB.

    Only the current component's batches are analysed — previous components
    are ignored, matching the behaviour of the live monitor.
    """
    from datetime import date, timedelta
    from .pipeline.data_fetcher import fetch_prescription_data
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    pw_cfg        = config.get("prescription_watchdog", {})
    tolerance_pct = float(pw_cfg.get("tolerance_pct", 3.0))
    skip_zero     = bool(pw_cfg.get("skip_zero_batches", True))
    monitored     = pw_cfg.get("monitored_params") or _PRESC_PARAMS

    fl_id   = int(config.get("foundry_line_id", 1))
    current_comp = ""
    try:
        eng = get_engine(config)
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT `component_id` FROM `additive` "
                     "WHERE `foundry_line_id` = :fl_id AND `deleted` = 0 "
                     "  AND `component_id` IS NOT NULL AND `component_id` != '' "
                     "ORDER BY `pkey` DESC LIMIT 1"),
                {"fl_id": fl_id},
            ).mappings().first()
        current_comp = str(row["component_id"]).strip() if row else ""
    except Exception as exc:
        logger.info("  WARNING: could not fetch current component: %s", exc)

    if not current_comp:
        logger.info("  No active component found in additive table.")
        return

    # -- Fetch prescription data for the last `days` days ----------------------
    end_date   = date.today()
    start_date = end_date - timedelta(days=days)

    logger.info("  Current Component : %s", current_comp)
    logger.info("  Date range        : %s  ->  %s", start_date, end_date)
    logger.info("  Tolerance         : ±%s%%", tolerance_pct)
    logger.info("  Monitoring        : %s", ", ".join(monitored))

    df = fetch_prescription_data(config, start_date=start_date, end_date=end_date)
    if df.empty:
        logger.info("  No batches found for this date range.")
        return

    # Filter to the current component only, then take the single last batch
    if "Component ID" in df.columns:
        df = df[df["Component ID"].astype(str).str.strip() == current_comp]

    if df.empty:
        logger.info("  No batches found for component %s in the last %d days.", current_comp, days)
        return

    # Keep only the last batch of the current component
    if "Batch pkey" in df.columns:
        last_pkey = df["Batch pkey"].max()
        df = df[df["Batch pkey"] == last_pkey]
    else:
        df = df.tail(1)

    if write_db:
        from .alert_db_writer import ensure_table, write_prescription_alert
        ensure_table(eng)

    SEP  = "=" * 90
    SEP2 = "-" * 90
    rows_written = 0

    row = df.iloc[0]
    batch_pkey = row.get("Batch pkey", "—")
    logger.info(SEP)
    logger.info("  Component : %s", current_comp)
    logger.info("  Batch     : %s  |  %s  Shift %s  |  Group: %s",
                batch_pkey, row.get("Date", ""), row.get("Shift", ""), row.get("Group", "—"))
    logger.info(SEP2)
    logger.info("  %-26s  %12s  %12s  %10s  %8s  Status", "Parameter", "Prescribed", "Actual", "Diff", "%Diff")
    logger.info(SEP2)

    if skip_zero and _all_actuals_zero(row, monitored):
        logger.info("  All actual values are zero — batch skipped.")
    else:
        deviations = _build_deviations_list(row, tolerance_pct, monitored)
        out_of_tol = [d for d in deviations if not d["within"]]

        for d in deviations:
            mark = " <--" if not d["within"] else ""
            logger.info("  %-26s  %12.3f  %12.3f  %+10.3f  %+7.2f%%  %s%s",
                        d["label"], d["prescribed"], d["actual"], d["diff"], d["pct_diff"],
                        "DEVIATION" if not d["within"] else "OK", mark)

        logger.info(SEP2)
        status = "DEVIATION FOUND" if out_of_tol else "ALL WITHIN TOLERANCE"
        logger.info("  Result: %s  (%d deviation(s) out of %d parameter(s))",
                    status, len(out_of_tol), len(deviations))

        if write_db and deviations:
            result = {
                "pkey"        : batch_pkey,
                "component_id": row.get("Component ID", ""),
                "group_name"  : row.get("Group", ""),
                "date"        : row.get("Date"),
                "shift"       : row.get("Shift", ""),
                "timestamp"   : row.get("Batch Time"),
                "deviations"  : deviations,
            }
            rows_written = write_prescription_alert(eng, result, fl_id, tolerance_pct,
                                                     customer_pkey=config.get("customer_pkey", 0))
            logger.info("  Alert written to DB: %s", "YES" if rows_written else "NO (duplicate or error)")

    logger.info(SEP)

if __name__ == "__main__":
    import argparse
    import json
    import sys
    from pathlib import Path

    _DEFAULT_CONFIG = Path(__file__).parent / "config" / "watchdog_config.json"

    parser = argparse.ArgumentParser(description="AI Watchdog -- Prescription Monitor")
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--check", action="store_true",
                        help="One-shot: fetch last batches, print deviation report, exit")
    parser.add_argument("--days",  type=int, default=1,
                        help="How many days back to check with --check (default 1)")
    parser.add_argument("--write", action="store_true",
                        help="With --check: also write alerts to the watchdog_alerts DB table")
    args   = parser.parse_args()

    cfg_path = args.config.resolve()
    if not cfg_path.exists():
        print(f"ERROR: config not found: {cfg_path}", file=sys.stderr)
        sys.exit(1)

    with open(cfg_path, encoding="utf-8") as f:
        config = json.load(f)

    logging.basicConfig(
        level   = logging.WARNING,   # suppress data-fetch noise in check mode
        format  = "%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    )

    if args.check:
        run_check(config, days=args.days, write_db=args.write)
    else:
        logging.getLogger().setLevel(logging.INFO)
        pw = PrescriptionWatchdog(config)
        pw.start()
