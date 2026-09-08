"""
Render slide 1 content panels as PNG via Playwright.
Prescription items are fully dynamic — passed from generate_component_pptx.
"""
import json as _json

# ── SVG icon library (inline, no external deps) ──────────────────────────────
_SVG = {
    # Component Details icons
    "cube":     '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>',
    "users":    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    "calendar": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
    "refresh":  '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
    "clock":    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
    "trending": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>',
    # Batch overview icons
    "database": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
    "box":      '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg>',
    "layers":   '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
    "warning":  '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    # Prescription icons
    "flask":    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6v7l3.5 6a2 2 0 0 1-1.7 3H7.2a2 2 0 0 1-1.7-3L9 10V3z"/><line x1="6" y1="6" x2="18" y2="6"/></svg>',
    "leaf":     '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 8C8 10 5.9 16.17 3.82 19.27 2.89 20.72 2 21 2 21s1-.4 2-2c0 0 1-2 2-3 .83-1.6 1.5-2.07 3-3 2-1.25 4-2 6-1 3.75 1.63 5.42 4.46 5 6-1 4-5 4-5 4s4-1 4-5c0-1.53-.68-2.87-2-4z"/><path d="M2 21s.5-2.5 3-5"/></svg>',
    "flame":    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>',
    "droplets": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 16.3c2.2 0 4-1.83 4-4.05 0-1.16-.57-2.26-1.71-3.19S7.29 6.75 7 5.3c-.29 1.45-1.14 2.84-2.29 3.76S3 11.09 3 12.25c0 2.22 1.8 4.05 4 4.05z"/><path d="M12.56 6.6A10.97 10.97 0 0 0 14 3.02c.5 2.5 2 4.9 4 6.5s3 3.5 3 5.5a6.98 6.98 0 0 1-11.91 4.97"/></svg>',
    "sand":     '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="2"/><circle cx="6" cy="14" r="2"/><circle cx="18" cy="14" r="2"/><circle cx="10" cy="18" r="1.5"/><circle cx="14" cy="18" r="1.5"/><circle cx="12" cy="13" r="1"/></svg>',
    "grain":    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 22 16 8"/><path d="M3.47 12.53 5 11l1.53 1.53a3.5 3.5 0 0 1 0 4.94L5 19l-1.53-1.53a3.5 3.5 0 0 1 0-4.94z"/><path d="M7.47 8.53 9 7l1.53 1.53a3.5 3.5 0 0 1 0 4.94L9 15l-1.53-1.53a3.5 3.5 0 0 1 0-4.94z"/><path d="M11.47 4.53 13 3l1.53 1.53a3.5 3.5 0 0 1 0 4.94L13 11l-1.53-1.53a3.5 3.5 0 0 1 0-4.94z"/><path d="M20 2H22v2a4 4 0 0 1-4 4h-2V6a4 4 0 0 1 4-4z"/><path d="M11.47 17.47 13 19l-1.53 1.53a3.5 3.5 0 0 1-4.94 0L5 19l1.53-1.53a3.5 3.5 0 0 1 4.94 0z"/><path d="M15.47 13.47 17 15l-1.53 1.53a3.5 3.5 0 0 1-4.94 0L9 15l1.53-1.53a3.5 3.5 0 0 1 4.94 0z"/><path d="M19.47 9.47 21 11l-1.53 1.53a3.5 3.5 0 0 1-4.94 0L13 11l1.53-1.53a3.5 3.5 0 0 1 4.94 0z"/></svg>',
}

# Icon per known param key
_PARAM_ICONS = {
    "bentonite":       "leaf",
    "lca":             "flame",
    "freshSilicaSand": "grain",
    "freshsilicasand": "grain",
    "water":           "droplets",
    "pibond":          "leaf",
    "new_sand":        "grain",
    "coal_dust":       "flame",
}

def _icon(key, color="currentColor"):
    """Return SVG string for a param key or generic flask."""
    k = key.lower().replace("_", "").replace(" ", "")
    for kk, icon_name in _PARAM_ICONS.items():
        if kk.lower().replace("_", "") in k or k in kk.lower().replace("_", ""):
            return _SVG.get(icon_name, _SVG["flask"])
    return _SVG.get("flask")


