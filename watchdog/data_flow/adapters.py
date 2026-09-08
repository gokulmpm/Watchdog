"""
watchdog/data_flow/adapters.py
-------------------------------
Source adapters — each source table has a different format.
All adapters expose the same interface so the monitor
never deals with raw SQL differences.

Interface:
  check_presence(engine, foundry_line_id, window_minutes) -> PresenceResult
  get_last_seen(engine, foundry_line_id)                 -> datetime | None
  get_timestamps(engine, foundry_line_id, days)          -> list[datetime]

Adding a new source = add a new class here + register in SOURCE_ADAPTERS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class PresenceResult:
    source_name:    str
    has_data:       bool
    last_seen:      Optional[datetime]
    record_count:   int
    window_minutes: int


# ── Base adapter ──────────────────────────────────────────────────────────────

class SourceAdapter:
    """
    Base class. Every subclass must implement the three methods below.
    Do not put business logic here — adapters only query, never decide.
    """

    source_name: str = "base"

    def check_presence(
        self, engine: Engine, foundry_line_id: int, window_minutes: int = 30
    ) -> PresenceResult:
        raise NotImplementedError

    def get_last_seen(
        self, engine: Engine, foundry_line_id: int
    ) -> Optional[datetime]:
        raise NotImplementedError

    def get_timestamps(
        self, engine: Engine, foundry_line_id: int, days: int = 365
    ) -> list[datetime]:
        raise NotImplementedError

    # Shared helper
    def _run(self, engine: Engine, sql: text, params: dict) -> dict:
        try:
            with engine.connect() as conn:
                row = conn.execute(sql, params).mappings().first()
            return dict(row) if row else {}
        except Exception as exc:
            logger.warning("[%s] query failed: %s", self.source_name, exc)
            return {}

    def _run_many(self, engine: Engine, sql: text, params: dict) -> list[dict]:
        try:
            with engine.connect() as conn:
                rows = conn.execute(sql, params).mappings().fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("[%s] query failed: %s", self.source_name, exc)
            return []


# ── SCADA adapter ─────────────────────────────────────────────────────────────

class ScadaAdapter(SourceAdapter):
    """
    scada_data — PLC/SCADA real-time data.

    Format differences vs other sources:
      - Uses foundry_line_pkey (not foundry_line_id)
      - Timestamp stored as separate date + time columns
      - deleted column may be NULL (not always 0)
    """

    source_name = "scada"

    def check_presence(
        self, engine: Engine, foundry_line_id: int, window_minutes: int = 30
    ) -> PresenceResult:
        sql = text("""
            SELECT
                COUNT(*)                             AS cnt,
                MAX(TIMESTAMP(`date`, `time`))       AS last_seen
            FROM `scada_data`
            WHERE `foundry_line_pkey` = :lid
              AND (deleted = 0 OR deleted IS NULL)
              AND TIMESTAMP(`date`, `time`) >= NOW() - INTERVAL :mins MINUTE
        """)
        row = self._run(engine, sql, {"lid": foundry_line_id, "mins": window_minutes})
        return PresenceResult(
            source_name    = self.source_name,
            has_data       = (row.get("cnt") or 0) > 0,
            last_seen      = row.get("last_seen"),
            record_count   = int(row.get("cnt") or 0),
            window_minutes = window_minutes,
        )

    def get_last_seen(self, engine: Engine, foundry_line_id: int) -> Optional[datetime]:
        sql = text("""
            SELECT MAX(TIMESTAMP(`date`, `time`)) AS last_seen
            FROM `scada_data`
            WHERE `foundry_line_pkey` = :lid
              AND (deleted = 0 OR deleted IS NULL)
        """)
        row = self._run(engine, sql, {"lid": foundry_line_id})
        return row.get("last_seen")

    def get_timestamps(
        self, engine: Engine, foundry_line_id: int, days: int = 365
    ) -> list[datetime]:
        sql = text("""
            SELECT TIMESTAMP(`date`, `time`) AS ts
            FROM `scada_data`
            WHERE `foundry_line_pkey` = :lid
              AND (deleted = 0 OR deleted IS NULL)
              AND `date` >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
            ORDER BY `date` ASC, `time` ASC
        """)
        rows = self._run_many(engine, sql, {"lid": foundry_line_id, "days": days})
        return [r["ts"] for r in rows if r.get("ts")]


# ── Additive adapter ──────────────────────────────────────────────────────────

class AdditiveAdapter(SourceAdapter):
    """
    additive — Mixer/PLC batch data.
    Can be real-time (PLC stream) or cron-based (bulk insert at intervals).
    Default window is 30 min for real-time; set higher for cron-based sources.
    """

    source_name = "additive"

    def check_presence(
        self, engine: Engine, foundry_line_id: int, window_minutes: int = 30
    ) -> PresenceResult:
        sql = text("""
            SELECT COUNT(*) AS cnt, MAX(timestamp) AS last_seen
            FROM `additive`
            WHERE foundry_line_id = :lid
              AND deleted = 0
              AND timestamp >= NOW() - INTERVAL :mins MINUTE
        """)
        row = self._run(engine, sql, {"lid": foundry_line_id, "mins": window_minutes})
        return PresenceResult(
            source_name    = self.source_name,
            has_data       = (row.get("cnt") or 0) > 0,
            last_seen      = row.get("last_seen"),
            record_count   = int(row.get("cnt") or 0),
            window_minutes = window_minutes,
        )

    def get_last_seen(self, engine: Engine, foundry_line_id: int) -> Optional[datetime]:
        sql = text("""
            SELECT MAX(timestamp) AS last_seen
            FROM `additive`
            WHERE foundry_line_id = :lid AND deleted = 0
        """)
        return self._run(engine, sql, {"lid": foundry_line_id}).get("last_seen")

    def get_timestamps(
        self, engine: Engine, foundry_line_id: int, days: int = 365
    ) -> list[datetime]:
        sql = text("""
            SELECT timestamp AS ts
            FROM `additive`
            WHERE foundry_line_id = :lid
              AND deleted = 0
              AND timestamp >= NOW() - INTERVAL :days DAY
            ORDER BY timestamp ASC
        """)
        rows = self._run_many(engine, sql, {"lid": foundry_line_id, "days": days})
        return [r["ts"] for r in rows if r.get("ts")]


# ── Prepared sand adapter ─────────────────────────────────────────────────────

class PreparedSandAdapter(SourceAdapter):
    """
    preparedsand — Lab measurements. Periodic/manual entry.
    Timestamp stored as separate date + time columns.
    """

    source_name = "preparedsand"

    def check_presence(
        self, engine: Engine, foundry_line_id: int, window_minutes: int = 120
    ) -> PresenceResult:
        sql = text("""
            SELECT
                COUNT(*)                          AS cnt,
                MAX(TIMESTAMP(`date`, `time`))    AS last_seen
            FROM `preparedsand`
            WHERE foundry_line_id = :lid
              AND deleted = 0
              AND TIMESTAMP(`date`, `time`) >= NOW() - INTERVAL :mins MINUTE
        """)
        row = self._run(engine, sql, {"lid": foundry_line_id, "mins": window_minutes})
        return PresenceResult(
            source_name    = self.source_name,
            has_data       = (row.get("cnt") or 0) > 0,
            last_seen      = row.get("last_seen"),
            record_count   = int(row.get("cnt") or 0),
            window_minutes = window_minutes,
        )

    def get_last_seen(self, engine: Engine, foundry_line_id: int) -> Optional[datetime]:
        sql = text("""
            SELECT MAX(TIMESTAMP(`date`, `time`)) AS last_seen
            FROM `preparedsand`
            WHERE foundry_line_id = :lid AND deleted = 0
        """)
        return self._run(engine, sql, {"lid": foundry_line_id}).get("last_seen")

    def get_timestamps(
        self, engine: Engine, foundry_line_id: int, days: int = 365
    ) -> list[datetime]:
        sql = text("""
            SELECT TIMESTAMP(`date`, `time`) AS ts
            FROM `preparedsand`
            WHERE foundry_line_id = :lid
              AND deleted = 0
              AND `date` >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
            ORDER BY `date` ASC, `time` ASC
        """)
        rows = self._run_many(engine, sql, {"lid": foundry_line_id, "days": days})
        return [r["ts"] for r in rows if r.get("ts")]


# ── Prepared sand extra adapter ───────────────────────────────────────────────

class PreparedSandExtraAdapter(SourceAdapter):
    """
    prepared_sand_extra — High-frequency auxiliary PLC/mixer data.
    Similar format to preparedsand but higher frequency.
    """

    source_name = "preparedsand_extra"

    def check_presence(
        self, engine: Engine, foundry_line_id: int, window_minutes: int = 30
    ) -> PresenceResult:
        sql = text("""
            SELECT COUNT(*) AS cnt, MAX(TIMESTAMP(`date`, `time`)) AS last_seen
            FROM `prepared_sand_extra`
            WHERE foundry_line_id = :lid
              AND TIMESTAMP(`date`, `time`) >= NOW() - INTERVAL :mins MINUTE
        """)
        row = self._run(engine, sql, {"lid": foundry_line_id, "mins": window_minutes})
        return PresenceResult(
            source_name    = self.source_name,
            has_data       = (row.get("cnt") or 0) > 0,
            last_seen      = row.get("last_seen"),
            record_count   = int(row.get("cnt") or 0),
            window_minutes = window_minutes,
        )

    def get_last_seen(self, engine: Engine, foundry_line_id: int) -> Optional[datetime]:
        sql = text("""
            SELECT MAX(TIMESTAMP(`date`, `time`)) AS last_seen
            FROM `prepared_sand_extra`
            WHERE foundry_line_id = :lid
        """)
        return self._run(engine, sql, {"lid": foundry_line_id}).get("last_seen")

    def get_timestamps(
        self, engine: Engine, foundry_line_id: int, days: int = 365
    ) -> list[datetime]:
        sql = text("""
            SELECT TIMESTAMP(`date`, `time`) AS ts
            FROM `prepared_sand_extra`
            WHERE foundry_line_id = :lid
              AND `date` >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
            ORDER BY `date` ASC, `time` ASC
        """)
        rows = self._run_many(engine, sql, {"lid": foundry_line_id, "days": days})
        return [r["ts"] for r in rows if r.get("ts")]


# ── Consumption adapter ───────────────────────────────────────────────────────

class ConsumptionAdapter(SourceAdapter):
    """
    consumption — Shift-based. Arrives once per shift, after shift ends.
    Use a large default window (8 hours) because it's not continuous.
    """

    source_name = "consumption"

    def check_presence(
        self, engine: Engine, foundry_line_id: int, window_minutes: int = 480
    ) -> PresenceResult:
        sql = text("""
            SELECT COUNT(*) AS cnt, MAX(`date`) AS last_seen
            FROM `consumption`
            WHERE foundry_line_id = :lid
              AND deleted = 0
              AND `date` >= DATE_SUB(NOW(), INTERVAL :mins MINUTE)
        """)
        row = self._run(engine, sql, {"lid": foundry_line_id, "mins": window_minutes})
        return PresenceResult(
            source_name    = self.source_name,
            has_data       = (row.get("cnt") or 0) > 0,
            last_seen      = row.get("last_seen"),
            record_count   = int(row.get("cnt") or 0),
            window_minutes = window_minutes,
        )

    def get_last_seen(self, engine: Engine, foundry_line_id: int) -> Optional[datetime]:
        sql = text("""
            SELECT MAX(`date`) AS last_seen
            FROM `consumption`
            WHERE foundry_line_id = :lid AND deleted = 0
        """)
        return self._run(engine, sql, {"lid": foundry_line_id}).get("last_seen")

    def get_timestamps(
        self, engine: Engine, foundry_line_id: int, days: int = 365
    ) -> list[datetime]:
        sql = text("""
            SELECT `date` AS ts
            FROM `consumption`
            WHERE foundry_line_id = :lid
              AND deleted = 0
              AND `date` >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
            ORDER BY `date` ASC
        """)
        rows = self._run_many(engine, sql, {"lid": foundry_line_id, "days": days})
        return [r["ts"] for r in rows if r.get("ts")]


# ── Rejections adapter ────────────────────────────────────────────────────────

class RejectionsAdapter(SourceAdapter):
    """
    rejections — Periodic batch. Less time-sensitive.
    Large default window (24 hours).
    """

    source_name = "rejections"

    def check_presence(
        self, engine: Engine, foundry_line_id: int, window_minutes: int = 1440
    ) -> PresenceResult:
        sql = text("""
            SELECT COUNT(*) AS cnt, MAX(`date`) AS last_seen
            FROM `rejections`
            WHERE foundry_line_id = :lid
              AND `date` >= DATE_SUB(NOW(), INTERVAL :mins MINUTE)
        """)
        row = self._run(engine, sql, {"lid": foundry_line_id, "mins": window_minutes})
        return PresenceResult(
            source_name    = self.source_name,
            has_data       = (row.get("cnt") or 0) > 0,
            last_seen      = row.get("last_seen"),
            record_count   = int(row.get("cnt") or 0),
            window_minutes = window_minutes,
        )

    def get_last_seen(self, engine: Engine, foundry_line_id: int) -> Optional[datetime]:
        sql = text("""
            SELECT MAX(`date`) AS last_seen
            FROM `rejections`
            WHERE foundry_line_id = :lid
        """)
        return self._run(engine, sql, {"lid": foundry_line_id}).get("last_seen")

    def get_timestamps(
        self, engine: Engine, foundry_line_id: int, days: int = 365
    ) -> list[datetime]:
        sql = text("""
            SELECT `date` AS ts
            FROM `rejections`
            WHERE foundry_line_id = :lid
              AND `date` >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
            ORDER BY `date` ASC
        """)
        rows = self._run_many(engine, sql, {"lid": foundry_line_id, "days": days})
        return [r["ts"] for r in rows if r.get("ts")]


# ── Registry ──────────────────────────────────────────────────────────────────

SOURCE_ADAPTERS: dict[str, SourceAdapter] = {
    "scada"              : ScadaAdapter(),
    "additive"           : AdditiveAdapter(),
    "preparedsand"       : PreparedSandAdapter(),
    "preparedsand_extra" : PreparedSandExtraAdapter(),
    "consumption"        : ConsumptionAdapter(),
    "rejections"         : RejectionsAdapter(),
}

# Priority tiers — used by the cross-table inference engine
SOURCE_TIERS: dict[str, int] = {
    "scada"              : 1,   # Ground truth — machine state
    "additive"           : 2,   # PLC stream
    "preparedsand_extra" : 2,   # PLC stream
    "preparedsand"       : 3,   # Manual / lab
    "consumption"        : 3,   # Shift upload
    "rejections"         : 4,   # Batch / periodic
}

# Behaviour type per source — used by learner and monitor
SOURCE_BEHAVIOUR: dict[str, str] = {
    "scada"              : "continuous",
    "additive"           : "continuous",
    "preparedsand_extra" : "continuous",
    "preparedsand"       : "periodic_manual",
    "consumption"        : "shift_completion",
    "rejections"         : "event_driven",
}


def get_adapter(source_name: str) -> SourceAdapter:
    adapter = SOURCE_ADAPTERS.get(source_name)
    if not adapter:
        raise ValueError(f"No adapter registered for source: {source_name!r}")
    return adapter
