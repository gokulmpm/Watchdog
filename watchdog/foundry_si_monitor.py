"""
watchdog/foundry_si_monitor.py
--------------------------------
Thread-safe, class-based SI watchdog for a single (foundry_db, foundry_line_id).

Unlike scheduler.py (which uses a module-level _state dict and cannot run
multiple instances safely in one process), FoundrySIMonitor stores all state
as instance attributes -- so N instances can run concurrently in N threads.

Usage
-----
    monitor = FoundrySIMonitor(config=foundry_config, label="caspro_sandman_L1")
    thread  = threading.Thread(target=monitor.start, daemon=True)
    thread.start()
"""

import logging
import time
import traceback
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

_DB_REFRESH_MINUTES     = 10
_CONFIG_REFRESH_MINUTES = 30   # not used here -- config comes from registry, not a file


class FoundrySIMonitor:
    """Continuous SI watchdog for one (database, foundry_line_id) pair."""

    def __init__(self, config: dict, label: str) -> None:
        self._config          = config
        self._label           = label           # e.g. "caspro_sandman_L1"
        self._db_limits: dict = {}
        self._shifts:    dict = {}
        self._last_db_refresh: datetime | None = None

    # -- Public ---------------------------------------------------------------

    def start(self) -> None:
        """Run forever -- call this from a daemon thread."""
        import threading as _threading

        logger.info("[%s]  SI monitor starting", self._label)
        self._refresh_db_configs()

        # -- Ensure alert table exists before any sub-monitors start -----------
        try:
            from .alert_db_writer        import ensure_table
            from .pipeline.db_connector  import get_engine
            _eng = get_engine(self._config)
            ensure_table(_eng)
        except Exception as _te:
            logger.warning("[%s]  Table pre-creation failed (will retry): %s", self._label, _te)

        # -- Optional: Component-Change monitor --------------------------------
        cc_cfg   = self._config.get("component_change_watchdog", {})
        cc_on    = bool(cc_cfg.get("enabled", False))
        if cc_on:
            from .component_change_monitor import ComponentChangeMonitor
            _cc = ComponentChangeMonitor(self._config, label=f"{self._label}/comp_change")
            _threading.Thread(target=_cc.start, name=f"comp_change-{self._label}",
                              daemon=True).start()
            logger.info("[%s]  Component-change monitor started", self._label)
        else:
            logger.info("[%s]  Component-change monitor disabled "
                        "(set component_change_watchdog.enabled=true to enable)", self._label)

        # -- Optional: Bad-Batch monitor ---------------------------------------
        bb_cfg = self._config.get("bad_batch_watchdog", {})
        bb_on  = bool(bb_cfg.get("enabled", False))
        if bb_on:
            from .bad_batch_monitor import BadBatchMonitor
            _bb = BadBatchMonitor(self._config, label=f"{self._label}/bad_batch")
            _threading.Thread(target=_bb.start, name=f"bad_batch-{self._label}",
                              daemon=True).start()
            logger.info("[%s]  Bad-batch monitor started  (threshold=±%s)",
                        self._label,
                        bb_cfg.get("threshold", 2.0))
        else:
            logger.info("[%s]  Bad-batch monitor disabled "
                        "(set bad_batch_watchdog.enabled=true to enable)", self._label)

        # -- Optional: Sieve % Change monitor ----------------------------------
        sw_cfg = self._config.get("sieve_watchdog", {})
        sw_on  = bool(sw_cfg.get("enabled", False))
        if sw_on:
            from .sieve_monitor import SieveChangeMonitor
            _sw = SieveChangeMonitor(self._config, label=f"{self._label}/sieve")
            _threading.Thread(target=_sw.start, name=f"sieve-{self._label}",
                              daemon=True).start()
            logger.info("[%s]  Sieve monitor started  (threshold=±%s%%)",
                        self._label,
                        sw_cfg.get("pct_change_warning", 5.0))
        else:
            logger.info("[%s]  Sieve monitor disabled "
                        "(set sieve_watchdog.enabled=true to enable)", self._label)

        # -- Optional: Prescription Deviation monitor --------------------------
        pw_cfg = self._config.get("prescription_watchdog", {})
        pw_on  = bool(pw_cfg.get("enabled", False))
        if pw_on:
            from .prescription_watchdog import PrescriptionWatchdog
            _pw = PrescriptionWatchdog(self._config, label=f"{self._label}/presc")
            _threading.Thread(target=_pw.start, name=f"presc-{self._label}",
                              daemon=True).start()
            logger.info("[%s]  Prescription monitor started  (poll=%ss)",
                        self._label, pw_cfg.get("poll_interval_sec", 60))
        else:
            logger.info("[%s]  Prescription monitor disabled "
                        "(set prescription_watchdog.enabled=true to enable)", self._label)

        # -- Optional: SMC Batch (Mixer / prepared_sand_extra) monitor ---------
        sm_cfg = self._config.get("smc_batch_watchdog", {})
        sm_on  = bool(sm_cfg.get("enabled", False))
        if sm_on:
            from .smc_batch_monitor import SMCBatchMonitor
            _sm = SMCBatchMonitor(self._config, label=f"{self._label}/smc_batch")
            _threading.Thread(target=_sm.start, name=f"smc_batch-{self._label}",
                              daemon=True).start()
            logger.info(
                "[%s]  SMC Batch monitor started  (poll=%ss, sigma=%s)",
                self._label,
                sm_cfg.get("poll_interval_sec", 30),
                sm_cfg.get("sigma_alerts_enabled", True),
            )
        else:
            logger.info("[%s]  SMC Batch monitor disabled "
                        "(set smc_batch_watchdog.enabled=true to enable)", self._label)

        self._log_shifts()

        import threading as _thr

        # ── Only Window mode is active — sigma zone analysis ─────────────────
        # Shift and Component modes are disabled pending SI engine update.
        _thr.Thread(
            target=self._run_mode_loop,
            args=("window",),
            name=f"{self._label}/window",
            daemon=True,
        ).start()
        logger.info("[%s]  Window mode started (sigma zone alerts)", self._label)

        # Keep the main thread alive (sub-thread is a daemon thread).
        while True:
            time.sleep(60)

    # -- Private -- per-mode trigger loops ------------------------------------

    def _run_mode_loop(self, mode: str) -> None:
        """
        Dedicated trigger loop for a single mode. Runs forever in a daemon thread.

        shift     -- fires at shift end (time-driven)
        window    -- fires when trigger.window_n new preparedsand rows arrive
        component -- fires on any new additive or preparedsand data
        """
        import copy

        watermarks     = self._get_watermarks()
        window_counter = watermarks.get("preparedsand", 0)
        last_fire_time = None
        last_fired_shift: tuple | None = None

        logger.info("[%s/%s]  Trigger loop started", self._label, mode)

        while True:
            try:
                self._maybe_refresh_db_configs()

                cfg      = self._config
                now      = datetime.now()
                _trig    = cfg.get("trigger", {})
                cooldown = int(_trig.get("cooldown_seconds", 300))
                interval = int(_trig.get("check_interval_seconds", 60))

                mode_cfg = copy.deepcopy(cfg)
                mode_cfg.setdefault("aggregation", {})["mode"] = mode

                if mode == "shift":
                    shift_name, is_end = self._shift_for_now(now)
                    if is_end and shift_name:
                        fire_key = (now.date(), shift_name)
                        if fire_key != last_fired_shift:
                            logger.info("[%s/shift]  SHIFT END  >  shift=%s  date=%s",
                                        self._label, shift_name, now.date())
                            self._fire_with_config(mode_cfg, "shift",
                                                   trigger_date=now.date(), shift=shift_name)
                            last_fired_shift = fire_key

                elif mode == "window":
                    window_n  = int(_trig.get("window_n", 1))
                    cur_wm    = self._get_watermarks()
                    cur_count = cur_wm.get("preparedsand", window_counter)
                    new_rows  = cur_count - window_counter
                    if new_rows >= window_n and self._cooldown_passed(last_fire_time, cooldown):
                        logger.info("[%s/window]  TRIGGER  >  %d new preparedsand row(s)  "
                                    "(threshold=%d)", self._label, new_rows, window_n)
                        # Sigma-zone analysis on latest PS row(s)
                        self._fire_sigma_window(cfg)
                        window_counter = cur_count
                        last_fire_time = now
                    elif new_rows > 0:
                        window_counter = cur_count

                elif mode == "component":
                    cur_wm  = self._get_watermarks()
                    new_add = cur_wm.get("additive",     0) > watermarks.get("additive",     0)
                    new_ps  = cur_wm.get("preparedsand", 0) > watermarks.get("preparedsand", 0)
                    if (new_add or new_ps) and self._cooldown_passed(last_fire_time, cooldown):
                        shift_now, _ = self._shift_for_now(now)
                        logger.info("[%s/component]  TRIGGER  >  new data  shift=%s",
                                    self._label, shift_now)
                        self._fire_with_config(mode_cfg, "component",
                                               trigger_date=now.date(), shift=shift_now)
                        watermarks.update(cur_wm)
                        last_fire_time = now
                    elif new_add or new_ps:
                        watermarks.update(cur_wm)

            except Exception:
                logger.error("[%s/%s]  Loop error:\n%s", self._label, mode, traceback.format_exc())

            time.sleep(interval)

    # -- Private -- fire & alert -----------------------------------------------

    def _fire(
        self,
        mode:         str,
        trigger_date: date | None = None,
        shift:        str | None  = None,
    ) -> None:
        """Fire analysis for the configured aggregation mode.

        When config["dual_mode"] is True, both component mode AND the configured
        secondary mode (config["dual_mode_secondary"], default "shift") are run
        simultaneously and both alerts are written to the DB.
        """
        dual = self._config.get("dual_mode", False)
        if dual:
            self._fire_dual(mode, trigger_date=trigger_date, shift=shift)
        else:
            self._fire_with_config(self._config, mode, trigger_date, shift)

    def _fire_dual(
        self,
        primary_mode: str,
        trigger_date: date | None = None,
        shift:        str | None  = None,
    ) -> None:
        """Run component mode AND a secondary shift/date/window mode simultaneously."""
        import copy

        secondary = self._config.get("dual_mode_secondary", "shift")
        logger.info(
            "[%s]  DUAL MODE  >  component  +  %s",
            self._label, secondary,
        )

        # -- 1. Component mode analysis ----------------------------------------
        comp_cfg = copy.deepcopy(self._config)
        comp_cfg["aggregation"]["mode"] = "component"
        self._fire_with_config(comp_cfg, "component", trigger_date, shift)

        # -- 2. Secondary mode analysis (shift / day / window) -----------------
        sec_cfg = copy.deepcopy(self._config)
        sec_cfg["aggregation"]["mode"] = secondary
        self._fire_with_config(sec_cfg, secondary, trigger_date, shift)

    def _fire_with_config(
        self,
        config:       dict,
        mode:         str,
        trigger_date: date | None = None,
        shift:        str | None  = None,
    ) -> None:
        """
        All modes now use sigma-zone analysis instead of rolling SI engines.
        SI engine code is preserved but not called — pending future update.
        """
        # Delegate to sigma-zone analysis for all modes
        self._fire_sigma_window(config, trigger_date=trigger_date, shift=shift, mode=mode)
        return

        # ── LEGACY SI ENGINE CODE (disabled — kept for future reference) ───────
        from .alert_db_writer       import ensure_table, write_si_alert
        from .pipeline.db_connector import get_engine

        try:
            if mode == "component":
                from .run_alert_monitor import _run_component_pipeline
                result, _ = _run_component_pipeline(
                    config, self._db_limits, trigger_date, shift
                )
            else:
                from .run_watchdog import run_for_period
                result = run_for_period(
                    config        = config,
                    trigger_mode  = mode,
                    trigger_date  = trigger_date,
                    trigger_shift = shift,
                    db_limits     = self._db_limits,
                )

            if result:
                self._log_alert_summary(result)
                try:
                    engine = get_engine(config)
                    ensure_table(engine)

                    # -- SI-based alerts ---------------------------------------
                    alert_level = str(result.get("final_alert") or result.get("si_alert") or "STABLE").upper()
                    write_si_alert(
                        engine, result, config.get("foundry_line_id", 1),
                        display_names=config.get("display_names", {}),
                        customer_pkey=config.get("customer_pkey", 0),
                        pct_warn=float(config.get("pct_change_warning", 5.0)),
                    )
                    logger.info(
                        "[%s]  SI alert written  mode=%-10s  level=%s  score=%.1f",
                        self._label, mode, alert_level,
                        float(result.get("si_score_100") or 0),
                    )

                    # -- Email notification for CRITICAL / ALERT / WARNING ------
                    import re as _re
                    _al = _re.sub(r'[^A-Z]', '', alert_level)
                    if _al in ("CRITICAL", "ALERT", "WARNING"):
                        try:
                            from .email_notifier import send_si_alert_email

                            # Build per-parameter list from result fields
                            # (result has param_labels, param_si, drift_labels etc.
                            #  NOT a pre-built params_json — that only exists in DB)
                            _param_labels  = result.get("param_labels",  {})
                            _param_si      = result.get("param_si",      {})
                            _drift_labels  = result.get("drift_labels",  {})
                            _var_labels    = result.get("var_labels",    {})
                            _osc_labels    = result.get("osc_labels",    {})
                            _deviations    = result.get("deviations",    {})
                            _raw_values    = result.get("raw_values",    {})
                            _pct_changes   = result.get("pct_changes",   {})
                            _disp          = config.get("display_names", {})
                            _pct_warn      = float(config.get("pct_change_warning", 5.0))

                            _SI_WARNING = "!! WARNING"

                            def _bare(p):
                                for pfx in ("ps_","pse_","add_","con_","sv_"):
                                    if p.startswith(pfx):
                                        return p[len(pfx):]
                                return p

                            _params_list = []
                            for _p, _plv in _param_labels.items():
                                # Email: only prepared-sand parameters
                                if not _p.startswith("ps_"):
                                    continue
                                _dev = _deviations.get(_p, "")
                                _pct = _pct_changes.get(_p)
                                # Escalate if LCL/UCL breach or large pct change
                                if not _plv or _plv == "STABLE":
                                    if (isinstance(_dev, str) and _dev.startswith("Deviated")) or \
                                       (abs(float(_pct or 0)) > _pct_warn):
                                        _plv = _SI_WARNING
                                if not _plv or _plv.upper() in ("STABLE", "OK", ""):
                                    continue   # skip truly stable params
                                _bare_name = _bare(_p)
                                _lbl  = _disp.get(_bare_name) or _disp.get(_p) or _bare_name
                                _lcl_ucl = ""
                                if isinstance(_dev, str) and "LCL=" in _dev:
                                    import re as _re2
                                    m = _re2.search(r'LCL=([\d.]+)', _dev)
                                    if m: _lcl_ucl = f"LCL={m.group(1)}"
                                elif isinstance(_dev, str) and "UCL=" in _dev:
                                    import re as _re2
                                    m = _re2.search(r'UCL=([\d.]+)', _dev)
                                    if m: _lcl_ucl = f"UCL={m.group(1)}"
                                _params_list.append({
                                    "param"       : _p,
                                    "label"       : _lbl,
                                    "alert_level" : _plv,
                                    "si_score"    : _param_si.get(_p),
                                    "raw_value"   : _raw_values.get(_p),
                                    "pct_change"  : _pct,
                                    "drift_label" : _drift_labels.get(_p) or "STABLE",
                                    "var_label"   : _var_labels.get(_p)   or "STABLE",
                                    "osc_label"   : _osc_labels.get(_p)   or "STABLE",
                                    "deviation"   : _dev if isinstance(_dev, str) and _dev not in ("OK","") else "",
                                    "lcl_ucl"     : _lcl_ucl,
                                })
                            # Sort: highest si_score first
                            _params_list.sort(key=lambda x: -(x.get("si_score") or 0))

                            send_si_alert_email({
                                "period_key"    : str(result.get("period_key") or ""),
                                "date"          : str(result.get("date") or ""),
                                "shift"         : str(result.get("shift") or ""),
                                "si_score"      : float(result.get("si_score_100") or 0),
                                "alert_level"   : _al,
                                "root_cause"    : str(result.get("root_cause") or ""),
                                "recommendation": str(result.get("recommendation") or ""),
                                "params_json"   : _params_list,
                            }, config, label=self._label)
                        except Exception:
                            logger.warning("[%s]  SI email failed:\n%s",
                                           self._label, traceback.format_exc())

                    # -- Webhook push notification ----------------------------
                    # LCL/UCL breach alerts only (window mode)
                    # SI-based deviation alerts removed — SI score no longer used
                    _wh_cfg = config.get("webhook", {})
                    if not _wh_cfg.get("send_si", True):
                        logger.debug("[%s]  Prepared Sand webhook disabled by config", self._label)
                    elif mode == "window":
                        try:
                            from .webhook_notifier import send_lcl_ucl_alerts
                            n_sent = send_lcl_ucl_alerts(
                                result        = result,
                                config        = config,
                                db_limits     = self._db_limits or {},
                                display_names = config.get("display_names", {}),
                                label         = self._label,
                            )
                            if n_sent:
                                logger.info("[%s]  LCL/UCL alerts sent: %d", self._label, n_sent)
                        except Exception:
                            logger.warning("[%s]  LCL/UCL webhook failed:\n%s",
                                           self._label, traceback.format_exc())

                except Exception:
                    logger.error(
                        "[%s]  DB alert write failed (mode=%s):\n%s",
                        self._label, mode, traceback.format_exc(),
                    )
            else:
                logger.warning(
                    "[%s]  No result for mode=%s  date=%s  shift=%s",
                    self._label, mode, trigger_date, shift,
                )
        except Exception:
            logger.error("[%s]  _fire_with_config failed (mode=%s):\n%s",
                         self._label, mode, traceback.format_exc())

    def _fire_sigma_window(self, config: dict,
                           trigger_date=None, shift=None, mode="window") -> None:
        """
        Sigma-zone SI analysis for ALL modes (window / shift / component).
        Replaces rolling drift/variance/oscillation engines.

        For the latest preparedsand row(s), compute per-parameter z-scores
        against the July-2025 baseline (mean ± std) and classify each as:

          |z| ≤ 1σ  ->  OK / STABLE
          1σ < |z| ≤ 2σ  ->  WARNING
          2σ < |z| ≤ 3σ  ->  ALERT
          |z| > 3σ  ->  CRITICAL

        The overall alert level = worst parameter.
        Sends email + webhook for WARNING and above.
        """
        import numpy as _np
        import pandas as _pd
        import re as _re
        from .pipeline.db_connector import get_engine as _ge
        from .alert_db_writer import ensure_table, write_si_alert
        from sqlalchemy import text as _t

        try:
            engine      = _ge(config)
            ensure_table(engine)
            fl_id       = int(config.get("foundry_line_id", 1))
            fetch_days  = int(config.get("window_fetch_days", 14))
            ps_params   = config.get("parameters", {}).get("prepared_sand", [])
            disp        = config.get("display_names", {})

            # ── Sigma thresholds: 4-zone (STABLE / WATCH / WARNING / CRITICAL) ─────
            ok_sigma    = float(config.get("sigma_ok_thr",    1.0))  # ≤ ok_sigma  -> stable
            warn_sigma  = float(config.get("sigma_warn_thr",  2.0))  # ≤ warn_sigma -> watch
            alert_sigma = float(config.get("sigma_alert_thr", 3.0))  # ≤ alert_sigma -> warning; > -> critical
            delta_thr   = float(config.get("delta_warning_pct", 5.0))

            # ── Fetch baseline stats ───────────────────────────────────────────
            from datetime import date as _date, timedelta as _td
            bl_start = _pd.to_datetime(config["baseline"]["start_date"]).date()
            bl_end   = _pd.to_datetime(config["baseline"]["end_date"]).date()

            baseline_cfg = {**config}
            _si_params = (config.get("si_params", {}).get("params", [])
                          or config.get("parameters", {}).get("prepared_sand", []))
            _safe_bl = [c for c in _si_params if c.replace("_", "").isalnum()]
            baseline_cfg["parameters"] = {
                **config.get("parameters", {}),
                "prepared_sand"       : _safe_bl,
                "additive"            : config.get("add_params", []),
                "consumption"         : config.get("con_params",
                                            config.get("parameters", {}).get("consumption", [])),
                "prepared_sand_extra" : config.get("pse_params",
                                            config.get("parameters", {}).get("prepared_sand_extra", [])),
            }

            from .pipeline.data_fetcher import fetch_all as _fa
            from .pipeline.aggregator import build_dataset as _bd

            raw_bl = _fa(baseline_cfg, start_date=bl_start, end_date=bl_end)
            df_bl  = _bd(raw_bl, {**baseline_cfg, "aggregation": {**baseline_cfg.get("aggregation",{}), "mode":"shift"}})

            if df_bl.empty:
                logger.warning("[%s/sigma]  Baseline empty — cannot compute sigma zones", self._label)
                return

            # ── Fetch the TWO latest PS rows (no averaging) ──────────────────
            # latest = trigger row; prev = previous row for delta check
            from datetime import date as _date2
            from sqlalchemy import text as _sql_text
            today    = trigger_date or _date2.today()
            # Read checked parameters from si_params.params (set in Config UI)
            # Falls back to parameters.prepared_sand for backward compat
            ps_cols  = (config.get("si_params", {}).get("params", [])
                        or config.get("parameters", {}).get("prepared_sand", []))
            _safe    = [c for c in ps_cols if c.replace("_", "").isalnum()]
            _col_sql = ", ".join(f"`{c}`" for c in _safe) if _safe else "NULL"

            _q = f"""
                SELECT pkey, DATE(`date`) AS date, `shift`, {_col_sql}
                FROM   `preparedsand`
                WHERE  `foundry_line_id` = :fl AND `deleted` = 0
                ORDER  BY `pkey` DESC
                LIMIT  1
            """
            with engine.connect() as _conn:
                _rows = _conn.execute(_sql_text(_q), {"fl": fl_id}).mappings().fetchall()

            if not _rows:
                logger.warning("[%s/sigma]  No PS rows found — skipping", self._label)
                return

            # Map raw column names to ps_-prefixed names to match baseline
            def _to_ps(row_dict):
                out = {"date": row_dict.get("date"), "shift": row_dict.get("shift"),
                       "pkey": row_dict.get("pkey")}
                for c in _safe:
                    out[f"ps_{c}" if not c.startswith("ps_") else c] = row_dict.get(c)
                return out

            # Only use the new pkey row — no prev comparison, no carry-forward
            latest   = _to_ps(dict(_rows[0]))
            prev_row = None  # disabled: check only the new row as-is

            logger.info("[%s/sigma]  Trigger row pkey=%s  date=%s  shift=%s",
                        self._label, latest.get("pkey"), latest.get("date"), latest.get("shift"))

            # ── Compute z-scores per parameter ────────────────────────────────
            # 3-zone: STABLE (≤1σ) / ALERT (1σ–2σ or delta/LCL/UCL) / CRITICAL (>2σ)
            _RANK = {"stable": 0, "watch": 1, "warning": 2, "alert": 2, "critical": 3}
            _LVL_LABEL = {"stable": "STABLE", "watch": "WATCH", "warning": "WARNING", "alert": "ALERT", "critical": "CRITICAL"}
            param_results = []
            worst_rank    = 0
            worst_level   = "STABLE"

            # Only check parameters that are CHECKED in config (si_params)
            _checked_cols = [f"ps_{c}" if not c.startswith("ps_") else c
                             for c in _safe]
            for col in _checked_cols:
                if col not in df_bl.columns:
                    continue

                val = latest.get(col)
                if val is None or (hasattr(val, '__float__') and val != val):
                    continue
                val = float(val)

                bl_vals = df_bl[col].dropna()
                if len(bl_vals) < 15:
                    continue

                bl_mean = float(bl_vals.mean())
                bl_std  = float(bl_vals.std(ddof=1))
                if bl_std < 1e-9:
                    continue

                # Skip if val is 0 but baseline mean is non-zero
                # (0 means "not entered" — DB stores 0 for missing values)
                if val == 0.0 and abs(bl_mean) > 0.5:
                    continue



                z    = abs(val - bl_mean) / bl_std
                bare = col[3:]  # strip ps_

                # ── 3-zone sigma classification ───────────────────────────────
                # STABLE: z ≤ ok_sigma (1σ)
                # WATCH:  ok_sigma < z ≤ alert_sigma (3σ)
                # CRITICAL: z > alert_sigma (3σ)
                # LCL/UCL override applied below regardless of sigma zone
                if z <= ok_sigma:
                    lvl = "stable"
                elif z <= alert_sigma:
                    lvl = "watch"     # includes the 2-3σ range — monitor only
                else:
                    lvl = "critical"  # only above alert_sigma (default 3σ)

                # ── Delta check: shift-to-shift change ─────────────────────
                delta_note = ""
                if prev_row is not None:
                    prev_raw = prev_row.get(col)
                    if prev_raw is not None:
                        try:
                            prev_val = float(prev_raw)
                            if prev_val == prev_val and abs(prev_val) > 1e-9:
                                delta_pct = (val - prev_val) / abs(prev_val) * 100
                                delta_abs = abs(delta_pct)
                                if delta_abs > delta_thr:
                                    delta_note = f"{'↑' if delta_pct > 0 else '↓'}{delta_abs:.1f}% shift change"
                                    if lvl in ("stable", "watch"):
                                        lvl = "warning"  # pct change breach
                        except (ValueError, TypeError):
                            pass

                # ── LCL/UCL override — final highest-priority override ────────
                limit_note = ""
                limits = (self._db_limits or {}).get(bare, {})
                lcl = limits.get("lcl")
                ucl = limits.get("ucl")
                if lcl is not None and val < lcl:
                    limit_note = f"Below LCL ({lcl})"
                    lvl = "critical"  # hard limit breach always = critical
                elif ucl is not None and val > ucl:
                    limit_note = f"Above UCL ({ucl})"
                    lvl = "critical"  # hard limit breach always = critical

                label = disp.get(bare) or disp.get(col) or bare

                if _RANK[lvl] > worst_rank:
                    worst_rank  = _RANK[lvl]
                    worst_level = _LVL_LABEL[lvl]

                if lvl != "stable":
                    deviation_str = limit_note or delta_note or f"{'High' if val > bl_mean else 'Low'} ({z:.2f}σ)"
                    param_results.append({
                        "param"       : col,
                        "label"       : label,
                        "source"      : "ps",
                        "alert_level" : _LVL_LABEL.get(lvl, "STABLE"),
                        "si_score"    : round(min(z / alert_sigma, 1.0) * 100, 1),
                        "raw_value"   : round(val, 4),
                        "drift_label" : f"{z:.2f}σ from baseline",
                        "var_label"   : "STABLE",
                        "osc_label"   : "STABLE",
                        "deviation"   : deviation_str,
                        "pct_change"  : round((val - bl_mean) / bl_mean * 100, 2) if bl_mean else 0,
                        "bl_mean"     : round(bl_mean, 4),
                        "bl_std"      : round(bl_std,  4),
                        "z_score"     : round(z, 3),
                        "limit_breach": bool(limit_note),
                        "delta_flag"  : bool(delta_note),
                    })

            if worst_rank == 0:
                logger.debug("[%s/sigma]  All parameters within 1σ — STABLE", self._label)
                return

            # Overall SI score: 1=WARNING->50, 2=CRITICAL->100
            overall_si = round(min(worst_rank / 2.0, 1.0) * 100, 1)

            # Build period key
            d    = str(latest.get("date", today))
            sh   = shift or str(latest.get("shift", ""))
            mode_tag = {"window":"Win-σ","shift":"Shift-σ","component":"Comp-σ"}.get(mode,"σ")
            pk   = f"{mode_tag} | {d} | Shift {sh}"

            # 3-zone final level from SI score
            _level_map = {0:"STABLE", 1:"!! ALERT", 2:"CRITICAL"}
            final_alert = _level_map.get(worst_rank, "STABLE")
            # Override with worst individual param level (param detection is more sensitive)
            _param_rank = {"STABLE":0,"WATCH":1,"WARNING":2,"ALERT":2,"CRITICAL":3}
            _worst_param_lv = max(
                (p.get("alert_level","STABLE") for p in param_results),
                key=lambda lv: _param_rank.get(str(lv).upper().strip("!! "), 0),
                default="STABLE"
            )
            _worst_param_lv = str(_worst_param_lv).upper().replace("!! ","").strip()
            if _param_rank.get(_worst_param_lv, 0) > _param_rank.get(
                    final_alert.replace("!! ","").strip(), 0):
                final_alert = _worst_param_lv

            result = {
                "period_key"  : pk,
                "date"        : d,
                "shift"       : sh,
                "component_id": "",
                "final_alert" : final_alert,
                "si_alert"    : final_alert,
                "si_score_100": overall_si,
                "param_labels": {p["param"]: p["alert_level"] for p in param_results},
                "param_si"    : {p["param"]: p["si_score"]    for p in param_results},
                "drift_labels": {p["param"]: p["drift_label"] for p in param_results},
                "var_labels"  : {p["param"]: "STABLE"         for p in param_results},
                "osc_labels"  : {p["param"]: "STABLE"         for p in param_results},
                "deviations"  : {p["param"]: p["deviation"]   for p in param_results},
                "raw_values"  : {p["param"]: p["raw_value"]   for p in param_results},
                "pct_changes"    : {p["param"]: p.get("pct_change",0) for p in param_results},
                "baseline_means" : {p["param"][3:]: p.get("bl_mean") for p in param_results},
                "root_cause"  : f"Sigma zone breach: {len(param_results)} parameter(s) outside {ok_sigma}σ. "
                                + "; ".join(f"{p['label']} {p['deviation']}" for p in param_results[:3]),
                "recommendation": "Review recent changes to sand mix or additive dosing.",
            }

            logger.info("[%s/sigma]  SI alert  level=%s  score=%.1f  params=%d",
                        self._label, final_alert, overall_si, len(param_results))

            write_si_alert(engine, result, fl_id,
                           display_names=disp,
                           customer_pkey=config.get("customer_pkey", 0),
                           pct_warn=float(config.get("pct_change_warning", 5.0)))

            # Email + webhook
            import re as _re2
            _al = _re2.sub(r'[^A-Z]', '', final_alert)  # strips "!!" -> "ALERT" or "CRITICAL"
            if _al in ("CRITICAL", "ALERT"):
                try:
                    from .email_notifier import send_si_alert_email
                    send_si_alert_email({
                        "period_key"    : pk,
                        "date"          : d,
                        "shift"         : sh,
                        "si_score"      : overall_si,
                        "alert_level"   : _al,
                        "root_cause"    : result["root_cause"],
                        "recommendation": result["recommendation"],
                        "params_json"   : param_results,
                    }, config, label=self._label)
                except Exception:
                    logger.warning("[%s/sigma]  Email failed:\n%s", self._label, traceback.format_exc())

            # Webhook push notification
            _wh_cfg = config.get("webhook", {})
            if _wh_cfg.get("enabled") and _wh_cfg.get("send_si", True):
                try:
                    from .webhook_notifier import send_si_alerts
                    send_si_alerts(
                        result        = result,
                        config        = config,
                        db_limits     = self._db_limits or {},
                        display_names = config.get("display_names", {}),
                        label         = self._label,
                    )
                except Exception:
                    logger.warning("[%s/sigma]  Webhook failed:\n%s",
                                   self._label, traceback.format_exc())

        except Exception:
            logger.error("[%s/sigma]  _fire_sigma_window failed:\n%s",
                         self._label, traceback.format_exc())

    def _write_property_alerts(self, engine, result: dict, config: dict) -> None:
        pass  # property alerts feature removed

    def _log_alert_summary(self, result: dict) -> None:
        alert    = result.get("final_alert", "?")
        si_score = result.get("si_score_100", 0.0)
        period   = result.get("period_key", "?")
        root     = result.get("root_cause", "")

        divider = "-" * 60
        logger.info(divider)
        logger.info(
            "[%s]  ALERT  |  %s  |  SI=%.1f  |  %s",
            self._label, alert, si_score, period,
        )
        if root:
            logger.info("[%s]  Root cause: %s", self._label, root)
        devs = [
            (k, v) for k, v in result.get("deviations", {}).items()
            if isinstance(v, str) and v.startswith("Deviated")
        ]
        if devs:
            logger.info("[%s]  Deviations (%d):", self._label, len(devs))
            for col, status in devs[:10]:
                logger.info("[%s]    %-35s  %s", self._label, col, status)
        logger.info(divider)

    # -- Private -- DB config refresh ------------------------------------------

    def _refresh_db_configs(self) -> None:
        from .pipeline.data_fetcher import fetch_shift_config, fetch_control_limits, fetch_monitored_parameters, fetch_display_names
        from .config_store          import get_registry_engine, load_foundry_config

        # -- Foundry-specific config is ALWAYS loaded from DB, never from JSON --
        # The JSON file supplies only infrastructure settings (DB host/port/user,
        # webhook URL, API keys). All analysis settings (si_params, si_weights,
        # alert_thresholds, baseline, aggregation, engines, trigger, etc.) are
        # authoritative in the DB and overwrite whatever the JSON file says.
        _label = (f"{self._config['database']['name']}"
                  f"_L{self._config.get('foundry_line_id', 1)}")
        try:
            _reg = get_registry_engine(self._config)
            if not _reg:
                raise RuntimeError("Registry DB not reachable")
            fc = load_foundry_config(_reg, _label)
            if not fc:
                raise RuntimeError(f"No DB config found for {_label}")

            import copy as _c
            # Scalar overrides
            for key in ("dual_mode", "dual_mode_secondary", "report_window",
                        "pct_change_warning", "si_alerts_enabled",
                        "property_alerts_enabled", "customer_pkey"):
                if key in fc:
                    self._config[key] = fc[key]
            # Deep-replace analysis sections
            for section in ("si_params", "si_weights", "alert_thresholds",
                            "alert_labels", "optimal_values", "optimal_variance",
                            "si_param_weights"):
                if section in fc:
                    self._config[section] = _c.deepcopy(fc[section])
            # Merge dict sections (DB wins on every key present in DB)
            for section in ("aggregation", "engines", "baseline", "trigger",
                            "prescription_watchdog", "component_change_watchdog",
                            "bad_batch_watchdog", "sieve_watchdog", "notifications",
                            "property_engine_config", "property_engine_flags"):
                if section in fc:
                    self._config.setdefault(section, {}).update(fc[section])
            logger.info("[%s]  Per-foundry config loaded from DB", self._label)
        except Exception as exc:
            logger.warning(
                "[%s]  DB config load failed — continuing with current config: %s",
                self._label, exc,
            )

        try:
            db_shifts = fetch_shift_config(self._config)
            if db_shifts:
                self._shifts = db_shifts
                logger.info(
                    "[%s]  Shifts: %s",
                    self._label,
                    {k: f"{v['start']}-{v['end']}" for k, v in db_shifts.items()},
                )
            else:
                raise ValueError("empty")
        except Exception as exc:
            if not self._shifts:
                self._shifts = self._config.get("trigger", {}).get("shifts", {})
                logger.warning(
                    "[%s]  Shift load failed -- using config fallback: %s  (%s)",
                    self._label, list(self._shifts.keys()), exc,
                )
            else:
                logger.warning("[%s]  Shift refresh failed -- keeping cached: %s", self._label, exc)

        try:
            monitored = fetch_monitored_parameters(self._config)
            for key in ("prepared_sand", "consumption", "additive", "prepared_sand_extra"):
                if monitored.get(key):
                    self._config["parameters"][key] = monitored[key]
                    logger.info("[%s]  Monitored params [%s]: %s", self._label, key, monitored[key])
        except Exception as exc:
            logger.warning("[%s]  fetch_monitored_parameters failed -- keeping current: %s", self._label, exc)

        try:
            display_names = fetch_display_names(self._config)
            if display_names:
                self._config["display_names"] = display_names
        except Exception as exc:
            logger.warning("[%s]  fetch_display_names failed -- keeping current: %s", self._label, exc)

        try:
            db_limits = fetch_control_limits(self._config)
            if db_limits:
                self._db_limits = db_limits
                logger.info("[%s]  Control limits: %d params", self._label, len(db_limits))
        except Exception as exc:
            logger.warning("[%s]  Limit refresh failed: %s", self._label, exc)

        self._last_db_refresh = datetime.now()

    def _maybe_refresh_db_configs(self) -> None:
        last = self._last_db_refresh
        if last is None or (datetime.now() - last).total_seconds() >= _DB_REFRESH_MINUTES * 60:
            try:
                self._refresh_db_configs()
            except Exception as exc:
                logger.warning("[%s]  DB refresh failed: %s", self._label, exc)

    # -- Private -- watermarks -------------------------------------------------

    def _get_watermarks(self) -> dict:
        from .pipeline.db_connector import get_engine
        from sqlalchemy import text

        fl_id         = self._config["foundry_line_id"]
        skip_additive = self._config.get("trigger", {}).get("skip_additive", False)
        result = {"preparedsand": 0, "additive": 0}
        queries = {
            "preparedsand": text(
                "SELECT COALESCE(MAX(`pkey`), 0) AS max_id "
                "FROM `preparedsand` WHERE `foundry_line_id` = :fl_id AND `deleted` = 0"
            ),
        }
        if not skip_additive:
            queries["additive"] = text(
                "SELECT COALESCE(MAX(`pkey`), 0) AS max_id "
                "FROM `additive` WHERE `foundry_line_id` = :fl_id AND `deleted` = 0"
            )
        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                for table, sql in queries.items():
                    try:
                        row = conn.execute(sql, {"fl_id": fl_id}).mappings().first()
                        result[table] = int(row["max_id"]) if row else 0
                    except Exception as exc:
                        logger.warning("[%s]  Watermark query failed for %s: %s", self._label, table, exc)
        except Exception as exc:
            logger.warning("[%s]  Watermark DB connection failed: %s", self._label, exc)
        return result

    # -- Private -- shift helpers ----------------------------------------------

    def _shift_for_now(self, now: datetime) -> tuple[str | None, bool]:
        cur_min = now.hour * 60 + now.minute
        for name, bounds in self._shifts.items():
            eh, em  = self._parse_hhmm(bounds["end"])
            end_min = eh * 60 + em or 24 * 60
            if end_min <= cur_min < end_min + 1:
                return name, True
        for name, bounds in self._shifts.items():
            sh, sm = self._parse_hhmm(bounds["start"])
            eh, em = self._parse_hhmm(bounds["end"])
            s_min  = sh * 60 + sm
            e_min  = eh * 60 + em or 24 * 60
            if e_min <= s_min:
                if cur_min >= s_min or cur_min < e_min:
                    return name, False
            else:
                if s_min <= cur_min < e_min:
                    return name, False
        return None, False

    @staticmethod
    def _parse_hhmm(s: str) -> tuple[int, int]:
        h, m = s.strip().split(":")
        return int(h), int(m)

    @staticmethod
    def _cooldown_passed(last_fire_time, cooldown_seconds: int) -> bool:
        if last_fire_time is None:
            return True
        return (datetime.now() - last_fire_time).total_seconds() >= cooldown_seconds

    def _log_shifts(self) -> None:
        if not self._shifts:
            logger.info("[%s]  No shift config loaded yet", self._label)
            return
        for name, bounds in self._shifts.items():
            logger.info("[%s]  Shift %-4s  %s - %s", self._label, name, bounds["start"], bounds["end"])