def _build_presc_html(presc_items):
    """Build N-column prescription HTML from dynamic item list."""
    if not presc_items:
        return '<div style="padding:20px;color:#9CA3AF;font-size:16px;">No prescription data available</div>'

    cols = []
    for item in presc_items:
        label = item.get("label", item.get("param", "—"))
        value = item.get("value")
        unit  = item.get("unit", "kg")
        key   = item.get("param", label)
        val_str  = f"{float(value):.2f}" if value is not None and value != "" else "—"
        val_color = "#166534" if value and float(value) > 0 else "#9CA3AF"
        icon_svg  = _icon(key)

        col = f"""
        <div class="presc-col">
          <div class="presc-card-top">
            <div class="presc-card-icon" style="color:#166534;">{icon_svg}</div>
            <div class="presc-card-label">{label}</div>
          </div>
          <div class="presc-val-row">
            <div class="presc-val" style="color:{val_color};">{val_str}</div>
            <div class="presc-unit">{unit}</div>
          </div>
        </div>"""
        cols.append(col)
    return "\n".join(cols)


_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    width: 1920px; height: 870px; overflow: hidden;
    background: #F8F9FA;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    color: #111827;
    font-size: 20px;
  }}
  .layout {{
    display: flex; flex-direction: column;
    gap: 14px; padding: 16px 20px;
    height: 870px;
  }}
  .top-row {{ display: flex; gap: 16px; height: 458px; flex-shrink: 0; }}

  /* Panel base */
  .panel {{
    border-radius: 16px;
    border: 1px solid #E5E7EB;
    background: #fff;
    display: flex; flex-direction: column;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
    overflow: hidden;
  }}
  .panel-hdr {{
    display: flex; align-items: center; gap: 18px;
    padding: 16px 24px;
    border-bottom: 1px solid rgba(0,0,0,0.06);
  }}
  .panel-title {{ font-size: 30px; font-weight: 900; letter-spacing: -0.3px; }}
  .hdr-icon {{
    width: 60px; height: 60px; border-radius: 14px;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
  }}

  /* Component Details */
  .cd {{ flex: 1; min-width: 0; }}
  .cd .panel-hdr {{ background: linear-gradient(135deg,#F0FDF4 0%,#DCFCE7 100%); border-color:#D1FAE5; }}
  .cd .panel-title {{ color: #14532D; }}
  .cd .hdr-icon {{ background: linear-gradient(135deg,#22C55E,#16A34A); color:#fff; }}
  .cd-rows {{ flex:1; display:flex; flex-direction:column; }}
  .cd-row {{
    display:flex; align-items:center;
    padding: 0 22px; gap:14px;
    flex:1; border-bottom:1px solid #F3F4F6;
  }}
  .cd-row:last-child {{ border-bottom:none; }}
  .row-icon {{
    width:42px; height:42px; border-radius:10px;
    display:flex; align-items:center; justify-content:center;
    flex-shrink:0;
  }}
  .cd .row-icon {{ background:#DCFCE7; color:#166534; }}
  .row-label {{ font-size:20px; color:#111827; flex:1; font-weight:500; }}
  .row-value {{ font-size:20px; font-weight:800; color:#111827; white-space:nowrap; }}

  /* Batch Overview */
  .bo {{ flex: 1; min-width: 0; display:flex; flex-direction:column; }}
  .bo .panel-hdr {{ background: linear-gradient(135deg,#EFF6FF 0%,#DBEAFE 100%); border-color:#BFDBFE; }}
  .bo .panel-title {{ color: #1E3A5F; }}
  .bo .hdr-icon {{ background: linear-gradient(135deg,#3B82F6,#2563EB); color:#fff; }}
  .bo-rows {{ flex:1; display:flex; flex-direction:column; }}
  .bo-row {{
    display:flex; align-items:center;
    padding:0 22px; gap:14px;
    flex:1; border-bottom:1px solid #F0F6FF;
  }}
  .bo-row:last-child {{ border-bottom:none; }}
  .bo .row-icon {{ background:#DBEAFE; color:#2563EB; }}
  .bo-val-ok  {{ font-size:22px; font-weight:800; color:#111827; }}
  .bo-val-bad {{ font-size:22px; font-weight:800; color:#DC2626; }}
  .bo-divider {{ height:1px; background:#E5E7EB; margin:0 22px; flex-shrink:0; }}
  .bo-status-row {{
    display:flex; align-items:center;
    padding:16px 22px; gap:14px; flex-shrink:0;
  }}
  .bo .status-icon {{ background:#FEF2F2; color:#DC2626; }}
  .bo .status-icon.ok {{ background:#F0FDF4; color:#16A34A; }}
  .status-badge {{
    padding:10px 24px; border-radius:8px;
    font-size:17px; font-weight:800; letter-spacing:0.4px;
    border:1.5px solid;
  }}
  .badge-bad {{ background:#FEF2F2; color:#DC2626; border-color:#FECACA; }}
  .badge-ok  {{ background:#F0FDF4; color:#166534; border-color:#86EFAC; }}

  /* Prescription */
  .presc {{ flex:1; min-height:0; }}
  .presc .panel-hdr {{ background:linear-gradient(135deg,#F0FDF4 0%,#DCFCE7 100%); border-color:#D1FAE5; }}
  .presc .panel-title {{ color:#14532D; }}
  .presc .hdr-icon {{ background:linear-gradient(135deg,#22C55E,#16A34A); color:#fff; }}
  .presc-body {{ flex:1; padding:14px 18px; display:flex; gap:0; }}
  .presc-inner {{
    flex:1; background:#fff; border:1.5px solid #E5E7EB;
    border-radius:12px; display:flex; overflow:hidden;
  }}
  .presc-col {{
    flex:1; padding:18px 24px;
    display:flex; flex-direction:column; gap:12px;
    border-right:1px solid #E5E7EB;
  }}
  .presc-col:last-child {{ border-right:none; }}
  .presc-card-top {{ display:flex; align-items:center; gap:12px; }}
  .presc-card-icon {{
    width:46px; height:46px; border-radius:12px; background:#DCFCE7;
    display:flex; align-items:center; justify-content:center;
    flex-shrink:0;
  }}
  .presc-card-label {{ font-size:18px; color:#111827; font-weight:700; }}
  .presc-val-row {{ display:flex; align-items:baseline; gap:8px; margin-top:4px; }}
  .presc-val  {{ font-size:48px; font-weight:900; line-height:1; }}
  .presc-unit {{ font-size:22px; color:#374151; font-weight:700; }}
</style>
</head>
<body>
<div class="layout">

  <div class="top-row">

    <!-- Component Details -->
    <div class="panel cd">
      <div class="panel-hdr">
        <div class="hdr-icon">{ICON_CUBE}</div>
        <div class="panel-title">Component Details</div>
      </div>
      <div class="cd-rows">
        <div class="cd-row">
          <div class="row-icon">{ICON_CUBE}</div>
          <div class="row-label">Component</div>
          <div class="row-value">{cid}</div>
        </div>
        <div class="cd-row">
          <div class="row-icon">{ICON_USERS}</div>
          <div class="row-label">Group</div>
          <div class="row-value">{group_name}</div>
        </div>
        <div class="cd-row">
          <div class="row-icon">{ICON_CAL}</div>
          <div class="row-label">Date</div>
          <div class="row-value">{date_label}</div>
        </div>
        <div class="cd-row">
          <div class="row-icon">{ICON_REFRESH}</div>
          <div class="row-label">Shift</div>
          <div class="row-value">Shift {shift}</div>
        </div>
        <div class="cd-row">
          <div class="row-icon">{ICON_CLOCK}</div>
          <div class="row-label">Time Range</div>
          <div class="row-value">{t_start} to {t_end}</div>
        </div>
        <div class="cd-row">
          <div class="row-icon">{ICON_TREND}</div>
          <div class="row-label">Line</div>
          <div class="row-value">Line {line_id}</div>
        </div>
      </div>
    </div>

    <!-- Batch Overview -->
    <div class="panel bo">
      <div class="panel-hdr">
        <div class="hdr-icon">{ICON_DB}</div>
        <div class="panel-title">Batch Overview</div>
      </div>
      <div class="bo-rows">
        <div class="bo-row">
          <div class="row-icon">{ICON_BOX}</div>
          <div class="row-label">Total Batches</div>
          <div class="bo-val-ok">{n_total}</div>
        </div>
        <div class="bo-row">
          <div class="row-icon">{ICON_BOX}</div>
          <div class="row-label">Bad Batches</div>
          <div class="{bad_val_cls}">{n_bad}</div>
        </div>
        <div class="bo-row">
          <div class="row-icon">{ICON_LAYERS}</div>
          <div class="row-label">Outside Threshold</div>
          <div class="{bad_val_cls}">{n_bad} / {n_total}</div>
        </div>
      </div>
      <div class="bo-divider"></div>
      <div class="bo-status-row">
        <div class="row-icon status-icon {status_ok_cls}">{ICON_WARN}</div>
        <div class="row-label">Status</div>
        <div class="status-badge {badge_cls}">{status_text}</div>
      </div>
    </div>

  </div>

  <!-- Prescription -->
  <div class="panel presc">
    <div class="panel-hdr">
      <div class="hdr-icon">{ICON_FLASK}</div>
      <div class="panel-title">Prescription</div>
    </div>
    <div class="presc-body">
      <div class="presc-inner">
        {PRESC_COLS}
      </div>
    </div>
  </div>

</div>
</body>
</html>"""


def render_slide1_html(cid, group_name, date_label, shift, line_id,
                       t_start, t_end, n_total, n_bad,
                       presc_items=None,
                       # Legacy compat: accept old positional args
                       bent_pred=None, mag_pred=None, ns_pred=None) -> bytes:
    """Render content panels as 1920x870 PNG."""

    # Build prescription items from legacy args if presc_items not given
    if presc_items is None:
        presc_items = []
        if bent_pred is not None and bent_pred:
            presc_items.append({"param":"bentonite","label":"Bentonite","value":bent_pred,"unit":"kg"})
        if mag_pred is not None and mag_pred:
            presc_items.append({"param":"lca","label":"Coal Dust / LCA","value":mag_pred,"unit":"kg"})
        if ns_pred is not None and ns_pred:
            presc_items.append({"param":"freshSilicaSand","label":"Fresh Silica Sand","value":ns_pred,"unit":"kg"})

    is_bad = n_bad > 0
    bad_val_cls  = "bo-val-bad" if is_bad else "bo-val-ok"
    status_ok_cls= "" if is_bad else "ok"
    badge_cls    = "badge-bad" if is_bad else "badge-ok"
    status_text  = "BAD BATCHES" if is_bad else "WITHIN THRESHOLD"

    presc_html = _build_presc_html(presc_items)

    html = _TEMPLATE.format(
        cid=cid, group_name=group_name, date_label=date_label,
        shift=shift, line_id=line_id,
        t_start=t_start, t_end=t_end,
        n_total=n_total, n_bad=n_bad,
        bad_val_cls=bad_val_cls,
        status_ok_cls=status_ok_cls,
        badge_cls=badge_cls, status_text=status_text,
        PRESC_COLS=presc_html,
        ICON_CUBE   =_SVG["cube"],
        ICON_USERS  =_SVG["users"],
        ICON_CAL    =_SVG["calendar"],
        ICON_REFRESH=_SVG["refresh"],
        ICON_CLOCK  =_SVG["clock"],
        ICON_TREND  =_SVG["trending"],
        ICON_DB     =_SVG["database"],
        ICON_BOX    =_SVG["box"],
        ICON_LAYERS =_SVG["layers"],
        ICON_WARN   =_SVG["warning"],
        ICON_FLASK  =_SVG["flask"],
    )

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 870})
        page.set_content(html, wait_until="networkidle")
        png = page.screenshot(full_page=False, type="png")
        browser.close()
    return png
