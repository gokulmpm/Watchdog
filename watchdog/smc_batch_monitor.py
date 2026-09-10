"""
smc_batch_monitor.py
---------------------
Per-batch SMC (prepared_sand_extra) LCL/UCL monitor.

For every new row that lands in `prepared_sand_extra`, checks each configured
parameter against its LCL/UCL from the DB (smc measure → properties.cpk_min/max).

Also tracks a rolling shift average so two views are always available:
  1. Individual batch value  vs LCL/UCL
  2. Current shift average   vs LCL/UCL

Sigma-based alerts (baseline mean ± Nσ) are a separate toggle
(smc_batch_watchdog.sigma_alerts_enabled, default True).

Config keys (under "smc_batch_watchdog" in the per-foundry config):
  enabled               -- true / false (default false)
  poll_interval_sec     -- seconds between watermark polls (default 30)
  sigma_alerts_enabled  -- include baseline σ alerts alongside LCL/UCL (default true)
  sigma_window_days     -- baseline lookback in days for σ computation (default 30)
  sigma_threshold       -- z-score threshold to flag (default 3.0)
  cooldown_sec          -- min seconds between alert emails (default 300)
  idle_timeout_min      -- stop after N idle minutes (0 = never, default 0)

Usage (embedded via foundry_si_monitor.py):
    from watchdog.smc_batch_monitor import SMCBatchMonitor
    monitor = SMCBatchMonitor(config, label="caspro_sandman_L1")
    threading.Thread(target=monitor.start, daemon=True).start()
"""

import logging
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy import text

logger = logging.getLogger(__name__)

_SAFE_COLS = frozenset([
    "co1", "co_final_percentage", "cosp_percent",
    "moisture_percentage", "temp_st1c", "total_seconds",
    "total_water", "wd1", "current",
])

# java_name (from properties) -> table column name in prepared_sand_extra
_JAVA_TO_COL = {
    "co1"                : "co1",
    "coFinalPercentage"  : "co_final_percentage",
    "cospPercentage"     : "cosp_percent",
    "moisturePercentage" : "moisture_percentage",
    "temp"               : "temp_st1c",
    "totalSeconds"       : "total_seconds",
    "totalWater"         : "total_water",
    "wd1"                : "wd1",
    "current"            : "current",
}

# Display-friendly names
_COL_DISPLAY = {
    "co1"                 : "CO1 (%)",
    "co_final_percentage" : "Compactability SMC (%)",
    "cosp_percent"        : "COSP (%)",
    "moisture_percentage" : "Moisture SMC (%)",
    "temp_st1c"           : "Temperature (°C)",
    "total_seconds"       : "Total Seconds (s)",
    "total_water"         : "Total Water (ltr)",
    "wd1"                 : "WD1 (ltr)",
    "current"             : "Current (A)",
}

def _camel_to_snake(name: str) -> str:
    import re
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()

def _load_limits(engine, foundry_line_id: int) -> dict:
    """
    Fetch LCL/UCL from DB: properties.cpk_min/cpk_max where measure.name='smc'.
    Returns {col_name: {"lcl": float|None, "ucl": float|None, "display": str}}.
    """
    limits = {}
    try:
        sql = text("""
            SELECT p.java_name, p.cpk_min, p.cpk_max
            FROM   properties p
            JOIN   measures   m ON p.measure_pkey = m.pkey
            WHERE  m.foundry_line_id = :fl
              AND  m.name = 'smc'
              AND  m.isActive = 1
              AND  p.is_active = 1
              AND  p.deleted = 0
        """)
        with engine.connect() as conn:
            rows = conn.execute(sql, {"fl": foundry_line_id}).mappings().all()

        for row in rows:
            java = str(row["java_name"] or "").strip()
            col  = _JAVA_TO_COL.get(java) or _camel_to_snake(java)
            lcl  = float(row["cpk_min"]) if row["cpk_min"] is not None else None
            ucl  = float(row["cpk_max"]) if row["cpk_max"] is not None else None
            if lcl is None and ucl is None:
                continue
            limits[col] = {
                "lcl"    : lcl,
                "ucl"    : ucl,
                "display": _COL_DISPLAY.get(col, col),
            }
        logger.info("SMCBatchMonitor: %d LCL/UCL limits loaded from smc measure", len(limits))
    except Exception as exc:
        logger.warning("SMCBatchMonitor: _load_limits failed: %s", exc)
    return limits

