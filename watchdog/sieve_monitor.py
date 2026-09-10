"""
sieve_monitor.py
-----------------
Sieve % Change Monitor.

Fires a SIEVE_CHANGE alert whenever a new sieve measurement appears in the
``sieves`` table and any band's value has changed by more than the configured
% threshold compared to the previous reading for the same (sand_type, band_type).

All sand types are monitored together (PS=1, NS=2, CS=3, RS=0).
Band coverage is limited to bands listed in config["sv_params"] when set;
defaults to all available bands.

Config keys (under "sieve_watchdog"):
  enabled             -- true / false (default false)
  pct_change_warning  -- % change threshold per band (default 5.0)
  poll_interval_sec   -- seconds between polls (default 60)
  idle_timeout_min    -- stop after N idle minutes  (0 = never, default 0)

Usage
-----
  Embedded (from foundry_si_monitor.py):
      from watchdog.sieve_monitor import SieveChangeMonitor
      monitor = SieveChangeMonitor(config, label="foundry_L1/sieve")
      threading.Thread(target=monitor.start, daemon=True).start()
"""

import json
import logging
import time
import traceback
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_SAND_LABEL = {0: "Return Sand", 1: "Prepared Sand", 2: "New Sand", 3: "Core Sand"}
_SAND_SHORT  = {0: "RS",         1: "PS",            2: "NS",       3: "CS"}


