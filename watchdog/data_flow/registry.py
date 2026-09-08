"""
watchdog/data_flow/registry.py
-------------------------------
Source Registry — discovers which sources exist per foundry line
and manages the watchdog_source_registry table.

Discovery logic:
  For each foundry line × each known source:
    Query the last 30 days — if any records exist -> source is active.
    If no records exist -> source is NOT configured for this line -> skip.

This runs:
  - Once on first startup (auto-discovery)
  - Weekly to catch new sources being added
  - On demand via API or CLI
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .adapters import SOURCE_ADAPTERS, SOURCE_BEHAVIOUR, get_adapter

logger = logging.getLogger(__name__)

# Minimum records to consider a source "active"
MIN_RECORDS_FOR_ACTIVE = 5

# How many days to look back during discovery.
# Uses ALL historical data (unbounded) so sources that stopped months/years ago
# are still recognised and monitored — the monitor will classify them as "missing"
# based on how long ago data stopped.
DISCOVERY_LOOKBACK_DAYS = 3650  # 10 years — effectively "all data"


# ── Schema bootstrap ──────────────────────────────────────────────────────────

def ensure_schema(engine: Engine) -> None:
    """Create the data flow tables if they don't exist. Safe to call repeatedly."""
    import os
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    try:
        with open(schema_path, encoding="utf-8") as f:
            raw = f.read()
        # Strip comment lines before splitting to avoid partial statement issues
        lines = [l for l in raw.splitlines() if not l.strip().startswith("--")]
        sql = "\n".join(lines)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        created = 0
        for stmt in statements:
            if stmt.upper().startswith("CREATE"):
                try:
                    with engine.begin() as conn:
                        conn.execute(text(stmt))
                    created += 1
                except Exception as exc:
                    if "already exists" in str(exc).lower():
                        pass  # table already exists — fine
                    else:
                        logger.error("[data_flow] CREATE TABLE failed: %s", exc)
                        raise
        logger.info("[data_flow] schema ready (%d table(s) checked)", created)
    except Exception as exc:
        logger.error("[data_flow] schema creation failed: %s", exc)
        raise  # re-raise so bootstrap knows it failed


# ── Discovery ─────────────────────────────────────────────────────────────────

def discover_sources(engine: Engine, foundry_line_id: int) -> dict[str, dict]:
    """
    Query every known source table for this foundry line.
    Returns a dict of source_name -> discovery result.

    Example return:
      {
        'scada':    {'is_active': True,  'first_seen': ..., 'last_seen': ..., 'record_count': 8420},
        'additive': {'is_active': True,  'first_seen': ..., 'last_seen': ..., 'record_count': 1240},
        'sieves':   {'is_active': False, 'first_seen': None,'last_seen': None,'record_count': 0},
      }
    """
    results = {}
    for source_name, adapter in SOURCE_ADAPTERS.items():
        try:
            # Check presence over the last 30 days
            presence = adapter.check_presence(
                engine,
                foundry_line_id,
                window_minutes = DISCOVERY_LOOKBACK_DAYS * 24 * 60,
            )
            first_seen = _get_first_seen(engine, source_name, foundry_line_id)
            results[source_name] = {
                "is_active"    : presence.record_count >= MIN_RECORDS_FOR_ACTIVE,
                "first_seen"   : first_seen,
                "last_seen"    : presence.last_seen,
                "record_count" : presence.record_count,
            }
            status = "ACTIVE" if results[source_name]["is_active"] else "not found"
            logger.debug(
                "[data_flow][line=%d] %s -> %s (%d records)",
                foundry_line_id, source_name, status, presence.record_count
            )
        except Exception as exc:
            logger.warning(
                "[data_flow][line=%d] discovery failed for %s: %s",
                foundry_line_id, source_name, exc
            )
            results[source_name] = {
                "is_active": False, "first_seen": None,
                "last_seen": None,  "record_count": 0,
            }
    return results


def _get_first_seen(engine: Engine, source_name: str, foundry_line_id: int) -> Optional[datetime]:
    """Get the earliest record timestamp for this source/line (for context only)."""
    adapter = get_adapter(source_name)
    try:
        timestamps = adapter.get_timestamps(engine, foundry_line_id, days=3650)
        return timestamps[0] if timestamps else None
    except Exception:
        return None


# ── Registry CRUD ─────────────────────────────────────────────────────────────

