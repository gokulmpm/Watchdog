"""
bad_batch_monitor.py
---------------------
Bad Batch alert monitor.

Fires a BAD_BATCH alert for every additive batch where the absolute
difference between SMC-discharge compactability and COSP exceeds the
configured threshold.

  |compactability_smc_pct - cosp_percentage_pct| > threshold  =>  BAD_BATCH

Config keys (under "bad_batch_watchdog" in watchdog_config.json):
  enabled             -- true / false (default false)
  threshold           -- abs-difference trigger value (default 2.0)
  poll_interval_sec   -- seconds between polls (default 30)
  idle_timeout_min    -- stop after N idle minutes  (0 = never, default 0)
  smc_col             -- additive column for SMC discharge compactability
                         (default "compactability_smc_pct")
  cosp_col            -- additive column for COSP
                         (default "cosp_percentage_pct")

Usage
-----
  Embedded (from foundry_si_monitor.py):
      from watchdog.bad_batch_monitor import BadBatchMonitor
      monitor = BadBatchMonitor(config, label="caspro_sandman_L1")
      thread  = threading.Thread(target=monitor.start, daemon=True)
      thread.start()
"""

import json
import logging
import re
import time
import traceback
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ── Severity thresholds from smc_badbatch_config ─────────────────────────────

def _load_badbatch_thresholds(engine, foundry_line_id: int, config: dict = None) -> Optional[dict]:
    """
    Fetch per-foundry severity thresholds from smc_badbatch_config.
    Returns None if the table is unavailable or has no row for this line.

    Structure returned:
      {
        "upper_raw": {lowUpperMin, modUpperMin, criticUpperMin},
        "lower_raw": {lowLowerMin, lowLowerMax, modLowerMin, modLowerMax, criticLowerMax},
        "min_trigger": float  -- lowest absolute threshold across all ranges
      }
    """
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""SELECT critical_config_json, moderate_config_json, low_config_json
                        FROM smc_badbatch_config
                        WHERE foundry_line_pkey = :fl AND deleted = 0
                        ORDER BY pkey DESC LIMIT 1"""),
                {"fl": int(foundry_line_id)},
            ).mappings().first()
        if not row:
            return None

        def _j(col):
            raw = row[col]
            return json.loads(raw) if isinstance(raw, str) else (raw or {})

        crit = _j("critical_config_json")
        mod  = _j("moderate_config_json")
        low  = _j("low_config_json")

        def _f(d, key, default):
            v = d.get(key)
            return float(v) if v is not None else float(default)

        thr = {
            "upper_raw": {
                "lowUpperMin"   : _f(low,  "lowUpperMin",     3.0),
                "modUpperMin"   : _f(mod,  "modUpperMin",     4.0),
                "criticUpperMin": _f(crit, "criticUpperMin",  5.0),
            },
            "lower_raw": {
                "lowLowerMin"   : _f(low,  "lowLowerMin",     -3.0),
                "lowLowerMax"   : _f(low,  "lowLowerMax",     -1.0),
                "modLowerMin"   : _f(mod,  "modLowerMin",     -5.0),
                "modLowerMax"   : _f(mod,  "modLowerMax",     -3.0),
                "criticLowerMax": _f(crit, "criticLowerMax",  -7.0),  # more negative than modLowerMin
            },
        }
        # Minimum absolute value that triggers any severity
        thr["min_trigger"] = min(
            thr["upper_raw"]["lowUpperMin"],
            abs(thr["lower_raw"]["lowLowerMax"]),
        )

        # Fetch SMC valid range from properties table (set via SandMan UI config)
        smc_min, smc_max = 0.0, 100.0
        try:
            import re as _re
            smc_col = config.get("bad_batch_watchdog", {}).get("smc_col", "compactability_smc_pct") \
                      if config else "compactability_smc_pct"
            parts = smc_col.split("_")
            camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
            candidates = list({smc_col, camel, camel[0].upper() + camel[1:]})

            from sqlalchemy import text as _text
            with engine.connect() as conn:
                prop_row = conn.execute(_text("""
                    SELECT p.cpk_min, p.cpk_max
                    FROM   properties p
                    JOIN   measures   m ON p.measure_pkey = m.pkey
                    WHERE  m.foundry_line_id = :fl
                      AND  p.java_name IN :names
                      AND  p.deleted = 0 AND p.is_active = 1
                      AND  m.isActive = 1
                      AND  (p.cpk_min IS NOT NULL OR p.cpk_max IS NOT NULL)
                    ORDER BY p.cpk_min IS NULL ASC, p.pkey DESC
                    LIMIT 1
                """), {"fl": int(foundry_line_id), "names": tuple(candidates)}).mappings().first()
                if prop_row:
                    if prop_row["cpk_min"] is not None:
                        smc_min = float(prop_row["cpk_min"])
                    if prop_row["cpk_max"] is not None:
                        smc_max = float(prop_row["cpk_max"])
        except Exception:
            pass

        thr["smc_min"] = smc_min
        thr["smc_max"] = smc_max

        # Read detection_mode from DB config JSON if present
        try:
            raw_crit = row.get("critical_config_json") or "{}"
            db_meta  = json.loads(raw_crit) if isinstance(raw_crit, str) else (raw_crit or {})
            if "detection_mode" in db_meta:
                thr["detection_mode"] = str(db_meta["detection_mode"]).lower()
        except Exception:
            pass

        return thr
    except Exception as exc:
        logger.warning("_load_badbatch_thresholds failed (line=%d): %s", foundry_line_id, exc)
        return None


def _deviation_severity(dev: float, thr: dict) -> str:
    """
    Determine severity for a single signed deviation (SMC − COSP).
    Returns 'critical' | 'warning' | 'ok'.
    """
    u = thr["upper_raw"]
    l = thr["lower_raw"]
    if dev >= u["criticUpperMin"] or dev <= l["criticLowerMax"]:
        return "critical"
    if (u["modUpperMin"] <= dev < u["criticUpperMin"]) or \
       (l["modLowerMin"] < dev <= l["modLowerMax"]):
        return "critical"
    if (u["lowUpperMin"] <= dev < u["modUpperMin"]) or \
       (l["lowLowerMin"] < dev <= l["lowLowerMax"]):
        return "warning"
    return "ok"

# Only allow plain SQL identifiers (letters, digits, underscores, no backtick escapes needed).
_SAFE_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

_SMC_DEFAULT  = "compactability_smc_pct"
_COSP_DEFAULT = "cosp_percentage_pct"


def _safe_col(name: str, default: str) -> str:
    """Return name if it is a safe SQL identifier, otherwise return default and warn."""
    if _SAFE_IDENTIFIER_RE.match(name):
        return name
    logger.warning("Unsafe column name %r rejected — using default %r", name, default)
    return default

def _fmt_comp_id(v) -> str:
    """Normalize component_id: pandas/DB may give float '51011090010.0' -> strip to '51011090010'."""
    if v is None:
        return ""
    try:
        f = float(v)
        if f != f:  # NaN
            return ""
        return str(int(f))
    except (TypeError, ValueError):
        return str(v).strip()


class BadBatchMonitor:
    """Poll additive batches and alert when SMC discharge vs COSP difference exceeds threshold."""

    def __init__(self, config: dict, label: str = "bad_batch") -> None:
        self._config             = config
        self._label              = label
        self._last_batch_pkey    = 0
        self._current_component  = ""   # currently running component_id
        self._current_shift      = ""   # shift currently being processed
        self._current_date       = ""   # date currently being processed

    # -- Public ---------------------------------------------------------------

    def start(self) -> None:
        """Run forever -- call from a daemon thread."""
        bb_cfg       = self._config.get("bad_batch_watchdog", {})
        poll_sec     = int(bb_cfg.get("poll_interval_sec", 30))
        idle_timeout = int(bb_cfg.get("idle_timeout_min",  0))

        logger.info("[%s]  Bad-batch monitor starting  (poll=%ds)", self._label, poll_sec)

        self._last_batch_pkey   = self._fetch_max_pkey()
        self._current_component = self._fetch_current_component()
        logger.info("[%s]  Starting from batch pkey=%d  component=%s",
                    self._label, self._last_batch_pkey, self._current_component or "(none)")

        last_new_batch_time: Optional[datetime] = datetime.now()

        while True:
            try:
                # Re-read all config values each cycle — picks up live DB updates
                bb_cfg         = self._config.get("bad_batch_watchdog", {})
                detection_mode = str(bb_cfg.get("detection_mode", "db")).lower()
                if detection_mode not in ("db", "percentage"):
                    detection_mode = "db"
                smc_col  = _safe_col(str(bb_cfg.get("smc_col",  _SMC_DEFAULT)),  _SMC_DEFAULT)
                cosp_col = _safe_col(str(bb_cfg.get("cosp_col", _COSP_DEFAULT)), _COSP_DEFAULT)

                if detection_mode == "percentage":
                    _missing = [k for k in ("pct_ok_thr", "pct_warn_thr", "pct_critical_thr")
                                if bb_cfg.get(k) is None]
                    if _missing:
                        logger.warning(
                            "[%s]  Skipping poll — missing thresholds in DB config: %s",
                            self._label, _missing,
                        )
                        time.sleep(poll_sec)
                        continue
                pct_ok_thr   = float(bb_cfg["pct_ok_thr"])       if detection_mode == "percentage" else 0.0
                pct_warn_thr = float(bb_cfg["pct_warn_thr"])      if detection_mode == "percentage" else 0.0
                pct_crit_thr = float(bb_cfg["pct_critical_thr"])  if detection_mode == "percentage" else 0.0

                new_alerts = self._poll(smc_col, cosp_col,
                                        detection_mode=detection_mode,
                                        pct_ok_thr=pct_ok_thr,
                                        pct_warn_thr=pct_warn_thr,
                                        pct_crit_thr=pct_crit_thr)

                if new_alerts > 0:
                    last_new_batch_time = datetime.now()
                    logger.info("[%s]  %d bad-batch alert(s) written", self._label, new_alerts)
                else:
                    if idle_timeout > 0 and last_new_batch_time is not None:
                        idle_secs = (datetime.now() - last_new_batch_time).total_seconds()
                        if idle_secs >= idle_timeout * 60:
                            logger.info(
                                "[%s]  Idle %.0f min -- stopping bad-batch monitor",
                                self._label, idle_secs / 60,
                            )
                            return

            except Exception:
                logger.error("[%s]  Poll error:\n%s", self._label, traceback.format_exc())

            time.sleep(poll_sec)

    # -- Private ---------------------------------------------------------------

    def _poll(self, smc_col: str, cosp_col: str,
              detection_mode: str = "db",
              pct_ok_thr: float = 1.0,
              pct_warn_thr: float = 3.0,
              pct_crit_thr: float = 5.0) -> int:
        from .pipeline.db_connector import get_engine
        from .alert_db_writer       import ensure_table, write_bad_batch_alert

        fl_id  = int(self._config.get("foundry_line_id", 1))
        engine = get_engine(self._config)
        ensure_table(engine)

        # ── Threshold resolution ───────────────────────────────────────────────
        # Two modes only — no watchdog absolute difference thresholds:
        #   "db"         → signed-difference bands from smc_badbatch_config (foundry DB)
        #   "percentage" → |SMC − COSP| / COSP × 100 vs watchdog config % thresholds
        bb_cfg = self._config.get("bad_batch_watchdog", {})

        if detection_mode == "percentage":
            thr = None
            logger.debug("[%s]  BB mode: %% band  ok<%.1f%%  warn<%.1f%%  crit>=%.1f%%",
                         self._label, pct_ok_thr, pct_warn_thr, pct_crit_thr)
        else:
            # DB signed-difference — always the default; no fallback to watchdog thresholds
            thr = _load_badbatch_thresholds(engine, fl_id, config=self._config)
            if thr is not None:
                logger.debug("[%s]  BB mode: DB signed-difference  min_trigger=%.2f  smc=[%.1f,%.1f]",
                             self._label, thr["min_trigger"], thr.get("smc_min", 0.0), thr.get("smc_max", 100.0))
            else:
                logger.warning("[%s]  No smc_badbatch_config row for foundry_line_id=%d — "
                               "skipping bad batch poll", self._label, fl_id)
                return 0

        smc_min = thr.get("smc_min", 0.0)   if thr else 0.0
        smc_max = thr.get("smc_max", 100.0) if thr else 100.0

        # Fetch ALL new batches to detect component changes
        batches = _fetch_new_batches(self._config, self._last_batch_pkey, smc_col, cosp_col)
        if batches.empty:
            return 0

        # Process every batch in strict pkey (time) order.
        # Collect critical results per component — send ONE email per component at end
        written = 0
        _crit_by_comp: dict = {}   # component_id -> list of critical results
        for _, row in batches.sort_values("pkey").iterrows():
            comp_id = _fmt_comp_id(row.get("component_id"))
            if comp_id and comp_id not in ("nan", "None"):
                if comp_id != self._current_component:
                    logger.info(
                        "[%s]  Component change: %s -> %s",
                        self._label,
                        self._current_component or "(none)",
                        comp_id,
                    )
                    self._current_component = comp_id

            smc_val  = _to_float(row.get("smc_value"))
            cosp_val = _to_float(row.get("cosp_value"))

            if smc_val is None or cosp_val is None:
                continue

            # Skip sensor errors outside the valid SMC range
            if not (smc_min <= smc_val <= smc_max):
                logger.debug(
                    "[%s]  Skipping sensor error: SMC=%.2f outside valid range [%.1f, %.1f]  batch=%s",
                    self._label, smc_val, smc_min, smc_max, int(row.get("pkey", 0)),
                )
                continue

            diff = round(smc_val - cosp_val, 3)

            # ── Classify severity ───────────────────────────────────────────────
            if detection_mode == "percentage":
                if cosp_val == 0:
                    continue
                dev_pct = abs(diff / cosp_val) * 100
                if dev_pct <= pct_ok_thr * 100:
                    continue
                elif dev_pct <= pct_warn_thr * 100:
                    severity = "warning"
                else:
                    severity = "critical"
            else:
                # DB mode — signed-diff bands from smc_badbatch_config
                severity = _deviation_severity(diff, thr)
                if severity == "ok":
                    continue

            result = {
                "batch_pkey"     : int(row["pkey"]),
                "component_id"   : comp_id,
                "group_name"     : str(row.get("group_name") or "").strip(),
                "date"           : row.get("date"),
                "shift"          : str(row.get("shift") or ""),
                "batch_time"     : row.get("batch_time"),
                "smc_value"      : smc_val,
                "cosp_value"     : cosp_val,
                "smc_cosp_diff"  : diff,
                "severity"       : severity,
                "detection_mode" : detection_mode,
                "pct_deviation"  : round(abs(diff / cosp_val * 100), 2) if cosp_val else None,
                # Band bounds (for display in email/webhook)
                "band_lower"     : round(cosp_val * (1 - pct_crit_thr), 3) if detection_mode == "percentage" and cosp_val else None,
                "band_upper"     : round(cosp_val * (1 + pct_crit_thr), 3) if detection_mode == "percentage" and cosp_val else None,
            }

            n = write_bad_batch_alert(engine, result, fl_id,
                                      customer_pkey=self._config.get("customer_pkey", 0))
            written += n

            if n > 0:
                from .webhook_notifier import _get_cfg, _severity_passes
                _wh_cfg = _get_cfg(self._config)
                _sev    = str(result.get("severity") or "").lower()
                # Collect warning/critical results — email sent ONCE per component after loop
                if _sev in ("warning", "critical") and _severity_passes(_wh_cfg, _sev) \
                        and _wh_cfg.get("send_bad_batch", False):
                    _cid = result.get("component_id") or ""
                    _crit_by_comp.setdefault(_cid, []).append(result)
                # Webhook — fire per batch, respects send_bad_batch + min_severity
                if _wh_cfg.get("enabled") and _wh_cfg.get("send_bad_batch", False):
                    try:
                        self._send_webhook(result)
                    except Exception as _whe:
                        logger.warning("[%s]  Bad-batch webhook failed: %s", self._label, _whe)

        # ── Send ONE summary email per component for all critical batches ───────
        # Instead of 10 emails for 10 batches, sends 1 email per component:
        #   "Component X had N critical bad batches in this poll cycle"
        if _crit_by_comp:
            try:
                from .email_notifier import send_bad_batch_email, check_and_send_combined
                for _cid, _results in _crit_by_comp.items():
                    # Use the worst (largest diff) result as the representative
                    _rep = max(_results, key=lambda r: abs(r.get("smc_cosp_diff") or 0))
                    _rep["_batch_count"] = len(_results)   # inject count for email template
                    send_bad_batch_email(_rep, self._config, label=self._label)
                    check_and_send_combined(
                        engine, self._config,
                        component_id    = _cid,
                        date_str        = str(_rep.get("date", "")),
                        shift           = str(_rep.get("shift", "")),
                        foundry_line_id = fl_id,
                        label           = self._label,
                    )
                    logger.info("[%s]  Email sent for %s — %d critical batch(es)",
                                self._label, _cid, len(_results))
            except Exception as _email_exc:
                logger.warning("[%s]  Bad-batch email failed: %s", self._label, _email_exc)

        # ── Shift boundary detection — emit summary when shift changes ────────
        # Use the last batch in this cycle to detect shift/date
        last_row = batches.sort_values("pkey").iloc[-1] if not batches.empty else None
        if last_row is not None:
            new_shift = str(last_row.get("shift") or "")
            new_date  = str(last_row.get("date")  or "")
            prev_shift = self._current_shift
            prev_date  = self._current_date

            if prev_shift and (new_shift != prev_shift or new_date != prev_date):
                # Shift boundary crossed — send summary for the completed shift
                logger.info("[%s]  Shift boundary: %s/%s → %s/%s — sending summary",
                            self._label, prev_date, prev_shift, new_date, new_shift)
                try:
                    self._send_shift_summary(engine, fl_id, prev_date, prev_shift)
                except Exception as _se:
                    logger.warning("[%s]  Shift summary email failed: %s", self._label, _se)

                # Day boundary — send end-of-day summary for the completed date
                if prev_date and new_date != prev_date:
                    logger.info("[%s]  Day boundary: %s → %s — sending daily summary",
                                self._label, prev_date, new_date)
                    try:
                        self._send_daily_summary(engine, fl_id, prev_date)
                    except Exception as _de:
                        logger.warning("[%s]  Daily summary email failed: %s", self._label, _de)

            self._current_shift = new_shift
            self._current_date  = new_date

        # Advance watermark only up to last fully-processed batch
        # (not max pkey) so null-value batches don't get permanently skipped
        processed_pkeys = batches["pkey"].dropna()
        if not processed_pkeys.empty:
            self._last_batch_pkey = int(processed_pkeys.max())
        return written

    def _send_shift_summary(self, engine, fl_id: int, date_str: str, shift: str) -> None:
        """Query bad batch counts for the completed shift and send a summary email."""
        from sqlalchemy import text as _text

        smc_col  = _safe_col(str(self._config.get("bad_batch_watchdog", {}).get("smc_col",  _SMC_DEFAULT)),  _SMC_DEFAULT)
        cosp_col = _safe_col(str(self._config.get("bad_batch_watchdog", {}).get("cosp_col", _COSP_DEFAULT)), _COSP_DEFAULT)

        try:
            with engine.connect() as conn:
                # Total batches with both SMC + COSP for this shift
                total_row = conn.execute(_text(f"""
                    SELECT COUNT(*) AS total
                    FROM   `additive`
                    WHERE  foundry_line_id = :fl
                      AND  deleted = 0
                      AND  `date` >= :dt AND `date` < DATE_ADD(:dt, INTERVAL 1 DAY)
                      AND  `shift`      = :sh
                      AND  `{smc_col}`  IS NOT NULL
                      AND  `{cosp_col}` IS NOT NULL
                """), {"fl": fl_id, "dt": date_str, "sh": shift}).mappings().first()

                # Bad batches recorded in watchdog_alerts for this shift
                bad_row = conn.execute(_text("""
                    SELECT COUNT(*) AS bad
                    FROM   `watchdog_alerts`
                    WHERE  foundry_line_id = :fl
                      AND  alert_type      = 'BAD_BATCH'
                      AND  `date`          = :dt
                      AND  `shift`         = :sh
                """), {"fl": fl_id, "dt": date_str, "sh": shift}).mappings().first()

            total = int(total_row["total"]) if total_row else 0
            bad   = int(bad_row["bad"])     if bad_row   else 0
            pct   = round(bad / total * 100, 1) if total > 0 else 0.0

        except Exception as exc:
            logger.warning("[%s]  _send_shift_summary query failed: %s", self._label, exc)
            return

        try:
            from .email_notifier import send_bad_batch_shift_summary
            send_bad_batch_shift_summary(
                config    = self._config,
                date_str  = date_str,
                shift     = shift,
                total     = total,
                bad       = bad,
                pct       = pct,
                label     = self._label,
            )
        except Exception as exc:
            logger.warning("[%s]  send_bad_batch_shift_summary failed: %s", self._label, exc)

    def _send_daily_summary(self, engine, fl_id: int, date_str: str) -> None:
        """Query bad batch counts for every shift on date_str and send a daily summary email."""
        if not self._config.get("bad_batch_watchdog", {}).get("enabled", False):
            logger.debug("[%s]  Daily summary skipped — bad batch monitoring not enabled", self._label)
            return

        from sqlalchemy import text as _text

        smc_col  = _safe_col(str(self._config.get("bad_batch_watchdog", {}).get("smc_col",  _SMC_DEFAULT)),  _SMC_DEFAULT)
        cosp_col = _safe_col(str(self._config.get("bad_batch_watchdog", {}).get("cosp_col", _COSP_DEFAULT)), _COSP_DEFAULT)

        try:
            with engine.connect() as conn:
                # Total batches per shift for the day
                total_rows = conn.execute(_text(f"""
                    SELECT `shift`, COUNT(*) AS total
                    FROM   `additive`
                    WHERE  foundry_line_id = :fl
                      AND  deleted = 0
                      AND  `date` >= :dt AND `date` < DATE_ADD(:dt, INTERVAL 1 DAY)
                      AND  `{smc_col}`  IS NOT NULL
                      AND  `{cosp_col}` IS NOT NULL
                    GROUP BY `shift`
                    ORDER BY `shift`
                """), {"fl": fl_id, "dt": date_str}).mappings().all()

                # Bad batches per shift from watchdog_alerts
                bad_rows = conn.execute(_text("""
                    SELECT `shift`, COUNT(*) AS bad
                    FROM   `watchdog_alerts`
                    WHERE  foundry_line_id = :fl
                      AND  alert_type      = 'BAD_BATCH'
                      AND  `date`          = :dt
                    GROUP BY `shift`
                    ORDER BY `shift`
                """), {"fl": fl_id, "dt": date_str}).mappings().all()

            bad_by_shift   = {r["shift"]: int(r["bad"])   for r in bad_rows}
            total_by_shift = {r["shift"]: int(r["total"]) for r in total_rows}
            shifts         = sorted(set(list(bad_by_shift.keys()) + list(total_by_shift.keys())))

            rows = []
            for sh in shifts:
                total = total_by_shift.get(sh, 0)
                bad   = bad_by_shift.get(sh, 0)
                pct   = round(bad / total * 100, 1) if total > 0 else 0.0
                rows.append({"shift": sh, "total": total, "bad": bad, "pct": pct})

        except Exception as exc:
            logger.warning("[%s]  _send_daily_summary query failed: %s", self._label, exc)
            return

        if not rows:
            logger.info("[%s]  Daily summary: no batch data for %s — skipping email", self._label, date_str)
            return

        try:
            from .email_notifier import send_bad_batch_daily_summary
            send_bad_batch_daily_summary(
                config   = self._config,
                date_str = date_str,
                rows     = rows,
                label    = self._label,
            )
        except Exception as exc:
            logger.warning("[%s]  send_bad_batch_daily_summary failed: %s", self._label, exc)

        try:
            from .webhook_notifier import send_bad_batch_daily_webhook
            send_bad_batch_daily_webhook(
                config   = self._config,
                date_str = date_str,
                rows     = rows,
                label    = self._label,
            )
        except Exception as exc:
            logger.warning("[%s]  send_bad_batch_daily_webhook failed: %s", self._label, exc)

    def _send_webhook(self, result: dict) -> None:
        """Push bad-batch alert to the external push-notification API."""
        from .webhook_notifier import _get_cfg, _post_alert_type, _post_alert, \
                                       _shift_name, _safe_round, _registered_lcl_ucl_types
        from datetime import datetime as _dt

        cfg = _get_cfg(self._config)
        if not cfg.get("enabled", False):
            return

        severity_raw = result.get("severity") or "warning"
        # Only send warning or critical — skip ok and unknown
        if severity_raw not in ("warning", "critical"):
            return
        from .webhook_notifier import _severity_passes
        if not _severity_passes(cfg, severity_raw):
            return

        severity    = severity_raw.capitalize()
        diff        = result.get("smc_cosp_diff", 0)
        direction   = "High" if diff >= 0 else "Low"
        comp        = result.get("component_id", "")
        smc         = _safe_round(result.get("smc_value"))
        cosp        = _safe_round(result.get("cosp_value"))
        shift       = _shift_name(cfg, result.get("shift", ""))
        foundry_key = str(self._config.get("customer_pkey", ""))
        line_pkey   = int(self._config.get("foundry_line_id", 1))
        period_key  = str(result.get("date", "")) + "-" + str(result.get("shift", ""))
        ts          = str(result.get("batch_time") or result.get("date") or _dt.now().isoformat(timespec="seconds"))

        alert_name  = f"{direction} SMC Discharge"
        type_key    = f"bb::smc_cosp::{alert_name}"

        # threshold_min/max = COSP ± configured threshold
        bb_threshold = float(self._config.get("bad_batch_watchdog", {}).get("threshold", 2.0))
        thr_min   = _safe_round(cosp - bb_threshold) if cosp is not None else None
        thr_max   = _safe_round(cosp + bb_threshold) if cosp is not None else None
        # threshold_percentage = % deviation of SMC from COSP
        thr_pct   = result.get("pct_deviation")  # already calculated in _poll as |(diff/cosp)*100|
        if thr_pct is not None and diff < 0:
            thr_pct = -round(float(thr_pct), 2)
        elif thr_pct is not None:
            thr_pct = round(float(thr_pct), 2)

        root_cause = (
            f"SMC discharge compactability has deviated from COSP. "
            f"SMC={smc}%  COSP={cosp}%  Difference={diff:+.2f}  "
            f"Component: {comp}"
        )

        # Register alert-type once per session
        if type_key not in _registered_lcl_ucl_types:
            type_payload = {
                "foundry_key"    : foundry_key,
                "line_pkey"      : line_pkey,
                "name"           : alert_name,
                "parameter_label": "SMC Discharge",
                "category"       : "Additive",
                "severity"       : severity,
                "threshold_min"  : thr_min,
                "threshold_max"  : thr_max,
                "threshold_unit" : "%",
            }
            if _post_alert_type(cfg, type_payload, self._label):
                _registered_lcl_ucl_types.add(type_key)

        alert_payload = {
            "alert_id"            : f"ALT-BB-{period_key}-{int(result.get('batch_pkey',0))}",
            "foundry_key"         : foundry_key,
            "process"             : "Additive",
            "parameter"           : "SMC Discharge",
            "parameter_label"     : alert_name,
            "actual_value"        : _safe_round(diff),
            "threshold_min"       : thr_min,
            "threshold_max"       : thr_max,
            "threshold_percentage": thr_pct,
            "unit"                : "%",
            "severity"            : severity,
            "shift"               : shift,
            "line_pkey"           : line_pkey,
            "root_cause"          : root_cause,
            "recommendation"      : "Check mixer settings, water addition, and clay dosing for this component.",
            "triggered_at"        : str(ts)[:19],
        }
        _post_alert(cfg, alert_payload, self._label)

    def _fetch_max_pkey(self) -> int:
        from .pipeline.db_connector import get_engine
        from sqlalchemy import text

        fl_id = self._config.get("foundry_line_id", 1)
        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT COALESCE(MAX(`pkey`), 0) AS max_id "
                         "FROM `additive` WHERE `foundry_line_id` = :fl_id AND `deleted` = 0"),
                    {"fl_id": fl_id},
                ).mappings().first()
            return int(row["max_id"]) if row else 0
        except Exception as exc:
            logger.warning("[%s]  _fetch_max_pkey failed: %s", self._label, exc)
            return 0

    def _fetch_current_component(self) -> str:
        """Return the component_id of the most recent additive batch."""
        from .pipeline.db_connector import get_engine
        from sqlalchemy import text

        fl_id = self._config.get("foundry_line_id", 1)
        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT `component_id` FROM `additive` "
                         "WHERE `foundry_line_id` = :fl_id AND `deleted` = 0 "
                         "  AND `component_id` IS NOT NULL AND `component_id` != '' "
                         "ORDER BY `pkey` DESC LIMIT 1"),
                    {"fl_id": fl_id},
                ).mappings().first()
            return _fmt_comp_id(row["component_id"]) if row else ""
        except Exception as exc:
            logger.warning("[%s]  _fetch_current_component failed: %s", self._label, exc)
            return ""


# -- Module helpers ------------------------------------------------------------

def _fetch_new_batches(config: dict, last_pkey: int,
                       smc_col: str, cosp_col: str) -> pd.DataFrame:
    """
    Return additive rows newer than last_pkey that have non-null SMC and COSP
    columns, joined with group info.
    """
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    fl_id    = int(config.get("foundry_line_id", 1))
    smc_col  = _safe_col(smc_col,  _SMC_DEFAULT)
    cosp_col = _safe_col(cosp_col, _COSP_DEFAULT)

    sql = text(f"""
        SELECT a.pkey,
               a.component_id,
               DATE(a.date)           AS date,
               a.shift,
               a.timestamp            AS batch_time,
               a.`{smc_col}`          AS smc_value,
               a.`{cosp_col}`         AS cosp_value,
               g.name                 AS group_name
        FROM   `additive` a
        LEFT JOIN `foundry_line_group_component` gc
               ON gc.component_id = a.component_id AND gc.deleted = 0
        LEFT JOIN `foundry_line_group` g
               ON g.pkey = gc.foundry_line_group_pkey
              AND g.deleted = 0
              AND g.foundry_line_pkey = :fl_id
        WHERE  a.foundry_line_id = :fl_id
          AND  a.deleted = 0
          AND  a.pkey    > :last_pkey
          AND  a.`{smc_col}`  IS NOT NULL
          AND  a.`{cosp_col}` IS NOT NULL
        ORDER  BY a.pkey ASC
    """)

    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"fl_id": fl_id, "last_pkey": last_pkey})
        if not df.empty and "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        # Rename generic columns back to the actual column names for the caller
        if "smc_value" in df.columns:
            df[smc_col]  = df["smc_value"]
        if "cosp_value" in df.columns:
            df[cosp_col] = df["cosp_value"]
        return df
    except Exception as exc:
        logger.warning("_fetch_new_batches (bad_batch) failed: %s", exc)
        return pd.DataFrame()


def _to_float(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None   # NaN guard
    except (TypeError, ValueError):
        return None


# -- One-shot check ------------------------------------------------------------

def run_check(config: dict, write_db: bool = False,
              last_n: int = 20) -> None:
    """
    Fetch the last `last_n` batches that have both SMC and COSP readings
    for the most-recent component, evaluate each against the threshold,
    and print a per-batch report.  Optionally writes bad-batch alerts to DB.
    """
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text
    import pandas as pd

    bb_cfg    = config.get("bad_batch_watchdog", {})
    threshold = float(bb_cfg.get("threshold", 2.0))
    smc_col   = _safe_col(str(bb_cfg.get("smc_col",  _SMC_DEFAULT)),  _SMC_DEFAULT)
    cosp_col  = _safe_col(str(bb_cfg.get("cosp_col", _COSP_DEFAULT)), _COSP_DEFAULT)

    fl_id  = int(config.get("foundry_line_id", 1))
    engine = get_engine(config)

    # Find the most-recent component
    latest_sql = text("""
        SELECT `component_id`
        FROM   `additive`
        WHERE  `foundry_line_id` = :fl_id AND `deleted` = 0
          AND  `component_id` IS NOT NULL AND `component_id` != ''
        ORDER  BY `pkey` DESC
        LIMIT  1
    """)
    with engine.connect() as conn:
        row = conn.execute(latest_sql, {"fl_id": fl_id}).mappings().first()

    if not row:
        print("No additive batches found for this foundry line.")
        return

    last_comp = _fmt_comp_id(row["component_id"])

    # Fetch last_n batches for that component with SMC + COSP readings
    sql = text(f"""
        SELECT a.pkey, a.component_id, DATE(a.date) AS date, a.shift,
               a.timestamp AS batch_time,
               a.`{smc_col}`  AS smc_value,
               a.`{cosp_col}` AS cosp_value,
               g.name AS group_name
        FROM   `additive` a
        LEFT JOIN `foundry_line_group_component` gc
               ON gc.component_id = a.component_id AND gc.deleted = 0
        LEFT JOIN `foundry_line_group` g
               ON g.pkey = gc.foundry_line_group_pkey
              AND g.deleted = 0 AND g.foundry_line_pkey = :fl_id
        WHERE  a.foundry_line_id  = :fl_id
          AND  a.deleted          = 0
          AND  a.component_id     = :comp
          AND  a.`{smc_col}`      IS NOT NULL
          AND  a.`{cosp_col}`     IS NOT NULL
        ORDER  BY a.pkey DESC
        LIMIT  :n
    """)

    with engine.connect() as conn:
        df = pd.read_sql(sql, conn,
                         params={"fl_id": fl_id, "comp": last_comp, "n": last_n})

    if df.empty:
        print(f"\nNo batches with both '{smc_col}' and '{cosp_col}' found "
              f"for component {last_comp}.")
        return

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.iloc[::-1].reset_index(drop=True)   # oldest -> newest

    SEP  = "=" * 80
    SEP2 = "-" * 80
    print()
    print(SEP)
    print(f"  BAD BATCH CHECK  —  Component: {last_comp}")
    print(f"  Threshold: ±{threshold}   SMC col: {smc_col}   COSP col: {cosp_col}")
    print(SEP)
    print(f"  {'#':<5}  {'Date':<12}  {'Shift':<6}  {'Batch':>8}  "
          f"{'SMC':>7}  {'COSP':>7}  {'Diff':>8}  Status")
    print(SEP2)

    bad_batches = 0
    if write_db:
        from .alert_db_writer import ensure_table, write_bad_batch_alert
        ensure_table(engine)

    for i, row in df.iterrows():
        smc_val  = _to_float(row.get("smc_value"))
        cosp_val = _to_float(row.get("cosp_value"))
        if smc_val is None or cosp_val is None:
            continue
        diff    = round(smc_val - cosp_val, 3)
        is_bad  = abs(diff) > threshold
        flag    = "  *** BAD BATCH ***" if is_bad else ""
        sign    = "+" if diff >= 0 else ""

        print(f"  {i+1:<5}  {str(row.get('date','')):<12}  "
              f"{str(row.get('shift','')):<6}  {int(row.get('pkey',0)):>8}  "
              f"{smc_val:>7.2f}  {cosp_val:>7.2f}  {sign}{diff:>7.2f}  "
              f"{'BAD' if is_bad else 'OK'}{flag}")

        if is_bad:
            bad_batches += 1
            if write_db:
                result = {
                    "batch_pkey"   : int(row["pkey"]),
                    "component_id" : last_comp,
                    "group_name"   : str(row.get("group_name") or ""),
                    "date"         : row.get("date"),
                    "shift"        : str(row.get("shift") or ""),
                    "batch_time"   : row.get("batch_time"),
                    "smc_value"    : smc_val,
                    "cosp_value"   : cosp_val,
                    "smc_cosp_diff": diff,
                    "threshold"    : threshold,
                }
                write_bad_batch_alert(engine, result, fl_id,
                                      customer_pkey=config.get("customer_pkey", 0))

    print(SEP2)
    status = f"BAD BATCHES FOUND: {bad_batches}" if bad_batches else "ALL BATCHES OK"
    print(f"  Result: {status}  (checked {len(df)} batches for component {last_comp})")
    if write_db and bad_batches:
        print(f"  {bad_batches} bad-batch alert(s) written to DB.")
    print(SEP)
    print()
