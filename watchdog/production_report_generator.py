"""
watchdog/production_report_generator.py
-----------------------------------------
Generates a full-day production report PDF using matplotlib charts.
Data is pulled live from the foundry database (additive + analytics_report tables).

Report structure (one section per component, sorted by production order):
  Cover page  — date, component count, shifts
  Summary     — production order table (component, group, shift, first batch, prescription)
  Per component:
    A. Prescription Monitoring  — actual vs predicted (±3%) line charts + deviation % bar charts
    B. Compactability           — SMC Discharge vs COSP setpoint + deviation bar chart
    C. Bad Batch Analysis       — bad batch summary table
    D. SMC Trends               — mix time, water, temperature, moisture trend lines
"""

import io
import json
import logging
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg") 

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.ticker import MaxNLocator

logger = logging.getLogger(__name__)

# ── Tolerances — fallback defaults (overridden at runtime via foundry_cfg) ──────
_DEFAULT_MATERIAL_TOL_PCT = 3.0   # ±3 % for Bentonite / Magnacoal / New Sand
_DEFAULT_COMP_TOL         = 2.0   # ±2 absolute for SMC vs COSP
# Keep module-level aliases for any external imports that reference these directly
MATERIAL_TOL_PCT = _DEFAULT_MATERIAL_TOL_PCT
COMP_TOL         = _DEFAULT_COMP_TOL

# ── Default chart limits — fallback (overridden at runtime via foundry_cfg) ─────
_DEFAULT_CHART_LIMITS = {
    "bentonite":      {"min": 1,    "max": None},
    "magnacoal":      {"min": 1,    "max": None},
    "new_sand":       {"min": 1,    "max": None},
    "compactability": {"min": 1,    "max": None},
    "water":          {"min": 1,    "max": None},
    "temperature":    {"min": None, "max": None},
    "cycle_time":     {"min": None, "max": 200 },
    "moisture":       {"min": None, "max": None},
}
DEFAULT_CHART_LIMITS = _DEFAULT_CHART_LIMITS  # backward-compat alias


def _resolve_cfg(foundry_cfg: dict) -> tuple:
    """
    Extract (material_tol_pct, comp_tol, chart_limits) from foundry_cfg.
    Falls back to module-level defaults for any missing key.

    Config keys read:
      prescription_watchdog.critical_thr -> MATERIAL_TOL_PCT (CRITICAL % threshold)
      bad_batch_watchdog.threshold        -> COMP_TOL
      param_chart_limits                  -> DEFAULT_CHART_LIMITS (merged with defaults)
    """
    cfg = foundry_cfg or {}
    pw      = cfg.get("prescription_watchdog") or {}
    mat_tol = float(pw.get("critical_thr") or _DEFAULT_MATERIAL_TOL_PCT)
    bb      = cfg.get("bad_batch_watchdog") or {}
    cmp_tol = float(bb.get("threshold") or _DEFAULT_COMP_TOL)
    # Merge: config values override defaults, defaults fill the gaps
    limits  = {**_DEFAULT_CHART_LIMITS, **(cfg.get("param_chart_limits") or {})}
    return mat_tol, cmp_tol, limits

# ── Colours ────────────────────────────────────────────────────────────────────
CLR_GOOD = "#2196F3"
CLR_BAD  = "#F44336"
CLR_PRED = "#E65100"
CLR_BAND = "#4CAF50"
CLR_HEAD = "#1F497D"


def _clamp(values: list, lo, hi) -> list:
    """
    Replace values outside [lo, hi] with None (NaN — excluded from plot).
    Gaps in the line are bridged so no visible break appears.
    None lo/hi means no bound on that side.
    """
    out = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            out.append(None)
            continue
        if (lo is not None and f < lo) or (hi is not None and f > hi):
            out.append(None)
        else:
            out.append(v)
    return out


def _plot_with_gap_break(ax, xs, ys, gap_minutes: int = 30, **kwargs):
    """
    Plot trend line but BREAK it when the time gap between consecutive points
    exceeds gap_minutes.  Each continuous segment is drawn independently so
    the viewer sees a clear separation between production runs rather than a
    misleading diagonal line through a long idle period.

    xs must be datetime objects; ys are numeric values (None skipped).
    """
    from datetime import timedelta

    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if not pairs:
        return

    threshold = timedelta(minutes=gap_minutes)
    segments  = []
    seg       = [pairs[0]]

    for i in range(1, len(pairs)):
        gap = pairs[i][0] - pairs[i - 1][0]
        if gap > threshold:
            segments.append(seg)
            seg = [pairs[i]]
        else:
            seg.append(pairs[i])
    segments.append(seg)

    for seg in segments:
        sx, sy = zip(*seg)
        ax.plot(list(sx), list(sy), marker="o", markersize=5, **kwargs)


def _plot_no_gap(ax, xs, ys, **kwargs):
    """
    Plot a line through (xs, ys) where some ys may be None.
    Skips None points but bridges the gap with a thin dashed grey line
    so there is no visible break in the trend.
    """
    xs_a = list(xs)
    ys_a = list(ys)
    n    = len(xs_a)

    # Collect valid-point segments and bridge points for gaps
    valid_x, valid_y = [], []
    for i in range(n):
        if ys_a[i] is not None:
            valid_x.append(xs_a[i])
            valid_y.append(float(ys_a[i]))

    if not valid_x:
        return

    # Plot the valid data with the supplied style
    ax.plot(valid_x, valid_y, **kwargs)

    # Draw thin dashed bridge across every gap
    for i in range(n - 1):
        if ys_a[i] is None or ys_a[i + 1] is None:
            # find the last valid point before the gap and the first after
            li = i - 1
            while li >= 0 and ys_a[li] is None:
                li -= 1
            ri = i + 1
            while ri < n and ys_a[ri] is None:
                ri += 1
            if li >= 0 and ri < n:
                ax.plot([xs_a[li], xs_a[ri]],
                        [float(ys_a[li]), float(ys_a[ri])],
                        color="#BBBBBB", linewidth=0.9,
                        linestyle="--", zorder=2)


def _draw_batch_table(ax_t, batches, ts_list, diff_vals, n_total, bad_idx,
                      rank, cid, group_name, shift, comp_tol):
    """Show ALL batches; highlight bad rows pink, good rows white."""
    bad_set = set(bad_idx)
    cols = ["Batch #", "Time", "SMC Discharge", "COSP %", "Difference", "Status"]
    rows = []
    for i, (b, ts_obj) in enumerate(zip(batches, ts_list)):
        diff_v = b.get("cosp_diff")
        rows.append([
            i + 1,
            ts_obj.strftime("%H:%M") if ts_obj else "—",
            f"{b['smc']:.2f}"  if b.get("smc")  is not None else "—",
            f"{b['cosp']:.2f}" if b.get("cosp") is not None else "—",
            f"{diff_v:+.3f}"   if diff_v is not None else "—",
            "BAD BATCH" if i in bad_set else "OK",
        ])


    row_h = max(1.1, min(1.8, 18 / max(n_total, 1)))
    tbl = ax_t.table(cellText=rows, colLabels=cols,
                     loc="upper center", cellLoc="center",
                     bbox=[0, 0, 1, 1])   # fill the full axes
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, row_h)

    for j in range(len(cols)):
        tbl[0, j].set_facecolor(CLR_HEAD)
        tbl[0, j].set_text_props(color="white", fontweight="bold")
    for i in range(1, len(rows) + 1):
        bad = (i - 1) in bad_set
        for j in range(len(cols)):
            tbl[i, j].set_facecolor("#FFEBEB" if bad else "white")
        tbl[i, len(cols)-1].set_text_props(
            color="#C01616" if bad else "#388E3C",
            fontweight="bold" if bad else "normal"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_predictions(engine, line_id: int, group_name: str,
                       date_str: str, shift) -> dict:
    """
    Return predicted additive values from analytics_report for a group/date/shift.
    Keys returned: bent, mag, ns  (float kg values).
    Returns {} when no prescription exists.
    """
    from sqlalchemy import text
    if not group_name:
        return {}
    try:
        import re as _re
        grp_norm = _re.sub(r'[-\s]', '', (group_name or '').strip().lower())
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT predicted_additives_json
                FROM   analytics_report
                WHERE  foundry_line_pkey = :fl
                  AND  LOWER(REPLACE(REPLACE(IFNULL(foundry_line_group_name,''),'-',''),' ','')) = :grp
                  AND  DATE(date)        = :dt
                  AND  shift             = :sh
                  AND  deleted           = 0
                ORDER  BY pkey DESC LIMIT 1
            """), {"fl": int(line_id), "grp": grp_norm,
                   "dt": date_str, "sh": str(shift)}).mappings().first()
        if row and row["predicted_additives_json"]:
            raw = row["predicted_additives_json"]
            d   = json.loads(raw) if isinstance(raw, str) else raw
            return {
                "bent": float(d.get("bentonite",       0) or 0),
                "mag":  float(d.get("lca",             0) or 0),
                "ns":   float(d.get("freshSilicaSand", 0) or 0),
            }
    except Exception as exc:
        logger.debug("_fetch_predictions failed: %s", exc)
    return {}


def _fetch_additive_labels(engine, line_id: int) -> dict:
    """
    Fetch ui_name display labels for key additive columns from the properties table.

    Returns a dict keyed by snake_case column name:
      {
        "bentonite_actual" : "Bentonite (Kg)",
        "coal_dust_actual" : "Coal Dust (Kg)",
        "fss_actual"       : "FSS (Kg)",
        ...
      }
    Falls back to sensible defaults when the table has no ui_name set.
    """
    import re as _re

    # snake_case additive col -> camelCase java_name
    TARGET_COLS = {
        "bentonite_actual" : "bentoniteActual",
        "coal_dust_actual" : "coalDustActual",
        "fss_actual"       : "fssActual",
        "total_water_ltr"  : "totalWaterLtr",
        "temperature_c"    : "temperatureC",
        "total_seconds"    : "totalSeconds",
        "moisture_smc_pct" : "moistureSmcPct",
    }

    defaults = {
        "bentonite_actual" : "Bentonite (Kg)",
        "coal_dust_actual" : "Coal Dust (Kg)",
        "fss_actual"       : "FSS (Kg)",
        "total_water_ltr"  : "Water (Ltr)",
        "temperature_c"    : "Temperature (°C)",
        "total_seconds"    : "Cycle Time (sec)",
        "moisture_smc_pct" : "Moisture SMC (%)",
    }

    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT p.java_name, p.ui_name, p.alias_name
                FROM   properties p
                JOIN   measures   m ON p.measure_pkey = m.pkey
                WHERE  m.name             = 'additive'
                  AND  m.foundry_line_id  = :fl
                  AND  p.deleted          = 0
                  AND  p.is_active        = 1
                  AND  m.isActive         = 1
                  AND  p.java_name IN :names
            """), {"fl": int(line_id),
                   "names": tuple(TARGET_COLS.values())}).mappings().fetchall()

        # alias_name takes priority over ui_name
        java_to_label = {
            r["java_name"]: (r["alias_name"] or r["ui_name"] or "")
            for r in rows
            if (r["alias_name"] or r["ui_name"])
        }
        result = {}
        for snake, java in TARGET_COLS.items():
            result[snake] = java_to_label.get(java) or defaults.get(snake, snake)
        return result
    except Exception:
        return defaults.copy()