def upsert_registry(engine: Engine, foundry_line_id: int, discovery: dict[str, dict]) -> None:
    """
    Write discovery results into watchdog_source_registry.
    Uses INSERT ... ON DUPLICATE KEY UPDATE so it's safe to run repeatedly.
    """
    sql = text("""
        INSERT INTO `watchdog_source_registry`
          (foundry_line_id, source_name, behaviour_type,
           is_active, discovery_mode, confidence_state,
           first_seen, last_seen)
        VALUES
          (:lid, :src, :btype,
           :active, 'auto', :cstate,
           :first_seen, :last_seen)
        ON DUPLICATE KEY UPDATE
          is_active       = IF(discovery_mode = 'manual', is_active, VALUES(is_active)),
          last_seen       = VALUES(last_seen),
          updated_at      = NOW()
    """)
    rows = []
    for source_name, result in discovery.items():
        rows.append({
            "lid"        : foundry_line_id,
            "src"        : source_name,
            "btype"      : SOURCE_BEHAVIOUR.get(source_name, "continuous"),
            "active"     : 1 if result["is_active"] else 0,
            "cstate"     : "learning" if result["is_active"] else "suspended",
            "first_seen" : result.get("first_seen"),
            "last_seen"  : result.get("last_seen"),
        })
    try:
        with engine.begin() as conn:
            for row in rows:
                conn.execute(sql, row)
        logger.info("[data_flow][line=%d] registry updated (%d sources)", foundry_line_id, len(rows))
    except Exception as exc:
        logger.error("[data_flow][line=%d] registry upsert failed: %s", foundry_line_id, exc)


def load_active_sources(engine: Engine, foundry_line_id: int) -> list[dict]:
    """
    Load all active (is_active=1) sources for a foundry line.
    Returns list of registry rows as dicts.
    """
    sql = text("""
        SELECT *
        FROM `watchdog_source_registry`
        WHERE foundry_line_id = :lid
          AND is_active = 1
        ORDER BY source_name
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"lid": foundry_line_id}).mappings().fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("[data_flow][line=%d] load_active_sources failed: %s", foundry_line_id, exc)
        return []


def load_all_active_lines(engine: Engine) -> list[dict]:
    """
    Load all unique foundry_line_ids that have at least one active source.
    Used by the monitor to know which lines to watch.
    """
    sql = text("""
        SELECT DISTINCT foundry_line_id
        FROM `watchdog_source_registry`
        WHERE is_active = 1
        ORDER BY foundry_line_id
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [r[0] for r in rows]
    except Exception as exc:
        logger.error("[data_flow] load_all_active_lines failed: %s", exc)
        return []


def mark_calibrated(engine: Engine, foundry_line_id: int, source_name: str) -> None:
    """Mark a source as fully calibrated — rhythm has been learned."""
    sql = text("""
        UPDATE `watchdog_source_registry`
        SET confidence_state = 'calibrated', updated_at = NOW()
        WHERE foundry_line_id = :lid AND source_name = :src
    """)
    try:
        with engine.begin() as conn:
            conn.execute(sql, {"lid": foundry_line_id, "src": source_name})
    except Exception as exc:
        logger.warning("[data_flow] mark_calibrated failed: %s", exc)


def run_discovery_for_all_lines(engine: Engine, registry_engine: Engine) -> None:
    """
    Discover sources for all known foundry lines.
    Reads foundry_line from the registry DB, queries each foundry DB.

    In a multi-foundry setup, engine = foundry DB, registry_engine = sandman_dev.
    For single-foundry setups they may be the same engine.
    """
    try:
        with registry_engine.connect() as conn:
            lines = conn.execute(text("""
                SELECT pkey, foundry_line_id
                FROM foundry_line
                WHERE is_active = 1
            """)).mappings().fetchall()
    except Exception as exc:
        logger.error("[data_flow] failed to load foundry lines: %s", exc)
        return

    for line in lines:
        line_id = line.get("pkey") or line.get("foundry_line_id")
        logger.info("[data_flow] discovering sources for line %d ...", line_id)
        discovery = discover_sources(engine, line_id)
        active = [s for s, r in discovery.items() if r["is_active"]]
        inactive = [s for s, r in discovery.items() if not r["is_active"]]
        logger.info(
            "[data_flow][line=%d] active: %s | not found: %s",
            line_id, active, inactive
        )
        upsert_registry(registry_engine, line_id, discovery)
