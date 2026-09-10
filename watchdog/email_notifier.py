"""
watchdog/email_notifier.py
---------------------------
Email notification module for SandMan® AI Watchdog.

Sends HTML alert emails for Bad Batch and Prescription Deviation events.
Uses Python stdlib (smtplib + email.mime) — no extra dependencies.

Config path:  config["notifications"]["email"]

  enabled        : true / false  (default false)
  smtp_host      : SMTP server hostname   (e.g. "smtp.gmail.com")
  smtp_port      : SMTP port              (587 for TLS, 465 for SSL, 25 for plain)
  use_tls        : true = STARTTLS, false = plain/SSL  (default true)
  use_ssl        : true = SSL from connect  (port 465 style, default false)
  username       : SMTP login username (leave "" to skip auth)
  password       : SMTP login password
  from_address   : sender address  (e.g. "alerts@sandman.co.in")
  to_addresses   : list of recipient addresses
  reply_to       : optional reply-to address
  alert_types    : list of types to email — ["BAD_BATCH", "PRESCRIPTION", "SI", "SMC_BATCH"] (default all)
  dashboard_url  : URL shown in email footer  (default "http://localhost:5055")
"""

import logging
import smtplib
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_email_log_path = Path(__file__).parent.parent / "logs" / "email_alerts.log"
_email_log_path.parent.mkdir(parents=True, exist_ok=True)
_email_file_handler = logging.FileHandler(_email_log_path, encoding="utf-8")
_email_file_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
_email_logger = logging.getLogger("sandman.email_alerts")
_email_logger.setLevel(logging.DEBUG)
if not _email_logger.handlers:
    _email_logger.addHandler(_email_file_handler)
_email_logger.propagate = False

_DASH_ENGINE = None

def _dashboard_url_for_label(email_cfg: dict, label: str) -> str:
    """
    Build the dashboard URL with the correct ?user= parameter.

    Lookup order:
      1. email_cfg["dashboard_user"]  — explicit override in Config UI
      2. Registry DB: find user_name whose customer.db_properties contains
         the DB name extracted from the foundry label (e.g. caspro_sandman)
      3. Fallback: base URL without user param
    """
    base = str(email_cfg.get("dashboard_url", _DASHBOARD_URL_DEFAULT)).rstrip("/")

    # 1. Explicit override
    user = email_cfg.get("dashboard_user", "").strip()
    if user:
        return f"{base}?user={user}"

    # 2. Derive DB name from label (e.g. "caspro_sandman_L1" -> "caspro_sandman")
    db_name = ""
    try:
        import re as _re
        m = _re.match(r"^(.+?)_L\d+$", str(label or ""))
        if m:
            db_name = m.group(1)
    except Exception:
        pass

    if db_name:
        try:
            global _DASH_ENGINE
            import json as _j
            from sqlalchemy import create_engine as _ce, text as _t
            # Try to load registry config from the same config file the main app uses
            import json, pathlib
            _cfg_path = pathlib.Path(__file__).parent / "config" / "watchdog_config.json"
            _rcfg = json.loads(_cfg_path.read_text(encoding="utf-8")).get("registry_database", {})
            _host = _rcfg.get("host", "localhost")
            _port = _rcfg.get("port", 3306)
            _name = _rcfg.get("name", "sandman_dev")
            _user = _rcfg.get("user", "root")
            _pw   = _rcfg.get("password", "")
            from urllib.parse import quote_plus as _qp
            if _DASH_ENGINE is None:
                _DASH_ENGINE = _ce(f"mysql+pymysql://{_qp(_user)}:{_qp(_pw)}@{_host}:{_port}/{_name}",
                                   pool_pre_ping=True)
            with _DASH_ENGINE.connect() as conn:
                row = conn.execute(_t("""
                    SELECT u.user_name
                    FROM   users     u
                    JOIN   customers c ON c.pkey = u.customer_pkey
                    WHERE  c.db_properties LIKE :pat
                      AND  c.deleted = 0
                      AND  u.deleted = 0
                    ORDER BY u.pkey ASC
                    LIMIT 1
                """), {"pat": f"%{db_name}%"}).mappings().first()
            if row and row["user_name"]:
                return f"{base}?user={row['user_name']}"
        except Exception:
            pass

    return base

_C = {
    "ink"    : "#111111",
    "bg"     : "#f5f0e8",
    "white"  : "#ffffff",
    "yellow" : "#ffe14d",
    "sage"   : "#2d7a4f",
    "sage_lt": "#d4f0e0",
    "red"    : "#c01616",
    "red_lt" : "#ffe0e0",
    "orange" : "#c44a08",
    "orange_lt": "#ffe8d0",
    "border" : "#111111",
    "subtle" : "#666666",
    "muted"  : "#333333",
}

_DASHBOARD_URL_DEFAULT = "http://localhost:5055"

#  PUBLIC API

def send_alerts_batch_email(alerts: list, config: dict, label: str = "") -> bool:
    """
    Send ONE grouped email for a batch of alerts (any mix of types) for a single date.

    Subject: [SANDMAN ALERT] SI WARNING ×21 | BAD BATCH ×3 | PRESCRIPTION ×2 — 2026-04-14
    Body: one section per alert type listing every alert.

    Returns True if sent, False if disabled / nothing to send / failed.
    """
    import re as _re

    email_cfg = _get_email_cfg(config)
    if not _is_enabled(email_cfg, "BAD_BATCH"):
        return False
    if not alerts:
        return False

    try:
        from collections import defaultdict
        by_type = defaultdict(list)
        for a in alerts:
            by_type[a.get("alert_type", "UNKNOWN")].append(a)

        date_str = str(alerts[0].get("date") or "")

        type_labels = {
            "SI"          : "SI WARNING",
            "BAD_BATCH"   : "BAD BATCH",
            "PRESCRIPTION": "PRESCRIPTION",
        }
        parts = []
        worst_col = _C["orange"]
        worst_bg  = _C["orange_lt"]
        for atype in ("SI", "BAD_BATCH", "PRESCRIPTION"):
            if atype not in by_type:
                continue
            count = len(by_type[atype])
            # Determine worst level across this type
            levels = [_re.sub(r'[^A-Z]', '', str(a.get("alert_level") or "").upper()) for a in by_type[atype]]
            if "CRITICAL" in levels:
                worst_col = _C["red"]
                worst_bg  = _C["red_lt"]
            parts.append(f"{type_labels.get(atype, atype)} ×{count}")

        if not parts:
            return False

        subject = "Alert from Sandman"

        body_sections = ""

        # SI section
        if "SI" in by_type:
            rows_html = ""
            for a in sorted(by_type["SI"], key=lambda x: -(x.get("si_score") or 0)):
                al      = _re.sub(r'[^A-Z]', '', str(a.get("alert_level") or "").upper())
                acol    = _C["red"] if al == "CRITICAL" else _C["orange"]
                abg     = "#ffe0e0" if al == "CRITICAL" else "#fff3e0"
                score   = a.get("si_score")
                sc_str  = f"{float(score):.1f}" if score is not None else "—"
                pk      = str(a.get("period_key") or f"{a.get('date','')} Sh{a.get('shift','')}")
                rc      = str(a.get("root_cause") or "")[:120]
                rows_html += (
                    f'<tr style="background:{abg};border-bottom:1px solid #e0e0e0">'
                    f'<td style="padding:8px 12px;font-weight:700;color:{acol}">{pk}</td>'
                    f'<td style="padding:8px 12px;text-align:center;font-weight:900;'
                    f'font-family:monospace;color:{acol};font-size:15px">{sc_str}</td>'
                    f'<td style="padding:8px 12px;text-align:center;font-size:10px;'
                    f'font-weight:800;text-transform:uppercase;color:{acol}">{al}</td>'
                    f'<td style="padding:8px 12px;font-size:11px;color:{_C["subtle"]}">{rc}</td>'
                    f'</tr>'
                )
            body_sections += f"""
            <div style="background:{_C['yellow']};border:2px solid {_C['border']};
                        padding:8px 14px;margin-bottom:8px;margin-top:16px">
              <div style="font-size:11px;font-weight:800;text-transform:uppercase;
                          letter-spacing:.08em">SI Stability Index Alerts — {len(by_type['SI'])} shifts/components</div>
            </div>
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="border:2px solid {_C['border']};margin-bottom:8px;font-size:12px">
              <thead>
                <tr style="background:{_C['ink']}">
                  <th style="padding:8px 12px;text-align:left;color:#ffe14d;font-size:10px;text-transform:uppercase">Period</th>
                  <th style="padding:8px 12px;text-align:center;color:#ffe14d;font-size:10px;text-transform:uppercase">SI Score</th>
                  <th style="padding:8px 12px;text-align:center;color:#ffe14d;font-size:10px;text-transform:uppercase">Level</th>
                  <th style="padding:8px 12px;color:#ffe14d;font-size:10px;text-transform:uppercase">Root Cause</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>"""

        # BAD BATCH section
        if "BAD_BATCH" in by_type:
            rows_html = ""
            for a in by_type["BAD_BATCH"]:
                diff   = a.get("smc_cosp_diff")
                thr    = a.get("threshold", 2.0)
                dcol   = _C["red"] if diff is not None and abs(float(diff)) > float(thr)*1.5 else _C["orange"]
                dstr   = f"{float(diff):+.2f}" if diff is not None else "—"
                comp   = str(a.get("component_id") or "—")
                btime  = str(a.get("batch_time") or a.get("date") or "—")[:16]
                smc    = f"{float(a['smc_value']):.2f}" if a.get("smc_value") is not None else "—"
                cosp   = f"{float(a['cosp_value']):.2f}" if a.get("cosp_value") is not None else "—"
                rows_html += (
                    f'<tr style="background:#ffe0e0;border-bottom:1px solid #e0e0e0">'
                    f'<td style="padding:8px 12px;font-weight:700;font-family:monospace">{comp}</td>'
                    f'<td style="padding:8px 12px;font-size:11px">{btime}</td>'
                    f'<td style="padding:8px 12px;text-align:center;font-family:monospace">{smc}%</td>'
                    f'<td style="padding:8px 12px;text-align:center;font-family:monospace">{cosp}%</td>'
                    f'<td style="padding:8px 12px;text-align:center;font-weight:900;'
                    f'font-family:monospace;color:{dcol};font-size:14px">{dstr}%</td>'
                    f'</tr>'
                )
            body_sections += f"""
            <div style="background:{_C['yellow']};border:2px solid {_C['border']};
                        padding:8px 14px;margin-bottom:8px;margin-top:16px">
              <div style="font-size:11px;font-weight:800;text-transform:uppercase;
                          letter-spacing:.08em">Bad Batch Alerts — {len(by_type['BAD_BATCH'])} batches</div>
            </div>
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="border:2px solid {_C['border']};margin-bottom:8px;font-size:12px">
              <thead>
                <tr style="background:{_C['ink']}">
                  <th style="padding:8px 12px;text-align:left;color:#ffe14d;font-size:10px;text-transform:uppercase">Component</th>
                  <th style="padding:8px 12px;color:#ffe14d;font-size:10px;text-transform:uppercase">Time</th>
                  <th style="padding:8px 12px;text-align:center;color:#ffe14d;font-size:10px;text-transform:uppercase">SMC</th>
                  <th style="padding:8px 12px;text-align:center;color:#ffe14d;font-size:10px;text-transform:uppercase">COSP</th>
                  <th style="padding:8px 12px;text-align:center;color:#ffe14d;font-size:10px;text-transform:uppercase">Diff</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>"""

        # PRESCRIPTION section
        if "PRESCRIPTION" in by_type:
            rows_html = ""
            for a in by_type["PRESCRIPTION"]:
                comp  = str(a.get("component_id") or "—")
                n_out = len([d for d in (a.get("deviations") or []) if not d.get("within", True)])
                total = len(a.get("deviations") or [])
                pcol  = _C["red"] if n_out >= 2 else _C["orange"]
                rows_html += (
                    f'<tr style="background:#fff3e0;border-bottom:1px solid #e0e0e0">'
                    f'<td style="padding:8px 12px;font-weight:700;font-family:monospace">{comp}</td>'
                    f'<td style="padding:8px 12px;text-align:center;font-weight:800;'
                    f'color:{pcol}">{n_out}/{total} out of tolerance</td>'
                    f'</tr>'
                )
            body_sections += f"""
            <div style="background:{_C['yellow']};border:2px solid {_C['border']};
                        padding:8px 14px;margin-bottom:8px;margin-top:16px">
              <div style="font-size:11px;font-weight:800;text-transform:uppercase;
                          letter-spacing:.08em">Prescription Deviation Alerts — {len(by_type['PRESCRIPTION'])} components</div>
            </div>
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="border:2px solid {_C['border']};margin-bottom:8px;font-size:12px">
              <thead>
                <tr style="background:{_C['ink']}">
                  <th style="padding:8px 12px;text-align:left;color:#ffe14d;font-size:10px;text-transform:uppercase">Component</th>
                  <th style="padding:8px 12px;text-align:center;color:#ffe14d;font-size:10px;text-transform:uppercase">Deviations</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>"""

        headline = f"Sandman Alert Summary — {date_str}"
        html = _wrap_email(
            title     = "Sandman Alert Summary",
            badge_txt = f"{len(alerts)} ALERTS",
            badge_col = worst_col,
            badge_bg  = worst_bg,
            headline  = headline,
            subline   = " &nbsp;·&nbsp; ".join(parts),
            body      = body_sections,
            dashboard_url=_dashboard_url_for_label(email_cfg, label),
        )

        _send(email_cfg, subject, html, alert_type="BAD_BATCH")
        logger.info("[%s]  Batch alert email sent (%d alerts) -> %s",
                    label, len(alerts), ", ".join(_recipients(email_cfg)))
        return True

    except Exception:
        logger.warning("[%s]  Batch alert email FAILED:\n%s", label, traceback.format_exc())
        return False