def _fetch_group_name(engine, line_id: int, component_id: str) -> str:
    """
    Look up the foundry line group for a component.

    Two-step strategy to handle foundries that store different identifiers
    in additive.component_id vs foundry_line_group_component.component_id:

    Step 1 — Direct match (works for numeric-ID foundries like GPI):
        foundry_line_group_component.component_id = additive.component_id

    Step 2 — Via components master (works for name-based foundries like Munjal):
        additive.component_id  matches  components.component_name
        components.component_id  matches  foundry_line_group_component.component_id
    """
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            # Step 1: direct match
            g = conn.execute(text("""
                SELECT g.name
                FROM   foundry_line_group_component gc
                JOIN   foundry_line_group g ON g.pkey = gc.foundry_line_group_pkey
                WHERE  gc.component_id     = :comp AND gc.deleted = 0
                  AND  g.foundry_line_pkey = :fl   AND g.deleted  = 0
                LIMIT 1
            """), {"comp": component_id, "fl": int(line_id)}).mappings().first()
            if g:
                return g["name"] or ""

            # Step 2: resolve via components master table
            # additive.component_id -> components.component_name -> components.component_id
            # -> foundry_line_group_component.component_id -> group name
            g2 = conn.execute(text("""
                SELECT g.name
                FROM   components c
                JOIN   foundry_line_group_component gc
                       ON gc.component_id = c.component_id AND gc.deleted = 0
                JOIN   foundry_line_group g
                       ON g.pkey = gc.foundry_line_group_pkey
                      AND g.foundry_line_pkey = :fl AND g.deleted = 0
                WHERE  c.deleted = 0
                  AND  (c.component_name = :comp OR c.component_id = :comp)
                LIMIT 1
            """), {"comp": component_id, "fl": int(line_id)}).mappings().first()
            return g2["name"] or "" if g2 else ""
    except Exception:
        return ""


def fetch_production_data(engine, line_id: int, date_str: str,
                          foundry_cfg: dict = None) -> dict:
    """
    Fetch all production data for a given date from the database.
    Returns a dict:  { "date": str, "components": [{ rank, component_id, shift,
                       group_name, t_start, n_batches, batches[], predictions{} }] }
    """
    from sqlalchemy import text

    fl_id = int(line_id)

    # ── Read additive delay from caller-supplied foundry config ───────────────
    _additive_delay = int((foundry_cfg or {}).get("additive_delay_seconds") or 0)

    # ── Fetch every batch in strict time order ────────────────────────────────
    # Do NOT group by component — we need to detect contiguous production runs.
    # When additive_delay_seconds > 0, reassign component_id from consumption_booking
    # by shifting the booking window back by the delay (mirrors GPI Merging.py logic:
    #   START_DT_ADJ = START_DT - delay,  END_DT_ADJ = END_DT - delay).
    with engine.connect() as conn:
        if _additive_delay > 0:
            all_batches = conn.execute(text("""
                SELECT
                    a.pkey,
                    COALESCE(
                        SUBSTRING_INDEX(cb.component_id, ' ', 1),
                        a.component_id
                    ) AS component_id,
                    a.shift, a.timestamp,
                    a.bentonite_actual,   a.bentonite_set_point,
                    a.coal_dust_actual,   a.coal_dust_set_point,
                    a.fss_actual,         a.fss_set_point,
                    a.compactability_smc_pct                        AS smc,
                    a.cosp_percentage_pct                           AS cosp,
                    ROUND(a.compactability_smc_pct
                          - a.cosp_percentage_pct, 3)               AS cosp_diff,
                    a.total_water_ltr, a.temperature_c,
                    a.total_seconds,   a.moisture_smc_pct
                FROM additive a
                LEFT JOIN consumption_booking cb
                    ON  cb.foundry_line_id = :fl
                    AND DATE(cb.date) = :dt
                    AND a.timestamp BETWEEN
                        (TIMESTAMP(DATE(cb.date), cb.start_time) - INTERVAL :delay SECOND)
                        AND
                        (CASE WHEN cb.end_time < cb.start_time
                              THEN TIMESTAMP(DATE(cb.date) + INTERVAL 1 DAY, cb.end_time)
                              ELSE TIMESTAMP(DATE(cb.date), cb.end_time)
                         END - INTERVAL :delay SECOND)
                WHERE a.foundry_line_id = :fl
                  AND a.deleted = 0
                  AND DATE(a.timestamp) = :dt
                ORDER BY a.timestamp ASC
            """), {"fl": fl_id, "dt": date_str, "delay": _additive_delay}).mappings().fetchall()
        else:
            all_batches = conn.execute(text("""
                SELECT
                    pkey, component_id, shift, timestamp,
                    bentonite_actual,   bentonite_set_point,
                    coal_dust_actual,   coal_dust_set_point,
                    fss_actual,         fss_set_point,
                    compactability_smc_pct                        AS smc,
                    cosp_percentage_pct                           AS cosp,
                    ROUND(compactability_smc_pct
                          - cosp_percentage_pct, 3)               AS cosp_diff,
                    total_water_ltr, temperature_c,
                    total_seconds,   moisture_smc_pct
                FROM   additive
                WHERE  foundry_line_id = :fl
                  AND  deleted = 0
                  AND  DATE(timestamp) = :dt
                  AND  component_id IS NOT NULL
                  AND  component_id != ''
                  AND  LOWER(TRIM(component_id)) != 'null'
                ORDER  BY timestamp ASC
            """), {"fl": fl_id, "dt": date_str}).mappings().fetchall()

    all_batches = [dict(b) for b in all_batches]
    if not all_batches:
        return {"components": [], "date": date_str}

    # ── Detect contiguous runs ────────────────────────────────────────────────
    # A new run starts when component_id changes vs the previous batch.
    # Skip any batch where component_id is null/empty.
    runs = []          # list of {"component_id", "shift", "t_start", "t_end", "batches"}
    prev_comp = None
    for b in all_batches:
        comp = b["component_id"]
        if not comp or str(comp).strip().lower() == 'null':
            continue
        if comp != prev_comp:
            runs.append({
                "component_id": comp,
                "shift"       : b.get("shift"),
                "t_start"     : b["timestamp"],
                "t_end"       : b["timestamp"],
                "batches"     : [b],
            })
        else:
            runs[-1]["batches"].append(b)
            runs[-1]["t_end"] = b["timestamp"]
        prev_comp = comp

    components = []
    for rank, run in enumerate(runs, start=1):
        cid     = run["component_id"]
        shift   = run["shift"]
        batches = run["batches"]

        # Fetch pouring/metal data for this run window.
        # Metal rows rarely match additive batch count 1:1, so we match each
        # metal row to the nearest additive batch by timestamp difference.
        # Note: metal table uses separate `date` + `time` columns (no timestamp).
        try:
            with engine.connect() as conn:
                metal_rows = conn.execute(text("""
                    SELECT TIMESTAMP(`date`, `time`) AS metal_ts,
                           pouring_temp, pouring_time,
                           NULL AS current_scada
                    FROM   metal
                    WHERE  foundry_line_id = :fl
                      AND  deleted         = 0
                      AND  component_id    = :comp
                      AND  `date`          = :dt
                      AND  shift           = :sh
                    ORDER  BY `time` ASC
                """), {"fl": fl_id, "comp": cid,
                       "dt": date_str, "sh": str(shift)}).mappings().fetchall()
            metal_rows = [dict(m) for m in metal_rows]
            if metal_rows and batches:
                # Build list of (batch_index, batch_timestamp) for matching
                batch_ts = []
                for i, b in enumerate(batches):
                    ts = _parse_ts(b.get("timestamp"))
                    batch_ts.append((i, ts))

                for m in metal_rows:
                    m_ts = _parse_ts(m.get("metal_ts"))
                    if m_ts is None:
                        continue
                    # Find nearest batch by absolute time difference
                    best_idx = min(
                        (i for i, ts in batch_ts if ts is not None),
                        key=lambda i: abs((batch_ts[i][1] - m_ts).total_seconds()),
                        default=None,
                    )
                    if best_idx is not None:
                        b = batches[best_idx]
                        # Only overwrite if this metal row is closer than any previous
                        prev_diff = b.get("_metal_diff", float("inf"))
                        cur_diff  = abs((batch_ts[best_idx][1] - m_ts).total_seconds())
                        if cur_diff <= prev_diff:
                            b["pouring_temp"]  = m.get("pouring_temp")
                            b["pouring_time"]  = m.get("pouring_time")
                            b["current_scada"] = m.get("current_scada")
                            b["_metal_diff"]   = cur_diff

                # Clean up internal key
                for b in batches:
                    b.pop("_metal_diff", None)
        except Exception:
            pass

        group_name = _fetch_group_name(engine, fl_id, cid)
        preds      = _fetch_predictions(engine, fl_id, group_name, date_str, shift)

        # Fetch human-readable component name from components table
        comp_name = ""
        try:
            from sqlalchemy import text as _ct
            with engine.connect() as _cc:
                _cr = _cc.execute(_ct(
                    "SELECT component_name FROM components WHERE component_id=:cid LIMIT 1"
                ), {"cid": str(cid).rstrip('.0') or cid}).mappings().first()
                if _cr:
                    comp_name = str(_cr["component_name"] or "")
        except Exception:
            pass

        components.append({
            "rank"          : rank,
            "component_id"  : cid,
            "component_name": comp_name,
            "shift"         : shift,
            "group_name"    : group_name,
            "t_start"       : run["t_start"],
            "t_end"         : run["t_end"],
            "n_batches"     : len(batches),
            "batches"       : batches,
            "predictions"   : preds,
        })

    return {"components": components, "date": date_str}


# ══════════════════════════════════════════════════════════════════════════════
#  CHART HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_xaxis(ax, datetimes):
    ax.xaxis.set_major_locator(MaxNLocator(nbins=5))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=7)


def _grid(ax):
    ax.grid(True, linestyle=":", alpha=0.4)
    ax.tick_params(labelsize=7)


def _scatter_good_bad(ax, x, y, is_bad):
    colors = [CLR_BAD if b else CLR_GOOD for b in is_bad]
    ax.scatter(x, y, c=colors, s=22, zorder=5)
    # Draw line connecting all points — bridge over any None gaps
    _plot_no_gap(ax, x, y, color=CLR_GOOD, linewidth=1.2, alpha=0.55)


