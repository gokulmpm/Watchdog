"""
scheduler.py
-------------
Background daemon for the AI Watchdog. Runs forever.

Trigger modes (config -> trigger.mode)
--------------------------------------
  day        -- fires once after day_end_hour each production day
  shift      -- fires at the end of every shift (boundaries from DB)
  window     -- fires every time N new rows appear in preparedsand
  continuous -- fires whenever new rows arrive in preparedsand OR additive
               with a cooldown (trigger.cooldown_seconds, default 300 s)

DB configs (shifts + control limits) are refreshed every 20 min.
JSON config is re-read every 30 min -- no restart needed for config changes.
"""

import logging
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_REFRESH_MINUTES     = 20
_CONFIG_REFRESH_MINUTES = 30

_state = {
    "config"          : None,
    "db_limits"       : {},
    "shifts"          : {},
    "last_db_refresh" : None,
    "last_cfg_refresh": None,
}


def start(config_path: Path) -> None:
    """Start the background daemon. Blocks forever."""
    logger.info("=" * 60)
    logger.info("  AI Watchdog  --  Background Daemon  starting")
    logger.info("  Config : %s", config_path)
    logger.info("=" * 60)

    _load_json_config(config_path)
    _refresh_db_configs()

    mode     = _state["config"]["trigger"]["mode"]
    interval = int(_state["config"]["trigger"].get("check_interval_seconds", 60))

    logger.info("Mode     : %s", mode)
    logger.info("Interval : %ds", interval)
    _log_shift_table()

    last_fired_day   = None
    last_fired_shift = None
    last_fire_time   = None
    watermarks       = _get_table_watermarks()
    window_counter   = watermarks.get("preparedsand", 0)

    logger.info("Daemon running -- waiting for first trigger ...")
    skip_add_startup = _state["config"].get("trigger", {}).get("skip_additive", False)
    if skip_add_startup:
        logger.info("Table watermarks at start: preparedsand max_id=%s  (additive skipped)",
                    watermarks.get("preparedsand"))
    else:
        logger.info("Table watermarks at start: preparedsand max_id=%s  additive max_id=%s",
                    watermarks.get("preparedsand"), watermarks.get("additive"))

    while True:
        try:
            _maybe_refresh_json_config(config_path)
            _maybe_refresh_db_configs()

            config   = _state["config"]
            mode     = config["trigger"]["mode"]
            now      = datetime.now()
            cooldown = int(config["trigger"].get("cooldown_seconds", 300))

            if mode == "day":
                day_end_hour   = int(config["trigger"].get("day_end_hour", 6))
                day_end_minute = int(config["trigger"].get("day_end_minute", 0))
                if _is_day_end(now, day_end_hour, day_end_minute):
                    trigger_date = (now - timedelta(days=1)).date()
                    if trigger_date != last_fired_day:
                        logger.info(
                            "DAY END  >  %s  (day-end=%02d:%02d)",
                            trigger_date, day_end_hour, day_end_minute
                        )
                        _fire("day", trigger_date=trigger_date)
                        last_fired_day = trigger_date

            elif mode == "shift":
                shifts     = _state["shifts"]
                shift_name, is_end = _shift_for_now(now, shifts)
                if is_end and shift_name:
                    fire_key = (now.date(), shift_name)
                    if fire_key != last_fired_shift:
                        sh = shifts[shift_name]
                        logger.info(
                            "SHIFT END  >  shift=%s (%s-%s)  date=%s",
                            shift_name, sh["start"], sh["end"], now.date()
                        )
                        _fire("shift", trigger_date=now.date(), shift=shift_name)
                        last_fired_shift = fire_key

            elif mode == "window":
                window_n   = int(config["trigger"].get("window_n", 10))
                cur_wm     = _get_table_watermarks()
                cur_count  = cur_wm.get("preparedsand", window_counter)
                new_rows   = cur_count - window_counter
                if new_rows >= window_n and _cooldown_passed(last_fire_time, cooldown):
                    logger.info(
                        "WINDOW  >  %d new preparedsand rows  (threshold=%d)",
                        new_rows, window_n
                    )
                    _fire("window")
                    window_counter = cur_count
                    last_fire_time = now

            elif mode == "continuous":
                cur_wm       = _get_table_watermarks()
                skip_additive = config.get("trigger", {}).get("skip_additive", False)
                new_ps  = cur_wm.get("preparedsand", 0) > watermarks.get("preparedsand", 0)
                new_add = (not skip_additive) and (
                    cur_wm.get("additive", 0) > watermarks.get("additive", 0)
                )

                if (new_ps or new_add) and _cooldown_passed(last_fire_time, cooldown):
                    changed = []
                    if new_ps:
                        changed.append(
                            f"preparedsand (was {watermarks.get('preparedsand')} -> {cur_wm.get('preparedsand')})"
                        )
                    if new_add:
                        changed.append(
                            f"additive (was {watermarks.get('additive')} -> {cur_wm.get('additive')})"
                        )
                    logger.info("NEW DATA  >  %s", "  |  ".join(changed))
                    _fire("continuous", trigger_date=now.date())
                    watermarks.update(cur_wm)
                    last_fire_time = now
                elif new_ps or new_add:
                    watermarks.update(cur_wm)

        except Exception:
            logger.error("Daemon loop error:\n%s", traceback.format_exc())

        time.sleep(interval)