def send_si_alert_email(result: dict, config: dict, label: str = "") -> bool:
    """
    Send a plain left-aligned SI alert email matching the standard Sandman format.
    """
    email_cfg = _get_email_cfg(config)
    if not _is_enabled(email_cfg, "SI"):
        return False

    import re as _re
    alert_level = _re.sub(r'[^A-Z]', '', str(result.get("alert_level") or "").upper())
    if alert_level not in ("CRITICAL", "WARNING", "ALERT"):
        return False

    try:
        from datetime import datetime as _dt
        date_str = str(result.get("date") or "")
        shift    = str(result.get("shift") or "")
        params   = result.get("params_json") or []
        if isinstance(params, str):
            import json as _j
            params = _j.loads(params)

        # Date: 2026-07-10 -> 10-Jul-2026
        try:
            date_disp = _dt.strptime(date_str, "%Y-%m-%d").strftime("%d-%b-%Y")
        except Exception:
            date_disp = date_str

        # Customer from email config; foundry name auto-fetched from foundry_line table
        email_cfg_local = config.get("notifications", {}).get("email", {})
        _cust_name = (
            email_cfg_local.get("dashboard_user", "").strip()
            or email_cfg_local.get("customer_name", "").strip()
            or "-"
        )
        _line_name = _get_foundry_line_name(config)
        foundry_name = _line_name or _cust_name

        # Flagged PS params only
        flagged = [
            p for p in params
            if str(p.get("param") or "").startswith("ps_")
            and (p.get("alert_level") or "").upper() not in ("STABLE", "OK", "")
        ]

        # Build bullet lines: • Active Clay (%): 8.498(8.32-8.43) — High (2.23σ)
        bullet_html  = ""
        bullet_plain = ""
        for p in flagged:
            lbl = p.get("label") or p.get("param", "").replace("ps_", "").replace("_", " ").title()
            val = p.get("raw_value")
            bm  = p.get("bl_mean")
            bs  = p.get("bl_std")
            dev = (p.get("deviation") or "").strip()
            lvl = _re.sub(r'[^A-Z]', '', str(p.get("alert_level") or "").upper())

            val_str = f"{float(val):.3f}" if val is not None else "—"
            rng_str = (f"({float(bm - bs):.2f}-{float(bm + bs):.2f})"
                       if bm is not None and bs is not None else "")
            status  = dev if dev and dev.upper() not in ("OK", "—", "") else p.get("drift_label", "")

            col = "#c01616" if lvl == "CRITICAL" else "#b03a06"
            bullet_html  += (f"<li style='margin-bottom:4px'>"
                             f"<b>{lbl}</b>: "
                             f"<span style='color:{col};font-weight:700'>{val_str}</span>"
                             f"<span style='color:#555'>{rng_str}</span>"
                             + (f' &mdash; <span style="color:{col}">{status}</span>' if status else '')
                             + "</li>")
            _status_txt = f' — {status}' if status else ''
            bullet_plain += f"• {lbl}: {val_str}{rng_str}{_status_txt}\n"

        dashboard_url = _dashboard_url_for_label(email_cfg, label)
        dash_html  = ""
        dash_plain = ""

        html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;font-size:13px;color:#1a1a1a;line-height:1.8;
             max-width:580px;margin:0;padding:24px 28px">

<p style="margin:0 0 16px">We would like to bring to your notice that the following event has occurred,</p>

<p style="margin:0 0 6px">Foundry : {foundry_name}</p>
<p style="margin:0 0 6px">Date : {date_disp}</p>
<p style="margin:0 0 6px">Shift : {shift}</p>
<p style="margin:0 0 16px">Alert Level : <strong style="color:{'#c01616' if alert_level=='CRITICAL' else '#b03a06'}">{alert_level}</strong></p>

{f'<ul style="margin:0 0 16px;padding-left:20px">{bullet_html}</ul>' if flagged else ''}

