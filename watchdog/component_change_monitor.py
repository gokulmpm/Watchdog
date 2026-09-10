"""
component_change_monitor.py
----------------------------
Component Change alert monitor.

Fires a COMPONENT_CHANGE alert every time the component_id in the additive
table switches to a new value.  Each alert carries:

  - component_id, group_name
  - component weight  (nett_casting_wt from the rejections table)
  - SMR              (total_preparedsand_qty / liq_metal_poured from consumption)
  - current AI prescription for the component group (from analytics_report)

Config keys (under "component_change_watchdog" in watchdog_config.json):
  poll_interval_sec   -- seconds between polls (default 30)
  idle_timeout_min    -- stop after N minutes with no new batches (0 = never, default 0)

Usage
-----
  Embedded (from foundry_si_monitor.py):
      from watchdog.component_change_monitor import ComponentChangeMonitor
      monitor = ComponentChangeMonitor(config, label="caspro_sandman_L1")
      thread  = threading.Thread(target=monitor.start, daemon=True)
      thread.start()
"""

import json
import logging
import time
import traceback
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

class ComponentChangeMonitor:
    """Poll additive batches and alert whenever component_id changes."""

    def __init__(self, config: dict, label: str = "comp_change") -> None:
        self._config             = config
        self._label              = label
        self._last_batch_pkey    = 0
        self._last_component_id  = None   

    def start(self) -> None:
        """Run forever -- call from a daemon thread."""
        cc_cfg       = self._config.get("component_change_watchdog", {})
        poll_sec     = int(cc_cfg.get("poll_interval_sec", 30))
        idle_timeout = int(cc_cfg.get("idle_timeout_min",  0))

        logger.info(
            "[%s]  Component-change monitor starting  (poll=%ds  idle_timeout=%dmin)",
            self._label, poll_sec, idle_timeout,
        )

        self._last_batch_pkey, self._last_component_id = self._fetch_initial_state()
        logger.info(
            "[%s]  Starting from batch pkey=%d  component=%s",
            self._label, self._last_batch_pkey, self._last_component_id or "--",
        )

        last_new_batch_time: Optional[datetime] = datetime.now()

        while True:
            try:
                new_alerts = self._poll(poll_sec)

                if new_alerts > 0:
                    last_new_batch_time = datetime.now()
                    logger.info("[%s]  %d component-change alert(s) written", self._label, new_alerts)
                else:
                    if idle_timeout > 0 and last_new_batch_time is not None:
                        idle_secs = (datetime.now() - last_new_batch_time).total_seconds()
                        if idle_secs >= idle_timeout * 60:
                            logger.info(
                                "[%s]  Idle %.0f min -- stopping component-change monitor",
                                self._label, idle_secs / 60,
                            )
                            return

            except Exception:
                logger.error("[%s]  Poll error:\n%s", self._label, traceback.format_exc())

            time.sleep(poll_sec)

    def _poll(self, poll_sec: int) -> int:
        from .pipeline.db_connector import get_engine
        from .alert_db_writer       import ensure_table, write_component_change_alert

        fl_id  = int(self._config.get("foundry_line_id", 1))
        engine = get_engine(self._config)
        ensure_table(engine)

        batches = _fetch_new_batches(self._config, self._last_batch_pkey)
        if batches.empty:
            return 0

        written = 0
        for _, row in batches.iterrows():
            new_comp = str(row.get("component_id") or "").strip()
            if not new_comp:
                continue

            if new_comp != self._last_component_id:
                info      = _build_component_info(self._config, row)
                comp_name = _fetch_component_name(self._config, new_comp)
                prev_name = _fetch_component_name(self._config, self._last_component_id or "")
                result = {
                    "batch_pkey"          : int(row["pkey"]),
                    "component_id"        : new_comp,
                    "component_name"      : comp_name,
                    "prev_component_id"   : self._last_component_id or "",
                    "prev_component_name" : prev_name,
                    "group_name"          : str(row.get("group_name") or ""),
                    "date"                : row.get("date"),
                    "shift"               : str(row.get("shift") or ""),
                    "batch_time"          : row.get("batch_time"),
                    "component_info"      : info,
                }
                written += write_component_change_alert(engine, result, fl_id,
                                                        customer_pkey=self._config.get("customer_pkey", 0))

                self._last_component_id = new_comp

        # Advance high-water mark
        if not batches.empty:
            self._last_batch_pkey = int(batches["pkey"].max())

        return written

    def _fetch_initial_state(self) -> tuple[int, Optional[str]]:
        """Return (max_pkey, component_id_of_last_batch) from the source table."""
        from .pipeline.db_connector import get_engine
        from sqlalchemy import text

        fl_id      = self._config.get("foundry_line_id", 1)
        use_scada  = _use_scada(self._config)

        if use_scada:
            sql = text("""
                SELECT `pkey`, `component_id`
                FROM   `scada_data`
                WHERE  `foundry_line_pkey` = :fl_id
                  AND  (deleted = 0 OR deleted IS NULL)
                  AND  `component_id` IS NOT NULL AND `component_id` != ''
                ORDER  BY `pkey` DESC
                LIMIT  1
            """)
        else:
            sql = text("""
                SELECT `pkey`, `component_id`
                FROM   `additive`
                WHERE  `foundry_line_id` = :fl_id AND `deleted` = 0
                ORDER  BY `pkey` DESC
                LIMIT  1
            """)

        try:
            engine = get_engine(self._config)
            with engine.connect() as conn:
                row = conn.execute(sql, {"fl_id": fl_id}).mappings().first()
            if row:
                return int(row["pkey"]), str(row["component_id"] or "").strip() or None
        except Exception as exc:
            logger.warning("[%s]  _fetch_initial_state failed: %s", self._label, exc)
        return 0, None