def _fire(mode: str, trigger_date: date | None = None, shift: str | None = None) -> None:
    """Call run_for_period with the current cached config + db_limits."""
    from .run_watchdog    import run_for_period
    from .alert_db_writer import ensure_table, write_si_alert
    from .pipeline.db_connector import get_engine

    config    = _state["config"]
    db_limits = _state["db_limits"]
    fl_id     = config.get("foundry_line_id", 1)

    try:
        result = run_for_period(
            config        = config,
            trigger_mode  = mode,
            trigger_date  = trigger_date,
            trigger_shift = shift,
            db_limits     = db_limits,
        )
        if result:
            _log_alert_summary(result)
            # -- Persist SI alert to watchdog_alerts table --------------
            try:
                engine = get_engine(config)
                ensure_table(engine)
                write_si_alert(engine, result, fl_id,
                               display_names=config.get("display_names", {}),
                               customer_pkey=config.get("customer_pkey", 0),
                               pct_warn=float(config.get("pct_change_warning", 5.0)))
            except Exception:
                logger.error("DB alert write failed (SI):\n%s", traceback.format_exc())
        else:
            logger.warning("run_for_period returned no result for %s %s", trigger_date, shift)

    except Exception:
        logger.error("_fire failed:\n%s", traceback.format_exc())


def _load_json_config(config_path: Path) -> None:
    import json
    with open(config_path, encoding="utf-8") as f:
        _state["config"] = json.load(f)
    _state["last_cfg_refresh"] = datetime.now()
    logger.info("Config loaded from %s", config_path.name)


def _maybe_refresh_json_config(config_path: Path) -> None:
    last = _state["last_cfg_refresh"]
    if last is None or (datetime.now() - last).total_seconds() >= _CONFIG_REFRESH_MINUTES * 60:
        try:
            _load_json_config(config_path)
        except Exception as exc:
            logger.warning("Config reload failed (keeping current): %s", exc)


def _refresh_db_configs() -> None:
    """Load shift boundaries, monitored parameters, and control limits from the DB."""
    from .pipeline.data_fetcher import fetch_shift_config, fetch_control_limits, fetch_monitored_parameters, fetch_display_names

    config = _state["config"]

    try:
        monitored = fetch_monitored_parameters(config)
        for key in ("prepared_sand", "consumption", "additive", "prepared_sand_extra"):
            if monitored.get(key):
                _state["config"]["parameters"][key] = monitored[key]
                logger.info("Monitored params [%s]: %s", key, monitored[key])
    except Exception as exc:
        logger.warning("fetch_monitored_parameters failed -- keeping current: %s", exc)

    try:
        display_names = fetch_display_names(config)
        if display_names:
            _state["config"]["display_names"] = display_names
    except Exception as exc:
        logger.warning("fetch_display_names failed -- keeping current: %s", exc)

    try:
        db_shifts = fetch_shift_config(config)
        if db_shifts:
            _state["shifts"] = db_shifts
            logger.info(
                "Shifts refreshed from DB: %s",
                {k: f"{v['start']}-{v['end']}" for k, v in db_shifts.items()}
            )
        else:
            raise ValueError("empty result")
    except Exception as exc:
        if not _state["shifts"]:
            # First load -- fall back to config
            _state["shifts"] = config.get("trigger", {}).get("shifts", {})
            logger.warning(
                "DB shift load failed -- using config fallback: %s  (%s)",
                list(_state["shifts"].keys()), exc
            )
        else:
            logger.warning("DB shift refresh failed -- keeping cached: %s", exc)

    try:
        db_limits = fetch_control_limits(config)
        if db_limits:
            _state["db_limits"] = db_limits
            logger.info("Control limits refreshed from DB (%d params)", len(db_limits))
    except Exception as exc:
        logger.warning("DB limit refresh failed -- keeping cached: %s", exc)

    _state["last_db_refresh"] = datetime.now()