<p style="margin:0">@Sandman Team</p>
{dash_html}
</body>
</html>"""

        _foundry_line = _get_foundry_line_name(config)
        _meta_lines = [
            "We would like to bring to your notice that the following event has occurred,",
            "",
            f"Customer    : {_cust_name}",
        ]
        if _foundry_line:
            _meta_lines.append(f"Foundry     : {_foundry_line}")
        _meta_lines += [
            f"Date        : {date_disp}",
            f"Shift       : {shift}",
            f"Alert Level : {alert_level}",
            "",
        ]
        plain = "\n".join(_meta_lines) + bullet_plain + "\n@Sandman Team" + dash_plain

        _send_plain(email_cfg, "Alert from Sandman", plain, alert_type="SI")
        logger.info("[%s]  SI alert email sent -> %s", label, ", ".join(_recipients(email_cfg)))
        return True

    except Exception:
        logger.warning("[%s]  SI alert email FAILED:\n%s", label, traceback.format_exc())
        return False

def _si_alert_body(date_str, shift, score_str, alert_level,
                   root_cause, recommend, critical_params,
                   sev_col, sev_bg) -> str:
    """Build the HTML body for an SI alert email."""

    # ── Parameter rows — only flagged (non-STABLE) parameters ─────────────────
    import re as _re_dev

    def _flag_label(lbl: str) -> str:
        lbl = (lbl or "STABLE").upper().strip()
        clean = _re_dev.sub(r'[^A-Z ]', '', lbl).strip()
        return "STABLE" if not clean else clean

    def _is_flagged(label_str: str) -> bool:
        return _flag_label(label_str) not in ("STABLE", "OK", "")

    def _flag_col(lbl: str) -> str:
        c = _flag_label(lbl)
        if c == "CRITICAL":                       return _C["red"]
        if c in ("ALERT", "WARNING"):             return _C["orange"]
        if any(x in c for x in ("DRIFT","TREND","VAR","OSC","WARN")):
            return _C["orange"]
        return _C["subtle"]

    param_rows = ""
    for p in critical_params:
        plv      = _flag_label(p.get("alert_level") or "STABLE")
        pcol     = _flag_col(p.get("alert_level") or "STABLE")
        pbg      = "#ffe0e0" if plv == "CRITICAL" else "#fff3e0"
        val      = p.get("raw_value")
        val_str  = f"{float(val):.3f}" if val is not None else "—"
        pct      = p.get("pct_change")
        pct_str  = f"{float(pct):+.1f}%" if pct is not None else "—"
        si       = p.get("si_score")
        si_str   = f"{float(si):.1f}" if si is not None else "—"
        drift    = p.get("drift_label") or "—"
        var_lbl  = p.get("var_label")   or "—"
        osc_lbl  = p.get("osc_label")   or "—"
        dev      = p.get("deviation")   or ""
        lcl_ucl  = p.get("lcl_ucl")    or ""
        lbl      = p.get("label") or p.get("param") or "—"

        # Colour-code drift/osc/var labels
        dcol = _C["red"] if "STRONG" in drift.upper() else (_C["orange"] if drift != "—" and drift.upper() != "STABLE" else _C["subtle"])
        vcol = _C["orange"] if "HIGH" in var_lbl.upper() or "ELEV" in var_lbl.upper() else _C["subtle"]
        ocol = _C["orange"] if osc_lbl.upper() not in ("STABLE","—") else _C["subtle"]
        devcol = _C["red"] if dev.startswith("Deviated") else _C["subtle"]

        param_rows += (
            f'<tr style="background:{pbg};border-bottom:1px solid #e8e8e8">'
            f'<td style="padding:7px 10px;font-weight:700;color:{pcol};white-space:nowrap">{lbl}</td>'
            f'<td style="padding:7px 8px;text-align:right;font-family:monospace;font-size:12px">{val_str}</td>'
            f'<td style="padding:7px 8px;text-align:right;font-family:monospace;font-weight:700;color:{pcol};font-size:12px">{pct_str}</td>'
            f'<td style="padding:7px 8px;text-align:right;font-family:monospace;font-weight:700;font-size:12px">{si_str}</td>'
            f'<td style="padding:7px 8px;font-size:11px;font-weight:600;color:{dcol}">{drift}</td>'
            f'<td style="padding:7px 8px;font-size:11px;font-weight:600;color:{vcol}">{var_lbl}</td>'
            f'<td style="padding:7px 8px;font-size:11px;font-weight:600;color:{ocol}">{osc_lbl}</td>'
            f'<td style="padding:7px 8px;font-size:11px;color:{devcol}">{dev if dev else "—"}</td>'
            f'<td style="padding:7px 8px;font-size:11px;color:{_C["subtle"]};font-family:monospace">{lcl_ucl}</td>'
            f'</tr>'
        )

    params_table = f"""
    <div style="font-size:11px;font-weight:700;letter-spacing:.10em;text-transform:uppercase;
                color:#1f497d;margin-bottom:8px;padding-bottom:6px;border-bottom:2px solid #1f497d">
      Flagged Prepared Sand Parameters &nbsp;({len(critical_params)})
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;margin-bottom:20px;font-size:12px">
      <thead>
        <tr style="background:#1f497d">
          <th style="padding:8px 10px;text-align:left;color:#ffe14d;font-size:10px;text-transform:uppercase;letter-spacing:.07em;font-weight:700">Parameter</th>
          <th style="padding:8px 8px;text-align:right;color:#ffe14d;font-size:10px;text-transform:uppercase;letter-spacing:.07em;font-weight:700">Value</th>
          <th style="padding:8px 8px;text-align:right;color:#ffe14d;font-size:10px;text-transform:uppercase;letter-spacing:.07em;font-weight:700">Δ%</th>
          <th style="padding:8px 8px;text-align:right;color:#ffe14d;font-size:10px;text-transform:uppercase;letter-spacing:.07em;font-weight:700">SI</th>
          <th style="padding:8px 8px;color:#ffe14d;font-size:10px;text-transform:uppercase;letter-spacing:.07em;font-weight:700">Drift</th>
          <th style="padding:8px 8px;color:#ffe14d;font-size:10px;text-transform:uppercase;letter-spacing:.07em;font-weight:700">Variance</th>
          <th style="padding:8px 8px;color:#ffe14d;font-size:10px;text-transform:uppercase;letter-spacing:.07em;font-weight:700">Oscillation</th>
          <th style="padding:8px 8px;color:#ffe14d;font-size:10px;text-transform:uppercase;letter-spacing:.07em;font-weight:700">Deviation</th>
          <th style="padding:8px 8px;color:#ffe14d;font-size:10px;text-transform:uppercase;letter-spacing:.07em;font-weight:700">Limit</th>
        </tr>
      </thead>
      <tbody>{param_rows if param_rows else '<tr><td colspan="9" style="padding:14px;text-align:center;color:#aaa;font-style:italic">No flagged parameters detected</td></tr>'}</tbody>
    </table>
    """ if critical_params else ""

    root_section = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;border-radius:4px;overflow:hidden">
      <tr>
        <td width="4" style="background:#555;padding:0"></td>
        <td style="background:#f4f4f4;padding:12px 14px">
          <div style="font-size:10px;font-weight:700;letter-spacing:.10em;text-transform:uppercase;color:#777;margin-bottom:5px">Root Cause</div>
          <div style="font-size:13px;color:#1a1a1a;line-height:1.6">{root_cause}</div>
        </td>
      </tr>
    </table>""" if root_cause else ""

    rec_section = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="border-radius:4px;overflow:hidden">
      <tr>
        <td width="4" style="background:{sev_col};padding:0"></td>
        <td style="background:#fff8f0;padding:12px 14px;border:1px solid {sev_col}33">
          <div style="font-size:10px;font-weight:700;letter-spacing:.10em;text-transform:uppercase;color:{sev_col};margin-bottom:5px">Recommendation</div>
          <div style="font-size:13px;color:#1a1a1a;line-height:1.6">{recommend}</div>
        </td>
      </tr>
    </table>""" if recommend else ""

    # ── Score card + meta ────────────────────────────────────────────────────
    score_card = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;border-radius:6px;overflow:hidden">
      <tr>
        <td width="30%" style="background:{sev_col};padding:16px 20px;text-align:center;vertical-align:middle">
          <div style="font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:rgba(255,255,255,.8);margin-bottom:4px">SI Score</div>
          <div style="font-size:44px;font-weight:900;color:#fff;line-height:1;font-family:monospace">{score_str}</div>
          <div style="font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:rgba(255,255,255,.9);margin-top:6px">{alert_level}</div>
        </td>
        <td style="background:#f8f9fa;padding:16px 20px;vertical-align:middle;border:1px solid #e0e0e0;border-left:none">
          <table cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding-right:24px;padding-bottom:6px">
                <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:#888;margin-bottom:2px">Date</div>
                <div style="font-size:14px;font-weight:700;color:#1a1a1a">{date_str}</div>
              </td>
              <td style="padding-bottom:6px">
                <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:#888;margin-bottom:2px">Shift</div>
                <div style="font-size:14px;font-weight:700;color:#1a1a1a">{shift}</div>
              </td>
            </tr>
            <tr>
              <td colspan="2">
                <div style="font-size:10px;color:#999;border-top:1px solid #e0e0e0;padding-top:8px;margin-top:4px">
                  ≤20 STABLE &nbsp;·&nbsp; ≤49 WATCH &nbsp;·&nbsp; ≤69 ALERT &nbsp;·&nbsp; &gt;69 CRITICAL
                </div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>"""

    return f"""
    {score_card}
    {params_table}
    {root_section}
    {rec_section}
    """

def send_bad_batch_email(result: dict, config: dict, label: str = "") -> bool:
    """Send a plain-text Bad Batch alert email."""
    email_cfg = _force_enabled_if_recipients(_get_email_cfg(config), "BAD_BATCH")
    if not _is_enabled(email_cfg, "BAD_BATCH"):
        return False

    try:
        component  = str(result.get("component_id") or "-")
        group      = str(result.get("group_name")   or "-")
        date_str   = str(result.get("date")         or "-")
        shift      = str(result.get("shift")        or "-")
        batch_pkey = str(result.get("batch_pkey")   or "-")
        smc        = result.get("smc_value")
        cosp       = result.get("cosp_value")
        diff       = result.get("smc_cosp_diff")
        threshold  = result.get("threshold", 2.0)

        smc_str   = f"{float(smc):.2f} %"   if smc  is not None else "-"
        cosp_str  = f"{float(cosp):.2f} %"  if cosp is not None else "-"
        diff_f    = float(diff) if diff is not None else 0.0
        diff_str  = f"{diff_f:+.2f} %"
        direction = "SMC higher than COSP" if diff_f > 0 else "SMC lower than COSP"

        _customer = email_cfg.get("dashboard_user", "")
        _foundry  = _get_foundry_line_name(config)

        try:
            date_fmt = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%b-%Y")
        except Exception:
            date_fmt = date_str

        lines = [
            "We would like to bring to your notice that the following event has occurred,",
            "",
            f"Customer          : {_customer or '-'}",
        ]
        if _foundry:
            lines.append(f"Foundry           : {_foundry}")
        lines += [
            f"Pattern/Component : {component}",
            f"Group             : {group}",
            f"Date              : {date_fmt}",
            f"Shift             : {shift}",
            "",
            "=" * 60,
            "Bad Batch Detected",
            "=" * 60,
            "The SMC discharge value deviates significantly from the COSP setpoint.",
            "This indicates a potential sand quality issue that may affect casting quality.",
            "",
            f"  SMC Discharge : {smc_str}",
            f"  COSP Setpoint : {cosp_str}",
            f"  Difference    : {diff_str}  (threshold: +/-{threshold})",
            f"  Direction     : {direction}",
            "",
            "@Sandman Team",
        ]
        body = "\n".join(lines)

        _send_plain(email_cfg, f"[SandMan] Bad Batch Detected — {component} | Shift {shift}", body, alert_type="BAD_BATCH")
        logger.info("[%s]  Bad-batch email sent -> %s", label, ", ".join(_recipients(email_cfg)))
        return True

    except Exception:
        logger.warning("[%s]  Bad-batch email FAILED:\n%s", label, traceback.format_exc())
        return False

def send_bad_batch_shift_summary(config: dict, date_str: str, shift: str,
                                  total: int, bad: int, pct: float,
                                  label: str = "") -> bool:
    """Send end-of-shift bad batch summary email."""
    email_cfg = _force_enabled_if_recipients(_get_email_cfg(config), "BAD_BATCH")
    if not _is_enabled(email_cfg, "BAD_BATCH"):
        return False

    try:
        _foundry = _get_foundry_line_name(config)

        try:
            date_fmt = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d - %b - %Y")
        except Exception:
            date_fmt = date_str

        ok_batches  = total - bad
        ok_pct      = round(100.0 - pct, 1) if total > 0 else 0.0
        bb_cfg      = config.get("bad_batch_watchdog", {})
        ok_thr      = float(bb_cfg.get("pct_ok_thr", 1.0))
        status_line = "OK" if pct <= ok_thr else "BAD BATCH"
        _customer   = email_cfg.get("dashboard_user", "") or "-"

        status_color = _C["sage"] if status_line == "OK" else _C["red"]
        status_bg    = _C["sage_lt"] if status_line == "OK" else _C["red_lt"]
        pct_color    = _C["red"] if pct > ok_thr else _C["sage"]

        subject = (
            f"[SandMan] Shift {shift} Summary — {date_fmt} | "
            f"{status_line} ({pct:.1f}%,  {bad}/{total} batches)"
        )

        html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f0ede6;font-family:Arial,sans-serif">
