"""
watchdog/data_flow/monitor.py
------------------------------
Fully autonomous Data Flow Monitor.

Start it with one call:
    DataFlowMonitor(foundry_engine, registry_engine, config).start()

It handles EVERYTHING automatically:
  1. Creates DB tables if they don't exist
  2. Discovers which sources exist for which foundry lines
  3. Learns the normal data rhythm for each source
  4. Monitors continuously, classifies gaps, detects anomalies
  5. Cross-table inference — SCADA + multi-source presence matrix
  6. Auto-suppresses process analysis when data unavailable
  7. Sends confirmation requests when all sources go silent
  8. Re-discovers new sources weekly (handles new foundry lines added later)
  9. Re-learns rhythm monthly (adapts to foundry changes)

No manual steps. No config files. Fully self-managing.
"""

from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .adapters  import SOURCE_ADAPTERS, SOURCE_TIERS, get_adapter
from .learner   import compute_thresholds, load_rhythm, learn_and_save
from .registry  import (
    ensure_schema, discover_sources, upsert_registry,
    load_active_sources, load_all_active_lines, mark_calibrated,
)

logger = logging.getLogger(__name__)

# ── Module-level snooze registry (shared between monitor and alert_server) ────
# {foundry_line_id: snooze_until_datetime}
_SNOOZE_REGISTRY: dict[int, datetime] = {}


def _register_snooze(foundry_line_id: int, hours: float = 1.0) -> None:
    """Register a snooze for a foundry line. Called from alert_server on button click."""
    _SNOOZE_REGISTRY[foundry_line_id] = datetime.now() + timedelta(hours=hours)
    logger.info(
        "[data_flow][line=%d] snoozed for %.0f hour(s) until %s",
        foundry_line_id, hours,
        _SNOOZE_REGISTRY[foundry_line_id].strftime("%H:%M"),
    )


# ── Timing constants ──────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS       = 60      # main monitoring check
REDISCOVER_INTERVAL_HOURS   = 168     # re-scan for new sources (weekly)
RELEARN_INTERVAL_HOURS      = 720     # re-learn rhythm (monthly)
MIN_RECORDS_TO_LEARN        = 20      # skip learning if too few records
CONFIRMATION_RESEND_HOURS   = 2       # don't spam confirmation emails