def _load_baseline(engine, foundry_line_id: int, cols: list, days: int) -> dict:
    """
    Compute per-column mean and std over the last `days` days.
    Returns {col: {"mean": float, "std": float}}.
    """
    if not cols:
        return {}
    col_list = ", ".join(f"`{c}`" for c in cols if c in _SAFE_COLS)
    if not col_list:
        return {}
    try:
        sql = text(f"""
            SELECT {col_list}
            FROM   `prepared_sand_extra`
            WHERE  foundry_line_id = :fl
              AND  deleted = 0
              AND  `date` >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
        """)
        with engine.connect() as conn:
            rows = conn.execute(sql, {"fl": foundry_line_id, "days": days}).fetchall()
        if not rows:
            return {}
        df = pd.DataFrame(rows, columns=cols)
        baseline = {}
        for col in cols:
            series = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(series) < 10:
                continue
            baseline[col] = {"mean": float(series.mean()), "std": float(series.std())}
        logger.info("SMCBatchMonitor: baseline computed from %d rows over last %d days", len(rows), days)
        return baseline
    except Exception as exc:
        logger.warning("SMCBatchMonitor: _load_baseline failed: %s", exc)
        return {}

def _check_row(row: dict, limits: dict, baseline: dict,
               sigma_enabled: bool, sigma_thr: float) -> list[dict]:
    """
    Check one batch row. Returns list of breach dicts:
      {col, display, value, lcl, ucl, status, z_score, sigma_breach}
    """
    breaches = []
    for col, lim in limits.items():
        val = row.get(col)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue

        lcl = lim["lcl"]
        ucl = lim["ucl"]
        display = lim["display"]

        # LCL/UCL check
        if lcl is not None and val < lcl:
            status = f"BELOW LCL (LCL={lcl})"
        elif ucl is not None and val > ucl:
            status = f"ABOVE UCL (UCL={ucl})"
        else:
            status = "OK"

        # Sigma check (optional)
        z_score = None
        sigma_breach = False
        if sigma_enabled and col in baseline:
            mean = baseline[col]["mean"]
            std  = baseline[col]["std"]
            if std and std > 0:
                z_score = abs(val - mean) / std
                sigma_breach = z_score > sigma_thr

        if status != "OK" or sigma_breach:
            breaches.append({
                "col"         : col,
                "display"     : display,
                "value"       : round(val, 3),
                "lcl"         : lcl,
                "ucl"         : ucl,
                "status"      : status,
                "z_score"     : round(z_score, 2) if z_score is not None else None,
                "sigma_breach": sigma_breach,
            })
    return breaches

def _check_shift_avg(avg_row: dict, limits: dict, baseline: dict,
                     sigma_enabled: bool, sigma_thr: float,
                     batch_count: int) -> list[dict]:
    """Same as _check_row but for shift average. Tags each breach with batch_count."""
    breaches = _check_row(avg_row, limits, baseline, sigma_enabled, sigma_thr)
    for b in breaches:
        b["batch_count"] = batch_count
        b["is_shift_avg"] = True
    return breaches