<div style="max-width:520px;margin:32px auto;background:#ffffff;border-radius:6px;
            border:1px solid #d0ccc4;overflow:hidden">

  <div style="background:{_C['ink']};padding:18px 24px">
    <div style="font-size:11px;letter-spacing:2px;color:#aaaaaa;text-transform:uppercase;margin-bottom:4px">SandMan AI Watchdog</div>
    <div style="font-size:18px;font-weight:700;color:#ffffff">Bad Batch Shift Summary</div>
  </div>

  <div style="padding:16px 24px 0;border-bottom:1px solid #eeeeee">
    <table style="font-size:13px;color:{_C['muted']};line-height:1.8;border-collapse:collapse">
      <tr><td style="padding-right:16px;color:{_C['subtle']}">Date</td>
          <td style="font-weight:600">{date_fmt}</td></tr>
      {'<tr><td style="padding-right:16px;color:' + _C["subtle"] + '">Foundry</td><td style="font-weight:600">' + _foundry + '</td></tr>' if _foundry else ''}
      <tr><td style="padding-right:16px;color:{_C['subtle']}">Customer</td>
          <td style="font-weight:600">{_customer}</td></tr>
    </table>
  </div>

  <div style="padding:16px 24px">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;border:1px solid #e0e0e0;border-radius:4px;overflow:hidden">
      <thead>
        <tr style="background:{_C['ink']}">
          <th style="padding:10px 16px;font-size:11px;color:#ffffff;text-align:left;font-weight:600;letter-spacing:0.5px">SHIFT</th>
          <th style="padding:10px 16px;font-size:11px;color:#ffffff;text-align:right;font-weight:600;letter-spacing:0.5px">BAD %</th>
          <th style="padding:10px 16px;font-size:11px;color:#ffffff;text-align:right;font-weight:600;letter-spacing:0.5px">BAD BATCHES</th>
          <th style="padding:10px 16px;font-size:11px;color:#ffffff;text-align:right;font-weight:600;letter-spacing:0.5px">TOTAL BATCHES</th>
          <th style="padding:10px 16px;font-size:11px;color:#ffffff;text-align:right;font-weight:600;letter-spacing:0.5px">OK BATCHES</th>
        </tr>
      </thead>
      <tbody>
        <tr style="background:#ffffff;border-bottom:1px solid #e8e8e8">
          <td style="padding:10px 16px;font-size:13px;color:{_C['muted']};font-weight:600">{shift}</td>
          <td style="padding:10px 16px;font-size:13px;text-align:right;color:{pct_color};font-weight:700">{pct:.1f}%</td>
          <td style="padding:10px 16px;font-size:13px;text-align:right;color:{_C['subtle']}">{bad}</td>
          <td style="padding:10px 16px;font-size:13px;text-align:right;color:{_C['subtle']}">{total}</td>
          <td style="padding:10px 16px;font-size:13px;text-align:right;color:{_C['subtle']}">{ok_batches} ({ok_pct:.1f}%)</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div style="padding:0 24px 20px">
    <div style="display:inline-block;background:{status_bg};border:1px solid {status_color};
                border-radius:4px;padding:8px 18px;font-size:13px;font-weight:700;color:{status_color}">
      Status : {status_line}
    </div>
  </div>

  <div style="background:#f5f5f5;border-top:1px solid #e0e0e0;padding:12px 24px;
              font-size:11px;color:{_C['subtle']};text-align:center">
    @Sandman Team
  </div>

</div>
</body></html>"""

        _send(email_cfg, subject, html, alert_type="BAD_BATCH")
        logger.info("[%s]  Shift summary email sent — Shift %s  bad=%d/%d (%.1f%%)",
                    label, shift, bad, total, pct)
        return True

    except Exception:
        logger.warning("[%s]  send_bad_batch_shift_summary FAILED:\n%s", label, traceback.format_exc())
        return False

def send_bad_batch_daily_summary(config: dict, date_str: str,
                                  rows: list, label: str = "") -> bool:
    """
    Send end-of-day bad batch summary email showing all shifts for the date.

    rows: [{"shift": "A", "total": 100, "bad": 5, "pct": 5.0}, ...]
    """
    email_cfg = _force_enabled_if_recipients(_get_email_cfg(config), "BAD_BATCH")
    if not _is_enabled(email_cfg, "BAD_BATCH"):
        return False

    try:
        _foundry  = _get_foundry_line_name(config)
        _customer = email_cfg.get("dashboard_user", "") or "-"

        try:
            date_fmt = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            date_fmt = date_str

        grand_total = sum(r["total"] for r in rows)
        grand_bad   = sum(r["bad"]   for r in rows)
        grand_pct   = round(grand_bad / grand_total * 100, 1) if grand_total > 0 else 0.0
        bb_cfg      = config.get("bad_batch_watchdog", {})
        ok_thr      = float(bb_cfg.get("pct_ok_thr", 1.0)) * 100
        status      = "OK" if grand_pct <= ok_thr else "BAD BATCH"

        status_color = _C["sage"] if status == "OK" else _C["red"]
        status_bg    = _C["sage_lt"] if status == "OK" else _C["red_lt"]

        shift_rows_html = ""
        for i, r in enumerate(rows):
            bg  = "#ffffff" if i % 2 == 0 else "#f9f9f9"
            pct = float(r["pct"])
            bad = int(r["bad"])
            tot = int(r["total"])
            pct_color = _C["red"] if pct > ok_thr else _C["sage"]
            shift_rows_html += f"""
                <tr style="background:{bg};border-bottom:1px solid #e8e8e8">
                  <td style="padding:10px 16px;font-size:13px;color:{_C['muted']};font-weight:600">{r['shift']}</td>
                  <td style="padding:10px 16px;font-size:13px;text-align:right;color:{pct_color};font-weight:700">{pct:.1f}%</td>
                  <td style="padding:10px 16px;font-size:13px;text-align:right;color:{_C['subtle']}">{bad}</td>
                  <td style="padding:10px 16px;font-size:13px;text-align:right;color:{_C['subtle']}">{tot}</td>
                </tr>"""

        subject = (
            f"[SandMan] Bad Batch Daily Summary — {date_fmt} | "
            f"{status} ({grand_pct:.1f}%,  {grand_bad}/{grand_total} batches)"
        )

        html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f0ede6;font-family:Arial,sans-serif">
<div style="max-width:560px;margin:32px auto;background:#ffffff;border-radius:6px;
            border:1px solid #d0ccc4;overflow:hidden">

  <!-- Header -->
  <div style="background:{_C['ink']};padding:18px 24px">
    <div style="font-size:11px;letter-spacing:2px;color:#aaaaaa;text-transform:uppercase;margin-bottom:4px">SandMan AI Watchdog</div>
    <div style="font-size:18px;font-weight:700;color:#ffffff">Bad Batch Daily Summary</div>
  </div>

  <!-- Meta -->
  <div style="padding:16px 24px 0;border-bottom:1px solid #eeeeee">
    <table style="font-size:13px;color:{_C['muted']};line-height:1.8;border-collapse:collapse">
      <tr><td style="padding-right:16px;color:{_C['subtle']}">Date</td>
          <td style="font-weight:600">{date_fmt}</td></tr>
      {'<tr><td style="padding-right:16px;color:' + _C['subtle'] + '">Foundry</td><td style="font-weight:600">' + _foundry + '</td></tr>' if _foundry else ''}
      <tr><td style="padding-right:16px;color:{_C['subtle']}">Customer</td>
          <td style="font-weight:600">{_customer}</td></tr>
    </table>
  </div>

  <!-- Table -->
  <div style="padding:16px 24px">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;border:1px solid #e0e0e0;border-radius:4px;overflow:hidden">
      <!-- Header -->
      <thead>
        <tr style="background:{_C['ink']}">
          <th style="padding:10px 16px;font-size:11px;color:#ffffff;text-align:left;font-weight:600;letter-spacing:0.5px">SHIFT</th>
          <th style="padding:10px 16px;font-size:11px;color:#ffffff;text-align:right;font-weight:600;letter-spacing:0.5px">BAD %</th>
          <th style="padding:10px 16px;font-size:11px;color:#ffffff;text-align:right;font-weight:600;letter-spacing:0.5px">BAD</th>
          <th style="padding:10px 16px;font-size:11px;color:#ffffff;text-align:right;font-weight:600;letter-spacing:0.5px">TOTAL</th>
        </tr>
      </thead>
      <tbody>{shift_rows_html}
        <!-- Total row -->
        <tr style="background:#f0f0f0;border-top:2px solid {_C['border']}">
          <td style="padding:11px 16px;font-size:13px;font-weight:700;color:{_C['ink']}">TOTAL</td>
          <td style="padding:11px 16px;font-size:13px;font-weight:700;text-align:right;color:{status_color}">{grand_pct:.1f}%</td>
          <td style="padding:11px 16px;font-size:13px;font-weight:700;text-align:right;color:{_C['ink']}">{grand_bad}</td>
          <td style="padding:11px 16px;font-size:13px;font-weight:700;text-align:right;color:{_C['ink']}">{grand_total}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- Status badge -->
  <div style="padding:0 24px 20px">
    <div style="display:inline-block;background:{status_bg};border:1px solid {status_color};
                border-radius:4px;padding:8px 18px;font-size:13px;font-weight:700;color:{status_color}">
      Status : {status}
    </div>
  </div>

  <!-- Footer -->
  <div style="background:#f5f5f5;border-top:1px solid #e0e0e0;padding:12px 24px;
              font-size:11px;color:{_C['subtle']};text-align:center">
    @Sandman Team
  </div>

</div>
</body></html>"""

        _send(email_cfg, subject, html, alert_type="BAD_BATCH")
        logger.info("[%s]  Daily summary email sent — %s  bad=%d/%d (%.1f%%)",
                    label, date_str, grand_bad, grand_total, grand_pct)
        return True

    except Exception:
        logger.warning("[%s]  send_bad_batch_daily_summary FAILED:\n%s", label, traceback.format_exc())
        return False

def send_sieve_email(result: dict, config: dict, label: str = "") -> bool:
    """Send a plain-text Sieve Change alert email."""
    email_cfg = _get_email_cfg(config)
    if not _is_enabled(email_cfg, "SIEVE_CHANGE"):
        return False

    try:
        from datetime import datetime as _dt

        sieve_pkey = str(result.get("sieve_pkey") or "-")
        date_val   = result.get("date")
        shift      = str(result.get("shift") or "-")
        changes    = result.get("changes", [])
        alert_lvl  = str(result.get("alert_level") or "WARNING")
        max_pct    = result.get("max_pct_change", 0.0)

        _customer = email_cfg.get("dashboard_user", "")
        _foundry  = _get_foundry_line_name(config)

        try:
            date_fmt = _dt.strptime(str(date_val), "%Y-%m-%d").strftime("%d-%b-%Y")
        except Exception:
            date_fmt = str(date_val or "-")

        SEP  = "-" * 60
        SEP2 = "=" * 60

        _SAND_FULL = {0: "Return Sand", 1: "Prepared Sand", 2: "New Sand", 3: "Core Sand"}

        header = [
            SEP2,
            "Sieve Change Alert",
            SEP2,
            "",
        ]
        if _customer:
            header.append(f"Customer  : {_customer}")
        if _foundry:
            header.append(f"Foundry   : {_foundry}")
        header += [
            f"Sieve     : #{sieve_pkey}",
            f"Date      : {date_fmt}",
            f"Shift     : {shift}",
            f"Level     : {alert_lvl}  (max change: {max_pct:+.1f}%)",
            "",
            SEP,
            "Sieve Band Changes",
            SEP,
            "",
            "The following sieve bands have changed significantly from the",
            "previous reading and may indicate a change in sand quality.",
            "",
        ]

        for ch in changes:
            sand_label = _SAND_FULL.get(int(ch.get("sand_type", 1)), "Sand")
            band  = str(ch.get("band", ""))
            prev  = ch.get("prev")
            curr  = ch.get("curr")
            pct   = ch.get("pct_change", 0.0)
            sev   = str(ch.get("severity", "")).upper()
            header.append(f"  {band}  ({sand_label})")
            header.append(f"  {'-' * (len(band) + len(sand_label) + 4)}")
            header.append(f"  Previous  : {float(prev):.2f} %" if prev is not None else "  Previous  : -")
            header.append(f"  Current   : {float(curr):.2f} %" if curr is not None else "  Current   : -")
            header.append(f"  Change    : {float(pct):+.2f} %  [{sev}]")
            header.append("")

        header += [
            "Action Required:",
            "Please investigate the change in sieve distribution.",
            "Check fresh sand addition rate and supplier batch AFS certificate.",
            "",
            SEP,
            "@Sandman Team",
            SEP,
        ]

        body = "\n".join(header)
        subject = f"[SandMan] Sieve Change Detected — Sieve #{sieve_pkey} | Shift {shift}"
        _send_plain(email_cfg, subject, body, alert_type="SIEVE_CHANGE")
        logger.info("[%s]  Sieve change email sent -> %s", label, ", ".join(_recipients(email_cfg)))
        return True

    except Exception:
        logger.warning("[%s]  Sieve email FAILED:\n%s", label, traceback.format_exc())
        return False