def _parse_ts(val) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(val[:19], fmt)
            except ValueError:
                continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  PDF GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_production_pdf(engine, line_id: int, date_str: str,
                            chart_limits: Optional[dict] = None,
                            foundry_cfg: Optional[dict] = None) -> bytes:
    """
    Generate a full-day production report PDF.
    Returns raw bytes ready for Flask send_file().
    chart_limits: per-parameter {min, max} dict from DB foundry config.
                  Falls back to DEFAULT_CHART_LIMITS for any missing parameter.
    """
    # Resolve tolerances and chart limits from foundry config
    MATERIAL_TOL_PCT, COMP_TOL, _DEFAULT_LIMITS = _resolve_cfg(foundry_cfg)
    _col_limits = {**_DEFAULT_LIMITS, **(chart_limits or {})}

    def _col_lim(col_name, friendly_key):
        """Return (lo, hi) for a column — column name wins over friendly alias."""
        b = _col_limits.get(col_name) or _col_limits.get(friendly_key) or {}
        return (b.get("min") if isinstance(b, dict) else None,
                b.get("max") if isinstance(b, dict) else None)
        return (b.get("min") if isinstance(b, dict) else None,
                b.get("max") if isinstance(b, dict) else None)
    data   = fetch_production_data(engine, line_id, date_str, foundry_cfg=foundry_cfg)
    comps  = data.get("components", [])
    # Fetch foundry-specific display labels for additive columns
    _add_labels = _fetch_additive_labels(engine, line_id)

    try:
        dt_obj     = datetime.strptime(date_str, "%Y-%m-%d")
        dt_display = dt_obj.strftime("%d  %B  %Y")
    except Exception:
        dt_display = date_str

    buf = io.BytesIO()

    with PdfPages(buf) as pdf:

        # ── Cover ─────────────────────────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(13, 7))
        ax.axis("off")
        fig.patch.set_facecolor("#F5F7FA")
        ax.text(0.5, 0.72, "Sand Mix  —  Production Report",
                ha="center", fontsize=28, fontweight="bold", color=CLR_HEAD,
                transform=ax.transAxes)
        ax.text(0.5, 0.57, dt_display,
                ha="center", fontsize=20, color="#444", transform=ax.transAxes)
        shifts_str = ", ".join(sorted({str(c["shift"]) for c in comps})) if comps else "—"
        ax.text(0.5, 0.46,
                f"{len(comps)} component(s) produced   |   Shifts: {shifts_str}",
                ha="center", fontsize=13, color="#666", transform=ax.transAxes)
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        if not comps:
            buf.seek(0)
            return buf.read()

        # ── Production order summary table ────────────────────────────────────
        n_rows_tbl = len(comps)
        # Scale figure height: min 7, grows with many rows
        tbl_fig_h  = max(7, 1.8 + n_rows_tbl * 0.38)
        fig, ax = plt.subplots(figsize=(13, tbl_fig_h))
        fig.patch.set_facecolor("#F5F7FA")
        ax.axis("off")

        # Title placed above the axes via fig.text (avoids overlap with table)
        fig.text(0.05, 0.97, f"Production Order  —  {dt_display}",
                 fontsize=13, fontweight="bold", color=CLR_HEAD,
                 ha="left", va="top")

        col_labels = ["#", "Shift", "Component", "Group",
                      "First Batch", "Batches",
                      _add_labels.get("bentonite_actual", "Bentonite"),
                      _add_labels.get("coal_dust_actual", "Coal Dust"),
                      _add_labels.get("fss_actual", "FSS")]
        # Column widths (fractions of total — must sum to 1.0)
        # Component and Group get the most space; numeric columns are narrow
        col_widths = [0.04, 0.06, 0.26, 0.20, 0.09, 0.07, 0.09, 0.09, 0.10]

        tbl_rows = []
        for c in comps:
            t_start = _parse_ts(c["t_start"])
            ts_str  = t_start.strftime("%H:%M") if t_start else "—"
            p       = c["predictions"]
            # Truncate long names so they fit the column
            comp_name  = (c["component_id"] or "")[:32]
            group_name = (c["group_name"] or "—")[:22]
            tbl_rows.append([
                c["rank"],
                str(c["shift"]),
                comp_name,
                group_name,
                ts_str,
                c["n_batches"],
                f"{p['bent']:.1f}" if p.get("bent") else "—",
                f"{p['mag']:.1f}"  if p.get("mag")  else "—",
                f"{p['ns']:.1f}"   if p.get("ns")   else "—",
            ])

        ax.set_position([0.02, 0.02, 0.96, 0.92])
        tbl = ax.table(cellText=tbl_rows, colLabels=col_labels,
                       colWidths=col_widths,
                       loc="upper center", cellLoc="center")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(8.5)
        tbl.scale(1, 1.5)
        for j in range(len(col_labels)):
            tbl[0, j].set_facecolor(CLR_HEAD)
            tbl[0, j].set_text_props(color="white", fontweight="bold")
        # Left-align Component and Group columns (index 2 and 3)
        for i in range(len(tbl_rows) + 1):
            for j in (2, 3):
                tbl[i, j].set_text_props(ha="left")
                tbl[i, j]._loc = "left"
        for i in range(1, len(tbl_rows) + 1):
            bg = "#F5F7FA" if i % 2 == 0 else "white"
            for j in range(len(col_labels)):
                tbl[i, j].set_facecolor(bg)

        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        # ── Per-component pages ───────────────────────────────────────────────
        for comp in comps:
            cid        = comp["component_id"]
            group_name = comp["group_name"] or "—"
            shift      = comp["shift"]
            rank       = comp["rank"]
            batches    = comp["batches"]
            preds      = comp["predictions"]

            if not batches:
                continue

            bent_pred = preds.get("bent", 0) or 0
            mag_pred  = preds.get("mag",  0) or 0
            ns_pred   = preds.get("ns",   0) or 0
            has_preds = bent_pred > 0 or mag_pred > 0 or ns_pred > 0

            # Parse timestamps once
            ts_list = [_parse_ts(b.get("timestamp")) for b in batches]
            valid_ts = [t for t in ts_list if t]
            date_label = valid_ts[0].strftime("%d-%m-%Y") if valid_ts else date_str

            def _col(key):
                return [b.get(key) for b in batches]

            bent_act     = _clamp(_col("bentonite_actual"),  *_col_lim("bentonite_actual",  "bentonite"))
            mag_act      = _clamp(_col("coal_dust_actual"),  *_col_lim("coal_dust_actual",  "magnacoal"))
            ns_act       = _clamp(_col("fss_actual"),        *_col_lim("fss_actual",        "new_sand"))
            smc_vals     = _clamp(_col("smc"),               *_col_lim("smc",               "compactability"))
            cosp_vals    = _col("cosp")
            diff_vals    = _col("cosp_diff")
            water_ltr    = _clamp(_col("total_water_ltr"),   *_col_lim("total_water_ltr",   "water"))
            temp_c       = _clamp(_col("temperature_c"),     *_col_lim("temperature_c",     "temperature"))
            cycle_sec    = _clamp(_col("total_seconds"),     *_col_lim("total_seconds",     "cycle_time"))
            moisture     = _clamp(_col("moisture_smc_pct"),  *_col_lim("moisture_smc_pct",  "moisture"))
            pour_temp    = _col("pouring_temp")
            pour_time    = _col("pouring_time")
            curr_scada   = _col("current_scada")

            n_total   = len(batches)
            n_bad     = sum(1 for d in diff_vals if d is not None and abs(d) > COMP_TOL)

            # ── Section header (light mode) ───────────────────────────────────
            fig = plt.figure(figsize=(13, 2.8))
            fig.patch.set_facecolor("#F0F4F8")
            ax_h = fig.add_axes([0.02, 0.06, 0.96, 0.88])
            ax_h.set_facecolor("white")
            ax_h.set_xlim(0, 1); ax_h.set_ylim(0, 1)
            ax_h.axis("off")
            for spine in ax_h.spines.values():
                spine.set_edgecolor("#B0C4D8"); spine.set_linewidth(1.2); spine.set_visible(True)
            # Left accent bar
            ax_h.axvline(0.005, color=CLR_HEAD, linewidth=6, solid_capstyle="butt")
            ax_h.text(0.025, 0.80,
                      f"#{rank}  Component: {cid}   |   Group: {group_name}   |   Shift {shift}",
                      fontsize=15, fontweight="bold", color=CLR_HEAD,
                      va="top", transform=ax_h.transAxes)
            _lbl_bent = _add_labels.get("bentonite_actual", "Bentonite")
            _lbl_mag  = _add_labels.get("coal_dust_actual", "Coal Dust")
            _lbl_ns   = _add_labels.get("fss_actual", "FSS")
            presc_str = (f"{_lbl_bent}: {bent_pred:.1f} kg    {_lbl_mag}: {mag_pred:.1f} kg    {_lbl_ns}: {ns_pred:.1f} kg"
                         if has_preds else "No prescription available for this component")
            ax_h.text(0.025, 0.50, f"Date: {date_label}    {presc_str}",
                      fontsize=11, color="#444444", va="top", transform=ax_h.transAxes)
            ax_h.text(0.025, 0.20,
                      f"Batches: {n_total}    Bad batches (Comp >±{COMP_TOL}%): {n_bad}",
                      fontsize=10,
                      color="#C01616" if n_bad else "#388E3C",
                      fontweight="bold" if n_bad else "normal",
                      va="top", transform=ax_h.transAxes)
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

            # ── PAGE A: Prescription Monitoring ──────────────────────────────
            if has_preds:
                materials = [
                    (bent_act, bent_pred, _lbl_bent),
                    (mag_act,  mag_pred,  _lbl_mag),
                    (ns_act,   ns_pred,   _lbl_ns),
                ]
                fig, axes_pm = plt.subplots(1, 3, figsize=(15, 5.5))
                fig.suptitle(
                    f"Prescription Monitoring  |  #{rank} {cid} ({group_name})  |  "
                    f"Shift {shift}  |  {date_label}",
                    fontsize=11, fontweight="bold", color=CLR_HEAD
                )

                for ax_pm, (actual_list, pred, ylabel) in zip(axes_pm, materials):
                    upper = pred * (1 + MATERIAL_TOL_PCT / 100)
                    lower = pred * (1 - MATERIAL_TOL_PCT / 100)
                    pairs = [(t, v) for t, v in zip(ts_list, actual_list)
                             if t is not None and v is not None]
                    if pairs:
                        xs, ys = zip(*pairs)
                        is_bad_pts = [v < lower or v > upper for v in ys]
                        _scatter_good_bad(ax_pm, list(xs), list(ys), is_bad_pts)
                        _fmt_xaxis(ax_pm, list(xs))
                    ax_pm.axhline(pred,  color=CLR_PRED, linewidth=1.8, linestyle="-",
                                  label=f"Predicted  {pred:.1f}")
                    ax_pm.axhline(upper, color=CLR_BAND, linewidth=1.1, linestyle="--",
                                  label=f"+{MATERIAL_TOL_PCT:.0f}%  {upper:.1f}")
                    ax_pm.axhline(lower, color=CLR_BAND, linewidth=1.1, linestyle="--",
                                  label=f"−{MATERIAL_TOL_PCT:.0f}%  {lower:.1f}")
                    ax_pm.set_xlabel(f"Time  ({date_label})", fontsize=8)
                    ax_pm.set_ylabel(ylabel, fontsize=8)
                    ax_pm.set_title(f"{ylabel.split(' ')[0]}  —  Actual vs Predicted",
                                    fontsize=9, fontweight="bold")
                    _grid(ax_pm)

                # Single generic shared legend below all 3 charts
                from matplotlib.lines import Line2D as _L2D
                _leg = [
                    _L2D([0],[0], color=CLR_GOOD,  linewidth=1.5, marker="o", markersize=4, label="Actual"),
                    _L2D([0],[0], color=CLR_BAD,   linewidth=0,   marker="o", markersize=5, label="Out of spec"),
                    _L2D([0],[0], color=CLR_PRED,  linewidth=1.8, linestyle="-",  label="Predicted"),
                    _L2D([0],[0], color=CLR_BAND,  linewidth=1.1, linestyle="--", label=f"+{MATERIAL_TOL_PCT:.0f}% tolerance"),
                    _L2D([0],[0], color=CLR_BAND,  linewidth=1.1, linestyle="--", label=f"-{MATERIAL_TOL_PCT:.0f}% tolerance"),
                ]
                fig.legend(handles=_leg, loc="lower center", ncol=5,
                           fontsize=8, frameon=True, framealpha=0.95,
                           bbox_to_anchor=(0.5, 0.0))
                fig.subplots_adjust(left=0.06, right=0.98, top=0.92,
                                    bottom=0.18, wspace=0.32)
                pdf.savefig(fig, bbox_inches="tight")
                plt.close(fig)

            # ── PAGE B: Compactability ────────────────────────────────────────
            valid_comp = [
                (t, s, c, d)
                for t, s, c, d in zip(ts_list, smc_vals, cosp_vals, diff_vals)
                if t is not None and s is not None
            ]
            if valid_comp:
                comp_ts, comp_smc, comp_cosp, comp_diff = zip(*valid_comp)

                fig, ax = plt.subplots(1, 1, figsize=(9, 5))
                fig.suptitle(
                    f"Compactability  |  #{rank} {cid} ({group_name})  |  Shift {shift}",
                    fontsize=11, fontweight="bold", color=CLR_HEAD
                )

                is_bad_c = [abs(d) > COMP_TOL if d is not None else False for d in comp_diff]
                _scatter_good_bad(ax, list(comp_ts), list(comp_smc), is_bad_c)
                if any(v is not None for v in comp_cosp):
                    cosp_arr = np.array([v if v is not None else np.nan for v in comp_cosp])
                    ax.plot(list(comp_ts), cosp_arr, color="red", linewidth=1.8,
                            linestyle="-", label="COSP Setpoint", zorder=4)
                    ax.plot(list(comp_ts), cosp_arr + COMP_TOL, color="orange",
                            linewidth=1, linestyle=":", label=f"+{COMP_TOL:.0f}")
                    ax.plot(list(comp_ts), cosp_arr - COMP_TOL, color="orange",
                            linewidth=1, linestyle=":", label=f"−{COMP_TOL:.0f}")
                _fmt_xaxis(ax, list(comp_ts))
                ax.set_xlabel(f"Time  ({date_label})", fontsize=8)
                ax.set_ylabel("Compactability (%)", fontsize=8)
                ax.set_title("SMC Discharge vs COSP Setpoint", fontsize=9, fontweight="bold")
                _grid(ax)
                handles_c, labels_c = ax.get_legend_handles_labels()
                fig.legend(handles_c, labels_c, loc="lower center",
                           ncol=5, fontsize=8, frameon=True, framealpha=0.95,
                           bbox_to_anchor=(0.5, 0.0))
                fig.subplots_adjust(bottom=0.16)
                pdf.savefig(fig, bbox_inches="tight")
                plt.close(fig)

            # ── PAGE C: Bad Batch Summary ─────────────────────────────────────
            bad_idx = [i for i, d in enumerate(diff_vals)
                       if d is not None and abs(d) > COMP_TOL]

            fig_h = max(2.8, 0.7 + (n_total + 1) * 0.22)
            fig = plt.figure(figsize=(13, fig_h))
            fig.suptitle(
                f"Batch Analysis  |  #{rank} {cid} ({group_name})  |  "
                f"Shift {shift}  |  {len(bad_idx)} bad / {n_total} total",
                fontsize=11, fontweight="bold", color=CLR_HEAD
            )
            ax_t = fig.add_axes([0.02, 0.02, 0.96, 0.96])
            ax_t.axis("off")
            ax_t.patch.set_visible(False)
            _draw_batch_table(ax_t, batches, ts_list, diff_vals,
                              n_total, bad_idx, rank, cid, group_name, shift, COMP_TOL)
            pdf.savefig(fig, bbox_inches="tight", pad_inches=0.1)
            plt.close(fig)

            # ── PAGE D: SMC Trend Charts ──────────────────────────────────────
            import math
            all_trend_cols = [
                (bent_act,   _lbl_bent),
                (mag_act,    _lbl_mag),
                (ns_act,     _lbl_ns),
                (cycle_sec,  _add_labels.get("total_seconds",    "Mix Cycle Time (sec)")),
                (water_ltr,  _add_labels.get("total_water_ltr",  "Water Added (ltr)")),
                (temp_c,     _add_labels.get("temperature_c",    "Sand Temperature (°C)")),
                (moisture,   _add_labels.get("moisture_smc_pct", "Moisture SMC (%)")),
                (pour_temp,  "Pouring Temp (°C)"),
                (pour_time,  "Pouring Time (sec)"),
                (curr_scada, "Current SCADA (Amp)"),
            ]
            avail = [(vals, lbl) for vals, lbl in all_trend_cols
                     if any(v is not None for v in vals)]

            # One page per chart
            for vals, lbl in avail:
                pairs = [(t, v) for t, v in zip(ts_list, vals)
                         if t is not None and v is not None]
                if not pairs:
                    continue
                fig, ax = plt.subplots(figsize=(11, 6))
                fig.patch.set_facecolor("white")
                fig.suptitle(f"{lbl}  |  #{rank} {cid}  |  Shift {shift}",
                             fontsize=11, fontweight="bold", color=CLR_HEAD)
                xs_all, ys_all = [p[0] for p in pairs], [p[1] for p in pairs]
                # Break line when gap > 30 min — avoids misleading diagonals
                _plot_with_gap_break(ax, xs_all, ys_all, gap_minutes=30,
                                     linewidth=2.0, color=CLR_GOOD)
                _fmt_xaxis(ax, xs_all)
                ax.set_xlabel(f"Time  ({date_label})", fontsize=9)
                ax.set_ylabel(lbl, fontsize=9)
                ax.set_title(lbl, fontsize=10, fontweight="bold")
                _grid(ax)
                plt.tight_layout(pad=1.5)
                pdf.savefig(fig, bbox_inches="tight")
                plt.close(fig)

    buf.seek(0)
    return buf.read()