def _use_scada(config: dict) -> bool:
    """Return True when this foundry line uses scada_data instead of additive."""
    return bool(config.get("component_change_watchdog", {}).get("use_scada", False))

def _fetch_new_batches(config: dict, last_pkey: int) -> pd.DataFrame:
    """Route to the correct source table based on config."""
    if _use_scada(config):
        return _fetch_new_batches_scada(config, last_pkey)
    return _fetch_new_batches_additive(config, last_pkey)

def _fetch_new_batches_additive(config: dict, last_pkey: int) -> pd.DataFrame:
    """
    Return additive rows newer than last_pkey, joined with group info.
    Ordered ascending so we process component changes in sequence.
    """
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    fl_id = int(config.get("foundry_line_id", 1))
    sql   = text("""
        SELECT a.pkey,
               a.component_id,
               DATE(a.date)      AS date,
               a.shift,
               a.timestamp       AS batch_time,
               g.name            AS group_name
        FROM   `additive` a
        LEFT JOIN `foundry_line_group_component` gc
               ON gc.component_id = a.component_id AND gc.deleted = 0
        LEFT JOIN `foundry_line_group` g
               ON g.pkey = gc.foundry_line_group_pkey
              AND g.deleted = 0
              AND g.foundry_line_pkey = :fl_id
        WHERE  a.foundry_line_id = :fl_id
          AND  a.deleted = 0
          AND  a.pkey > :last_pkey
        ORDER  BY a.pkey ASC
    """)

    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"fl_id": fl_id, "last_pkey": last_pkey})
        if not df.empty and "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        return df
    except Exception as exc:
        logger.warning("_fetch_new_batches_additive failed: %s", exc)
        return pd.DataFrame()

def _fetch_new_batches_scada(config: dict, last_pkey: int) -> pd.DataFrame:
    """
    Return scada_data rows newer than last_pkey, joined with group info.
    Normalises column names to match the additive path so the rest of the
    monitor code works unchanged.
    """
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    fl_id = int(config.get("foundry_line_id", 1))
    sql   = text("""
        SELECT s.pkey,
               s.component_id,
               DATE(s.date)          AS date,
               s.shift_no            AS shift,
               ADDTIME(DATE(s.date), s.time) AS batch_time,
               g.name                AS group_name
        FROM   `scada_data` s
        LEFT JOIN `foundry_line_group_component` gc
               ON gc.component_id = s.component_id AND gc.deleted = 0
        LEFT JOIN `foundry_line_group` g
               ON g.pkey = gc.foundry_line_group_pkey
              AND g.deleted = 0
              AND g.foundry_line_pkey = :fl_id
        WHERE  s.foundry_line_pkey = :fl_id
          AND  (s.deleted = 0 OR s.deleted IS NULL)
          AND  s.pkey > :last_pkey
          AND  s.component_id IS NOT NULL
          AND  s.component_id != ''
        ORDER  BY s.pkey ASC
    """)

    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"fl_id": fl_id, "last_pkey": last_pkey})
        if not df.empty and "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        if not df.empty and "shift" in df.columns:
            df["shift"] = df["shift"].astype(str).str.strip()
        return df
    except Exception as exc:
        logger.warning("_fetch_new_batches_scada failed: %s", exc)
        return pd.DataFrame()