def send_prescription_email(result: dict, deviations: list, config: dict,
                             label: str = "") -> bool:
    """Send a plain-text Prescription Deviation alert email."""
    email_cfg = _get_email_cfg(config)
    if not _is_enabled(email_cfg, "PRESCRIPTION"):
        return False

    try:
        component  = str(result.get("component_id") or "-")
        group      = str(result.get("group_name")   or "-")
        date_str   = str(result.get("date")         or "-")
        shift      = str(result.get("shift")        or "-")
        batch_pkey = str(result.get("pkey")         or "-")

        _customer = email_cfg.get("dashboard_user", "")
        # Auto-fetch from foundry_line table; falls back to "" which hides the line.
        _foundry  = _get_foundry_line_name(config)

        try:
            date_fmt = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%b-%Y")
        except Exception:
            date_fmt = date_str

        out_devs = [d for d in deviations
                    if not d.get("within", True) and d.get("severity") != "annotation"]

        grp_sp_presc  = [d for d in out_devs if d.get("comparison") == "setpoint_vs_prescribed"]
        grp_act_presc = [d for d in out_devs if d.get("comparison") == "actual_vs_predicted"]
        grp_act_sp    = [d for d in out_devs if d.get("comparison") == "actual_vs_setpoint"]

        SEP  = "-" * 60
        SEP2 = "=" * 60

        def _plain_section(title, note, action, devs, ref_key):
            if not devs:
                return ""
            lines = ["", SEP, title, SEP, "", note, ""]
            for d in devs:
                lbl    = d.get("label") or d.get("param", "")
                actual = d.get("actual")
                pres   = d.get("prescribed")
                sp     = d.get("setpoint")
                pct    = d.get("pct_diff", 0)
                trend  = d.get("trend", "")
                lines.append(f"  {lbl}")
                lines.append(f"  {'-' * (len(lbl) + 2)}")
                if ref_key == "prescribed":
                    abs_dev = (float(actual) - float(pres)) if actual and pres else None
                    pct_str = f"({float(pct):+.1f}%)" if pct is not None else ""
                    abs_str = f"{abs_dev:+.2f} Kg  {pct_str}" if abs_dev is not None else pct_str
                    lines.append(f"  Prescribed  : {float(pres):.2f} Kg" if pres else "  Prescribed  : -")
                    lines.append(f"  Actual      : {float(actual):.2f} Kg" if actual else "  Actual      : -")
                    lines.append(f"  Deviation   : {abs_str}")
                elif ref_key == "setpoint":
                    abs_dev = (float(actual) - float(sp)) if actual and sp else None
                    pct_str = f"({float(pct):+.1f}%)" if pct is not None else ""
                    abs_str = f"{abs_dev:+.2f} Kg  {pct_str}" if abs_dev is not None else pct_str
                    lines.append(f"  Setpoint    : {float(sp):.2f} Kg" if sp else "  Setpoint    : -")
                    lines.append(f"  Actual      : {float(actual):.2f} Kg" if actual else "  Actual      : -")
                    lines.append(f"  Deviation   : {abs_str}")
                else:  # setpoint_vs_presc
                    abs_dev = (float(sp) - float(pres)) if sp and pres else None
                    pct_str = f"({float(pct):+.1f}%)" if pct is not None else ""
                    abs_str = f"{abs_dev:+.2f} Kg  {pct_str}" if abs_dev is not None else pct_str
                    lines.append(f"  Prescribed  : {float(pres):.2f} Kg" if pres else "  Prescribed  : -")
                    lines.append(f"  Setpoint    : {float(sp):.2f} Kg" if sp else "  Setpoint    : -")
                    lines.append(f"  Deviation   : {abs_str}")
                if trend:
                    lines.append(f"  Trend       : {trend}")
                lines.append("")
            lines += ["Action Required:", action, ""]
            return "\n".join(lines)

        header = [
            SEP2,
            "Sandmix Prescription Deviation Alert",
            SEP2,
            "",
        ]
        if _customer:
            header.append(f"Customer  : {_customer}")
        if _foundry:
            header.append(f"Foundry   : {_foundry}")
        header += [
            f"Component : {component}",
            f"Group     : {group}",
            f"Date      : {date_fmt}",
            f"Shift     : {shift}",
        ]

        sections = []
        sections.append(_plain_section(
            "Setpoint Not Updated as per Sandmix Prescription",
            "The machine setpoint is not as per the Sandmix prescription.",
            "Please update the setpoint on the mixer control panel before the next batch.",
            grp_sp_presc, "setpoint_vs_presc",
        ))
        sections.append(_plain_section(
            "Actual Dosage Not as per Sandmix Prescription",
            "The actual dosage dispensed is not as per the Sandmix prescription.",
            "Please verify the actual dosage and ensure that the prescribed Sandmix\n"
            "values are followed during the mixing process.",
            grp_act_presc, "prescribed",
        ))
        sections.append(_plain_section(
            "Actual Dosage Not as per Machine Setpoint",
            "The actual dosage dispensed is not as per the machine setpoint.",
            "Please check the load cell calibration and verify that the dispensing\n"
            "equipment is functioning correctly.",
            grp_act_sp, "setpoint",
        ))

        footer = ["", SEP, "@Sandman Team", SEP]
        body = "\n".join(header + [s for s in sections if s] + footer)

        _send_plain(email_cfg, f"[SandMan] Prescription Deviation — {component} | Shift {shift}", body, alert_type="PRESCRIPTION")
        logger.info("[%s]  Prescription email sent -> %s", label, ", ".join(_recipients(email_cfg)))
        return True

    except Exception:
        logger.warning("[%s]  Prescription email FAILED:\n%s", label, traceback.format_exc())
        return False

def _combined_dev_rows(deviations: list) -> str:
    """Build HTML table rows for prescription deviations (avoids nested f-string issues)."""
    rows = []
    for d in deviations:
        within  = d.get("within", True)
        bg      = "#ffe0e0" if not within else "#ffffff"
        fw      = "800"    if not within else "500"
        col     = "#c01616" if not within else "#2d7a4f"
        status  = "OUT"    if not within else "OK"
        pres    = float(d.get("prescribed", 0))
        act     = float(d.get("actual", 0))
        pct     = float(d.get("pct_diff", 0))
        label   = d.get("label", d.get("param", ""))
        rows.append(
            f'<tr style="background:{bg};border-bottom:1px solid #ddd">'
            f'<td style="padding:8px 12px;font-weight:{fw}">{label}</td>'
            f'<td style="padding:8px 12px;text-align:right;font-family:monospace">{pres:.3f}</td>'
            f'<td style="padding:8px 12px;text-align:right;font-family:monospace">{act:.3f}</td>'
            f'<td style="padding:8px 12px;text-align:right;font-family:monospace;font-weight:700;color:{col}">{pct:+.2f}%</td>'
            f'<td style="padding:8px 12px;text-align:center;font-weight:800;font-size:10px;color:{col}">{status}</td>'
            f'</tr>'
        )
    return "".join(rows)

def send_smc_batch_email(
    batch_breaches: list,
    shift_breaches: list,
    config: dict,
    label: str = "",
) -> bool:
    """Send a plain-text SMC (Mixer / prepared_sand_extra) LCL/UCL alert email."""
    email_cfg = _get_email_cfg(config)
    if not _is_enabled(email_cfg, "SMC_BATCH"):
        return False

    all_breaches = batch_breaches + shift_breaches
    if not all_breaches:
        return False

    def _fmt(v, decimals=3):
        if v is None:
            return "-"
        try:
            return f"{float(v):.{decimals}f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return str(v)

    def _status_short(status: str) -> str:
        if "BELOW LCL" in status:
            return "Below LCL  [LOW]"
        if "ABOVE UCL" in status:
            return "Above UCL  [HIGH]"
        return status

    def _blocks(breaches: list, value_label: str = "Value") -> list:
        rows = []
        for i, b in enumerate(breaches, 1):
            display = b.get("display", b.get("col", "-"))
            val     = _fmt(b.get("value"))
            lcl     = _fmt(b.get("lcl"))
            ucl     = _fmt(b.get("ucl"))
            st      = _status_short(b.get("status", "-"))
            z       = b.get("z_score")
            z_str   = f"   [{z:.1f} sigma]" if z is not None else ""
            rows += [
                f"  [{i}]  {display}",
                f"       {value_label:<14} : {val}",
                f"       Allowed Range  : {lcl}  to  {ucl}",
                f"       Status         : {st}{z_str}",
                "",
            ]
        return rows

    try:
        _customer = email_cfg.get("dashboard_user", "")
        _foundry  = _get_foundry_line_name(config)

        first    = all_breaches[0]
        date_str = str(first.get("date") or "-")
        shift    = str(first.get("shift") or "-")
        try:
            date_fmt = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%b-%Y")
        except Exception:
            date_fmt = date_str

        n_batch = len(batch_breaches)
        n_shift = len(shift_breaches)

        lines = [
            "We would like to bring to your notice that the following event has occurred,",
            "",
            f"  Customer   : {_customer or '-'}",
        ]
        if _foundry:
            lines.append(f"  Foundry    : {_foundry}")
        lines += [
            f"  Date       : {date_fmt}",
            f"  Shift      : {shift}",
            "",
        ]

        if batch_breaches:
            from collections import defaultdict
            by_batch = defaultdict(list)
            for b in batch_breaches:
                by_batch[b.get("pkey") or "?"].append(b)

            lines += [
                "------------------------------------------------------------",
                f"  MIXER (SMC)  —  Batch Alerts  ({n_batch} parameter(s) across {len(by_batch)} batch(es))",
                "------------------------------------------------------------",
                "",
            ]
            for pkey, blist in by_batch.items():
                t = blist[0].get("time", "")
                lines.append(f"  Batch #{pkey}   {date_fmt}   Shift {shift}   {t}")
                lines.append("")
                lines += _blocks(blist, value_label="Actual Value")

        if shift_breaches:
            bc = shift_breaches[0].get("batch_count", "-")
            lines += [
                "------------------------------------------------------------",
                f"  MIXER (SMC)  —  Shift Average Alerts  ({n_shift} parameter(s), {bc} batches)",
                "------------------------------------------------------------",
                "",
            ]
            lines += _blocks(shift_breaches, value_label="Shift Average")

        lines.append("@Sandman Team")
        body = "\n".join(lines)

        n_total = n_batch + n_shift
        subject = (
            f"[SandMan] Mixer Alert  |  {n_total} Breach(es)"
            f"  |  Shift {shift}  |  {date_fmt}"
        )
        _send_plain(email_cfg, subject, body, alert_type="SMC_BATCH")
        logger.info("[%s]  SMC batch email sent -> %s", label, ", ".join(_recipients(email_cfg)))
        return True

    except Exception:
        logger.warning("[%s]  SMC batch email FAILED:\n%s", label, traceback.format_exc())
        return False