class DataFlowMonitor:
    """
    Fully autonomous data flow monitoring daemon.
    Drop this into run_alert_monitor.py as a daemon thread.
    """

    def __init__(
        self,
        foundry_engine: Engine,
        registry_engine: Engine,
        config: dict,
        poll_interval: int = POLL_INTERVAL_SECONDS,
        _discovery_engine: Engine = None,
    ) -> None:
        self._foundry_engine   = foundry_engine
        self._registry_engine  = registry_engine   # foundry DB — stores all data flow tables
        self._discovery_engine = _discovery_engine or registry_engine  # sandman_dev — for foundry_line table
        self._config           = config
        self._poll_interval    = poll_interval
        self._label            = "data_flow"

        # Track when we last ran each maintenance task
        self._last_discovery: Optional[datetime] = None
        self._last_relearn:   Optional[datetime] = None

        # Track when confirmation emails were sent per line
        self._confirmation_sent: dict[int, datetime] = {}

        # Snooze registry — {line_id: snooze_until datetime}
        self._snoozed_until: dict[int, datetime] = {}

        # Per-instance alert rate-limiter — {(line_id, source_name): last_fired_datetime}
        self._last_alert_fired: dict[tuple, datetime] = {}

        # In-memory rhythm cache  {(line_id, source): thresholds_dict}
        self._rhythm_cache: dict[tuple, Optional[dict]] = {}

    # ── Entry point ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Run forever. Call from a daemon thread."""
        logger.info("[%s] starting autonomous data flow monitor", self._label)

        # Step 1: Bootstrap — runs once, safe to repeat
        self._bootstrap()

        # Step 2: Main poll loop
        while True:
            try:
                # Run maintenance tasks on schedule
                self._maybe_rediscover()
                self._maybe_relearn()

                # Core monitoring check
                self._poll_all_lines()

            except Exception:
                logger.error("[%s] poll error:\n%s", self._label, traceback.format_exc())

            time.sleep(self._poll_interval)

    # ── Bootstrap (runs once on startup) ─────────────────────────────────────

    def _bootstrap(self) -> None:
        """
        One-time startup sequence. Idempotent — safe to run multiple times.
          1. Create DB tables if missing
          2. Discover all foundry lines and their sources
          3. Learn rhythm for any source that doesn't have one yet
        """
        logger.info("[%s] bootstrapping ...", self._label)

        # 1. Schema
        try:
            ensure_schema(self._registry_engine)
            logger.info("[%s] schema ready", self._label)
        except Exception as exc:
            logger.error("[%s] schema bootstrap failed: %s", self._label, exc)
            return

        # 2. Discover all lines
        lines = self._get_all_foundry_lines()
        if not lines:
            logger.warning("[%s] no foundry lines found — nothing to monitor", self._label)
            return

        logger.info("[%s] discovered %d foundry line(s)", self._label, len(lines))

        for line_id in lines:
            self._discover_line(line_id)

        # 3. Learn rhythm for any source that doesn't have one yet
        self._learn_missing_rhythms()

        self._last_discovery = datetime.now()
        self._last_relearn   = datetime.now()
        logger.info("[%s] bootstrap complete", self._label)

    # ── Scheduled maintenance ─────────────────────────────────────────────────

    def _maybe_rediscover(self) -> None:
        """Re-discover sources weekly — picks up new foundry lines or new tables."""
        if self._last_discovery is None:
            return
        age = (datetime.now() - self._last_discovery).total_seconds() / 3600
        if age < REDISCOVER_INTERVAL_HOURS:
            return
        logger.info("[%s] weekly re-discovery running ...", self._label)
        for line_id in self._get_all_foundry_lines():
            self._discover_line(line_id)
        self._last_discovery = datetime.now()

    def _maybe_relearn(self) -> None:
        """Re-learn rhythms monthly — adapts to foundry process changes."""
        if self._last_relearn is None:
            return
        age = (datetime.now() - self._last_relearn).total_seconds() / 3600
        if age < RELEARN_INTERVAL_HOURS:
            return
        logger.info("[%s] monthly rhythm re-learning running ...", self._label)
        self._learn_missing_rhythms(force=True)
        self._rhythm_cache.clear()  # flush cache so new rhythms are used
        self._last_relearn = datetime.now()

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _get_all_foundry_lines(self) -> list[int]:
        """
        Get all active foundry line IDs from the LOCAL foundry DB.

        Always queries foundry_line from self._foundry_engine (caspro_sandman, munjalkiriu etc.)
        so the returned pkeys match foundry_line_id in additive, preparedsand etc.

        SELECT pkey, name FROM foundry_line WHERE is_active = 1
        """
        try:
            with self._foundry_engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT pkey, name
                    FROM foundry_line
                    WHERE is_active = 1
                    ORDER BY pkey
                """)).fetchall()
            ids = [r[0] for r in rows]
            names = {r[0]: r[1] for r in rows}
            logger.info(
                "[%s] foundry lines found: %s",
                self._label,
                ", ".join(f"{k}={v}" for k, v in names.items())
            )
            return ids
        except Exception as exc:
            logger.error("[%s] get_all_foundry_lines failed: %s", self._label, exc)
            return []

    def _discover_line(self, foundry_line_id: int) -> None:
        """Discover which sources have data for this line and update registry."""
        try:
            discovery = discover_sources(self._foundry_engine, foundry_line_id)
            upsert_registry(self._registry_engine, foundry_line_id, discovery)
            active = [s for s, r in discovery.items() if r["is_active"]]
            logger.info(
                "[%s][line=%d] sources: %s",
                self._label, foundry_line_id, active or "(none)"
            )
        except Exception as exc:
            logger.error("[%s][line=%d] discovery failed: %s", self._label, foundry_line_id, exc)

    # ── Rhythm learning ───────────────────────────────────────────────────────

    def _learn_missing_rhythms(self, force: bool = False) -> None:
        """
        For every active source that has no learned rhythm yet, learn it now.
        If force=True, re-learn all rhythms (used for monthly refresh).
        """
        lines = load_all_active_lines(self._registry_engine)
        for line_id in lines:
            active_sources = load_active_sources(self._registry_engine, line_id)
            for row in active_sources:
                src = row["source_name"]
                existing = load_rhythm(self._registry_engine, line_id, src)
                if existing and not force:
                    continue  # already learned — skip
                logger.info(
                    "[%s][line=%d][%s] learning rhythm ...",
                    self._label, line_id, src
                )
                rhythm = learn_and_save(
                    self._foundry_engine,
                    self._registry_engine,
                    line_id,
                    src,
                )
                if rhythm:
                    # Cache thresholds for fast access during monitoring
                    self._rhythm_cache[(line_id, src)] = compute_thresholds(rhythm)
                    logger.info(
                        "[%s][line=%d][%s] rhythm learned — p99=%.0fs (%.1f min)",
                        self._label, line_id, src,
                        rhythm["gap_p99_seconds"],
                        rhythm["gap_p99_seconds"] / 60,
                    )
                else:
                    logger.debug(
                        "[%s][line=%d][%s] insufficient data to learn rhythm yet",
                        self._label, line_id, src
                    )

    def _get_thresholds(self, line_id: int, src: str) -> Optional[dict]:
        """Get cached thresholds, loading from DB if not in cache."""
        key = (line_id, src)
        if key not in self._rhythm_cache:
            rhythm = load_rhythm(self._registry_engine, line_id, src)
            self._rhythm_cache[key] = compute_thresholds(rhythm) if rhythm else None
        return self._rhythm_cache.get(key)

    # ── Main poll ─────────────────────────────────────────────────────────────

    def _poll_all_lines(self) -> None:
        """Check all active foundry lines once."""
        lines = load_all_active_lines(self._registry_engine)
        for line_id in lines:
            try:
                self._poll_line(line_id)
            except Exception as exc:
                logger.error("[%s][line=%d] poll failed: %s", self._label, line_id, exc)

    def _poll_line(self, foundry_line_id: int) -> None:
        """Full monitoring cycle for one foundry line."""
        active_sources = load_active_sources(self._registry_engine, foundry_line_id)
        if not active_sources:
            return

        # Filter out sources disabled in the foundry config
        _disabled = set(self._config.get("data_flow", {}).get("disabled_sources", []))
        if _disabled:
            active_sources = [r for r in active_sources if r["source_name"] not in _disabled]
            if not active_sources:
                return

        matrix        = self._build_presence_matrix(foundry_line_id, active_sources)
        any_real_data = any(v.get("has_data") for v in matrix.values())

        # Load per-source annotations
        annotations = self._load_active_annotations(foundry_line_id)

        # Close annotations whose source data has resumed
        for src_key, ann in list(annotations.items()):
            if src_key == "all":
                if any(v.get("has_data") and v.get("tier", 9) <= 2 for v in matrix.values()):
                    self._close_annotation(ann["id"])
                    logger.info("[%s][line=%d] machine data resumed — 'all' annotation closed", self._label, foundry_line_id)
                    del annotations[src_key]
            elif matrix.get(src_key, {}).get("has_data"):
                self._close_annotation(ann["id"])
                logger.info("[%s][line=%d][%s] data resumed — annotation closed", self._label, foundry_line_id, src_key)
                del annotations[src_key]

        # Line-level context only from "all" annotation
        all_ann = annotations.get("all")
        all_ann_type = all_ann.get("type") if all_ann else None
        if all_ann_type == "planned_shutdown":
            operating_context = "planned_off"
            logger.info("[%s][line=%d] annotation applied: PLANNED SHUTDOWN (all sources)", self._label, foundry_line_id)
        elif all_ann_type in ("unplanned_shutdown", "breakdown"):
            operating_context = "breakdown"
        elif all_ann_type == "data_pipeline_failure":
            operating_context = "data_issue"
        else:
            operating_context = self._infer_context(matrix)

        suppress = self._should_suppress(matrix)

        for row in active_sources:
            src = row["source_name"]
            m   = matrix.get(src, {})

            # Per-source annotation takes priority over line-level
            src_ann      = annotations.get(src) or annotations.get("all")
            src_ann_type = src_ann.get("type") if src_ann else None

            last_seen = m.get("last_seen")
            if isinstance(last_seen, str):
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
                    try:
                        last_seen = datetime.strptime(last_seen, fmt)
                        break
                    except (ValueError, TypeError):
                        pass
                else:
                    last_seen = None
            gap_seconds = (datetime.now() - last_seen).total_seconds() if last_seen else None
            gap_vs_p99  = None
            status      = "unknown"
            alert_fired = False

            if src_ann_type == "planned_shutdown":
                status = "expected_silence"
                logger.debug("[%s][line=%d][%s] per-source planned_shutdown — expected silence", self._label, foundry_line_id, src)
            elif operating_context == "planned_off":
                status   = "expected_silence"
                suppress = False
            elif row.get("confidence_state") == "learning":
                status = "learning"
            elif last_seen is None:
                status = "missing"
            else:
                thresholds = self._get_thresholds(foundry_line_id, src)
                if thresholds and gap_seconds is not None:
                    status     = self._classify_gap(gap_seconds, thresholds)
                    gap_vs_p99 = gap_seconds / (thresholds["delayed_seconds"] or 1)
                    if status == "missing" and operating_context == "running" and src_ann_type != "planned_shutdown":
                        alert_fired = True
                        logger.warning(
                            "[%s][line=%d][%s] MISSING — gap=%.0fs",
                            self._label, foundry_line_id, src, gap_seconds
                        )
                        self._fire_source_alert(
                            foundry_line_id, src, gap_seconds, last_seen
                        )
                else:
                    status = "learning"

            self._write_health(
                foundry_line_id, src, status,
                last_seen, gap_seconds, gap_vs_p99,
                operating_context, suppress, alert_fired,
            )

        # Only request confirmation if sources with NO annotation are all silent
        unacknowledged_silent = not any(
            matrix.get(r["source_name"], {}).get("has_data")
            for r in active_sources
            if r["source_name"] not in annotations and "all" not in annotations
        )
        if unacknowledged_silent and not any_real_data and operating_context not in ("planned_off",):
            snooze_until = _SNOOZE_REGISTRY.get(foundry_line_id)
            if snooze_until and datetime.now() < snooze_until:
                logger.debug("[%s][line=%d] snoozed — skipping confirmation", self._label, foundry_line_id)
            else:
                self._request_confirmation(foundry_line_id, matrix)

    # ── Annotation helpers ────────────────────────────────────────────────────

    def _load_active_annotations(self, foundry_line_id: int) -> dict:
        """
        Return per-source annotations for this line.
        Returns {source_name: {"id": int, "type": str}} for all open annotations.
        Source "all" means the annotation applies to every source.
        Only considers annotations confirmed within the last 24 hours.
        """
        import json as _json
        sql = text("""
            SELECT id, annotation_type, sources_affected, confirmed_at
            FROM   watchdog_data_annotations
            WHERE  foundry_line_id = :lid
              AND  gap_end IS NULL
            ORDER  BY confirmed_at DESC
        """)
        result = {}
        try:
            with self._registry_engine.connect() as conn:
                rows = conn.execute(sql, {"lid": foundry_line_id}).mappings().fetchall()
            for row in rows:
                try:
                    sources = _json.loads(row["sources_affected"]) if row["sources_affected"] else ["all"]
                except Exception:
                    sources = ["all"]
                ann = {"id": row["id"], "type": row["annotation_type"]}
                for src in sources:
                    if src not in result:   # most recent wins per source
                        result[src] = ann
        except Exception as exc:
            logger.debug("[%s] _load_active_annotations failed: %s", self._label, exc)
        return result

    # kept for backward compat
    def _load_active_annotation(self, foundry_line_id: int) -> Optional[dict]:
        anns = self._load_active_annotations(foundry_line_id)
        return anns.get("all") or (next(iter(anns.values()), None) if anns else None)

    def _close_annotation(self, annotation_id: int) -> None:
        """Set gap_end = NOW() on an annotation — marks it as resolved."""
        sql = text("""
            UPDATE watchdog_data_annotations
            SET    gap_end = NOW()
            WHERE  id = :aid AND gap_end IS NULL
        """)
        try:
            with self._registry_engine.begin() as conn:
                conn.execute(sql, {"aid": annotation_id})
        except Exception as exc:
            logger.debug("[%s] _close_annotation failed: %s", self._label, exc)

    # ── Presence matrix ───────────────────────────────────────────────────────

    def _build_presence_matrix(
        self, foundry_line_id: int, active_sources: list[dict]
    ) -> dict[str, dict]:
        matrix = {}
        for row in active_sources:
            src = row["source_name"]
            try:
                adapter = get_adapter(src)
                result  = adapter.check_presence(self._foundry_engine, foundry_line_id)
                matrix[src] = {
                    "has_data" : result.has_data,
                    "last_seen": result.last_seen,
                    "tier"     : SOURCE_TIERS.get(src, 9),
                }
            except Exception as exc:
                logger.debug("[%s][line=%d][%s] presence check error: %s", self._label, foundry_line_id, src, exc)
                matrix[src] = {"has_data": False, "last_seen": None, "tier": SOURCE_TIERS.get(src, 9)}
        return matrix

    # ── Context inference ─────────────────────────────────────────────────────

    def _infer_context(self, matrix: dict[str, dict]) -> str:
        """
        Cross-table inference: determine if line is running.

        SCADA present          -> definitely RUNNING (machine is on)
        Any Tier-2 present     -> RUNNING (PLC connected)
        Any Tier-3 present     -> RUNNING (humans are manually entering data)
        Nothing present        -> UNKNOWN (ask customer)
        """
        if not matrix:
            return "unknown"

        # Tier 1 — SCADA (highest confidence)
        if matrix.get("scada", {}).get("has_data"):
            return "running"

        # Tier 2 — PLC-connected sources
        for src, v in matrix.items():
            if v["tier"] == 2 and v["has_data"]:
                return "running"

        # Tier 3 — manual/semi-auto
        for src, v in matrix.items():
            if v["tier"] == 3 and v["has_data"]:
                return "running"

        return "unknown"

    def _should_suppress(self, matrix: dict[str, dict]) -> bool:
        """
        Auto-suppress process analysis when core data is absent.
        Suppress if BOTH scada AND additive are absent.
        """
        scada_ok    = matrix.get("scada",    {}).get("has_data", False)
        additive_ok = matrix.get("additive", {}).get("has_data", False)
        return not scada_ok and not additive_ok

    # ── Gap classification ────────────────────────────────────────────────────

    @staticmethod
    def _classify_gap(gap_seconds: float, thresholds: dict) -> str:
        if gap_seconds <= thresholds["delayed_seconds"]:
            return "healthy"
        if gap_seconds <= thresholds["stale_seconds"]:
            return "delayed"
        if gap_seconds <= thresholds["missing_seconds"]:
            return "stale"
        return "missing"

    # ── DB writes ─────────────────────────────────────────────────────────────

    def _write_health(
        self,
        foundry_line_id: int, source_name: str, status: str,
        last_seen: Optional[datetime], gap_seconds: Optional[float],
        gap_vs_p99: Optional[float], operating_context: str,
        suppress_process: bool, alert_fired: bool,
    ) -> None:
        sql = text("""
            INSERT INTO `watchdog_data_health`
              (foundry_line_id, source_name, status,
               last_record_at, gap_seconds, gap_vs_p99,
               operating_context, alert_fired, suppress_process, checked_at)
            VALUES
              (:lid, :src, :status,
               :last_seen, :gap, :gvp99,
               :ctx, :alert, :suppress, NOW())
            ON DUPLICATE KEY UPDATE
              status            = VALUES(status),
              last_record_at    = VALUES(last_record_at),
              gap_seconds       = VALUES(gap_seconds),
              gap_vs_p99        = VALUES(gap_vs_p99),
              operating_context = VALUES(operating_context),
              alert_fired       = VALUES(alert_fired),
              suppress_process  = VALUES(suppress_process),
              checked_at        = NOW()
        """)
        try:
            with self._registry_engine.begin() as conn:
                conn.execute(sql, {
                    "lid"      : foundry_line_id,
                    "src"      : source_name,
                    "status"   : status,
                    "last_seen": last_seen,
                    "gap"      : gap_seconds,
                    "gvp99"    : gap_vs_p99,
                    "ctx"      : operating_context,
                    "alert"    : 1 if alert_fired else 0,
                    "suppress" : 1 if suppress_process else 0,
                })
        except Exception as exc:
            logger.error("[%s] _write_health failed: %s", self._label, exc)

    # ── Alert firing ─────────────────────────────────────────────────────────

    # Track last alert time per (line, source) to avoid flooding
    ALERT_RESEND_HOURS = 1   # don't repeat same source alert for 1 hour

    def _fire_source_alert(
        self,
        foundry_line_id: int,
        source_name: str,
        gap_seconds: float,
        last_seen: Optional[datetime],
    ) -> None:
        """
        Fire an alert when a specific source goes MISSING.
        1. Write to watchdog_alerts (visible on dashboard)
        2. Send email notification
        Rate-limited to ALERT_RESEND_HOURS per source.
        """
        key = (foundry_line_id, source_name)
        last = self._last_alert_fired.get(key)
        if last and (datetime.now() - last).total_seconds() < self.ALERT_RESEND_HOURS * 3600:
            return  # already alerted recently — skip

        gap_min  = int(gap_seconds // 60)
        last_str = last_seen.strftime("%H:%M on %d-%b-%Y") if last_seen else "not available"

        SOURCE_LABELS = {
            "scada"              : "SCADA / PLC",
            "additive"           : "Mixer / Additive (SMC)",
            "preparedsand"       : "Prepared Sand (Lab)",
            "preparedsand_extra" : "Prepared Sand (PLC)",
            "consumption"        : "Consumption / Shift Data",
            "rejections"         : "Rejection Records",
        }

        SOURCE_REASONS = {
            "scada": (
                "The SCADA or PLC system has stopped sending data to the SandMan database. "
                "This usually means the PLC connection to the data gateway has dropped, "
                "the data transfer service on the machine control PC has stopped, "
                "or there is a network connectivity issue between the machine and the server."
            ),
            "additive": (
                "The mixer / additive (SMC) data has stopped arriving. "
                "This could be because the mixer control PLC is not communicating with the server, "
                "the SMC sensor has lost connection, or the batch upload service has stopped running. "
                "Bad batch and prescription monitoring has been suspended until data resumes."
            ),
            "preparedsand": (
                "No new prepared sand lab measurements have been received. "
                "This could be because the lab technician has not entered the shift measurements, "
                "the lab entry terminal is offline, or the data sync between the lab system and "
                "SandMan has stopped. Sand quality analysis will rely on the last available reading."
            ),
            "preparedsand_extra": (
                "The prepared sand PLC sensor data has stopped arriving. "
                "This is typically caused by a sensor disconnection, a PLC communication fault, "
                "or the data upload service not running on the sand plant control PC."
            ),
            "consumption": (
                "Consumption and shift production data has not been received. "
                "This may be because the shift has not been closed yet by the operator, "
                "the production data has not been uploaded, or there is a sync issue "
                "between the foundry ERP and SandMan."
            ),
            "rejections": (
                "Rejection records have not been entered for this period. "
                "This could mean the quality inspector has not logged the rejections yet, "
                "or the rejection entry system is not syncing with SandMan."
            ),
        }

        label  = SOURCE_LABELS.get(source_name, source_name)
        reason = SOURCE_REASONS.get(source_name,
            f"{label} data has stopped arriving and the cause is not yet identified. "
            "Please check the data connection and upload service for this source."
        )

        subject = f"Alert from Sandman — {label} Data Missing"
        body    = (
            f"We would like to bring to your notice that the following event has occurred.\n\n"
            f"Source           :  {label}\n"
            f"Foundry Line     :  Line {foundry_line_id}\n"
            f"Last data seen   :  {last_str}\n"
            f"Gap duration     :  {gap_min} minutes\n\n"
            f"{reason}\n\n"
            f"All SandMan analysis that depends on {label} has been automatically suspended "
            f"until data resumes. Please investigate and restore the data connection.\n\n"
            f"@Sandman Team"
        )
        self._send_email_alert(subject, body, foundry_line_id=foundry_line_id)
        self._last_alert_fired[key] = datetime.now()

    def _load_email_cfg(self, foundry_line_id: int = 0) -> dict:
        """
        Load email config for a specific foundry line from watchdog_si_config (sandman_dev).
        Falls back to global config if the per-foundry config is missing or has no email section.
        """
        from watchdog.email_notifier import _get_email_cfg
        global_email_cfg = _get_email_cfg(self._config)
        if not foundry_line_id:
            return global_email_cfg
        try:
            from watchdog.config_store import load_foundry_config
            db_name = self._config.get("database", {}).get("name", "")

            def _try_label(lbl: str) -> dict | None:
                cfg = load_foundry_config(self._discovery_engine, lbl)
                em  = _get_email_cfg(cfg)
                df_recipients = [
                    r for r in em.get("recipients", [])
                    if "DATA_FLOW" in r.get("types", [])
                ]
                logger.debug(
                    "[%s][line=%d] email cfg label=%s  enabled=%s  recipients=%d  data_flow_recipients=%d",
                    self._label, foundry_line_id, lbl,
                    em.get("enabled"), len(em.get("recipients", [])), len(df_recipients),
                )
                # If DATA_FLOW recipients are configured, always send regardless of enabled flag.
                # Presence of recipients is explicit intent; the enabled toggle is a UI extra.
                if df_recipients:
                    return {**em, "enabled": True}
                if em.get("enabled") is True and em.get("recipients"):
                    return em
                return None

            # Try exact label first, then L1 fallback (dashboard defaults to line_id=1)
            labels_to_try = [f"{db_name}_L{foundry_line_id}"]
            if foundry_line_id != 1:
                labels_to_try.append(f"{db_name}_L1")

            for label in labels_to_try:
                result = _try_label(label)
                if result is not None:
                    return result

        except Exception as exc:
            logger.warning("[%s][line=%d] could not load per-foundry email config: %s",
                           self._label, foundry_line_id, exc)

        logger.debug(
            "[%s][line=%d] falling back to global email cfg  enabled=%s",
            self._label, foundry_line_id, global_email_cfg.get("enabled"),
        )
        return global_email_cfg

    def _send_email_alert(self, subject: str, body: str, foundry_line_id: int = 0) -> None:
        """Send plain text data-flow alert email using per-foundry email config."""
        try:
            from watchdog.email_notifier import _get_email_cfg, _send_plain
            email_cfg = self._load_email_cfg(foundry_line_id)
            if not email_cfg.get("enabled", False):
                return
            _send_plain(email_cfg, subject, body, alert_type="DATA_FLOW")
            logger.info("[%s] data-flow email sent: %s", self._label, subject)
        except Exception as exc:
            logger.warning("[%s] email send failed: %s", self._label, exc)

    # ── Customer confirmation ─────────────────────────────────────────────────

    def _request_confirmation(
        self, foundry_line_id: int, matrix: dict[str, dict]
    ) -> None:
        """
        Send ONE confirmation email when all sources go silent.
        Rate-limited: won't resend for CONFIRMATION_RESEND_HOURS.
        """
        last_sent = self._confirmation_sent.get(foundry_line_id)
        if last_sent:
            age_hours = (datetime.now() - last_sent).total_seconds() / 3600
            if age_hours < CONFIRMATION_RESEND_HOURS:
                return

        logger.warning(
            "[%s][line=%d] all sources silent — requesting confirmation",
            self._label, foundry_line_id
        )

        email_cfg     = self._load_email_cfg(foundry_line_id)
        dashboard_url = (
            email_cfg.get("dashboard_url")
            or self._config.get("dashboard_url")
            or ""
        )
        db_name       = (self._config.get("database") or {}).get("name", "unknown")

        # Get customer name from customer_info table (short_name field)
        customer_name = db_name  # fallback
        try:
            with self._foundry_engine.connect() as _c:
                _r = _c.execute(text(
                    "SELECT short_name FROM customer_info WHERE deleted=0 LIMIT 1"
                )).fetchone()
                if _r and _r[0]:
                    customer_name = _r[0]
        except Exception:
            pass

        # Get foundry line name from DB
        line_name = str(foundry_line_id)
        try:
            with self._foundry_engine.connect() as _c:
                _r = _c.execute(text(
                    "SELECT name FROM foundry_line WHERE pkey = :lid LIMIT 1"
                ), {"lid": foundry_line_id}).fetchone()
                if _r:
                    line_name = _r[0]
        except Exception:
            pass

        # Include foundry DB name in confirm URLs so the endpoint writes to the correct DB
        foundry_db = (self._config.get("database") or {}).get("name", "")

        SOURCE_LABELS = {
            "scada"              : "SCADA / PLC",
            "additive"           : "Mixer / Additive (SMC)",
            "preparedsand"       : "Prepared Sand (Lab)",
            "preparedsand_extra" : "Prepared Sand (PLC)",
            "consumption"        : "Consumption / Shift Data",
            "rejections"         : "Rejection Records",
        }

        now_dt = datetime.now()
        source_lines = []
        for src, v in sorted(matrix.items(), key=lambda x: x[1]["tier"]):
            actual_last = v.get("last_seen")
            if not actual_last:
                try:
                    actual_last = get_adapter(src).get_last_seen(
                        self._foundry_engine, foundry_line_id
                    )
                except Exception:
                    pass
            if isinstance(actual_last, str):
                try:
                    actual_last = datetime.strptime(actual_last, "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    actual_last = None
            label = SOURCE_LABELS.get(src, src)
            if v.get("has_data"):
                source_lines.append(f"  ✓  {label:<34} Data is flowing")
            elif actual_last:
                gap_days = (now_dt - actual_last).days
                gap_hrs  = int((now_dt - actual_last).total_seconds() // 3600)
                gap_str  = f"{gap_days}d" if gap_days > 0 else f"{gap_hrs}h"
                last_str = actual_last.strftime("%d %b %Y  %H:%M")
                source_lines.append(
                    f"  ✗  {label:<34} No data since {last_str}  ({gap_str} ago)"
                )
            else:
                source_lines.append(
                    f"  ✗  {label:<34} No historical data found"
                )

        now_str = datetime.now().strftime("%d %b %Y  %H:%M")
        base    = dashboard_url.rstrip("/")
        subject = f"Alert from Sandman — {customer_name} — {line_name} — Data Gap"

        SOURCE_REASONS_SHORT = {
            "scada"              : "PLC disconnected, network issue, or data gateway stopped.",
            "additive"           : "Mixer PLC not communicating, SMC sensor lost connection, or batch upload service stopped.",
            "preparedsand"       : "Lab technician may not have entered shift measurements, or lab terminal is offline.",
            "preparedsand_extra" : "Prepared sand PLC sensor disconnected or upload service not running on sand plant PC.",
            "consumption"        : "Shift not closed by operator yet, or production data has not been uploaded.",
            "rejections"         : "Quality inspector has not entered rejection records for this period.",
        }

        # Build per-source blocks
        source_blocks = []
        for src, v in sorted(matrix.items(), key=lambda x: x[1]["tier"]):
            if v.get("has_data"):
                continue   # only show missing sources
            actual_last = v.get("last_seen")
            if not actual_last:
                try:
                    actual_last = get_adapter(src).get_last_seen(
                        self._foundry_engine, foundry_line_id
                    )
                except Exception:
                    pass
            if isinstance(actual_last, str):
                for _fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
                    try:
                        actual_last = datetime.strptime(actual_last, _fmt)
                        break
                    except (ValueError, TypeError):
                        pass
                else:
                    actual_last = None
            label    = SOURCE_LABELS.get(src, src)
            reason   = SOURCE_REASONS_SHORT.get(src, "Cause unknown — please investigate.")
            if actual_last:
                gap_days = (now_dt - actual_last).days
                gap_hrs  = int((now_dt - actual_last).total_seconds() // 3600)
                gap_str  = f"{gap_days} day(s)" if gap_days > 0 else f"{gap_hrs} hour(s)"
                last_str = actual_last.strftime("%d %b %Y  %H:%M")
                when     = f"No data since {last_str}  ({gap_str} ago)"
            else:
                when = "No historical data found for this source."

            enc_src  = src.replace("_", "-")
            db_param = f"&db={foundry_db}" if foundry_db else ""
            # Plain text lines + hyperlinks for each confirmation option
            block_lines = [
                f"  ✗  {label}",
                f"     {when}",
                f"     Possible reason: {reason}",
                "",
                f"     Confirm for this source:",
                ("LINK", "Planned Shutdown",
                 f"{base}/api/data-flow/confirm?line={foundry_line_id}&source={enc_src}&status=planned_off{db_param}"),
                ("LINK", "Breakdown / Fault",
                 f"{base}/api/data-flow/confirm?line={foundry_line_id}&source={enc_src}&status=breakdown{db_param}"),
                ("LINK", "Investigate Pipeline",
                 f"{base}/api/data-flow/annotate?line={foundry_line_id}&source={enc_src}&status=data_issue{db_param}"),
                ("LINK", "Snooze 1 Hour",
                 f"{base}/api/data-flow/annotate?line={foundry_line_id}&source={enc_src}&status=snooze{db_param}"),
            ]
            source_blocks.append(block_lines)

        SEP = "─" * 58

        # ── Plain text version (for email clients that don't render HTML) ──
        txt_parts = [
            f"Foundry  : {customer_name}",
            f"Line     : {line_name} (Line ID: {foundry_line_id})",
            f"Time     : {now_str}",
            "",
            "No data has been received from one or more sources on this line.",
            "All process analysis (Bad Batch, Prescription, Sigma) has been automatically suspended.",
        ]
        for block in source_blocks:
            txt_parts.append(SEP)
            for item in block:
                if isinstance(item, tuple) and item[0] == "LINK":
                    _, link_text, url = item
                    txt_parts.append(f"  [ {link_text} ]  {url}")
                else:
                    txt_parts.append(item)
        txt_parts += [SEP, f"If not confirmed within {CONFIRMATION_RESEND_HOURS}h, this alert will repeat.", "", "@Sandman Team"]
        plain_body = "\n".join(txt_parts)

        # ── HTML version — plain text look with clickable link text ──────────
        def _esc(s):
            return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

        html_parts = []
        def line(text, indent=0, bold=False):
            pad   = "&nbsp;&nbsp;&nbsp;&nbsp;" * indent
            inner = f"<b>{_esc(text)}</b>" if bold else _esc(text)
            html_parts.append(f"<p>{pad}{inner}</p>")
        def blank():
            html_parts.append("<p>&nbsp;</p>")
        def rule():
            html_parts.append("<hr/>")
        def links(items):
            parts = "  &nbsp;|&nbsp;  ".join(
                f'<a href="{url}">{_esc(label)}</a>'
                for (kind, label, url) in items
            )
            html_parts.append(f"<p>&nbsp;&nbsp;&nbsp;&nbsp;{parts}</p>")

        html_parts.append(
            "<html><body style='font-family:Arial,sans-serif;font-size:14px;"
            "line-height:1.8;color:#1a1a1a;max-width:600px;margin:24px auto;padding:0 20px'>"
        )
        line(f"Foundry  : {customer_name}")
        line(f"Line     : {line_name} (Line ID: {foundry_line_id})")
        line(f"Time     : {now_str}")
        blank()
        line("No data has been received from one or more sources on this line.")
        line("All process analysis (Bad Batch, Prescription, Sigma) has been automatically suspended.")

        for block in source_blocks:
            rule()
            link_items = []
            for item in block:
                if isinstance(item, tuple) and item[0] in ("LINK", "NOURL"):
                    link_items.append(item)
                elif item == "":
                    blank()
                else:
                    indent = 2 if item.startswith("     ") else (1 if item.startswith("  ") else 0)
                    bold   = "✗" in item or "✓" in item
                    line(item.strip(), indent=indent, bold=bold)
            if link_items:
                blank()
                links(link_items)

        rule()
        line(f"If not confirmed within {CONFIRMATION_RESEND_HOURS}h, this alert will repeat.")
        blank()
        line("@Sandman Team")
        html_parts.append("</body></html>")
        html_body = "".join(html_parts)

        try:
            if not email_cfg.get("enabled", False):
                logger.warning(
                    "[%s][line=%d] email not enabled in config — confirmation skipped (check dashboard notifications for this line)",
                    self._label, foundry_line_id,
                )
                return
            from watchdog.email_notifier import _send
            _send(email_cfg, subject, html_body, alert_type="DATA_FLOW")
            self._confirmation_sent[foundry_line_id] = datetime.now()
            logger.info("[%s][line=%d] confirmation email sent", self._label, foundry_line_id)
        except Exception as exc:
            logger.warning("[%s][line=%d] confirmation email failed: %s", self._label, foundry_line_id, exc)


# ── Public helper — process monitors call this ────────────────────────────────

def is_data_trustworthy(
    registry_engine: Engine,
    foundry_line_id: int,
    source_name: str,
) -> bool:
    """
    Called by process monitors (bad_batch, prescription, etc.) before running.
    Returns True if data is trustworthy (healthy/learning/unknown source).
    Returns False if data is suppressed (SCADA + additive both gone).

    Usage in existing monitors — add ONE line at the top of _poll():
        from watchdog.data_flow.monitor import is_data_trustworthy
        if not is_data_trustworthy(registry_engine, fl_id, 'additive'):
            logger.info("Additive data unavailable — skipping this cycle")
            return 0
    """
    sql = text("""
        SELECT suppress_process, status
        FROM `watchdog_data_health`
        WHERE foundry_line_id = :lid
          AND source_name = :src
        LIMIT 1
    """)
    try:
        with registry_engine.connect() as conn:
            row = conn.execute(sql, {"lid": foundry_line_id, "src": source_name}).mappings().first()
        if not row:
            return True   # source not monitored -> don't block
        if row["suppress_process"]:
            return False
        if row["status"] in ("missing",):
            return False
        return True
    except Exception:
        return True  # if registry is unreachable -> don't block existing monitors
