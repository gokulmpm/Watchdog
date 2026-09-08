"""
watchdog/prediction_monitor.py
--------------------------------
Monitors whether analytics_report predictions have been generated for
every active foundry_line_group in the current shift.

Logic:
  1. Reads shift timings from customer_foundry_info for this foundry.
  2. Reads all active groups from foundry_line_group for the line.
  3. Polls every poll_interval_sec.
  4. For each shift that has started + grace_minutes (default 30 min):
       — checks analytics_report for a row per (date, shift, group_name)
       — if missing, fires email + data-flow webhook alert
  5. Each (date, shift, group) alert is fired ONCE per day.
     The alerted set resets at midnight automatically.

Config keys (under "prediction_monitor"):
  enabled            -- true / false  (default false)
  grace_minutes      -- minutes after shift start before alerting (default 30)
  poll_interval_sec  -- seconds between checks (default 300)
"""

import logging
import time
import traceback
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


class PredictionMonitor:
    """Poll analytics_report and alert when a group prediction is missing."""

    def __init__(self, config: dict, label: str = "prediction") -> None:
        self._config = config
        self._label  = label

        # {(shift_date, shift_id, group_name)} — already alerted this calendar day
        self._alerted:      set  = set()
        self._alerted_date: date = date.today()

    # ── Entry point ───────────────────────────────────────────────────────────

    def start(self) -> None:
        pm_cfg       = self._config.get("prediction_monitor", {})
        poll_sec     = int(pm_cfg.get("poll_interval_sec", 300))
        grace_min    = int(pm_cfg.get("grace_minutes",     30))

        logger.info(
            "[%s]  Prediction monitor starting  (poll=%ds  grace=%dmin)",
            self._label, poll_sec, grace_min,
        )

        while True:
            try:
                self._poll(grace_min)
            except Exception:
                logger.error("[%s]  Poll error:\n%s", self._label, traceback.format_exc())
            time.sleep(poll_sec)

    # ── Core poll ─────────────────────────────────────────────────────────────

    def _poll(self, grace_min: int) -> None:
        # Reset alerted set at midnight
        today = date.today()
        if today != self._alerted_date:
            self._alerted      = set()
            self._alerted_date = today

        shift_defs = self._get_shift_timings()
        if not shift_defs:
            logger.debug("[%s]  No shift timings found — skipping", self._label)
            return

        groups = self._get_active_groups()
        if not groups:
            logger.debug("[%s]  No active groups found — skipping", self._label)
            return

        now = datetime.now()

        for sd in shift_defs:
            shift_id    = sd["shift"]
            shift_start = sd["start_dt"]   # datetime for today (or yesterday for night shift)
            shift_date  = sd["shift_date"] # date the shift belongs to

            # Only alert for the currently running shift:
            # skip if shift hasn't started + grace period yet
            if now < shift_start + timedelta(minutes=grace_min):
                continue
            # skip if shift has already ended
            shift_end = sd.get("end_dt")
            if shift_end and now >= shift_end:
                continue

            for group_name in groups:
                key = (shift_date, shift_id, group_name)
                if key in self._alerted:
                    continue

                if not self._prediction_exists(shift_date, shift_id, group_name):
                    self._alerted.add(key)
                    logger.warning(
                        "[%s]  Missing prediction: date=%s  shift=%s  group=%s",
                        self._label, shift_date, shift_id, group_name,
                    )
                    self._fire_alert(shift_date, shift_id, group_name, grace_min)

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _get_shift_timings(self) -> list[dict]:
        """
        Parse shift timings from customer_foundry_info for this customer.
        Returns list of dicts with shift id, start_dt, end_dt, shift_date.
        """
        from .pipeline.db_connector import get_engine
        customer_pkey = int(self._config.get("customer_pkey", 0))
        if not customer_pkey:
            return []

        sql = text("""
            SELECT shift, shift_timings
            FROM   customer_foundry_info
            WHERE  customer_pkey = :cpkey AND deleted = 0
            ORDER  BY pkey DESC LIMIT 1
        """)
        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                row = conn.execute(sql, {"cpkey": customer_pkey}).mappings().first()
            if not row or not row["shift"] or not row["shift_timings"]:
                return []

            shifts   = [s.strip() for s in str(row["shift"]).split(",")]
            timings  = [t.strip() for t in str(row["shift_timings"]).split(",")]
            if len(shifts) != len(timings):
                logger.warning("[%s]  shift/shift_timings length mismatch", self._label)
                return []

            result = []
            now    = datetime.now()
            today  = now.date()

            for shift_id, timing in zip(shifts, timings):
                if "-" not in timing:
                    continue
                start_str, end_str = timing.split("-", 1)
                try:
                    sh, sm, ss = [int(x) for x in start_str.strip().split(":")]
                    eh, em, es = [int(x) for x in end_str.strip().split(":")]
                except ValueError:
                    continue

                start_today = datetime(today.year, today.month, today.day, sh, sm, ss)
                end_today   = datetime(today.year, today.month, today.day, eh, em, es)

                # Night shift: end time is 00:00 (midnight) — crosses day boundary
                crosses_midnight = (eh == 0 and em == 0 and es == 0)

                if crosses_midnight:
                    # Shift started yesterday
                    start_dt   = start_today - timedelta(days=1)
                    end_dt     = datetime(today.year, today.month, today.day, 0, 0, 0)
                    shift_date = (today - timedelta(days=1))
                    # Also check today's occurrence (started today)
                    # We'll add both if shift started today is in the future
                    # For now: use whichever is more recent and has passed grace
                    if now >= start_today + timedelta(minutes=1):
                        # Night shift started today
                        start_dt   = start_today
                        end_dt     = datetime(today.year, today.month, today.day, 0, 0, 0) + timedelta(days=1)
                        shift_date = today
                    result.append({
                        "shift"      : shift_id,
                        "start_dt"   : start_dt,
                        "end_dt"     : end_dt,
                        "shift_date" : shift_date,
                    })
                else:
                    result.append({
                        "shift"      : shift_id,
                        "start_dt"   : start_today,
                        "end_dt"     : end_today if eh > 0 else None,
                        "shift_date" : today,
                    })

            return result

        except Exception as exc:
            logger.warning("[%s]  _get_shift_timings failed: %s", self._label, exc)
            return []

    def _get_active_groups(self) -> list[str]:
        """
        Return names of all visible foundry_line_groups for this line.
        Only groups with hide_for_non_admin = 0 are monitored —
        hidden groups are internal/admin-only and do not have regular predictions.
        """
        from .pipeline.db_connector import get_engine
        fl_id = int(self._config.get("foundry_line_id", 1))
        sql = text("""
            SELECT name
            FROM   foundry_line_group
            WHERE  foundry_line_pkey = :fl_id
              AND  deleted = 0
              AND  hide_for_non_admin = 0
            ORDER  BY pkey
        """)
        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                rows = conn.execute(sql, {"fl_id": fl_id}).fetchall()
            # Only named groups — skip NULL/empty (no-group) batches
            groups = [str(r[0]) for r in rows if r[0]]
            return groups
        except Exception as exc:
            logger.warning("[%s]  _get_active_groups failed: %s", self._label, exc)
            return []

    def _prediction_exists(self, shift_date: date, shift_id: str,
                            group_name: Optional[str]) -> bool:
        """Check if analytics_report has a row for (date, shift, group, line)."""
        from .pipeline.db_connector import get_engine
        fl_id = int(self._config.get("foundry_line_id", 1))

        if group_name is None:
            sql = text("""
                SELECT 1 FROM analytics_report
                WHERE  foundry_line_pkey = :fl_id
                  AND  DATE(`date`) = :dt
                  AND  shift = :sh
                  AND  (foundry_line_group_name IS NULL OR foundry_line_group_name = '')
                  AND  deleted = 0
                LIMIT  1
            """)
        else:
            sql = text("""
                SELECT 1 FROM analytics_report
                WHERE  foundry_line_pkey = :fl_id
                  AND  DATE(`date`) = :dt
                  AND  shift = :sh
                  AND  foundry_line_group_name = :grp
                  AND  deleted = 0
                LIMIT  1
            """)
        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                params = {"fl_id": fl_id, "dt": str(shift_date), "sh": shift_id}
                if group_name is not None:
                    params["grp"] = group_name
                row = conn.execute(sql, params).fetchone()
            return row is not None
        except Exception as exc:
            logger.warning("[%s]  _prediction_exists failed: %s", self._label, exc)
            return True  # fail-safe: don't alert if DB is unreachable

    # ── Alert firing ──────────────────────────────────────────────────────────

    def _fire_alert(self, shift_date: date, shift_id: str,
                    group_name: Optional[str], grace_min: int) -> None:
        group_display = group_name or "Default (no group)"
        shift_names   = {
            "1": "Morning", "2": "Afternoon", "3": "Night",
            "A": "Morning", "B": "Afternoon", "C": "Night",
        }
        shift_label = shift_names.get(str(shift_id), f"Shift {shift_id}")

        subject = (
            f"[SandMan] Prediction Missing — {shift_label} {shift_date}  |  {group_display}"
        )
        _foundry_line = ""
        try:
            from watchdog.email_notifier import _get_foundry_line_name
            _foundry_line = _get_foundry_line_name(self._config)
        except Exception:
            pass

        _customer = (
            self._config.get("notifications", {})
                        .get("email", {})
                        .get("dashboard_user", "")
        )

        body = (
            f"We would like to bring to your notice that the following event has occurred,\n\n"
            + (f"Customer  : {_customer}\n" if _customer else "")
            + (f"Foundry   : {_foundry_line}\n" if _foundry_line else "")
            + f"\nNo Sandmix prescription has been generated for:\n\n"
            f"  Date      : {shift_date}\n"
            f"  Shift     : {shift_label}\n"
            f"  Group     : {group_display}\n\n"
            f"The prescription was expected within {grace_min} minutes of shift start "
            f"but has not been received yet.\n\n"
            f"Possible causes:\n"
            f"  - Required input data (previous shift sand properties) is missing\n\n"
            f"Until the prescription arrives, operators will not have dosing targets "
            f"for this shift. Please check the Sandmix prediction service.\n\n"
            f"@Sandman Team"
        )

        self._send_email(subject, body)
        self._send_webhook(shift_date, shift_id, shift_label, group_display, grace_min)

    def _send_email(self, subject: str, body: str) -> None:
        try:
            from watchdog.email_notifier import _send_plain, _get_email_cfg
            email_cfg = _get_email_cfg(self._config)
            if not email_cfg.get("enabled", False):
                return
            _send_plain(email_cfg, subject, body, alert_type="PREDICTION_MISSING")
            logger.info("[%s]  Prediction missing email sent: %s", self._label, subject)
        except Exception as exc:
            logger.warning("[%s]  Email failed: %s", self._label, exc)

    def _send_webhook(self, shift_date: date, shift_id: str,
                      shift_label: str, group_display: str, grace_min: int) -> None:
        try:
            from watchdog.webhook_notifier import (
                _get_cfg, _post_alert_type, _post_alert,
                _registered_lcl_ucl_types, _severity_passes,
            )
            cfg = _get_cfg(self._config)
            if not cfg.get("enabled", False):
                return
            if not cfg.get("send_prediction", True):
                return
            if not _severity_passes(cfg, "Warning"):
                return

            fl_id       = int(self._config.get("foundry_line_id", 1))
            foundry_key = str(self._config.get("customer_pkey", ""))
            alert_name  = f"Missing Prediction — {group_display}"
            type_key    = f"prediction::{group_display}"

            type_payload = {
                "foundry_key"    : foundry_key,
                "line_pkey"      : fl_id,
                "name"           : alert_name,
                "parameter_label": "AI Prescription Prediction",
                "category"       : "Analytics",
                "severity"       : "Warning",
                "threshold_min"  : None,
                "threshold_max"  : None,
                "threshold_unit" : "",
            }
            if type_key not in _registered_lcl_ucl_types:
                if _post_alert_type(cfg, type_payload, self._label):
                    _registered_lcl_ucl_types.add(type_key)

            alert_payload = {
                "alert_id"            : f"ALT-PRED-{shift_date}-{shift_id}-{group_display[:20]}",
                "foundry_key"         : foundry_key,
                "process"             : "Analytics",
                "parameter"           : "AI Prescription Prediction",
                "parameter_label"     : alert_name,
                "actual_value"        : 0,
                "threshold_min"       : None,
                "threshold_max"       : None,
                "threshold_percentage": None,
                "unit"                : "",
                "severity"            : "Warning",
                "shift"               : shift_label,
                "line_pkey"           : fl_id,
                "root_cause"          : (
                    f"No prediction found in analytics_report for "
                    f"{group_display} on {shift_date} {shift_label}. "
                    f"Expected within {grace_min} minutes of shift start."
                ),
                "recommendation"      : (
                    "Check the AI prediction service. Verify that the previous shift's "
                    "sand properties are available as input for the model."
                ),
                "triggered_at"        : datetime.now().isoformat(timespec="seconds"),
            }
            _post_alert(cfg, alert_payload, self._label)
            logger.info("[%s]  Prediction missing webhook sent: %s", self._label, alert_name)
        except Exception as exc:
            logger.warning("[%s]  Webhook failed: %s", self._label, exc)