def send_combined_alert_email(bb_result: dict, presc_result: dict,
                               deviations: list, config: dict,
                               label: str = "") -> bool:
    """Send a plain-text Combined Alert (Bad Batch + Prescription) email."""
    email_cfg = _get_email_cfg(config)
    if not _is_enabled(email_cfg, "COMBINED"):
        return False

    try:
        component  = str(bb_result.get("component_id") or "-")
        group      = str(bb_result.get("group_name")   or "-")
        date_str   = str(bb_result.get("date")         or "-")
        shift      = str(bb_result.get("shift")        or "-")

        smc        = bb_result.get("smc_value")
        cosp       = bb_result.get("cosp_value")
        diff       = bb_result.get("smc_cosp_diff")
        threshold  = bb_result.get("threshold", 2.0)
        diff_f     = float(diff) if diff is not None else 0.0

        out_of_tol = [d for d in deviations if not d.get("within", True)
                      and d.get("severity") != "annotation"]
        n_out      = len(out_of_tol)
        total      = len(deviations)

        _customer = email_cfg.get("dashboard_user", "")
        _foundry  = _get_foundry_line_name(config)

        try:
            date_fmt = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%b-%Y")
        except Exception:
            date_fmt = date_str

        lines = [
            "We would like to bring to your notice that the following event has occurred,",
            "",
            f"Customer          : {_customer or '-'}",
        ]
        if _foundry:
            lines.append(f"Foundry           : {_foundry}")
        lines += [
            f"Pattern/Component : {component}",
            f"Group             : {group}",
            f"Date              : {date_fmt}",
            f"Shift             : {shift}",
            "",
            "COMBINED ALERT: Bad Batch + Prescription Deviation",
            "Both sand quality and additive dosing are deviated simultaneously.",
            "This significantly increases the risk of casting defects.",
            "Investigate additive dosing records and sand preparation immediately.",
            "",
            "=" * 60,
            "Bad Batch",
            "=" * 60,
            f"  SMC Discharge : {f'{float(smc):.2f} %' if smc else '-'}",
            f"  COSP Setpoint : {f'{float(cosp):.2f} %' if cosp else '-'}",
            f"  Difference    : {diff_f:+.2f} %  (threshold: +/-{threshold})",
            "",
        ]

        # Prescription section (reuse same grouping as prescription email)
        grp_sp_presc  = [d for d in out_of_tol if d.get("comparison") == "setpoint_vs_prescribed"]
        grp_act_presc = [d for d in out_of_tol if d.get("comparison") == "actual_vs_predicted"]
        grp_act_sp    = [d for d in out_of_tol if d.get("comparison") == "actual_vs_setpoint"]

        lines += [
            "=" * 60,
            f"Prescription Deviation ({n_out} of {total} parameters out of tolerance)",
            "=" * 60,
        ]

        def _presc_lines(devs, ref_key):
            out = []
            for d in devs:
                lbl  = d.get("label") or d.get("param", "")
                pct  = d.get("pct_diff", 0)
                pct_str = f"({float(pct):+.1f}%)" if pct is not None else ""
                out.append(f"  {lbl}")
                if ref_key == "prescribed":
                    p = d.get("prescribed")
                    a = d.get("actual")
                    out.append(f"    Sandmix Prescription  : {f'{float(p):.2f} Kg' if p else '-'}")
                    out.append(f"    {lbl} Actual : {f'{float(a):.2f} Kg' if a else '-'}  {pct_str}")
                elif ref_key == "setpoint":
                    s = d.get("setpoint")
                    a = d.get("actual")
                    out.append(f"    {lbl} Setpoint : {f'{float(s):.2f} Kg' if s else '-'}")
                    out.append(f"    {lbl} Actual : {f'{float(a):.2f} Kg' if a else '-'}  {pct_str}")
                else:
                    p = d.get("prescribed"); s = d.get("setpoint")
                    out.append(f"    Sandmix Prescription  : {f'{float(p):.2f} Kg' if p else '-'}")
                    out.append(f"    {lbl} Setpoint : {f'{float(s):.2f} Kg' if s else '-'}  {pct_str}")
                out.append("")
            return out

        if grp_sp_presc:
            lines += ["", "Setpoint Not Updated as per Sandmix Prescription:"]
            lines += _presc_lines(grp_sp_presc, "setpoint_vs_presc")
        if grp_act_presc:
            lines += ["", "Actual Not as per Sandmix Prescription:"]
            lines += _presc_lines(grp_act_presc, "prescribed")
        if grp_act_sp:
            lines += ["", "Actual Not as per Machine Setpoint:"]
            lines += _presc_lines(grp_act_sp, "setpoint")

        lines.append("@Sandman Team")
        body = "\n".join(lines)

        _send_plain(email_cfg, f"[SandMan] Combined Alert — {component} | Shift {shift}", body, alert_type="COMBINED")
        logger.info("[%s]  Combined alert email sent -> %s", label, ", ".join(_recipients(email_cfg)))
        return True

    except Exception:
        logger.warning("[%s]  Combined alert email FAILED:\n%s", label, traceback.format_exc())
        return False

def check_and_send_combined(engine, config: dict, component_id: str,
                             date_str: str, shift: str, foundry_line_id: int,
                             label: str = "") -> bool:
    """
    Check if BOTH a BAD_BATCH and PRESCRIPTION alert exist for the same
    component + date + shift. If so, and the combined email hasn't been sent,
    send the combined alert email.

    Call this from both monitors after writing an alert.
    Returns True if combined email was sent.
    """
    email_cfg = _get_email_cfg(config)
    if not _is_enabled(email_cfg, "COMBINED"):
        return False

    try:
        from sqlalchemy import text
        import json as _json

        # Check both alert types exist for this component+date+shift
        sql = text("""
            SELECT alert_type, id, smc_value, cosp_value, smc_cosp_diff, bb_threshold,
                   deviations_json, component_id, group_name, `date`, `shift`,
                   batch_time, batch_pkey, notified_at
            FROM watchdog_alerts
            WHERE foundry_line_id = :fl_id
              AND component_id    = :comp
              AND `date`          = :dt
              AND alert_type      IN ('BAD_BATCH', 'PRESCRIPTION')
            ORDER BY id DESC
            LIMIT 10
        """)
        with engine.connect() as conn:
            rows = conn.execute(sql, {
                "fl_id": foundry_line_id,
                "comp" : component_id,
                "dt"   : str(date_str),
            }).mappings().fetchall()

        bb_rows    = [r for r in rows if r["alert_type"] == "BAD_BATCH"]
        presc_rows = [r for r in rows if r["alert_type"] == "PRESCRIPTION"]

        if not bb_rows or not presc_rows:
            return False  # Both must exist

        # Check if combined already notified (both rows notified_at set)
        if bb_rows[0]["notified_at"] and presc_rows[0]["notified_at"]:
            return False  # Already sent

        bb    = bb_rows[0]
        presc = presc_rows[0]

        bb_result = {
            "component_id" : component_id,
            "group_name"   : bb["group_name"],
            "date"         : str(bb["date"] or date_str),
            "shift"        : str(bb["shift"] or shift),
            "smc_value"    : float(bb["smc_value"])      if bb["smc_value"]      else None,
            "cosp_value"   : float(bb["cosp_value"])     if bb["cosp_value"]     else None,
            "smc_cosp_diff": float(bb["smc_cosp_diff"])  if bb["smc_cosp_diff"]  else None,
            "threshold"    : float(bb["bb_threshold"])   if bb["bb_threshold"]   else 2.0,
        }
        deviations = _json.loads(presc["deviations_json"]) if presc["deviations_json"] else []
        presc_result = {
            "component_id": component_id,
            "group_name"  : presc["group_name"],
            "date"        : str(presc["date"] or date_str),
            "shift"       : str(presc["shift"] or shift),
            "pkey"        : presc["batch_pkey"],
        }

        sent = send_combined_alert_email(bb_result, presc_result, deviations, config, label)

        if sent:
            # Mark both alerts as notified
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE watchdog_alerts SET notified_at = NOW() WHERE id IN (:id1, :id2)"
                ), {"id1": bb["id"], "id2": presc["id"]})

        return sent

    except Exception:
        logger.warning("[%s]  check_and_send_combined failed:\n%s", label, traceback.format_exc())
        return False

#  HTML TEMPLATES

