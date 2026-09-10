"""
watchdog/data_flow/learner.py
------------------------------
Rhythm Learner — learns normal data arrival patterns from history.

For each source per foundry line, computes:
  - Median gap (p50) between consecutive records
  - p90, p95, p99 gap
  - Records per hour
  - Longest normal gap (used as the alert threshold baseline)

Rules:
  - Need at least MIN_SAMPLE_COUNT records to learn
  - Exclude gaps larger than MAX_PLAUSIBLE_GAP_HOURS
    (those are outages, not normal behaviour)
  - Store results in watchdog_source_rhythm
  - Mark source as 'calibrated' in registry after learning
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .adapters import get_adapter
from .registry import mark_calibrated

logger = logging.getLogger(__name__)

# Minimum records needed to compute reliable rhythm
MIN_SAMPLE_COUNT = 20

# Gaps larger than this are considered outages, not normal (hours)
MAX_PLAUSIBLE_GAP_HOURS = 8

# Learning window (days)
LEARNING_DAYS = 90  # must match DISCOVERY_LOOKBACK_DAYS to learn from same window


# ── Core learning function ────────────────────────────────────────────────────

def learn_rhythm(
    engine: Engine,
    foundry_line_id: int,
    source_name: str,
    days: int = LEARNING_DAYS,
) -> Optional[dict]:
    """
    Learn the data arrival rhythm for one source on one line.

    Returns a rhythm dict or None if insufficient data.

    Rhythm dict keys:
      gap_p50_seconds, gap_p90_seconds, gap_p95_seconds, gap_p99_seconds,
      records_per_hour, sample_count, baseline_days
    """
    adapter = get_adapter(source_name)

    # Get all timestamps from the learning window
    try:
        timestamps = adapter.get_timestamps(engine, foundry_line_id, days=days)
    except Exception as exc:
        logger.warning(
            "[learner][line=%d][%s] get_timestamps failed: %s",
            foundry_line_id, source_name, exc
        )
        return None

    if len(timestamps) < MIN_SAMPLE_COUNT:
        logger.debug(
            "[learner][line=%d][%s] only %d records — need %d to learn",
            foundry_line_id, source_name, len(timestamps), MIN_SAMPLE_COUNT
        )
        return None

    # Sort and compute gaps between consecutive records
    timestamps = sorted([ts for ts in timestamps if ts is not None])
    max_gap_seconds = MAX_PLAUSIBLE_GAP_HOURS * 3600

    gaps = []
    for i in range(len(timestamps) - 1):
        delta = (timestamps[i + 1] - timestamps[i]).total_seconds()
        if 0 < delta < max_gap_seconds:
            gaps.append(delta)

    if len(gaps) < MIN_SAMPLE_COUNT:
        logger.debug(
            "[learner][line=%d][%s] only %d usable gaps after filtering",
            foundry_line_id, source_name, len(gaps)
        )
        return None

    gaps.sort()
    n = len(gaps)

    def percentile(p: float) -> float:
        idx = min(int(n * p), n - 1)
        return gaps[idx]

    total_hours = (timestamps[-1] - timestamps[0]).total_seconds() / 3600
    records_per_hour = len(timestamps) / total_hours if total_hours > 0 else 0

    rhythm = {
        "foundry_line_id" : foundry_line_id,
        "source_name"     : source_name,
        "gap_p50_seconds" : round(percentile(0.50), 1),
        "gap_p90_seconds" : round(percentile(0.90), 1),
        "gap_p95_seconds" : round(percentile(0.95), 1),
        "gap_p99_seconds" : round(percentile(0.99), 1),
        "records_per_hour": round(records_per_hour, 2),
        "sample_count"    : len(timestamps),
        "baseline_days"   : days,
        "baseline_start"  : timestamps[0],
        "baseline_end"    : timestamps[-1],
    }

    logger.info(
        "[learner][line=%d][%s] learned — p50=%.0fs  p95=%.0fs  p99=%.0fs  %.1f rec/hr",
        foundry_line_id, source_name,
        rhythm["gap_p50_seconds"], rhythm["gap_p95_seconds"],
        rhythm["gap_p99_seconds"], rhythm["records_per_hour"],
    )
    return rhythm


# ── Alert thresholds from rhythm ──────────────────────────────────────────────

def compute_thresholds(rhythm: dict, config: dict = None) -> dict:
    """
    Derive alert thresholds from learned rhythm.

    DELAYED  = gap > p99 × stale_mult  (default 1×)
    STALE    = gap > p99 × stale_mult  (default 5×)
    MISSING  = gap > p99 × missing_mult (default 20×)

    Multipliers are configurable via data_flow.delayed_mult / stale_mult / missing_mult
    in watchdog_si_config so they can be tuned per foundry without restarting.
    """
    df_cfg       = (config or {}).get("data_flow", {})
    delayed_mult = float(df_cfg.get("delayed_mult",  1))
    stale_mult   = float(df_cfg.get("stale_mult",    5))
    missing_mult = float(df_cfg.get("missing_mult", 20))
    p99 = rhythm["gap_p99_seconds"]
    return {
        "delayed_seconds" : p99 * delayed_mult,
        "stale_seconds"   : p99 * stale_mult,
        "missing_seconds" : p99 * missing_mult,
    }


# ── Persistence ───────────────────────────────────────────────────────────────

def save_rhythm(registry_engine: Engine, rhythm: dict) -> None:
    """Save learned rhythm to watchdog_source_rhythm table."""
    sql = text("""
        INSERT INTO `watchdog_source_rhythm`
          (foundry_line_id, source_name,
           gap_p50_seconds, gap_p90_seconds, gap_p95_seconds, gap_p99_seconds,
           records_per_hour, sample_count, baseline_days,
           baseline_start, baseline_end, learned_at)
        VALUES
          (:lid, :src,
           :p50, :p90, :p95, :p99,
           :rph, :cnt, :days,
           :bstart, :bend, NOW())
        ON DUPLICATE KEY UPDATE
          gap_p50_seconds   = VALUES(gap_p50_seconds),
          gap_p90_seconds   = VALUES(gap_p90_seconds),
          gap_p95_seconds   = VALUES(gap_p95_seconds),
          gap_p99_seconds   = VALUES(gap_p99_seconds),
          records_per_hour  = VALUES(records_per_hour),
          sample_count      = VALUES(sample_count),
          baseline_days     = VALUES(baseline_days),
          baseline_start    = VALUES(baseline_start),
          baseline_end      = VALUES(baseline_end),
          learned_at        = NOW()
    """)
    try:
        with registry_engine.begin() as conn:
            conn.execute(sql, {
                "lid"   : rhythm["foundry_line_id"],
                "src"   : rhythm["source_name"],
                "p50"   : rhythm["gap_p50_seconds"],
                "p90"   : rhythm["gap_p90_seconds"],
                "p95"   : rhythm["gap_p95_seconds"],
                "p99"   : rhythm["gap_p99_seconds"],
                "rph"   : rhythm["records_per_hour"],
                "cnt"   : rhythm["sample_count"],
                "days"  : rhythm["baseline_days"],
                "bstart": rhythm["baseline_start"],
                "bend"  : rhythm["baseline_end"],
            })
        logger.debug(
            "[learner][line=%d][%s] rhythm saved to DB",
            rhythm["foundry_line_id"], rhythm["source_name"]
        )
    except Exception as exc:
        logger.error("[learner] save_rhythm failed: %s", exc)


def load_rhythm(
    registry_engine: Engine, foundry_line_id: int, source_name: str
) -> Optional[dict]:
    """Load previously learned rhythm from DB. Returns None if not yet learned."""
    sql = text("""
        SELECT *
        FROM `watchdog_source_rhythm`
        WHERE foundry_line_id = :lid AND source_name = :src
    """)
    try:
        with registry_engine.connect() as conn:
            row = conn.execute(sql, {"lid": foundry_line_id, "src": source_name}).mappings().first()
        return dict(row) if row else None
    except Exception as exc:
        logger.warning("[learner] load_rhythm failed: %s", exc)
        return None


# ── Convenience: learn + save + mark calibrated ───────────────────────────────

def learn_and_save(
    foundry_engine: Engine,
    registry_engine: Engine,
    foundry_line_id: int,
    source_name: str,
    days: int = LEARNING_DAYS,
) -> Optional[dict]:
    """
    Full workflow: learn rhythm from foundry DB, save to registry DB,
    mark source as calibrated. Returns rhythm dict or None.
    """
    rhythm = learn_rhythm(foundry_engine, foundry_line_id, source_name, days=days)
    if rhythm:
        save_rhythm(registry_engine, rhythm)
        mark_calibrated(registry_engine, foundry_line_id, source_name)
    return rhythm


def learn_all_sources(
    foundry_engine: Engine,
    registry_engine: Engine,
    foundry_line_id: int,
) -> dict[str, Optional[dict]]:
    """Learn rhythm for all active sources on a line."""
    from .registry import load_active_sources
    active = load_active_sources(registry_engine, foundry_line_id)
    results = {}
    for row in active:
        src = row["source_name"]
        results[src] = learn_and_save(
            foundry_engine, registry_engine, foundry_line_id, src
        )
    return results