def generate_component_pdf(
    engine,
    line_id: int,
    component_id: str,
    date_str: str,
    shift: str,
    chart_limits: Optional[dict] = None,
    foundry_cfg: Optional[dict] = None,
) -> bytes:
    """
    Generate a single-component PDF in the same chart format as the production report.
    Pages: header -> prescription monitoring -> compactability -> bad batch -> SMC trends.
    """
    MATERIAL_TOL_PCT, COMP_TOL, _DEFAULT_LIMITS = _resolve_cfg(foundry_cfg)
    _col_limits = {**_DEFAULT_LIMITS, **(chart_limits or {})}

    def _col_lim(col_name, friendly_key):
        b = _col_limits.get(col_name) or _col_limits.get(friendly_key) or {}
        return (b.get("min") if isinstance(b, dict) else None,
                b.get("max") if isinstance(b, dict) else None)

    all_data    = fetch_production_data(engine, line_id, date_str, foundry_cfg=foundry_cfg)
    _add_labels = _fetch_additive_labels(engine, line_id)
    comp_match  = [c for c in all_data.get("components", [])
                   if c["component_id"] == component_id
                   and str(c["shift"]) == str(shift)]

    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        if not comp_match:
            fig, ax = plt.subplots(figsize=(13, 4))
            ax.axis("off")
            ax.text(0.5, 0.5, f"No data found for {component_id} on {date_str} shift {shift}",
                    ha="center", va="center", fontsize=14, color="#888")
            pdf.savefig(fig, bbox_inches="tight"); plt.close(fig)
            buf.seek(0); return buf.read()

        comp       = comp_match[0]
        cid        = comp["component_id"]
        group_name = comp["group_name"] or "—"
        rank       = comp["rank"]
        batches    = comp["batches"]
        preds      = comp["predictions"]

        bent_pred = preds.get("bent", 0) or 0
        mag_pred  = preds.get("mag",  0) or 0
        ns_pred   = preds.get("ns",   0) or 0
        has_preds = bent_pred > 0 or mag_pred > 0 or ns_pred > 0

        ts_list    = [_parse_ts(b.get("timestamp")) for b in batches]
        valid_ts   = [t for t in ts_list if t]
        date_label = valid_ts[0].strftime("%d-%m-%Y") if valid_ts else date_str
        n_total    = len(batches)

        def _col(key): return [b.get(key) for b in batches]

        bent_act  = _clamp(_col("bentonite_actual"),  *_col_lim("bentonite_actual",  "bentonite"))
        mag_act   = _clamp(_col("coal_dust_actual"),  *_col_lim("coal_dust_actual",  "magnacoal"))
        ns_act    = _clamp(_col("fss_actual"),        *_col_lim("fss_actual",        "new_sand"))
        smc_vals  = _clamp(_col("smc"),               *_col_lim("smc",               "compactability"))
        cosp_vals = _col("cosp")
        diff_vals = _col("cosp_diff")
        water_ltr = _clamp(_col("total_water_ltr"),   *_col_lim("total_water_ltr",   "water"))
        temp_c    = _clamp(_col("temperature_c"),     *_col_lim("temperature_c",     "temperature"))
        cycle_sec = _clamp(_col("total_seconds"),     *_col_lim("total_seconds",     "cycle_time"))
        moisture   = _clamp(_col("moisture_smc_pct"),  *_col_lim("moisture_smc_pct",  "moisture"))
        pour_temp  = _col("pouring_temp")
        pour_time  = _col("pouring_time")
        curr_scada = _col("current_scada")

        n_bad    = sum(1 for d in diff_vals if d is not None and abs(d) > COMP_TOL)
        bad_idx  = [i for i, d in enumerate(diff_vals) if d is not None and abs(d) > COMP_TOL]

        # ── Page 1: Component header (light mode) ─────────────────────────────
        fig = plt.figure(figsize=(13, 2.8))
        fig.patch.set_facecolor("#F0F4F8")
        ax_h = fig.add_axes([0.02, 0.06, 0.96, 0.88])
        ax_h.set_facecolor("white"); ax_h.set_xlim(0,1); ax_h.set_ylim(0,1); ax_h.axis("off")
        for sp in ax_h.spines.values():
            sp.set_edgecolor("#B0C4D8"); sp.set_linewidth(1.2); sp.set_visible(True)
        ax_h.axvline(0.005, color=CLR_HEAD, linewidth=6, solid_capstyle="butt")
        ax_h.text(0.025, 0.80, f"#{rank}  Component: {cid}   |   Group: {group_name}   |   Shift {shift}",
                  fontsize=15, fontweight="bold", color=CLR_HEAD, va="top", transform=ax_h.transAxes)
        _lbl_bent = _add_labels.get("bentonite_actual", "Bentonite")
        _lbl_mag  = _add_labels.get("coal_dust_actual", "Coal Dust")
        _lbl_ns   = _add_labels.get("fss_actual", "FSS")
        presc_str = (f"{_lbl_bent}: {bent_pred:.1f} kg    {_lbl_mag}: {mag_pred:.1f} kg    {_lbl_ns}: {ns_pred:.1f} kg"
                     if has_preds else "No prescription available")
        ax_h.text(0.025, 0.50, f"Date: {date_label}    {presc_str}",
                  fontsize=11, color="#444444", va="top", transform=ax_h.transAxes)
        ax_h.text(0.025, 0.20, f"Batches: {n_total}    Bad batches (Comp >±{COMP_TOL}%): {n_bad}",
                  fontsize=10, color="#C01616" if n_bad else "#388E3C",
                  fontweight="bold" if n_bad else "normal", va="top", transform=ax_h.transAxes)
        pdf.savefig(fig, bbox_inches="tight"); plt.close(fig)

        # ── Page 2: Prescription Monitoring ───────────────────────────────────
        if has_preds:
            materials = [
                (bent_act, bent_pred, _lbl_bent),
                (mag_act,  mag_pred,  _lbl_mag),
                (ns_act,   ns_pred,   _lbl_ns),
            ]
            fig, axes_pm = plt.subplots(1, 3, figsize=(15, 5.5))
            fig.suptitle(f"Prescription Monitoring  |  #{rank} {cid} ({group_name})  |  Shift {shift}  |  {date_label}",
                         fontsize=11, fontweight="bold", color=CLR_HEAD)
            for ax_pm, (actual_list, pred, ylabel) in zip(axes_pm, materials):
                upper = pred * (1 + MATERIAL_TOL_PCT / 100)
                lower = pred * (1 - MATERIAL_TOL_PCT / 100)
                pairs = [(t, v) for t, v in zip(ts_list, actual_list)
                         if t is not None and v is not None]
                if pairs:
                    xs, ys = zip(*pairs)
                    is_bad_pts = [v < lower or v > upper for v in ys]
                    _scatter_good_bad(ax_pm, list(xs), list(ys), is_bad_pts)
                    _fmt_xaxis(ax_pm, list(xs))
                ax_pm.axhline(pred,  color=CLR_PRED, linewidth=1.8, linestyle="-")
                ax_pm.axhline(upper, color=CLR_BAND, linewidth=1.1, linestyle="--")
                ax_pm.axhline(lower, color=CLR_BAND, linewidth=1.1, linestyle="--")
                ax_pm.set_xlabel(f"Time  ({date_label})", fontsize=8)
                ax_pm.set_ylabel(ylabel, fontsize=8)
                ax_pm.set_title(f"{ylabel.split(' ')[0]}  —  Actual vs Predicted", fontsize=9, fontweight="bold")
                _grid(ax_pm)
            from matplotlib.lines import Line2D as _L2D
            _leg = [
                _L2D([0],[0], color=CLR_GOOD, linewidth=1.5, marker="o", markersize=4, label="Actual"),
                _L2D([0],[0], color=CLR_BAD,  linewidth=0,   marker="o", markersize=5, label="Out of spec"),
                _L2D([0],[0], color=CLR_PRED, linewidth=1.8, linestyle="-",  label="Predicted"),
                _L2D([0],[0], color=CLR_BAND, linewidth=1.1, linestyle="--", label=f"±{MATERIAL_TOL_PCT:.0f}% tolerance"),
            ]
            fig.legend(handles=_leg, loc="lower center", ncol=4, fontsize=8,
                       frameon=True, framealpha=0.95, bbox_to_anchor=(0.5, 0.0))
            fig.subplots_adjust(left=0.06, right=0.98, top=0.92, bottom=0.18, wspace=0.32)
            pdf.savefig(fig, bbox_inches="tight"); plt.close(fig)

        # ── Page 3: Compactability ─────────────────────────────────────────────
        valid_comp = [(t,s,c,d) for t,s,c,d in zip(ts_list,smc_vals,cosp_vals,diff_vals)
                      if t is not None and s is not None]
        if valid_comp:
            comp_ts, comp_smc, comp_cosp, comp_diff = zip(*valid_comp)
            fig, ax = plt.subplots(figsize=(10, 5.5))
            fig.suptitle(f"Compactability  |  #{rank} {cid} ({group_name})  |  Shift {shift}",
                         fontsize=11, fontweight="bold", color=CLR_HEAD)
            is_bad_c = [abs(d) > COMP_TOL if d is not None else False for d in comp_diff]
            _scatter_good_bad(ax, list(comp_ts), list(comp_smc), is_bad_c)
            if any(v is not None for v in comp_cosp):
                cosp_arr = np.array([v if v is not None else np.nan for v in comp_cosp])
                ax.plot(list(comp_ts), cosp_arr,             color="red", linewidth=1.8, linestyle="-",  label="COSP Setpoint")
                ax.plot(list(comp_ts), cosp_arr + COMP_TOL,  color="orange", linewidth=1, linestyle=":", label=f"+{COMP_TOL:.0f}")
                ax.plot(list(comp_ts), cosp_arr - COMP_TOL,  color="orange", linewidth=1, linestyle=":", label=f"-{COMP_TOL:.0f}")
            _fmt_xaxis(ax, list(comp_ts))
            ax.set_xlabel(f"Time  ({date_label})", fontsize=8)
            ax.set_ylabel("Compactability (%)", fontsize=8)
            ax.set_title("SMC Discharge vs COSP Setpoint", fontsize=9, fontweight="bold")
            _grid(ax)
            h_c, l_c = ax.get_legend_handles_labels()
            fig.legend(h_c, l_c, loc="lower center", ncol=5, fontsize=8,
                       frameon=True, framealpha=0.95, bbox_to_anchor=(0.5, 0.0))
            fig.subplots_adjust(bottom=0.16)
            pdf.savefig(fig, bbox_inches="tight"); plt.close(fig)

        # ── Page 4: Batch Analysis (all batches, bad ones highlighted) ────────
        fig_h = max(6, 1.5 + n_total * 0.35)
        fig = plt.figure(figsize=(13, fig_h))
        fig.suptitle(f"Batch Analysis  |  #{rank} {cid} ({group_name})  |  Shift {shift}  |  {len(bad_idx)} bad / {n_total} total",
                     fontsize=11, fontweight="bold", color=CLR_HEAD)
        ax_t = fig.add_axes([0.02, 0.02, 0.96, 0.96]); ax_t.axis("off")
        ax_t.patch.set_visible(False)
        _draw_batch_table(ax_t, batches, ts_list, diff_vals,
                          n_total, bad_idx, rank, cid, group_name, shift, COMP_TOL)
        pdf.savefig(fig, bbox_inches="tight", pad_inches=0.1); plt.close(fig)

        # ── Page 5: All trend charts ───────────────────────────────────────────
        import math as _math
        all_trend = [
            (bent_act,   _lbl_bent),
            (mag_act,    _lbl_mag),
            (ns_act,     _lbl_ns),
            (cycle_sec,  _add_labels.get("total_seconds",    "Mix Cycle Time (sec)")),
            (water_ltr,  _add_labels.get("total_water_ltr",  "Water Added (ltr)")),
            (temp_c,     _add_labels.get("temperature_c",    "Sand Temperature (°C)")),
            (moisture,   _add_labels.get("moisture_smc_pct", "Moisture SMC (%)")),
            (pour_temp,  "Pouring Temp (°C)"),
            (pour_time,  "Pouring Time (sec)"),
            (curr_scada, "Current SCADA (Amp)"),
        ]
        avail = [(v, l) for v, l in all_trend if any(x is not None for x in v)]
        # One page per chart — no combined grids
        for vals, lbl in avail:
            pairs = [(t, v) for t, v in zip(ts_list, vals) if t is not None and v is not None]
            if not pairs:
                continue
            fig, ax = plt.subplots(figsize=(11, 6))
            fig.patch.set_facecolor("white")
            fig.suptitle(f"{lbl}  |  #{rank} {cid}  |  Shift {shift}",
                         fontsize=11, fontweight="bold", color=CLR_HEAD)
            xs_all = [p[0] for p in pairs]
            ys_all = [p[1] for p in pairs]
            _plot_with_gap_break(ax, xs_all, ys_all, gap_minutes=30,
                                 linewidth=2.0, color=CLR_GOOD)
            _fmt_xaxis(ax, xs_all)
            ax.set_xlabel(f"Time  ({date_label})", fontsize=9)
            ax.set_ylabel(lbl, fontsize=9)
            ax.set_title(lbl, fontsize=10, fontweight="bold")
            _grid(ax)
            plt.tight_layout(pad=1.5)
            pdf.savefig(fig, bbox_inches="tight"); plt.close(fig)

    buf.seek(0)
    return buf.read()