def _bad_batch_body(component, group, date_str, shift, batch_pkey,
                    smc, cosp, diff, threshold, direction,
                    sev_col, sev_bg) -> str:
    return f"""
    <!-- Meta info row -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px">
      <tr>
        <td style="padding:10px 14px;background:#f0ece2;border:1px solid #ddd;width:50%">
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:{_C['subtle']};margin-bottom:3px">Component</div>
          <div style="font-size:14px;font-weight:800;color:{_C['ink']};font-family:monospace">{component}</div>
          <div style="font-size:11px;color:{_C['subtle']}">{group}</div>
        </td>
        <td style="padding:10px 14px;background:#f0ece2;border:1px solid #ddd;border-left:none;width:50%">
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:{_C['subtle']};margin-bottom:3px">Date &amp; Shift</div>
          <div style="font-size:14px;font-weight:800;color:{_C['ink']}">{date_str} &nbsp;·&nbsp; Shift {shift}</div>
          <div style="font-size:11px;color:{_C['subtle']};font-family:monospace">Batch #{batch_pkey}</div>
        </td>
      </tr>
    </table>

    <!-- Values table -->
    <table width="100%" cellpadding="0" cellspacing="0" style="border:2px solid {_C['border']};margin-bottom:16px">
      <thead>
        <tr style="background:{_C['yellow']};border-bottom:2px solid {_C['border']}">
          <th style="padding:10px 14px;text-align:left;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.1em">Measurement</th>
          <th style="padding:10px 14px;text-align:right;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.1em">Value</th>
          <th style="padding:10px 14px;text-align:left;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.1em">Note</th>
        </tr>
      </thead>
      <tbody>
        <tr style="border-bottom:1px solid #ddd;background:{_C['white']}">
          <td style="padding:10px 14px;font-weight:700;font-size:13px">SMC Discharge Compactability</td>
          <td style="padding:10px 14px;text-align:right;font-family:monospace;font-size:14px;font-weight:800">{smc} %</td>
          <td style="padding:10px 14px;font-size:12px;color:{_C['subtle']}">Measured value</td>
        </tr>
        <tr style="border-bottom:1px solid #ddd;background:#fafafa">
          <td style="padding:10px 14px;font-weight:700;font-size:13px">COSP</td>
          <td style="padding:10px 14px;text-align:right;font-family:monospace;font-size:14px;font-weight:800">{cosp} %</td>
          <td style="padding:10px 14px;font-size:12px;color:{_C['subtle']}">Measured value</td>
        </tr>
        <tr style="border-bottom:1px solid #ddd;background:{sev_bg}">
          <td style="padding:10px 14px;font-weight:800;font-size:13px;color:{sev_col}">Difference |SMC − COSP|</td>
          <td style="padding:10px 14px;text-align:right;font-family:monospace;font-size:18px;font-weight:800;color:{sev_col}">{diff} %</td>
          <td style="padding:10px 14px;font-size:12px;color:{sev_col};font-weight:700">{direction}</td>
        </tr>
        <tr style="background:{_C['white']}">
          <td style="padding:10px 14px;font-weight:700;font-size:13px;color:{_C['subtle']}">Alert Threshold</td>
          <td style="padding:10px 14px;text-align:right;font-family:monospace;font-size:14px;font-weight:700;color:{_C['subtle']}">±{threshold} %</td>
          <td style="padding:10px 14px;font-size:12px;color:{_C['subtle']}">Configured limit</td>
        </tr>
      </tbody>
    </table>

    <!-- Action note -->
    <div style="background:{sev_bg};border:2px solid {sev_col};padding:12px 16px;margin-bottom:16px">
      <div style="font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;color:{sev_col};margin-bottom:4px">Action Required</div>
      <div style="font-size:13px;color:{_C['ink']};line-height:1.6">
        Review the additive batch for component <strong>{component}</strong>.
        The absolute difference between SMC discharge compactability and COSP
        exceeds the configured threshold of ±{threshold}%.
        Check the batch records, verify sensor readings, and investigate additive dispensing.
      </div>
    </div>
    """

def _prescription_annotations_html(annotations: list) -> str:
    """Render a compact note block for zero/null batch parameters."""
    if not annotations:
        return ""
    rows = ""
    for a in annotations:
        tag  = a.get("annotation", "?")
        lbl  = a.get("label") or a.get("param", "")
        msg  = a.get("message", "")
        icon = "⚠" if tag == "ZERO" else "○"
        col  = "#c44a08" if tag == "ZERO" else "#888"
        rows += (
            f'<tr>'
            f'<td style="padding:4px 8px;font-size:12px;font-weight:700;color:{col};white-space:nowrap">'
            f'{icon} {lbl}</td>'
            f'<td style="padding:4px 0;font-size:11px;color:#888;font-style:italic">'
            f'— {tag} — {msg}</td>'
            f'</tr>'
        )
    return (
        f'<div style="margin:14px 0 8px;font-size:10px;font-weight:700;letter-spacing:.09em;'
        f'text-transform:uppercase;color:#aaa">Batch Annotations</div>'
        f'<table cellpadding="0" cellspacing="0" style="margin-bottom:16px;'
        f'border-top:1px solid #eee">{rows}</table>'
    )

def _prescription_body(component, group, date_str, shift, batch_pkey,
                        deviations, n_out, total, sev_col, sev_bg,
                        customer="", foundry="") -> str:
    """
    Prescription deviation email body.
    Deviations are grouped by comparison type — each section carries its own
    specific root-cause heading and explanation.
    """
    annotations = [d for d in deviations if d.get("severity") == "annotation"]
    out_devs    = [d for d in deviations if not d.get("within", True)
                   and d.get("severity") != "annotation"]

    try:
        date_fmt = datetime.strptime(str(date_str), "%Y-%m-%d").strftime("%d-%b-%Y")
    except Exception:
        date_fmt = str(date_str)

    # Group deviations by comparison type
    grp_sp_presc  = [d for d in out_devs if d.get("comparison") == "setpoint_vs_prescribed"]
    grp_act_presc = [d for d in out_devs if d.get("comparison") == "actual_vs_predicted"]
    grp_act_sp    = [d for d in out_devs if d.get("comparison") == "actual_vs_setpoint"]

    DASH = "—"   # em dash

    def _rows(devs, ref_key):
        html = ""
        for d in devs:
            lbl    = d.get("label") or d.get("param", "")
            actual = d.get("actual")
            pres   = d.get("prescribed")
            sp     = d.get("setpoint")
            pct    = d.get("pct_diff", 0)
            trend  = d.get("trend", "")
            sev    = str(d.get("severity", "warning")).lower()
            col    = _C["red"] if "critical" in sev else _C["orange"]
            pct_str = (f"{float(pct):+.1f}%" if pct is not None else "")

            a_fmt  = f"{float(actual):.2f}" if actual is not None else DASH
            p_fmt  = f"{float(pres):.2f}"   if pres   is not None else DASH
            sp_fmt = f"{float(sp):.2f}"     if sp     is not None else DASH

            if ref_key == "prescribed":
                cells = (
                    f'<td style="padding:4px 12px;font-size:13px;color:#555">Sandmix Prescription</td>'
                    f'<td style="padding:4px 8px;font-size:13px;font-weight:700;font-family:monospace;color:#1a1a1a">{p_fmt}</td>'
                    f'<td style="padding:4px 12px;font-size:13px;color:#555">{lbl} Actual</td>'
                    f'<td style="padding:4px 8px;font-size:13px;font-weight:700;font-family:monospace;color:{col}">'
                    f'{a_fmt} <span style="font-size:11px;margin-left:4px">({pct_str})</span></td>'
                )
            elif ref_key == "setpoint":
                cells = (
                    f'<td style="padding:4px 12px;font-size:13px;color:#555">{lbl} Setpoint</td>'
                    f'<td style="padding:4px 8px;font-size:13px;font-weight:700;font-family:monospace;color:#1a1a1a">{sp_fmt}</td>'
                    f'<td style="padding:4px 12px;font-size:13px;color:#555">{lbl} Actual</td>'
                    f'<td style="padding:4px 8px;font-size:13px;font-weight:700;font-family:monospace;color:{col}">'
                    f'{a_fmt} <span style="font-size:11px;margin-left:4px">({pct_str})</span></td>'
                )
            else:  # setpoint_vs_presc
                cells = (
                    f'<td style="padding:4px 12px;font-size:13px;color:#555">Sandmix Prescription</td>'
                    f'<td style="padding:4px 8px;font-size:13px;font-weight:700;font-family:monospace;color:#1a1a1a">{p_fmt}</td>'
                    f'<td style="padding:4px 12px;font-size:13px;color:#555">{lbl} Setpoint</td>'
                    f'<td style="padding:4px 8px;font-size:13px;font-weight:700;font-family:monospace;color:{col}">'
                    f'{sp_fmt} <span style="font-size:11px;margin-left:4px">({pct_str})</span></td>'
                )

            html += (
                f'<tr><td style="padding:6px 0;font-size:14px;font-weight:700;color:{col}'
                f';white-space:nowrap;width:160px">{lbl}</td>{cells}</tr>'
            )
            if trend:
                html += (
                    f'<tr><td colspan="5" style="padding:0 0 6px;font-size:11px'
                    f';color:#c44a08;font-weight:600">Trend: {trend}</td></tr>'
                )
        return html

    def _section(title, subtitle, devs, ref_key, border_col="#c44a08"):
        if not devs:
            return ""
        rows = _rows(devs, ref_key)
        return (
            f'<div style="margin-bottom:22px;border-left:4px solid {border_col};padding-left:14px">'
            f'<p style="font-size:14px;font-weight:700;color:{border_col};margin:0 0 4px">{title}</p>'
            f'<p style="font-size:13px;color:#555;margin:0 0 10px;line-height:1.5">{subtitle}</p>'
            f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
            f'{rows}</table></div>'
        )

    sections = ""

    sections += _section(
        title      = "Setpoint Not Updated as per Sandmix Prescription",
        subtitle   = (
            "The machine setpoint for the additive(s) below has not been changed to match "
            "the current AI prescription for this component. "
            "Please update the setpoint on the mixer control panel before the next batch."
        ),
        devs       = grp_sp_presc,
        ref_key    = "setpoint_vs_presc",
        border_col = "#8b0000",
    )

    sections += _section(
        title      = "Actual Not as per Sandmix Prescription",
        subtitle   = (
            "The actual dosage dispensed does not match the AI prescription. "
            "The operator may not be following the prescribed values, "
            "which will impact prepared sand properties if continued."
        ),
        devs       = grp_act_presc,
        ref_key    = "prescribed",
        border_col = "#c44a08",
    )

    sections += _section(
        title      = "Actual Not as per Machine Setpoint",
        subtitle   = (
            "The actual dosage dispensed does not match the machine setpoint. "
            "This may indicate a load cell calibration issue or a mechanical fault "
            "in the dispensing system. Please check the load cell and dosing mechanism."
        ),
        devs       = grp_act_sp,
        ref_key    = "setpoint",
        border_col = "#b5700a",
    )

    meta_rows = (
        f'<tr><td style="padding:3px 0;font-size:13px;color:#555;width:160px">Customer</td>'
        f'<td style="padding:3px 0;font-size:13px;font-weight:600;color:#1a1a1a">: {customer or DASH}</td></tr>'
        f'<tr><td style="padding:3px 0;font-size:13px;color:#555">Foundry</td>'
        f'<td style="padding:3px 0;font-size:13px;font-weight:600;color:#1a1a1a">: {foundry or DASH}</td></tr>'
        f'<tr><td style="padding:3px 0;font-size:13px;color:#555">Pattern / Component</td>'
        f'<td style="padding:3px 0;font-size:13px;font-weight:600;color:#1a1a1a">: {component}</td></tr>'
        f'<tr><td style="padding:3px 0;font-size:13px;color:#555">Group</td>'
        f'<td style="padding:3px 0;font-size:13px;font-weight:600;color:#1a1a1a">: {group or DASH}</td></tr>'
        f'<tr><td style="padding:3px 0;font-size:13px;color:#555">Date</td>'
        f'<td style="padding:3px 0;font-size:13px;font-weight:600;color:#1a1a1a">: {date_fmt}</td></tr>'
        f'<tr><td style="padding:3px 0;font-size:13px;color:#555">Shift</td>'
        f'<td style="padding:3px 0;font-size:13px;font-weight:600;color:#1a1a1a">: {shift}</td></tr>'
    )

    return (
        f'<p style="font-size:14px;color:#333;margin:0 0 18px;line-height:1.6">'
        f'We would like to bring to your notice that the following event has occurred,</p>'
        f'<table cellpadding="0" cellspacing="0" style="margin-bottom:24px">{meta_rows}</table>'
        f'<div style="border-top:2px solid #e0e0e0;padding-top:18px;margin-bottom:8px">{sections}</div>'
        f'{_prescription_annotations_html(annotations)}'
        f'<p style="font-size:13px;color:#888;margin:0">@Sandman Team</p>'
    )