class SieveChangeMonitor:
    """Poll the sieves table and alert on significant % change in any sieve band."""

    def __init__(self, config: dict, label: str = "sieve") -> None:
        self._config          = config
        self._label           = label
        self._last_sieve_pkey = 0
        # {(sand_type_int, band_type_str): float} — last known value per band
        self._prev_bands: dict = {}

    # -- Public ---------------------------------------------------------------

    def start(self) -> None:
        """Run forever -- call from a daemon thread."""
        sw_cfg          = self._config.get("sieve_watchdog", {})
        poll_sec        = int(sw_cfg.get("poll_interval_sec",  60))
        idle_timeout    = int(sw_cfg.get("idle_timeout_min",   0))
        threshold       = float(sw_cfg.get("pct_change_warning", 5.0))
        ok_thr          = float(sw_cfg.get("ok_thr",       1.0))
        warn_thr        = float(sw_cfg.get("warn_thr",     3.0))
        critical_thr    = float(sw_cfg.get("critical_thr", 5.0))
        band_thresholds = {str(k): float(v)
                           for k, v in sw_cfg.get("band_thresholds", {}).items()}

        logger.info(
            "[%s]  Sieve monitor starting  (poll=%ds  ok=%.1f%%  warn=%.1f%%  critical=%.1f%%)",
            self._label, poll_sec, ok_thr, warn_thr, critical_thr,
        )

        self._last_sieve_pkey = self._fetch_max_pkey()
        # Seed prev_bands from the most recent entry PER SAND TYPE so that
        # infrequent sand types (NS, CS updated monthly) get a valid baseline
        # even when the latest sieve pkey is a daily PS/RS-only entry.
        if self._last_sieve_pkey > 0:
            self._prev_bands = self._fetch_latest_bands_per_sand_type()
        logger.info("[%s]  Starting from sieve pkey=%d  (%d bands seeded)",
                    self._label, self._last_sieve_pkey, len(self._prev_bands))

        # Diagnostic: log configured bands vs bands actually found in DB
        band_filter = self._config.get("sv_params") or None
        db_bands    = sorted({k[1] for k in self._prev_bands}) if self._prev_bands else []
        if band_filter:
            _fl = {b.lower() for b in band_filter}
            matched   = [b for b in db_bands if b.lower() in _fl]
            unmatched = [b for b in band_filter if b.lower() not in {d.lower() for d in db_bands}]
            logger.info(
                "[%s]  Configured sv_params: %s  |  DB bands found: %s  |  "
                "Matched: %s  |  NOT matched (check spelling): %s",
                self._label, band_filter, db_bands, matched, unmatched,
            )
        else:
            logger.info("[%s]  No sv_params filter — monitoring all bands: %s",
                        self._label, db_bands)

        last_new_time: Optional[datetime] = datetime.now()

        while True:
            try:
                new_alerts = self._poll(threshold, band_thresholds,
                                        ok_thr=ok_thr, warn_thr=warn_thr,
                                        critical_thr=critical_thr)

                if new_alerts > 0:
                    last_new_time = datetime.now()
                    logger.info("[%s]  %d sieve change alert(s) written",
                                self._label, new_alerts)
                else:
                    if idle_timeout > 0 and last_new_time is not None:
                        idle_secs = (datetime.now() - last_new_time).total_seconds()
                        if idle_secs >= idle_timeout * 60:
                            logger.info(
                                "[%s]  Idle %.0f min -- stopping sieve monitor",
                                self._label, idle_secs / 60,
                            )
                            return

            except Exception:
                logger.error("[%s]  Poll error:\n%s",
                             self._label, traceback.format_exc())

            time.sleep(poll_sec)

    # -- Private ---------------------------------------------------------------

    def _poll(self, threshold: float, band_thresholds: dict = None,
              ok_thr: float = 1.0, warn_thr: float = 3.0,
              critical_thr: float = 5.0) -> int:
        from .pipeline.db_connector import get_engine
        from .alert_db_writer       import ensure_table, write_sieve_change_alert

        fl_id           = int(self._config.get("foundry_line_id", 1))
        band_thresholds = band_thresholds or {}
        engine          = get_engine(self._config)
        ensure_table(engine)

        band_filter = self._config.get("sv_params") or None

        new_entries = _fetch_new_sieve_entries(
            self._config, self._last_sieve_pkey, band_filter
        )
        if new_entries.empty:
            return 0

        written = 0

        # Process each new sieve pkey in chronological order
        for sieve_pkey, grp in new_entries.groupby("sieve_pkey", sort=True):
            sieve_pkey = int(sieve_pkey)

            # Average across wash types for each (sand_type, band_type)
            curr_bands = (
                grp.groupby(["sand_type", "band_type"])["value"]
                   .mean()
                   .dropna()
                   .to_dict()
            )

            changes = []
            for (sand_type, band_type), curr_val in curr_bands.items():
                prev_val = self._prev_bands.get((sand_type, band_type))
                if prev_val is None or prev_val == 0:
                    continue
                pct      = (curr_val - prev_val) / abs(prev_val) * 100.0
                abs_pct  = abs(pct)

                # Severity tier — 3-zone matching config UI (OK / WARNING / CRITICAL)
                # Use per-band override threshold if configured, else global thresholds
                band_ok   = band_thresholds.get(str(band_type), ok_thr)   if band_thresholds.get(str(band_type)) else ok_thr
                band_warn = band_thresholds.get(str(band_type), warn_thr) if band_thresholds.get(str(band_type)) else warn_thr
                if abs_pct <= band_ok:
                    continue          # OK — no alert
                elif abs_pct <= band_warn:
                    severity = "warning"
                else:
                    severity = "critical"  # above critical_thr (same as warn_thr in config)

                band_thr = band_thresholds.get(str(band_type), threshold)
                changes.append({
                    "sand_type" : int(sand_type),
                    "sand_label": _SAND_LABEL.get(int(sand_type), str(sand_type)),
                    "sand_short": _SAND_SHORT.get(int(sand_type), str(sand_type)),
                    "band"      : str(band_type),
                    "prev"      : round(float(prev_val), 4),
                    "curr"      : round(float(curr_val), 4),
                    "pct_change": round(pct, 2),
                    "threshold" : band_thr,
                    "severity"  : severity,
                })

            if changes:
                meta_row   = grp.iloc[0]
                _sev_rank  = {"warning": 1, "critical": 2}
                worst_sev  = max(changes, key=lambda c: _sev_rank.get(c.get("severity","warning"), 0))
                _sev_label = {"warning": "WARNING", "critical": "CRITICAL"}
                result = {
                    "sieve_pkey"    : sieve_pkey,
                    "date"          : meta_row.get("date"),
                    "shift"         : str(meta_row.get("shift") or ""),
                    "changes"       : changes,
                    "threshold"     : threshold,
                    "max_pct_change": max(abs(c["pct_change"]) for c in changes),
                    "alert_level"   : _sev_label.get(worst_sev.get("severity","warning"), "WARNING"),
                }
                n = write_sieve_change_alert(
                    engine, result, fl_id,
                    customer_pkey=self._config.get("customer_pkey", 0),
                )
                written += n
                if n > 0:
                    _wh_cfg = self._config.get("webhook", {})
                    if _wh_cfg.get("enabled") and _wh_cfg.get("send_sieve", False):
                        try:
                            self._send_webhook(result)
                        except Exception as _whe:
                            logger.warning("[%s]  Sieve webhook failed: %s", self._label, _whe)
                    try:
                        from .email_notifier import send_sieve_email
                        send_sieve_email(result, self._config, label=self._label)
                    except Exception as _eme:
                        logger.warning("[%s]  Sieve email failed: %s", self._label, _eme)

            # Always advance prev_bands so each reading is the baseline for the next
            self._prev_bands.update(curr_bands)

        self._last_sieve_pkey = int(new_entries["sieve_pkey"].max())
        return written

    def _send_webhook(self, result: dict) -> None:
        """Send one webhook POST per changed sieve band (Critical / Warning)."""
        from .webhook_notifier import (
            _get_cfg, _post_alert_type_batch, _post_alert,
            _shift_name, _safe_round, _severity_passes,
            _registered_lcl_ucl_types,
        )
        from datetime import datetime as _dt

        cfg         = _get_cfg(self._config)
        foundry_key = str(self._config.get("customer_pkey", ""))
        line_pkey   = int(self._config.get("foundry_line_id", 1))
        shift       = _shift_name(cfg, result.get("shift", ""))
        triggered   = str(result.get("date") or _dt.now().date()) + "T00:00:00"

        new_types  = []
        new_t_keys = []

        for ch in result.get("changes", []):
            severity_raw = str(ch.get("severity", "warning")).lower()
            severity     = severity_raw.capitalize()
            if not _severity_passes(cfg, severity):
                continue

            band       = str(ch.get("band", ""))
            sand_label = str(ch.get("sand_label", ""))
            curr       = ch.get("curr")
            prev       = ch.get("prev")
            pct        = ch.get("pct_change", 0.0)
            thr        = ch.get("threshold", 5.0)
            direction  = "High" if (pct or 0) > 0 else "Low"
            alert_name = f"{direction} {band} ({ch.get('sand_short','')})"

            # threshold_min/max = prev ± threshold%
            thr_min = _safe_round(float(prev) * (1 - thr / 100), 4) if prev else None
            thr_max = _safe_round(float(prev) * (1 + thr / 100), 4) if prev else None

            root_cause = (
                f"{band} ({sand_label}) changed by {pct:+.2f}% from previous reading "
                f"({prev:.4g} -> {curr:.4g})."
            )
            recommendation = (
                "Check fresh sand addition rate and supplier batch AFS certificate. "
                "Verify sieve equipment calibration."
                if "gfn" in band.lower() or "afs" in band.lower()
                else
                "Review sand system for changes in fines generation or fresh sand ratio."
            )

            type_key = f"sieve::{band}::{ch.get('sand_short','')}::{direction}"
            sand_short  = ch.get("sand_short", "")
            param_label = f"{band} ({sand_short})"

            type_payload = {
                "foundry_key"    : foundry_key,
                "line_pkey"      : line_pkey,
                "name"           : alert_name,
                "parameter_label": param_label,
                "category"       : "Prepared Sand",
                "severity"       : severity,
                "threshold_min"  : 0,
                "threshold_max"  : thr,
                "threshold_unit" : "%",
            }
            if type_key not in _registered_lcl_ucl_types:
                new_types.append(type_payload)
                new_t_keys.append(type_key)

            alert_payload = {
                "alert_id"            : f"ALT-SIEVE-{result.get('sieve_pkey','?')}-{band}-{sand_short}-L{line_pkey}",
                "foundry_key"         : foundry_key,
                "process"             : "Sand Analysis",
                "parameter"           : alert_name,
                "parameter_label"     : param_label,
                "actual_value"        : _safe_round(curr),
                "threshold_min"       : -thr,
                "threshold_max"       : thr,
                "threshold_percentage": round(float(pct), 2),
                "unit"                : "%",
                "severity"            : severity,
                "shift"               : str(result.get("shift", "")),
                "line_pkey"           : line_pkey,
                "root_cause"          : root_cause,
                "recommendation"      : recommendation,
                "triggered_at"        : triggered,
            }
            _post_alert(cfg, alert_payload, self._label,
                        _type_payload=type_payload, _type_key=type_key)

        if new_types:
            if _post_alert_type_batch(cfg, new_types, self._label):
                for k in new_t_keys:
                    _registered_lcl_ucl_types.add(k)

    def _fetch_max_pkey(self) -> int:
        from .pipeline.db_connector import get_engine
        from sqlalchemy import text

        fl_id = int(self._config.get("foundry_line_id", 1))
        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT COALESCE(MAX(`pkey`), 0) AS max_id FROM `sieves` WHERE `foundry_line_id` = :fl_id"),
                    {"fl_id": fl_id},
                ).mappings().first()
            return int(row["max_id"]) if row else 0
        except Exception as exc:
            logger.warning("[%s]  _fetch_max_pkey failed: %s", self._label, exc)
            return 0

    def _fetch_bands_for_pkey(self, sieve_pkey: int) -> dict:
        """Return {(sand_type, band_type): avg_value} for a specific sieve entry,
        restricted to the bands selected in sv_params (same filter as live polling)."""
        from .pipeline.db_connector import get_engine
        from sqlalchemy import text

        band_filter = self._config.get("sv_params") or None

        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT b.sand_type, b.band_type, AVG(b.value) AS avg_val
                        FROM   sieve_band_data b
                        WHERE  b.sieve_id = :sid
                          AND  b.value IS NOT NULL
                          AND  b.sand_type IN (0, 1, 2, 3)
                        GROUP  BY b.sand_type, b.band_type
                    """),
                    {"sid": sieve_pkey},
                ).fetchall()
            result = {(int(r[0]), str(r[1])): float(r[2]) for r in rows if r[2] is not None}
            # Apply band filter in Python so all sand types are seeded correctly
            if band_filter:
                _fl = {b.lower() for b in band_filter}
                result = {k: v for k, v in result.items() if k[1].lower() in _fl}
            return result
        except Exception as exc:
            logger.warning("[%s]  _fetch_bands_for_pkey failed: %s", self._label, exc)
            return {}

    def _fetch_latest_bands_per_sand_type(self) -> dict:
        """
        Return {(sand_type, band_type): avg_value} seeded from the most recent
        sieve entry FOR EACH sand type independently.

        This ensures infrequent sand types (NS, CS updated monthly) get a valid
        baseline on startup even when the latest sieve pkey is a PS/RS-only entry.
        """
        from .pipeline.db_connector import get_engine
        from sqlalchemy import text

        band_filter = self._config.get("sv_params") or None

        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT b.sand_type, b.band_type, AVG(b.value) AS avg_val
                    FROM   sieve_band_data b
                    INNER JOIN (
                        SELECT b2.sand_type, MAX(s2.pkey) AS latest_pkey
                        FROM   sieves s2
                        JOIN   sieve_band_data b2 ON b2.sieve_id = s2.pkey
                        WHERE  b2.value IS NOT NULL
                          AND  b2.sand_type IN (0, 1, 2, 3)
                        GROUP  BY b2.sand_type
                    ) latest ON latest.sand_type = b.sand_type
                           AND latest.latest_pkey = b.sieve_id
                    WHERE  b.value IS NOT NULL
                    GROUP  BY b.sand_type, b.band_type
                """)).fetchall()

            result = {(int(r[0]), str(r[1])): float(r[2]) for r in rows if r[2] is not None}
            if band_filter:
                _fl = {b.lower() for b in band_filter}
                result = {k: v for k, v in result.items() if k[1].lower() in _fl}

            # Log per-sand-type seeding so it's visible in the startup log
            by_sand = {}
            for (st, bt), v in result.items():
                by_sand.setdefault(st, []).append(f"{bt}={v:.4f}")
            for st, bands in sorted(by_sand.items()):
                label = {0: "RS", 1: "PS", 2: "NS", 3: "CS"}.get(st, str(st))
                logger.info("[%s]  Seeded %s: %s", self._label, label, ", ".join(bands))

            return result
        except Exception as exc:
            logger.warning("[%s]  _fetch_latest_bands_per_sand_type failed: %s", self._label, exc)
            # Fall back to seeding from the single latest pkey
            return self._fetch_bands_for_pkey(self._last_sieve_pkey)