def _fetch_component_weight(config: dict, component_id: str) -> Optional[float]:
    """Return the most-recent nett_casting_wt for this component_id."""
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    fl_id = int(config.get("foundry_line_id", 1))
    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT `nett_casting_wt`
                    FROM   `rejections`
                    WHERE  `foundry_line_id` = :fl_id
                      AND  `component_id`    = :comp
                      AND  `deleted`         = 0
                      AND  `nett_casting_wt` IS NOT NULL
                      AND  `nett_casting_wt` > 0
                    ORDER  BY `date` DESC
                    LIMIT  1
                """),
                {"fl_id": fl_id, "comp": component_id},
            ).mappings().first()
        if row:
            return float(row["nett_casting_wt"])
    except Exception as exc:
        logger.debug("_fetch_component_weight failed for %s: %s", component_id, exc)
    return None

def _fetch_component_name(config: dict, component_id: str) -> str:
    """Return component_name from the components table, or empty string."""
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    if not component_id:
        return ""
    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT `component_name` FROM `components` "
                     "WHERE `component_id` = :comp AND `deleted` = 0 LIMIT 1"),
                {"comp": component_id},
            ).mappings().first()
        return str(row["component_name"]).strip() if row and row["component_name"] else ""
    except Exception as exc:
        logger.debug("_fetch_component_name failed for %s: %s", component_id, exc)
    return ""

def _fetch_smr(config: dict, component_id: str) -> Optional[float]:
    """Return sand_metal_ratio for this component from the components table."""
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT `sand_metal_ratio`
                    FROM   `components`
                    WHERE  `component_id` = :comp
                      AND  `deleted`      = 0
                    LIMIT  1
                """),
                {"comp": component_id},
            ).mappings().first()
        if row and row["sand_metal_ratio"] is not None:
            return float(row["sand_metal_ratio"])
    except Exception as exc:
        logger.debug("_fetch_smr failed for %s: %s", component_id, exc)
    return None