def _maybe_refresh_db_configs() -> None:
    last = _state["last_db_refresh"]
    if last is None or (datetime.now() - last).total_seconds() >= _DB_REFRESH_MINUTES * 60:
        try:
            _refresh_db_configs()
        except Exception as exc:
            logger.warning("DB refresh cycle failed: %s", exc)


def _get_table_watermarks() -> dict:
    """Return MAX(id) for preparedsand and additive tables."""
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    config = _state["config"]
    fl_id  = config["foundry_line_id"]
    result = {"preparedsand": 0, "additive": 0}

    skip_additive = config.get("trigger", {}).get("skip_additive", False)
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
        engine = get_engine(config)
        with engine.connect() as conn:
            for table, sql in queries.items():
                try:
                    row = conn.execute(sql, {"fl_id": fl_id}).mappings().first()
                    result[table] = int(row["max_id"]) if row else 0
                except Exception as exc:
                    logger.warning("Watermark query failed for %s: %s", table, exc)
    except Exception as exc:
        logger.warning("_get_table_watermarks: DB connection failed: %s", exc)

    return result


def _cooldown_passed(last_fire_time, cooldown_seconds: int) -> bool:
    if last_fire_time is None:
        return True
    return (datetime.now() - last_fire_time).total_seconds() >= cooldown_seconds


def _is_day_end(now: datetime, hour: int, minute: int = 0) -> bool:
    return now.hour == hour and now.minute == minute


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def _shift_for_now(now: datetime, shifts: dict) -> tuple[str | None, bool]:
    """Return (shift_name, is_at_end).  Handles midnight wraparound."""
    cur_min = now.hour * 60 + now.minute

    for name, bounds in shifts.items():
        eh, em  = _parse_hhmm(bounds["end"])
        end_min = eh * 60 + em
        if end_min == 0:
            end_min = 24 * 60
        if end_min <= cur_min < end_min + 1:
            return name, True

    # Current shift (for context)
    for name, bounds in shifts.items():
        sh, sm = _parse_hhmm(bounds["start"])
        eh, em = _parse_hhmm(bounds["end"])
        s_min  = sh * 60 + sm
        e_min  = eh * 60 + em
        if e_min <= s_min:
            if cur_min >= s_min or cur_min < e_min:
                return name, False
        else:
            if s_min <= cur_min < e_min:
                return name, False
    return None, False


def _log_alert_summary(result: dict) -> None:
    alert    = result.get("final_alert", "?")
    si_score = result.get("si_score_100", 0.0)
    period   = result.get("period_key", "?")
    root     = result.get("root_cause", "")
    rec      = result.get("recommendation", "")

    divider = "-" * 70
    logger.info(divider)
    logger.info("  ALERT  |  %s  |  SI = %.1f  |  %s", alert, si_score, period)
    logger.info("  Root cause    : %s", root)
    logger.info("  Recommendation: %s", rec)

    # List any deviating parameters
    devs = [(k, v) for k, v in result.get("deviations", {}).items()
            if isinstance(v, str) and v.startswith("Deviated")]
    if devs:
        logger.info("  Deviations (%d):", len(devs))
        for col, status in devs[:10]:
            logger.info("    %-35s  %s", col, status)

    logger.info(divider)


def _log_shift_table() -> None:
    shifts = _state["shifts"]
    if not shifts:
        logger.info("No shift config loaded yet")
        return
    logger.info("Active shifts:")
    for name, bounds in shifts.items():
        logger.info("  Shift %-4s  %s - %s", name, bounds["start"], bounds["end"])