# ══════════════════════════════════════════════════════════════════════════════
#  PER-COMPONENT PPTX GENERATOR  (sandman slide template)
# ══════════════════════════════════════════════════════════════════════════════

# Slide template colours
_GREEN_HEADER = "#5D8C3C"    # dark green header bar
_FOOTER_GREEN = "#8DC63F"    # lime green footer badge
_FOOTER_TEXT  = "#2D5A1B"    # dark text on footer badge
_CHART_BLUE   = "#0070C0"    # actual value line
_CHART_RED    = "#FF0000"    # predicted / setpoint line
_CHART_GDASH  = "#00B050"    # tolerance dashed lines


def generate_component_pptx(
    engine,
    line_id: int,
    component_id: str,
    date_str: str,
    shift: str,
    chart_limits: Optional[dict] = None,
    foundry_cfg: Optional[dict] = None,
) -> bytes:
    """SandMan template-based PPTX: Info | Additives | SMC+Trends | Pouring | Batch Table | Prepared Sand."""
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    import pathlib as _pl, io as _io

    _BASE = _pl.Path(__file__).parent
    _LOGO_PATH = _BASE / "template_logo.png"
    _LOGO_BYTES = _LOGO_PATH.read_bytes() if _LOGO_PATH.exists() else None

    SW = Inches(13.33); SH = Inches(7.50)
    prs = Presentation(); prs.slide_width = SW; prs.slide_height = SH
    blank = prs.slide_layouts[6]

    _GRN="92D050"; _DARK="1F3864"; _WHT="FFFFFF"; _BLK="000000"; _MUT="595959"
    _FONT_HDR="Whitney Semibold"; _FONT_BODY="Calibri"; _SZ_HDR=28; _SZ_FTR=28

    def _rgb(h):
        h=h.lstrip("#"); return RGBColor(int(h[0:2],16),int(h[2:4],16),int(h[4:6],16))

    def _rect(sl,l,t,w,h,fill,ln=None,lw=0):
        r=sl.shapes.add_shape(1,l,t,w,h)
        r.fill.solid(); r.fill.fore_color.rgb=_rgb(fill)
        if ln: r.line.color.rgb=_rgb(ln); r.line.width=Pt(lw)
        else: r.line.fill.background()
        return r

    def _txt(sl,text,l,t,w,h,size,bold=False,color=_BLK,align=PP_ALIGN.LEFT,font=_FONT_BODY,wrap=True,italic=False):
        tb=sl.shapes.add_textbox(l,t,w,h); tf=tb.text_frame; tf.word_wrap=wrap
        p=tf.paragraphs[0]; p.alignment=align; rn=p.add_run(); rn.text=text
        rn.font.size=Pt(size); rn.font.bold=bold; rn.font.italic=italic
        rn.font.color.rgb=_rgb(color.lstrip("#")); rn.font.name=font; return tb

    def _pic(sl,img_bytes,l,t,w,h):
        sl.shapes.add_picture(_io.BytesIO(img_bytes),l,t,w,h)

    def _chrome(sl,title="",sub=""):
        # Green header bar: W=9.22", H=0.99" at (0",0")
        _rect(sl,Inches(0),Inches(0),Inches(9.22),Inches(0.99),_GRN)
        # Sandman logo: H=10.57" V=0.04" size W=2.52" H=0.83"
        if _LOGO_BYTES:
            _pic(sl,_LOGO_BYTES,Inches(10.57),Inches(0.04),Inches(2.52),Inches(0.83))
        # Header title text — BLACK
        if title:
            _txt(sl,title,Inches(0.18),Inches(0.08),Inches(9.0),Inches(0.48),
                 size=_SZ_HDR,bold=True,color=_BLK,font=_FONT_HDR,align=PP_ALIGN.LEFT)
        if sub:
            _txt(sl,sub,Inches(0.18),Inches(0.52),Inches(9.0),Inches(0.40),
                 size=12,bold=False,color=_BLK,font=_FONT_HDR,align=PP_ALIGN.LEFT)
        # Footer green card: W=6.41" H=0.81" pos H=6.92" V=6.69"
        _rect(sl,Inches(6.92),Inches(6.69),Inches(6.41),Inches(0.81),_GRN)
        # Footer text: W=6.13" H=0.57" pos H=7.06" V=6.81" BLACK
        _txt(sl,"MPM INFOSOFT PRIVATE LIMITED",
             Inches(7.06),Inches(6.81),Inches(6.13),Inches(0.57),
             size=_SZ_FTR,bold=True,color=_BLK,font=_FONT_HDR,align=PP_ALIGN.CENTER)

    MATERIAL_TOL_PCT, COMP_TOL, _DEFAULT_LIMITS = _resolve_cfg(foundry_cfg)
    _col_limits = {**_DEFAULT_LIMITS, **(chart_limits or {})}
    def _col_lim(col_name,friendly_key):
        b=_col_limits.get(col_name) or _col_limits.get(friendly_key) or {}
        return (b.get("min") if isinstance(b,dict) else None,
                b.get("max") if isinstance(b,dict) else None)

    # Bad-batch plot tolerance comes from _resolve_cfg above (foundry_cfg -> bad_batch_watchdog.threshold)
    _plot_tol = float(COMP_TOL)

    all_data=fetch_production_data(engine,line_id,date_str,foundry_cfg=foundry_cfg)
    comp_match=[c for c in all_data.get("components",[])
                if c["component_id"]==component_id and str(c["shift"])==str(shift)]

    if not comp_match:
        sl=prs.slides.add_slide(blank); _chrome(sl,"No Data")
        _txt(sl,f"No data for {component_id} on {date_str} Shift {shift}.",
             Inches(1),Inches(3),Inches(11),Inches(1),size=16,color=_MUT)
        out=_io.BytesIO(); prs.save(out); out.seek(0); return out.read()

    comp=comp_match[0]; cid=comp["component_id"]; group_name=comp["group_name"] or "—"
    rank=comp["rank"]; batches=comp["batches"]; preds=comp["predictions"]
    bent_pred=preds.get("bent",0) or 0; mag_pred=preds.get("mag",0) or 0
    ns_pred=preds.get("ns",0) or 0; has_preds=bent_pred>0 or mag_pred>0 or ns_pred>0

    ts_list=[_parse_ts(b.get("timestamp")) for b in batches]
    valid_ts=[t for t in ts_list if t]
    date_label=valid_ts[0].strftime("%d-%m-%Y") if valid_ts else date_str
    t_start=valid_ts[0].strftime("%H:%M") if valid_ts else "--"
    t_end=valid_ts[-1].strftime("%H:%M") if valid_ts else "--"
    n_total=len(batches)

    def _col(key): return [b.get(key) for b in batches]

    bent_act=_clamp(_col("bentonite_actual"),*_col_lim("bentonite_actual","bentonite"))
    mag_act=_clamp(_col("coal_dust_actual"),*_col_lim("coal_dust_actual","magnacoal"))
    ns_act=_clamp(_col("fss_actual"),*_col_lim("fss_actual","new_sand"))
    smc_vals=_clamp(_col("smc"),*_col_lim("smc","compactability"))
    cosp_vals=_col("cosp"); diff_vals=_col("cosp_diff")
    water_ltr=_clamp(_col("total_water_ltr"),*_col_lim("total_water_ltr","water"))
    temp_c=_clamp(_col("temperature_c"),*_col_lim("temperature_c","temperature"))
    cycle_sec=_clamp(_col("total_seconds"),*_col_lim("total_seconds","cycle_time"))
    moisture=_clamp(_col("moisture_smc_pct"),*_col_lim("moisture_smc_pct","moisture"))
    pour_temp=_col("pouring_temp"); pour_time=_col("pouring_time")
    n_bad=sum(1 for d in diff_vals if d is not None and abs(d)>COMP_TOL)
    bad_idx=[i for i,d in enumerate(diff_vals) if d is not None and abs(d)>COMP_TOL]

    _ps_rows=[]; _ps_cols=[]
    try:
        from .report_generator import fetch_component_data as _fcd
        _fd=_fcd(engine,line_id,component_id,date_str,shift or None)
        _ps_rows=_fd.get("ps_rows",[]); _ps_cols=_fd.get("ps_cols",[])
    except Exception: pass

    def _style(ax,xlabel=None,ylabel=None,title=None):
        ax.set_facecolor("#FFFFFF")
        ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color("#CBD5E1"); ax.spines["bottom"].set_color("#CBD5E1")
        ax.grid(True,linestyle="--",alpha=0.4,color="#E2E8F0")
        ax.tick_params(labelsize=10,colors="#374151")
        if xlabel: ax.set_xlabel(xlabel,fontsize=11,fontweight="bold",color="#374151",labelpad=4)
        if ylabel: ax.set_ylabel(ylabel,fontsize=11,fontweight="bold",color="#374151",labelpad=4)
        if title: ax.set_title(title,fontsize=13,fontweight="bold",color="#1E293B",pad=7)

    _CW=Inches(4.16); _CH=Inches(3.10)   # default chart size
    def _embed(sl,fig,px,py,cw=None,ch=None):
        buf=_io.BytesIO()
        fig.savefig(buf,format="png",dpi=150,bbox_inches="tight",facecolor="white")
        buf.seek(0); plt.close(fig)
        sl.shapes.add_picture(buf,px,py,cw or _CW,ch or _CH)

    # ── Chart layout ──────────────────────────────────────────────────────
    # Slide: 13.33"×7.50" | Header: 0"–0.99" | Content: 1.02"–7.08"
    _CV  = Inches(1.08)   # chart top (below header)
    _CH6 = Inches(3.10)   # chart height: landscape 4.16:3.10 = 1.34:1
    # 3-chart: W=4.16" each, margin=0.18", gap=0.25"
    _CW3 = Inches(4.16)
    _POS3_ADD = [(Inches(0.18), _CV), (Inches(4.59), _CV), (Inches(9.00), _CV)]
    _POS3_TR  = [(Inches(0.18), _CV), (Inches(4.59), _CV), (Inches(9.00), _CV)]
    # 2-chart: W=5.96" each, margin=0.50", gap=0.37"
    _CW2 = Inches(5.96)
    _POS2_PR  = [(Inches(0.50), _CV), (Inches(6.87), _CV)]

    def _chunks(lst,n):
        for i in range(0,len(lst),n): yield lst[i:i+n]

    # ── Slide 1: prescription from monitored config params only ────────────
    _presc_items = []
    try:
        from .config_store import load_foundry_config as _lcfgP
        _db_lblP = str(engine.url).split("/")[-1].split("?")[0] + f"_L{line_id}"
        _fcP = _lcfgP(engine, _db_lblP)
        # Only show params checked in Prescription Monitoring config
        _monitored = _fcP.get("prescription_watchdog", {}).get(
            "monitored_params", ["bentonite","freshSilicaSand","lca","water"])
        # Fetch UI labels for additive columns
        _add_lbls = _fetch_additive_labels(engine, line_id)
        # Map param key -> display label (strip unit from brackets)
        import re as _re3
        def _strip_unit(s):
            m = _re3.search(r"\(([^)]+)\)$", s)
            return (s[:s.rfind("(")].strip(), m.group(1)) if m else (s, "kg")
        # Param key -> actual column name mapping
        _KEY_COL = {
            "bentonite":       "bentonite_actual",
            "lca":             "coal_dust_actual",
            "freshSilicaSand": "fss_actual",
            "freshsilicasand": "fss_actual",
            "water":           "total_water_ltr",
        }
        # Get analytics_report predicted values for this group+date+shift
        _ar_vals = {}
        try:
            import re as _re2
            _grp_n = _re2.sub(r'[-\s]', '', (group_name or '').strip().lower())
            from sqlalchemy import text as _sqlT
            with engine.connect() as _arc:
                _ar_row = _arc.execute(_sqlT("""
                    SELECT predicted_additives_json
                    FROM analytics_report
                    WHERE foundry_line_pkey=:fl
                      AND LOWER(REPLACE(REPLACE(IFNULL(foundry_line_group_name,''),'-',''),' ',''))=:grp
                      AND DATE(date)=:dt AND shift=:sh AND deleted=0
                    ORDER BY pkey DESC LIMIT 1
                """), {"fl":line_id,"grp":_grp_n,"dt":date_str,"sh":str(shift)}).mappings().first()
            if _ar_row and _ar_row["predicted_additives_json"]:
                import json as _jj
                _raw = _ar_row["predicted_additives_json"]
                _ar_vals = _jj.loads(_raw) if isinstance(_raw,str) else dict(_raw)
        except Exception: pass
        for _pk in _monitored:
            _col = _KEY_COL.get(_pk, _pk+"_actual")
            _raw_lbl = _add_lbls.get(_col, _pk.replace("_"," ").title())
            _lbl, _unit = _strip_unit(_raw_lbl)
            # Get predicted value: try param key directly, then mapped key
            _val = _ar_vals.get(_pk) or _ar_vals.get(
                {"lca":"lca","freshSilicaSand":"freshSilicaSand"}.get(_pk,_pk))
            if _val is None: _val = _ar_vals.get(_pk.lower())
            _presc_items.append({"param":_pk,"label":_lbl,"value":_val,"unit":_unit})
    except Exception as _pe:
        import traceback; traceback.print_exc()
    # Fallback to preds if config fetch failed
    if not _presc_items:
        for _pk,_pv,_pl,_pu in [("bentonite",bent_pred,"Bentonite","kg"),
                                 ("lca",mag_pred,"Coal Dust / LCA","kg"),
                                 ("freshSilicaSand",ns_pred,"Fresh Silica Sand","kg")]:
            if _pv and _pv>0:
                _presc_items.append({"param":_pk,"label":_pl,"value":_pv,"unit":_pu})

    sl1=prs.slides.add_slide(blank)
    _chrome(sl1,"Component Report",
            f"{date_label}  ·  Shift {shift}  ·  Line {line_id}")
    try:
        from .slide1_renderer import render_slide1_html as _r1
        _png1 = _r1(
            cid=cid, group_name=group_name, date_label=date_label,
            shift=shift, line_id=line_id,
            t_start=t_start, t_end=t_end,
            n_total=n_total, n_bad=n_bad,
            presc_items=_presc_items,
        )
        # H.pos=1.7", V.pos=1.25", W=9.95", H=5.35"
        sl1.shapes.add_picture(_io.BytesIO(_png1),
            Inches(1.7), Inches(1.25),
            Inches(9.95), Inches(5.35))
    except Exception as _e:
        import traceback; traceback.print_exc()

    if has_preds:
        from matplotlib.lines import Line2D as _L2D
        sl2=prs.slides.add_slide(blank)
        _chrome(sl2,"Additive Prescription",f"{cid}  ·  {date_label}  ·  Shift {shift}")
        add_defs=[(bent_act,bent_pred,"Bentonite","Bentonite (kg)"),
                  (mag_act,mag_pred,"Coal Dust / LCA","Coal Dust / LCA (kg)"),
                  (ns_act,ns_pred,"Fresh Silica Sand","Fresh Silica Sand (kg)")]
        for (al,pred,pname,ylabel),(px,py) in zip(add_defs,_POS3_ADD):
            upper=pred*(1+MATERIAL_TOL_PCT/100); lower=pred*(1-MATERIAL_TOL_PCT/100)
            fig,ax=plt.subplots(figsize=(5.2,3.9)); fig.patch.set_facecolor("white")
            pairs=[(t,v) for t,v in zip(ts_list,al) if t and v is not None]
            if pairs:
                xs,ys=zip(*pairs)
                ax.plot(list(xs),list(ys),color="#3B82F6",linewidth=1.8,marker="o",markersize=4,label="Actual")
                _fmt_xaxis(ax,list(xs))
            ax.axhline(pred,color="#EF4444",linewidth=1.8,linestyle="-",label="Prescribed")
            ax.axhline(upper,color="#10B981",linewidth=1.0,linestyle="--",label=f"+{MATERIAL_TOL_PCT:.0f}% tol")
            ax.axhline(lower,color="#10B981",linewidth=1.0,linestyle=":",label=f"-{MATERIAL_TOL_PCT:.0f}% tol")
            _style(ax,ylabel=ylabel,title=pname)
            ax.legend(fontsize=9,loc="upper center",ncol=4,frameon=True,framealpha=0.95,bbox_to_anchor=(0.5,-0.16),borderpad=0.5)
            fig.subplots_adjust(left=0.18,right=0.97,top=0.88,bottom=0.26)
            _embed(sl2,fig,px,py,cw=_CW3,ch=_CH6)

    # ── Slides 3+: SMC + Trends (3 per slide) ────────────────────────────────────────────────────────────────────────────
    trend_items=[]
    valid_smc=[(t,s,c,d) for t,s,c,d in zip(ts_list,smc_vals,cosp_vals,diff_vals) if t and s is not None]
    if valid_smc: trend_items.append(("smc",valid_smc))
    # Additives already shown in Prescription slide — only show process params here
    for vals,lbl in [(water_ltr,"Water Added (ltr)"),
                     (temp_c,"Sand Temperature (°C)"),
                     (cycle_sec,"Mix Cycle Time (sec)"),
                     (moisture,"Moisture SMC (%)")]:
        pairs=[(t,v) for t,v in zip(ts_list,vals) if t and v is not None]
        if pairs: trend_items.append(("trend",pairs,lbl))
    for chunk in _chunks(trend_items,3):
        sl_t=prs.slides.add_slide(blank)
        _chrome(sl_t,"SMC & Process Trends",f"{cid}  ·  {date_label}  ·  Shift {shift}")
        _pos=_POS2_PR if len(chunk)==2 else _POS3_TR
        for item,(px,py) in zip(chunk,_pos):
            if item[0]=="smc":
                _,vd=item; ts_c,smc_c,cosp_c,diff_c=zip(*vd)
                is_bad=[abs(d)>_plot_tol if d is not None else False for d in diff_c]
                cosp_arr=np.array([v if v is not None else np.nan for v in cosp_c])
                fig,ax=plt.subplots(figsize=(5.2,3.9)); fig.patch.set_facecolor("white")
                ax.plot(list(ts_c),list(smc_c),color="#3B82F6",linewidth=1.8,zorder=3,label="SMC")
                ax.plot(list(ts_c),cosp_arr,color="#EF4444",linewidth=1.5,linestyle="--",zorder=4,label="COSP")
                ax.plot(list(ts_c),cosp_arr+_plot_tol,color="#10B981",linewidth=1.2,linestyle="--",zorder=4,label=f"+{_plot_tol:.1f} tol")
                ax.plot(list(ts_c),cosp_arr-_plot_tol,color="#10B981",linewidth=1.2,linestyle=":",zorder=4,label=f"-{_plot_tol:.1f} tol")
                gx=[x for x,b in zip(ts_c,is_bad) if not b]; gy=[y for y,b in zip(smc_c,is_bad) if not b]
                bx=[x for x,b in zip(ts_c,is_bad) if b]; by=[y for y,b in zip(smc_c,is_bad) if b]
                if gx: ax.scatter(gx,gy,color="#3B82F6",s=20,zorder=5)
                if bx: ax.scatter(bx,by,color="#EF4444",s=28,zorder=6,label="Bad")
                _fmt_xaxis(ax,list(ts_c))
                _style(ax,ylabel="Compactability (%)",title="SMC Discharge vs COSP")
                ax.legend(fontsize=9,loc="upper center",ncol=4,frameon=True,framealpha=0.95,bbox_to_anchor=(0.5,-0.16),borderpad=0.5)
                fig.subplots_adjust(left=0.18,right=0.97,top=0.88,bottom=0.26)
                _embed(sl_t,fig,px,py,cw=_CW3,ch=_CH6)
            else:
                _,pairs,lbl=item; xs,ys=zip(*pairs)
                fig,ax=plt.subplots(figsize=(5.2,3.9)); fig.patch.set_facecolor("white")
                ax.plot(list(xs),list(ys),color="#3B82F6",linewidth=1.8,marker="o",markersize=4)
                _fmt_xaxis(ax,list(xs)); _style(ax,ylabel=lbl,title=lbl)
                fig.subplots_adjust(left=0.18,right=0.97,top=0.88,bottom=0.26)
                _embed(sl_t,fig,px,py,cw=_CW3,ch=_CH6)

    # ── Pouring Data (2 per slide) ────────────────────────────────────────────────────────────────────────────────────
    pour_avail=[(v,l) for v,l in [(pour_temp,"Pouring Temp (°C)"),(pour_time,"Pouring Time (sec)")] if any(x is not None for x in v)]
    if pour_avail:
        for chunk in _chunks(pour_avail,2):
            sl_p=prs.slides.add_slide(blank)
            _chrome(sl_p,"Pouring Data",f"{cid}  ·  {date_label}  ·  Shift {shift}")
            for (vals,lbl),(px,py) in zip(chunk,_POS2_PR):
                pairs=[]
                for t,v in zip(ts_list,vals):
                    if t is None or v is None: continue
                    try:
                        from datetime import timedelta as _td
                        fv=v.total_seconds() if isinstance(v,_td) else float(v)
                        pairs.append((t,fv))
                    except Exception: pass
                if not pairs: continue
                xs,ys=zip(*pairs)
                fig,ax=plt.subplots(figsize=(5.2,3.9)); fig.patch.set_facecolor("white")
                ax.plot(list(xs),list(ys),color="#3B82F6",linewidth=1.8,marker="o",markersize=5)
                _fmt_xaxis(ax,list(xs)); _style(ax,xlabel=f"Time ({date_label})",ylabel=lbl,title=lbl)
                fig.subplots_adjust(left=0.18,right=0.97,top=0.88,bottom=0.26)
                _embed(sl_p,fig,px,py,cw=_CW2,ch=_CH6)

    # ── Batch Analysis Table ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # ── Batch Analysis — native PPT table with all mixer data ──────────────
    from pptx.oxml.ns import qn as _bqn
    from pptx.oxml import parse_xml as _bpxml
    _B_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

    _BATCH_COLS = [
        ("batch_no",          "Batch #"),
        ("time",              "Time"),
        ("smc",               "SMC Discharge"),
        ("cosp",              "COSP %"),
        ("cosp_diff",         "Difference"),
        ("bentonite_actual",  "Bentonite (kg)"),
        ("coal_dust_actual",  "Coal Dust (kg)"),
        ("fss_actual",        "New Sand (kg)"),
        ("water_actual",      "Water (ltr)"),
        ("total_water_ltr",   "Water Added (ltr)"),
        ("temperature_c",     "Temp (C)"),
        ("total_seconds",     "Cycle Time (s)"),
        ("moisture_smc_pct",  "Moisture (%)"),
        ("recycle_sand_actual","Return Sand"),
    ]

    _b_rows = []
    for bi, b in enumerate(batches):
        ts = _parse_ts(b.get("timestamp"))
        t_str = ts.strftime("%H:%M") if ts else "--"
        diff = b.get("cosp_diff")
        is_bad_b = diff is not None and abs(diff) > _plot_tol
        diff_str = (f"+{diff:.3f}" if diff >= 0 else f"{diff:.3f}") if diff is not None else "-"
        row_vals = []
        for col_key, _ in _BATCH_COLS:
            if col_key == "batch_no":
                row_vals.append(str(bi+1))
            elif col_key == "time":
                row_vals.append(t_str)
            elif col_key == "cosp_diff":
                row_vals.append(diff_str)
            else:
                v = b.get(col_key)
                if v is None:
                    row_vals.append("-")
                else:
                    try: row_vals.append(f"{float(v):.2f}")
                    except: row_vals.append(str(v))
        _b_rows.append((row_vals, is_bad_b))

    # Remove EXTRA columns where ALL values are "-" (completely null)
    _FIXED_KEYS = {"batch_no","time","cosp_diff","smc","cosp"}
    _FIXED = [(k,l) for k,l in _BATCH_COLS if k in _FIXED_KEYS]
    _ALL_EXTRA = [(k,l) for k,l in _BATCH_COLS if k not in _FIXED_KEYS]
    # Keep only columns that have at least one non-"-" value
    _col_idx_all = {c[0]: i for i, c in enumerate(_BATCH_COLS)}
    _EXTRA = [(k,l) for k,l in _ALL_EXTRA
              if any(row_vals[_col_idx_all.get(k,-1)] not in ("-","") 
                     for row_vals,_ in _b_rows if _col_idx_all.get(k,-1) >= 0)]
    _MAX_E = 6
    _col_chunks = [_EXTRA[i:i+_MAX_E] for i in range(0, max(len(_EXTRA),1), _MAX_E)]
    _col_idx_map = {c[0]: i for i, c in enumerate(_BATCH_COLS)}

    # Cell styler — defined ONCE outside the loop
    def _bc(cell, text, bold=False, txt_col="FFFFFF", size=10, bg=None):
        cell.text = ""
        tf = cell.text_frame; tf.word_wrap = False
        p = tf.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
        rn = p.add_run(); rn.text = str(text)
        rn.font.size = Pt(size); rn.font.bold = bold
        rn.font.color.rgb = _rgb(txt_col)
        rn.font.name = _FONT_HDR if bold else _FONT_BODY
        tc = cell._tc; tcPr = tc.get_or_add_tcPr()
        if bg:
            for old in tcPr.findall(_bqn("a:solidFill")): tcPr.remove(old)
            tcPr.insert(0, _bpxml(f'<a:solidFill xmlns:a="{_B_NS}"><a:srgbClr val="{bg}"/></a:solidFill>'))
        bdr = "BFC9D9"
        for side in ["lnL","lnR","lnT","lnB"]:
            for old in tcPr.findall(_bqn(f"a:{side}")): tcPr.remove(old)
            tcPr.append(_bpxml(f'<a:{side} xmlns:a="{_B_NS}" w="9525"><a:solidFill><a:srgbClr val="{bdr}"/></a:solidFill><a:prstDash val="solid"/></a:{side}>'))

    # Single slide for ALL rows — scale row height to fit
    _TBL_AVAIL_H = Inches(5.50)   # max table height (fits slide content area)
    _HH_B = Inches(0.38)          # header row height
    _n_data_rows = max(len(_b_rows), 1)
    _RH_B = int(max(Inches(0.22), (_TBL_AVAIL_H - _HH_B) / _n_data_rows))
    _font_sz = 9 if _n_data_rows > 18 else 10 if _n_data_rows > 12 else 11
    # All rows in ONE chunk — single slide
    _row_chunks = [_b_rows]
    _total_slides = len(_col_chunks)
    _slide_num = 0
    for _ci, _extra_chunk in enumerate(_col_chunks):
        _slide_cols = _FIXED + _extra_chunk
        _n_cols = len(_slide_cols)
        for _ri, _row_page in enumerate(_row_chunks):
            _slide_num += 1
            _n_rows = len(_row_page)
            _pg = f" ({_slide_num}/{_total_slides})" if _total_slides > 1 else ""
            _r_start = _ri * max(len(_row_page), 1) + 1
            _r_end   = _r_start + _n_rows - 1
            _row_info = f"Rows {_r_start}–{_r_end}" if len(_row_chunks) > 1 else ""
            sl_b = prs.slides.add_slide(blank)
            _sub = f"{cid}  ·  Shift {shift}  ·  {len(bad_idx)} bad / {n_total} total"
            if _row_info: _sub += f"  ·  {_row_info}"
            _chrome(sl_b, f"Batch Analysis{_pg}", _sub)
            _TL=Inches(0.86); _TT=Inches(1.09); _TW=Inches(11.22)
            _HH=_HH_B; _RH=_RH_B  # fixed readable row height, no shrink
            tshape = sl_b.shapes.add_table(_n_rows+1, _n_cols, _TL, _TT, _TW, _HH+_RH*_n_rows)
            tobj = tshape.table
            tobj.rows[0].height = _HH
            for _rri in range(_n_rows): tobj.rows[_rri+1].height = _RH
            _cw = _TW // _n_cols
            for _c in tobj.columns: _c.width = _cw
            for j, (_, lbl) in enumerate(_slide_cols):
                _bc(tobj.cell(0, j), lbl, bold=True, txt_col="FFFFFF", size=_font_sz, bg="1F3864")
            for ri, (row_vals, is_bad_b) in enumerate(_row_page):
                row_bg = "FEE2E2" if is_bad_b else ("F0F4FA" if ri%2==0 else "FFFFFF")
                for j, (col_key, _) in enumerate(_slide_cols):
                    orig_idx = _col_idx_map.get(col_key, -1)
                    val = row_vals[orig_idx] if orig_idx >= 0 else "-"
                    if col_key == "cosp_diff":
                        tc_col = "DC2626" if is_bad_b else "16A34A"
                        _bc(tobj.cell(ri+1, j), val, bold=is_bad_b, txt_col=tc_col, size=_font_sz, bg=row_bg)
                    else:
                        _bc(tobj.cell(ri+1, j), val, bold=False, txt_col="1E293B", size=_font_sz, bg=row_bg)

    # ── Prepared Sand: add as bottom table on LAST batch slide ──────────────
    if _ps_rows and _ps_rows:
        # Build PS table data (reuse existing _PS_DISP + col_keys logic done above)
        try:
            from pptx.oxml.ns import qn as _qn2
            from pptx.oxml import parse_xml as _pxml2
            _B2_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

            # Determine which PS columns to show
            _PS_DISP2 = {
                "active_clay":"Active Clay (%)","compactibility":"Compactability (%)",
                "gcs":"GCS (gm/cm²)","gfn_afs":"GFN/AFS (no)",
                "inert_fines":"Inert Fines (%)","loi":"LOI (%)",
                "moisture":"Moisture (%)","permeability":"Permeability (no)",
                "shear_strength":"Shear Strength (gm/cm²)","split_strength":"Split Strength (gm/cm²)",
                "temp_of_sand_after_mix":"Sand Temp (°C)","volatile_matter":"Volatile Matter (%)",
            }
            _ps_col_keys2 = [k for k in _PS_DISP2
                             if any(r.get(k) is not None for r in _ps_rows)]
            _ps_headers2 = ["Time","Date"] + [_PS_DISP2[k] for k in _ps_col_keys2]
            _ps_data2 = []
            for row in _ps_rows:
                from datetime import timedelta as _td2
                _t = row.get("time") or ""
                _d = row.get("date") or ""
                if isinstance(_t, _td2):
                    _s = int(_t.total_seconds())
                    t_s = f"{_s//3600:02d}:{(_s%3600)//60:02d}"
                elif hasattr(_t,"strftime"): t_s = _t.strftime("%H:%M")
                else: t_s = str(_t)[:5] if _t else "-"
                if hasattr(_d,"strftime"): d_s = _d.strftime("%d-%m-%Y")
                else: d_s = str(_d)[:10] if _d else "-"
                cells = [t_s, d_s]
                for k in _ps_col_keys2:
                    v = row.get(k)
                    try: cells.append(f"{float(v):.2f}" if v is not None else "-")
                    except: cells.append(str(v) if v else "-")
                _ps_data2.append(cells)

            _ps_n_cols = len(_ps_headers2)
            _ps_n_rows = len(_ps_data2)

            if _ps_n_cols > 0 and _ps_n_rows > 0:
                # Add small label + table on the LAST batch slide (sl_b)
                # Position: below batch table with small gap
                _PS_T = int(Inches(1.09) + Inches(0.46) + _RH_B * len(_row_chunks[-1]) + Inches(0.18))
                _PS_H_avail = Inches(7.05) - _PS_T  # space to footer
                _PS_HDR_H = Inches(0.38); _PS_ROW_H = Inches(0.30)
                _PS_TH = _PS_HDR_H + _PS_ROW_H * _ps_n_rows

                # If PS fits below batch table on last slide — add there
                # Otherwise create a new minimal slide
                if _PS_TH <= _PS_H_avail and _row_chunks:
                    _target_slide = sl_b  # last batch slide
                else:
                    # New compact slide — no big title, just label + table
                    _target_slide = prs.slides.add_slide(blank)
                    _chrome(_target_slide, "Prepared Sand Properties",
                            f"{cid}  ·  {date_label}  ·  Shift {shift}")
                    _PS_T = Inches(1.08)
                    _PS_H_avail = Inches(6.0)

                # Section label
                from pptx.util import Pt as _Pt2
                _lbl_tb = _target_slide.shapes.add_textbox(
                    Inches(0.86), _PS_T - Inches(0.28), Inches(8), Inches(0.24))
                _lbl_p = _lbl_tb.text_frame.paragraphs[0]
                _lbl_r = _lbl_p.add_run(); _lbl_r.text = "Prepared Sand Properties"
                _lbl_r.font.size = _Pt2(10); _lbl_r.font.bold = True
                _lbl_r.font.color.rgb = _rgb("1F3864")

                # Native PPT table
                _PS_TW = Inches(11.22)
                _ps_tsh = _target_slide.shapes.add_table(
                    _ps_n_rows+1, _ps_n_cols,
                    Inches(0.86), _PS_T, _PS_TW,
                    _PS_HDR_H + _PS_ROW_H * _ps_n_rows)
                _ps_tobj = _ps_tsh.table
                _ps_tobj.rows[0].height = _PS_HDR_H
                for _rri in range(_ps_n_rows): _ps_tobj.rows[_rri+1].height = _PS_ROW_H
                _ps_cw = _PS_TW // _ps_n_cols
                for _pc in _ps_tobj.columns: _pc.width = _ps_cw

                def _ps_cell(cell, text, bold=False, tc="FFFFFF", sz=9, bg=None):
                    cell.text = ""; tf = cell.text_frame; tf.word_wrap = False
                    p = tf.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
                    rn = p.add_run(); rn.text = str(text)
                    rn.font.size = _Pt2(sz); rn.font.bold = bold
                    rn.font.color.rgb = _rgb(tc); rn.font.name = _FONT_BODY
                    _tc = cell._tc; _tcPr = _tc.get_or_add_tcPr()
                    if bg:
                        for _o in _tcPr.findall(_qn2("a:solidFill")): _tcPr.remove(_o)
                        _tcPr.insert(0, _pxml2(f'<a:solidFill xmlns:a="{_B2_NS}"><a:srgbClr val="{bg}"/></a:solidFill>'))
                    for side in ["lnL","lnR","lnT","lnB"]:
                        for _o in _tcPr.findall(_qn2(f"a:{side}")): _tcPr.remove(_o)
                        _tcPr.append(_pxml2(f'<a:{side} xmlns:a="{_B2_NS}" w="9525"><a:solidFill><a:srgbClr val="BFC9D9"/></a:solidFill><a:prstDash val="solid"/></a:{side}>'))

                for j, hdr in enumerate(_ps_headers2):
                    _ps_cell(_ps_tobj.cell(0, j), hdr, bold=True, tc="FFFFFF", sz=9, bg="1F3864")
                for i, row_cells in enumerate(_ps_data2):
                    bg2 = "F0F4FA" if i%2==0 else "FFFFFF"
                    for j, val in enumerate(row_cells):
                        _ps_cell(_ps_tobj.cell(i+1, j), val, bold=False, tc="1E293B", sz=9, bg=bg2)
        except Exception as _pse:
            import traceback; traceback.print_exc()

    out=_io.BytesIO(); prs.save(out); out.seek(0); return out.read()