def _fetch_current_prescription(config: dict, group_name: str,
                                 target_date: date, shift: str) -> Optional[dict]:
    """
    Return the most-recent predicted_additives_json for this group/date/shift
    from analytics_report.  Returns None when no prescription exists.
    """
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    fl_id = int(config.get("foundry_line_id", 1))
    if not group_name:
        return None
    try:
        engine = get_engine(config)
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT `predicted_additives_json`
                    FROM   `analytics_report`
                    WHERE  `foundry_line_pkey`       = :fl_id
                      AND  `foundry_line_group_name` = :grp
                      AND  DATE(`date`)              = :dt
                      AND  `shift`                   = :sh
                      AND  `deleted`                 = 0
                    ORDER  BY `pkey` DESC
                    LIMIT  1
                """),
                {"fl_id": fl_id, "grp": group_name, "dt": str(target_date), "sh": str(shift)},
            ).mappings().first()
        if row and row["predicted_additives_json"]:
            raw = row["predicted_additives_json"]
            return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        logger.debug("_fetch_current_prescription failed: %s", exc)
    return None

def _build_component_info(config: dict, row: pd.Series) -> dict:
    """Assemble all component metadata for the alert payload."""
    component_id = str(row.get("component_id") or "").strip()
    group_name   = str(row.get("group_name")   or "").strip()
    batch_date   = row.get("date")
    shift        = str(row.get("shift") or "").strip()

    weight       = _fetch_component_weight(config, component_id)
    smr          = _fetch_smr(config, component_id)
    prescription = _fetch_current_prescription(config, group_name, batch_date, shift) if batch_date else None

    return {
        "component_weight_kg": weight,
        "smr"                : smr,
        "group_name"         : group_name or None,
        "prescription"       : prescription,
    }

def _to_float(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None   # NaN guard
    except (TypeError, ValueError):
        return None

# -- One-shot check ------------------------------------------------------------

def run_check(config: dict, write_db: bool = False) -> None:
    """
    Fetch the last two distinct components from the additive table,
    treat the most-recent one as a "component change" event, print a full
    summary of the component info, and optionally write the alert to DB.
    """
    from .pipeline.db_connector import get_engine
    from sqlalchemy import text

    fl_id = int(config.get("foundry_line_id", 1))
    engine = get_engine(config)

    # Fetch the last 2 distinct component batches
    sql = text("""
        SELECT a.pkey, a.component_id, DATE(a.date) AS date, a.shift,
               a.timestamp AS batch_time, g.name AS group_name
        FROM   `additive` a
        LEFT JOIN `foundry_line_group_component` gc
               ON gc.component_id = a.component_id AND gc.deleted = 0
        LEFT JOIN `foundry_line_group` g
               ON g.pkey = gc.foundry_line_group_pkey
              AND g.deleted = 0 AND g.foundry_line_pkey = :fl_id
        WHERE  a.foundry_line_id = :fl_id AND a.deleted = 0
          AND  a.component_id IS NOT NULL AND a.component_id != ''
        ORDER  BY a.pkey DESC
        LIMIT  50
    """)

    import pandas as pd
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"fl_id": fl_id})

    if df.empty:
        logger.info("No additive batches found for this foundry line.")
        return

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date

    current_row = df.iloc[0]
    current_comp = str(current_row["component_id"]).strip()

    prev_comp = None
    for _, r in df.iterrows():
        c = str(r["component_id"]).strip()
        if c != current_comp:
            prev_comp = c
            break

    SEP = "=" * 70
    logger.info(SEP)
    logger.info("  COMPONENT CHANGE CHECK")
    logger.info(SEP)
    logger.info("  Previous Component : %s", prev_comp or "(unknown — no earlier batch)")
    logger.info("  Current  Component : %s", current_comp)
    logger.info("  Date / Shift       : %s  · Shift %s", current_row.get("date"), current_row.get("shift", ""))
    batch_time = current_row.get("batch_time")
    if batch_time is not None:
        logger.info("  Batch Time         : %s", batch_time)

    info      = _build_component_info(config, current_row)
    comp_name = _fetch_component_name(config, current_comp)
    prev_name = _fetch_component_name(config, prev_comp or "")

    logger.info("  Previous Component : %s  %s", prev_comp or "(unknown)", ("— " + prev_name) if prev_name else "")
    logger.info("  Current  Component : %s  %s", current_comp, ("— " + comp_name) if comp_name else "")
    logger.info("  Group              : %s", info.get("group_name") or "—")
    weight = info.get("component_weight_kg")
    logger.info("  Component Weight   : %s", f"{weight:.3f} kg" if weight is not None else "—")
    smr = info.get("smr")
    logger.info("  SMR                : %s", f"{smr:.4f}" if smr is not None else "—")

    presc = info.get("prescription") or {}
    if presc:
        logger.info("  Current Prescription:")
        _PLBLS = {
            "bentonite": "Bentonite", "freshSilicaSand": "Fresh Silica Sand",
            "lca": "LCA / Coal Dust", "water": "Water",
        }
        for k, v in presc.items():
            label = _PLBLS.get(k, k)
            unit  = "ltr" if k == "water" else "kg"
            val   = f"{float(v):.3f} {unit}" if v is not None else "—"
            logger.info("    %-22s: %s", label, val)
    else:
        logger.info("  Current Prescription : No prescription found for this group/date/shift")

    logger.info(SEP)

    if write_db:
        from .alert_db_writer import ensure_table, write_component_change_alert
        ensure_table(engine)
        result = {
            "batch_pkey"          : int(current_row["pkey"]),
            "component_id"        : current_comp,
            "component_name"      : comp_name,
            "prev_component_id"   : prev_comp or "",
            "prev_component_name" : prev_name,
            "group_name"          : str(current_row.get("group_name") or ""),
            "date"                : current_row.get("date"),
            "shift"               : str(current_row.get("shift") or ""),
            "batch_time"          : current_row.get("batch_time"),
            "component_info"      : info,
        }
        written = write_component_change_alert(engine, result, fl_id,
                                               customer_pkey=config.get("customer_pkey", 0))
        logger.info("  Alert written to DB : %s", "YES" if written else "NO (duplicate or error)")