# -- Module helpers ------------------------------------------------------------

def _fetch_new_sieve_entries(
    config: dict,
    last_pkey: int,
    band_filter: Optional[list],
) -> pd.DataFrame:
    """
    Return sieve band rows for sieve entries newer than last_pkey.
    Columns: sieve_pkey, date, shift, sand_type, band_type, value.

    band_filter is applied in Python (not SQL) to avoid named-param IN-clause
    binding issues across SQLAlchemy/pandas versions, and to guarantee all
    sand types (0=RS, 1=PS, 2=NS, 3=CS) are always included.
    """
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    sql = text("""
        SELECT s.pkey        AS sieve_pkey,
               DATE(s.date)  AS date,
               s.shift,
               b.sand_type,
               b.band_type,
               b.value
        FROM   sieves          s
        JOIN   sieve_band_data b ON b.sieve_id = s.pkey
        WHERE  s.pkey   > :last_pkey
          AND  b.value  IS NOT NULL
          AND  b.sand_type IN (0, 1, 2, 3)
        ORDER  BY s.pkey ASC, b.sand_type, b.band_type
    """)

    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"last_pkey": last_pkey})
        if not df.empty and "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        # Apply band filter in Python so all sand types are always represented
        if band_filter and not df.empty:
            _fl = {b.lower() for b in band_filter}
            df = df[df["band_type"].str.lower().isin(_fl)].copy()
        return df
    except Exception as exc:
        logger.warning("_fetch_new_sieve_entries failed: %s", exc)
        return pd.DataFrame()