def _wrap_email(title: str, badge_txt: str, badge_col: str, badge_bg: str,
                headline: str, subline: str, body: str, dashboard_url: str) -> str:
    now = datetime.now().strftime("%d %b %Y, %H:%M")
    # Severity bar colour for left border accent
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:14px;color:#1a1a1a">

<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:32px 0">
<tr><td align="center" style="padding:0 16px">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.10)">

  <!-- ── Navy header bar ── -->
  <tr>
    <td style="background:#1f497d;padding:20px 28px">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td>
            <span style="font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#ffe14d">SandMan® AI Watchdog</span><br>
            <span style="font-size:13px;font-weight:400;color:rgba(255,255,255,.7)">MPM Infosoft Private Limited</span>
          </td>
          <td align="right">
            <span style="display:inline-block;padding:4px 14px;background:{badge_col};color:#fff;
                         font-size:11px;font-weight:800;letter-spacing:.10em;text-transform:uppercase;
                         border-radius:3px">{badge_txt}</span>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- ── Alert headline ── -->
  <tr>
    <td style="background:#ffffff;padding:24px 28px 0;border-left:4px solid {badge_col}">
      <div style="font-size:22px;font-weight:700;color:#1f497d;line-height:1.2;margin-bottom:4px">{headline}</div>
      <div style="font-size:13px;color:#555;margin-bottom:20px">{subline}</div>
      <hr style="border:none;border-top:1px solid #e8e8e8;margin:0">
    </td>
  </tr>

  <!-- ── Body ── -->
  <tr>
    <td style="background:#ffffff;padding:20px 28px 24px;border-left:4px solid {badge_col}">
      {body}
    </td>
  </tr>

  <!-- ── Footer ── -->
  <tr>
    <td style="background:#f8f9fa;border-top:1px solid #e0e0e0;padding:16px 28px">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="font-size:11px;color:#888">
            Generated: {now} &nbsp;·&nbsp; SandMan® AI Watchdog
          </td>
          <td align="right">
          </td>
        </tr>
      </table>
    </td>
  </tr>

</table>
</td></tr>
</table>

</body>
</html>"""

#  SMTP SEND

def _send_plain(email_cfg: dict, subject: str, body: str, alert_type: str = "") -> None:
    """Send a plain-text only email (no HTML part)."""
    from_addr = email_cfg.get("from_address", "")
    to_addrs  = _recipients(email_cfg, alert_type)
    reply_to  = email_cfg.get("reply_to", "")

    if not from_addr:
        raise ValueError("email notifications: 'from_address' is not configured")
    if not to_addrs:
        raise ValueError("email notifications: 'to_addresses' is empty")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    if reply_to:
        msg["Reply-To"] = reply_to

    host     = email_cfg.get("smtp_host", "smtp.gmail.com")
    port     = int(email_cfg.get("smtp_port", 587))
    use_tls  = bool(email_cfg.get("use_tls", True))
    use_ssl  = bool(email_cfg.get("use_ssl", False))
    username = email_cfg.get("username", "")
    password = email_cfg.get("password", "")

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if use_tls:
                server.starttls()
        try:
            if username:
                server.login(username, password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
        finally:
            server.quit()
        _email_logger.info(
            "SENT  type=%-16s  to=%s  subject=%s  smtp=%s:%d",
            alert_type, ", ".join(to_addrs), subject, host, port,
        )
    except Exception as exc:
        _email_logger.error(
            "SEND_FAILED  type=%-16s  to=%s  smtp=%s:%d  error=%s",
            alert_type, ", ".join(to_addrs), host, port, exc,
        )
        raise

def _send(email_cfg: dict, subject: str, html: str, alert_type: str = "") -> None:
    """Build and send the MIME email. Raises on failure (caller handles logging)."""
    from_addr = email_cfg.get("from_address", "")
    to_addrs  = _recipients(email_cfg, alert_type)
    reply_to  = email_cfg.get("reply_to", "")

    if not from_addr:
        _email_logger.error("SEND_FAILED  type=%-16s  error=from_address not configured", alert_type)
        raise ValueError("email notifications: 'from_address' is not configured")
    if not to_addrs:
        _email_logger.error("SEND_FAILED  type=%-16s  error=to_addresses is empty", alert_type)
        raise ValueError("email notifications: 'to_addresses' is empty")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    if reply_to:
        msg["Reply-To"] = reply_to

    plain = _html_to_plain(subject)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))

    host     = email_cfg.get("smtp_host", "smtp.gmail.com")
    port     = int(email_cfg.get("smtp_port", 587))
    use_tls  = bool(email_cfg.get("use_tls", True))
    use_ssl  = bool(email_cfg.get("use_ssl", False))
    username = email_cfg.get("username", "")
    password = email_cfg.get("password", "")

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if use_tls:
                server.starttls()
        try:
            if username:
                server.login(username, password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
        finally:
            server.quit()
        _email_logger.info(
            "SENT  type=%-16s  to=%s  subject=%s  smtp=%s:%d",
            alert_type, ", ".join(to_addrs), subject, host, port,
        )
    except Exception as exc:
        _email_logger.error(
            "SEND_FAILED  type=%-16s  to=%s  smtp=%s:%d  error=%s",
            alert_type, ", ".join(to_addrs), host, port, exc,
        )
        raise

def _send_raw(email_cfg: dict, msg, to_addrs: list, alert_type: str = "") -> None:
    """Send a pre-built MIMEMultipart message."""
    host     = email_cfg.get("smtp_host", "smtp.gmail.com")
    port     = int(email_cfg.get("smtp_port", 587))
    use_tls  = bool(email_cfg.get("use_tls", True))
    use_ssl  = bool(email_cfg.get("use_ssl", False))
    username = email_cfg.get("username", "")
    password = email_cfg.get("password", "")
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if use_tls:
                server.starttls()
        try:
            if username:
                server.login(username, password)
            server.sendmail(msg["From"], to_addrs, msg.as_string())
        finally:
            server.quit()
        _email_logger.info("SENT  type=%-16s  to=%s  smtp=%s:%d",
                           alert_type, ", ".join(to_addrs), host, port)
    except Exception as exc:
        _email_logger.error("SEND_FAILED  type=%-16s  to=%s  smtp=%s:%d  error=%s",
                            alert_type, ", ".join(to_addrs), host, port, exc)
        raise

def _html_to_plain(subject: str) -> str:
    return (
        f"{subject}\n\n"
        "This is an automated alert from SandMan® AI Watchdog.\n"
        "Please open the dashboard for full details."
    )

#  HELPERS

def _get_email_cfg(config: dict) -> dict:
    return config.get("notifications", {}).get("email", {})

def _get_foundry_line_name(config: dict) -> str:
    """
    Auto-fetch the foundry line name from the foundry_line table.
    Returns e.g. "SAVELLI", "Disa 230C (Line1)", "Disa 231Y (Line2)".
    Falls back to "" on any error so the Foundry line is simply omitted.
    """
    fl_id = config.get("foundry_line_id")
    if not fl_id:
        return ""
    try:
        from .pipeline.db_connector import get_engine
        from sqlalchemy import text
        engine = get_engine(config)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT `name` FROM `foundry_line` WHERE `pkey` = :id AND `deleted` = 0"),
                {"id": int(fl_id)},
            ).mappings().first()
        return str(row["name"]).strip() if row else ""
    except Exception:
        return ""

def _force_enabled_if_recipients(email_cfg: dict, alert_type: str) -> dict:
    """If enabled=False but recipients exist for this alert_type, return a copy with enabled=True."""
    if email_cfg.get("enabled", False):
        return email_cfg
    typed_recipients = [
        r for r in email_cfg.get("recipients", [])
        if alert_type in r.get("types", [])
    ]
    if typed_recipients:
        return {**email_cfg, "enabled": True}
    return email_cfg

def _is_enabled(email_cfg: dict, alert_type: str) -> bool:
    if not email_cfg.get("enabled", False):
        _email_logger.debug("SKIPPED  type=%-16s  reason=email disabled in config", alert_type)
        return False
    # Per-recipient structure: derive allowed set from all recipients' types
    recipients = email_cfg.get("recipients", [])
    if recipients:
        allowed = {t for r in recipients for t in r.get("types", [])}
    else:
        allowed = set(email_cfg.get("alert_types", ["BAD_BATCH", "PRESCRIPTION", "SI", "SMC_BATCH"]))
    if alert_type not in allowed:
        _email_logger.debug("SKIPPED  type=%-16s  reason=type not in alert_types=%s", alert_type, sorted(allowed))
        return False
    return True

def _recipients(email_cfg: dict, alert_type: str = "") -> list:
    """
    Return recipient email addresses, optionally filtered by alert_type.

    New structure (per-recipient types):
      email_cfg["recipients"] = [{"email": "x@y.com", "types": ["SI", "BAD_BATCH", ...]}]

    Legacy fallback:
      email_cfg["to_addresses"] = ["x@y.com"]
      email_cfg["alert_types"]  = ["SI", "BAD_BATCH"]
    """
    recipients = email_cfg.get("recipients", [])
    if recipients:
        all_emails = []
        typed_emails = []
        for r in recipients:
            email = str(r.get("email", "")).strip()
            if not email:
                continue
            all_emails.append(email)
            if alert_type:
                if alert_type in r.get("types", []):
                    typed_emails.append(email)
            else:
                typed_emails.append(email)
        # If alert_type provided but no recipient has it configured, send to all.
        # This handles system-level types (DATA_FLOW) that pre-date per-recipient config.
        if alert_type and not typed_emails:
            return all_emails
        return typed_emails

    addrs = email_cfg.get("to_addresses", [])
    if isinstance(addrs, str):
        addrs = [a.strip() for a in addrs.split(",") if a.strip()]
    else:
        addrs = [str(a).strip() for a in addrs if str(a).strip()]
    if alert_type:
        # DATA_FLOW and other system types not in alert_types default → send to all
        _default_types = {"BAD_BATCH", "PRESCRIPTION", "SI", "SMC_BATCH",
                          "SIEVE_CHANGE", "COMBINED", "DATA_FLOW"}
        allowed = set(email_cfg.get("alert_types", _default_types))
        if allowed and alert_type not in allowed:
            return []
    return addrs