class SMCBatchMonitor:
    """
    Watermark-driven per-batch monitor for prepared_sand_extra.

    Polls the table for new rows (by max pkey), checks each row against
    LCL/UCL from the smc measure, and also tracks a rolling shift average.
    """

    def __init__(self, config: dict, label: str = "smc_batch") -> None:
        self._config    = config
        self._label     = label
        self._max_pkey  = 0          # watermark — last processed pkey
        self._limits    : dict = {}  # {col: {lcl, ucl, display}}
        self._baseline  : dict = {}  # {col: {mean, std}}
        self._shift_acc : dict = {}  # accumulator: {col: [values]} for current shift
        self._shift_key : str  = ""  # "YYYY-MM-DD|S1"
        self._last_email: float = 0  # epoch of last alert email

    def start(self) -> None:
        """Run forever — call from a daemon thread."""
        cfg = self._config.get("smc_batch_watchdog", {})
        poll     = int(cfg.get("poll_interval_sec",   30))
        idle_min = int(cfg.get("idle_timeout_min",    0))
        last_new = time.time()

        while True:
            try:
                self._boot()
                break  # init succeeded
            except Exception as _init_exc:
                logger.warning("[%s] startup init failed: %s — retrying in 30s", self._label, _init_exc)
                time.sleep(30)

        while True:
            try:
                n = self._poll_cycle()
                if n > 0:
                    last_new = time.time()
                elif idle_min and (time.time() - last_new) > idle_min * 60:
                    logger.info("[%s]  Idle timeout (%d min) — stopping", self._label, idle_min)
                    return
            except Exception:
                logger.warning("[%s]  poll_cycle error:\n%s", self._label, traceback.format_exc())
            time.sleep(poll)

    def _boot(self) -> None:
        """Load limits, baseline, and set watermark to current max pkey."""
        from .pipeline.db_connector import get_engine
        engine = get_engine(self._config)
        fl_id  = int(self._config.get("foundry_line_id", 1))
        cfg    = self._config.get("smc_batch_watchdog", {})

        self._limits   = _load_limits(engine, fl_id)
        sigma_days     = int(cfg.get("sigma_window_days", 30))
        sigma_enabled  = bool(cfg.get("sigma_alerts_enabled", True))
        if sigma_enabled and self._limits:
            self._baseline = _load_baseline(engine, fl_id, list(self._limits.keys()), sigma_days)

        # Watermark: start from latest pkey so we only alert on NEW rows
        try:
            with engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT MAX(pkey) AS mp FROM `prepared_sand_extra` WHERE foundry_line_id = :fl"
                ), {"fl": fl_id}).mappings().first()
            self._max_pkey = int(row["mp"] or 0) if row else 0
        except Exception as exc:
            logger.warning("[%s]  watermark init failed: %s", self._label, exc)

        logger.info(
            "[%s]  SMCBatchMonitor ready | limits=%d | baseline=%d | watermark=%d | sigma=%s",
            self._label, len(self._limits), len(self._baseline),
            self._max_pkey, sigma_enabled,
        )

    def _poll_cycle(self) -> int:
        """Fetch new rows, check each, update shift accumulator. Returns count of new rows."""
        from .pipeline.db_connector import get_engine
        engine = get_engine(self._config)
        fl_id  = int(self._config.get("foundry_line_id", 1))
        cfg    = self._config.get("smc_batch_watchdog", {})

        if not self._limits:
            return 0  # nothing configured

        sigma_enabled = bool(cfg.get("sigma_alerts_enabled", True))
        sigma_thr     = float(cfg.get("sigma_threshold", 3.0))
        cooldown      = float(cfg.get("cooldown_sec", 300))

        # Build column select list (only monitored cols that exist in table)
        monitored_cols = list(self._limits.keys())
        safe_monitored = [c for c in monitored_cols if c in _SAFE_COLS]
        if not safe_monitored:
            return 0

        col_sql = ", ".join(f"`{c}`" for c in safe_monitored)
        sql = text(f"""
            SELECT pkey, `date`, `time`, shift, {col_sql}
            FROM   `prepared_sand_extra`
            WHERE  foundry_line_id = :fl
              AND  deleted = 0
              AND  pkey > :wm
            ORDER BY pkey ASC
            LIMIT  200
        """)

        try:
            with engine.connect() as conn:
                rows = conn.execute(sql, {"fl": fl_id, "wm": self._max_pkey}).mappings().all()
        except Exception as exc:
            logger.warning("[%s]  fetch failed: %s", self._label, exc)
            return 0

        if not rows:
            return 0

        batch_breaches  : list[dict] = []  # individual row breaches
        shift_breaches  : list[dict] = []  # shift-avg breaches (emitted at shift end)

        for row in rows:
            row = dict(row)
            pkey  = int(row["pkey"])
            dt    = row.get("date")
            shift = str(row.get("shift") or "?")
            date_str = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
            shift_key = f"{date_str}|{shift}"

            # -- Detect shift boundary → emit shift-avg alert for previous shift
            if self._shift_key and shift_key != self._shift_key:
                s_avg_breaches = self._emit_shift_avg(sigma_enabled, sigma_thr)
                shift_breaches.extend(s_avg_breaches)
                self._shift_acc = {}

            self._shift_key = shift_key

            # Accumulate for shift average
            for col in safe_monitored:
                val = row.get(col)
                if val is not None:
                    try:
                        self._shift_acc.setdefault(col, []).append(float(val))
                    except (TypeError, ValueError):
                        pass

            # Per-batch LCL/UCL + sigma check
            row_breaches = _check_row(row, self._limits, self._baseline, sigma_enabled, sigma_thr)
            for b in row_breaches:
                b.update({
                    "pkey"     : pkey,
                    "date"     : date_str,
                    "shift"    : shift,
                    "time"     : str(row.get("time") or ""),
                    "is_shift_avg": False,
                    "batch_count" : None,
                })
            batch_breaches.extend(row_breaches)

            self._max_pkey = max(self._max_pkey, pkey)

        all_breaches = batch_breaches + shift_breaches
        if all_breaches and (time.time() - self._last_email) >= cooldown:
            self._send_alert(all_breaches, batch_breaches, shift_breaches)
            self._last_email = time.time()
        elif all_breaches:
            logger.info("[%s]  %d breach(es) suppressed (cooldown)", self._label, len(all_breaches))

        logger.debug("[%s]  Processed %d new PSE rows | breaches=%d",
                     self._label, len(rows), len(all_breaches))
        return len(rows)

    def _emit_shift_avg(self, sigma_enabled: bool, sigma_thr: float) -> list[dict]:
        """Compute shift average from accumulator, check against limits, return breaches."""
        if not self._shift_acc:
            return []
        avg_row = {col: float(np.mean(vals)) for col, vals in self._shift_acc.items() if vals}
        batch_count = max(len(v) for v in self._shift_acc.values()) if self._shift_acc else 0
        parts = self._shift_key.split("|") if self._shift_key else ["?", "?"]
        date_str = parts[0]
        shift    = parts[1] if len(parts) > 1 else "?"

        breaches = _check_shift_avg(avg_row, self._limits, self._baseline,
                                    sigma_enabled, sigma_thr, batch_count)
        for b in breaches:
            b.update({"date": date_str, "shift": shift, "pkey": None, "time": None})
        if breaches:
            logger.info("[%s]  Shift avg breaches — %s shift %s: %d param(s)",
                        self._label, date_str, shift, len(breaches))
        return breaches

    def _send_alert(self, all_breaches: list, batch_breaches: list, shift_breaches: list) -> None:
        """Log breach summary, fire email and webhook."""
        lcl_ucl = [b for b in all_breaches if b["status"] != "OK"]
        sigma   = [b for b in all_breaches if b.get("sigma_breach")]
        logger.warning(
            "[%s]  SMC alert | LCL/UCL breaches=%d | sigma breaches=%d",
            self._label, len(lcl_ucl), len(sigma),
        )
        try:
            from .email_notifier import send_smc_batch_email
            send_smc_batch_email(
                batch_breaches = batch_breaches,
                shift_breaches = shift_breaches,
                config         = self._config,
                label          = self._label,
            )
        except Exception:
            logger.warning("[%s]  SMC email failed:\n%s", self._label, traceback.format_exc())

        wh_cfg = self._config.get("webhook", {})
        if wh_cfg.get("enabled") and wh_cfg.get("send_smc_batch", False):
            try:
                from .webhook_notifier import send_smc_batch_webhook
                send_smc_batch_webhook(
                    batch_breaches = batch_breaches,
                    shift_breaches = shift_breaches,
                    config         = self._config,
                    label          = self._label,
                )
            except Exception:
                logger.warning("[%s]  SMC webhook failed:\n%s", self._label, traceback.format_exc())