# -- One-shot check ------------------------------------------------------------

def run_check(config: dict, write_db: bool = False,
              date_from: str = None, date_to: str = None) -> None:
    """
    For each sieve entry in [date_from, date_to], find the previous reading
    for each (sand_type, band_type) independently, compute % change, and alert
    if threshold exceeded.  No arbitrary row-count limit.

    Baseline is seeded from the last reading per sand type BEFORE date_from so
    the first entry in range always has a valid prev to compare against.
    """
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text
    from datetime import date as _date

    sw_cfg          = config.get("sieve_watchdog", {})
    threshold       = float(sw_cfg.get("pct_change_warning", 5.0))
    ok_thr          = float(sw_cfg.get("ok_thr",       1.0))
    warn_thr        = float(sw_cfg.get("warn_thr",     3.0))
    critical_thr    = float(sw_cfg.get("critical_thr", 5.0))
    band_thresholds = {str(k): float(v) for k, v in sw_cfg.get("band_thresholds", {}).items()}
    fl_id           = int(config.get("foundry_line_id", 1))
    band_filter     = config.get("sv_params") or None
    engine          = get_engine(config)

    if date_to is None:
        date_to = str(_date.today())
    if date_from is None:
        date_from = str(_date.today())

    # ── Seed prev_bands: last reading per (sand_type, band_type) before date_from ──
    seed_sql = text("""
        SELECT b.sand_type, b.band_type, AVG(b.value) AS avg_val
        FROM   sieve_band_data b
        INNER JOIN (
            SELECT b2.sand_type, MAX(s2.pkey) AS latest_pkey
            FROM   sieves s2
            JOIN   sieve_band_data b2 ON b2.sieve_id = s2.pkey
            WHERE  DATE(s2.date) < :d_from
              AND  b2.value IS NOT NULL
              AND  b2.sand_type IN (0, 1, 2, 3)
            GROUP  BY b2.sand_type
        ) latest ON latest.sand_type = b.sand_type
               AND latest.latest_pkey = b.sieve_id
        WHERE  b.value IS NOT NULL
        GROUP  BY b.sand_type, b.band_type
    """)

    sql = text("""
        SELECT s.pkey AS sieve_pkey, DATE(s.date) AS date, s.shift,
               b.sand_type, b.band_type, AVG(b.value) AS avg_val
        FROM   sieves s
        JOIN   sieve_band_data b ON b.sieve_id = s.pkey
        WHERE  DATE(s.date) BETWEEN :d_from AND :d_to
          AND  b.sand_type IN (0, 1, 2, 3)
          AND  b.value IS NOT NULL
        GROUP  BY s.pkey, b.sand_type, b.band_type
        ORDER  BY s.pkey ASC
    """)

    # Seed prev_bands: last reading per (sand_type, band_type) before date_from
    prev_bands: dict = {}
    with engine.connect() as conn:
        seed_rows = conn.execute(seed_sql, {"d_from": date_from}).fetchall()
    for r in seed_rows:
        prev_bands[(int(r[0]), str(r[1]))] = float(r[2])

    if band_filter:
        _fl = {b.lower() for b in band_filter}
        prev_bands = {k: v for k, v in prev_bands.items() if k[1].lower() in _fl}

    logger.info("Seeded %d band baselines from entries before %s", len(prev_bands), date_from)

    # Fetch all entries in the date range
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"d_from": date_from, "d_to": date_to})

    if band_filter and not df.empty:
        _fl = {b.lower() for b in band_filter}
        df  = df[df["band_type"].str.lower().isin(_fl)].copy()

    if df.empty:
        print("No sieve data found for this date range.")
        return

    df["date"] = pd.to_datetime(df["date"]).dt.date

    SEP  = "=" * 80
    SEP2 = "-" * 80
    band_label = ", ".join(band_filter) if band_filter else "all bands"
    thr_label  = (", ".join(f"{b}=±{t}%" for b, t in band_thresholds.items())
                  if band_thresholds else f"all bands ±{threshold}%")
    print()
    print(SEP)
    print(f"  SIEVE % CHANGE CHECK  |  {date_from} -> {date_to}  |  bands: {band_label}")
    print(f"  Thresholds: OK≤{ok_thr}%  WARN≤{warn_thr}%  CRIT≤{critical_thr}%  CRIT_HIGH>{critical_thr}%  |  overrides: {thr_label}")
    print(SEP)

    if write_db:
        from .alert_db_writer import ensure_table, write_sieve_change_alert
        ensure_table(engine)

    pkeys = sorted(df["sieve_pkey"].unique())
    for pkey in pkeys:
        grp = df[df["sieve_pkey"] == pkey]
        meta = grp.iloc[0]
        curr_bands = (
            grp.groupby(["sand_type", "band_type"])["avg_val"]
               .mean().dropna().to_dict()
        )
        print(f"\n  pkey={pkey}  date={meta.get('date', '?')}  shift={meta.get('shift', '?')}")
        print(SEP2)

        changes = []
        for (st, bt), curr in sorted(curr_bands.items()):
            prev     = prev_bands.get((st, bt))
            band_thr = band_thresholds.get(str(bt), threshold)

            if prev is None or prev == 0:
                print(f"    {_SAND_SHORT.get(int(st), str(st)):<4}  {str(bt):<20}  "
                      f"prev={'(no baseline)':>12}  curr={curr:>8.4f}  (skipped)")
                continue

            pct     = (curr - prev) / abs(prev) * 100.0
            abs_pct = abs(pct)

            if abs_pct <= ok_thr:
                severity = "OK"
            elif abs_pct <= warn_thr:
                severity = "WARNING"
            else:
                severity = "CRITICAL"

            flag = f"  *** {severity} ***" if severity != "OK" else ""
            print(f"    {_SAND_SHORT.get(int(st), str(st)):<4}  {str(bt):<20}  "
                  f"prev={prev:>8.4f}  curr={curr:>8.4f}  {pct:+.2f}%{flag}")

            if severity != "OK":
                changes.append({
                    "sand_type" : int(st),
                    "sand_label": _SAND_LABEL.get(int(st), str(st)),
                    "sand_short": _SAND_SHORT.get(int(st), str(st)),
                    "band"      : str(bt),
                    "prev"      : round(float(prev), 4),
                    "curr"      : round(float(curr), 4),
                    "pct_change": round(pct, 2),
                    "threshold" : band_thr,
                    "severity"  : severity.lower(),
                })

        if changes and write_db:
            _sev_rank = {"warning": 1, "critical": 2}
            worst = max(changes, key=lambda c: _sev_rank.get(c.get("severity", "warning"), 0))
            result = {
                "sieve_pkey"    : int(pkey),
                "date"          : meta.get("date"),
                "shift"         : str(meta.get("shift") or ""),
                "changes"       : changes,
                "threshold"     : threshold,
                "max_pct_change": max(abs(c["pct_change"]) for c in changes),
                "alert_level"   : worst.get("severity", "warning").upper(),
            }
            write_sieve_change_alert(engine, result, fl_id,
                                     customer_pkey=config.get("customer_pkey", 0))
            print(f"    -> {len(changes)} alert(s) written to DB")

        prev_bands.update(curr_bands)

    print()
    print(SEP)
    print()
