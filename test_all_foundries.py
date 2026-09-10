"""
Test bad_batch, sieve, prescription, and SI monitors for every foundry
in the sandman_dev watchdog_si_config registry.

Run: python test_all_foundries.py
"""
import json, re, sys, logging
from datetime import date, timedelta
import pandas as pd
import pymysql

sys.path.insert(0, '.')
logging.basicConfig(level=logging.WARNING)

SEP  = "=" * 70
SEP2 = "-" * 70

# ── 1. Load all registry rows ─────────────────────────────────────
reg = pymysql.connect(host='localhost', port=3306, user='root',
                      password='Password@123', db='sandman_dev')
cur = reg.cursor()
cur.execute("SELECT id, foundry_label, config_json FROM watchdog_si_config ORDER BY foundry_label")
REGISTRY = cur.fetchall()
reg.close()

print(f"Registry foundries: {len(REGISTRY)}")
print()

# ── 2. Helper: derive DB name + line_id from label ────────────────
def _parse_label(label: str, cfg: dict):
    m = re.match(r'^(.+)_sandman_L(\d+)$', label, re.IGNORECASE)
    if m:
        prefix  = m.group(1).lower()
        line_id = int(m.group(2))
        db_name = f"{prefix}_sandman"
    else:
        db_name = cfg.get('database', {}).get('name') or '?'
        line_id = cfg.get('foundry_line_id') or 0
    return db_name, line_id


# ── 3. Per-foundry check function ─────────────────────────────────
def test_foundry(reg_id: int, label: str, cfg_json: str):
    cfg      = json.loads(cfg_json)
    db_name, line_id = _parse_label(label, cfg)

    # Inject DB connection
    cfg['database'] = {
        'host': 'localhost', 'port': 3306,
        'name': db_name, 'user': 'root',
        'password': 'Password@123', 'pool_size': 2,
    }
    cfg['foundry_line_id'] = line_id

    print(SEP)
    print(f"FOUNDRY : {label}")
    print(f"DB      : {db_name}   line_id={line_id}")
    print(SEP)

    # Check DB is reachable
    try:
        conn_test = pymysql.connect(host='localhost', port=3306, user='root',
                                    password='Password@123', db=db_name,
                                    connect_timeout=3)
        conn_test.close()
    except Exception as e:
        print(f"  [SKIP] Cannot connect to {db_name}: {e}")
        print()
        return

    from watchdog.pipeline.db_connector import get_engine
    from sqlalchemy import text as sa_text

    engine = get_engine(cfg)

    # Latest date
    try:
        with engine.connect() as c:
            row = c.execute(sa_text(
                "SELECT MAX(DATE(date)) FROM additive "
                "WHERE deleted=0 AND foundry_line_id=:fl"
            ), {"fl": line_id}).fetchone()
        latest_date = str(row[0]) if row and row[0] else None
    except Exception as e:
        print(f"  [SKIP] additive table error: {e}")
        print()
        return

    if not latest_date or latest_date == 'None':
        print(f"  [SKIP] No data in additive for line_id={line_id}")
        print()
        return

    print(f"  Latest additive date: {latest_date}")
    print()

    # ── BAD BATCH ─────────────────────────────────────────────────
    bb_cfg   = cfg.get('bad_batch_watchdog', {})
    bb_en    = bb_cfg.get('enabled', False)
    smc_col  = bb_cfg.get('smc_col',  'compactability_smc_pct')
    cosp_col = bb_cfg.get('cosp_col', 'cosp_percentage_pct')
    abs_warn = float(bb_cfg.get('abs_warn_thr', 2.0))
    abs_crit = float(bb_cfg.get('abs_crit_thr', 3.0))

    print(f"  [BAD BATCH]  enabled={bb_en}")
    try:
        with engine.connect() as c:
            df_bb = pd.read_sql(sa_text(f"""
                SELECT a.pkey, a.component_id, DATE(a.date) as date, a.shift,
                       a.`{smc_col}` as smc, a.`{cosp_col}` as cosp
                FROM additive a
                WHERE a.foundry_line_id = :fl AND a.deleted = 0
                  AND DATE(a.date) = :dt
                  AND a.`{smc_col}` IS NOT NULL AND a.`{cosp_col}` IS NOT NULL
                ORDER BY a.pkey
            """), c, params={"fl": line_id, "dt": latest_date})

        if df_bb.empty:
            print(f"  -> No batches found for {latest_date}")
        else:
            df_bb['diff'] = (df_bb['smc'] - df_bb['cosp']).abs()
            ok_n   = (df_bb['diff'] <= 1.0).sum()
            warn_n = ((df_bb['diff'] > 1.0) & (df_bb['diff'] <= abs_crit)).sum()
            crit_n = (df_bb['diff'] > abs_crit).sum()
            print(f"  -> {len(df_bb)} batches on {latest_date}: "
                  f"OK={ok_n}  WARNING={warn_n}  CRITICAL={crit_n}")
            worst = df_bb.nlargest(3, 'diff')
            for _, r in worst.iterrows():
                sev = 'CRITICAL' if r['diff'] > abs_crit else ('WARNING' if r['diff'] > abs_warn else 'OK')
                if sev != 'OK':
                    print(f"    {str(r['component_id']):<20}  sh={r['shift']}  "
                          f"smc={r['smc']:.1f}  cosp={r['cosp']:.1f}  diff={r['diff']:.2f}  {sev}")
    except Exception as e:
        print(f"  -> ERROR: {e}")

    # ── SIEVE ─────────────────────────────────────────────────────
    sv_cfg = cfg.get('sieve_watchdog', {})
    sv_en  = sv_cfg.get('enabled', False)
    ok_thr   = float(sv_cfg.get('ok_thr',      1.0))
    crit_thr = float(sv_cfg.get('critical_thr', 5.0))
    warn_thr = float(sv_cfg.get('warn_thr',     3.0))

    print(f"\n  [SIEVE]  enabled={sv_en}")
    try:
        with engine.connect() as c:
            df_sv = pd.read_sql(sa_text("""
                SELECT b.sieve_id as pkey, DATE(b.date) as date,
                       b.band_type, b.value as pct, b.sand_type
                FROM sieve_band_data b
                WHERE b.deleted = 0 AND b.sand_type = 1
                  AND b.foundry_line_id = :fl
                ORDER BY b.sieve_id DESC
                LIMIT 80
            """), c, params={"fl": line_id})

        if df_sv.empty:
            print(f"  -> No sieve data found")
        else:
            pkeys = df_sv['pkey'].unique()
            curr  = df_sv[df_sv['pkey'] == pkeys[0]].set_index('band_type')['pct']
            curr_dt = df_sv[df_sv['pkey'] == pkeys[0]]['date'].iloc[0]
            print(f"  -> Latest sieve: {curr_dt}  bands={dict(curr.round(2))}")
            if len(pkeys) > 1:
                prev = df_sv[df_sv['pkey'] == pkeys[1]].set_index('band_type')['pct']
                alerts = []
                for band in sorted(set(curr.index) & set(prev.index)):
                    pv = float(prev[band])
                    if pv == 0:
                        continue
                    chg = abs((float(curr[band]) - pv) / pv * 100)
                    sev = 'OK' if chg <= ok_thr else ('WARNING' if chg <= warn_thr else 'CRITICAL')
                    if sev != 'OK':
                        alerts.append(f"{band}:{chg:.1f}%={sev}")
                if alerts:
                    print(f"  -> Changes vs previous: {' | '.join(alerts)}")
                else:
                    print(f"  -> All bands within threshold vs previous sieve")
    except Exception as e:
        print(f"  -> ERROR: {e}")

    # ── PRESCRIPTION ─────────────────────────────────────────────
    pw_cfg    = cfg.get('prescription_watchdog', {})
    pw_en     = pw_cfg.get('enabled', False)
    monitored = pw_cfg.get('monitored_params', [])

    print(f"\n  [PRESCRIPTION]  enabled={pw_en}  params={monitored}")
    if monitored:
        try:
            with engine.connect() as c:
                desc = c.execute(sa_text("DESCRIBE `additive`")).fetchall()
                db_cols = {r[0] for r in desc}
                sel = [f"a.`{p}_actual`" for p in monitored if f"{p}_actual" in db_cols]
                if sel:
                    df_p = pd.read_sql(sa_text(f"""
                        SELECT {', '.join(sel)}
                        FROM additive a
                        WHERE a.foundry_line_id = :fl AND a.deleted = 0
                          AND DATE(a.date) = :dt
                    """), c, params={"fl": line_id, "dt": latest_date})
                    for p in monitored:
                        col = f"{p}_actual"
                        if col in df_p.columns:
                            s = df_p[col].dropna()
                            if len(s):
                                print(f"  -> {p}: n={len(s)}  mean={s.mean():.3f}  "
                                      f"min={s.min():.3f}  max={s.max():.3f}")
                            else:
                                print(f"  -> {p}: NO DATA")
                else:
                    print(f"  -> None of {[p+'_actual' for p in monitored]} exist in additive table")
        except Exception as e:
            print(f"  -> ERROR: {e}")
    else:
        print(f"  -> No monitored_params configured")

    # ── SI ────────────────────────────────────────────────────────
    si_params = cfg.get('si_params', {}).get('params', [])
    optimal   = cfg.get('optimal_values', {})

    print(f"\n  [SI]  params={si_params[:5]}{'...' if len(si_params)>5 else ''}")
    if si_params:
        from watchdog.pipeline.data_fetcher import fetch_prepared_sand
        cfg_si = dict(cfg)
        cfg_si['parameters'] = dict(cfg.get('parameters', {}))
        cfg_si['parameters']['prepared_sand'] = si_params

        try:
            start_dt = date.fromisoformat(latest_date) - timedelta(days=7)
            end_dt   = date.fromisoformat(latest_date)
            df_si    = fetch_prepared_sand(cfg_si, start_date=start_dt, end_date=end_dt)

            if df_si.empty:
                print(f"  -> No preparedsand data for {start_dt} to {end_dt}")
            else:
                print(f"  -> {len(df_si)} rows ({start_dt} to {end_dt})")
                alerts = []
                for p in si_params:
                    if p in df_si.columns and p in optimal:
                        col = df_si[p].dropna()
                        if len(col) and float(optimal[p]):
                            dev = (col.mean() - float(optimal[p])) / float(optimal[p]) * 100
                            sev = 'OK' if abs(dev) < 5 else ('WATCH' if abs(dev) < 10 else 'ALERT')
                            if sev != 'OK':
                                alerts.append(f"{p}:{dev:+.1f}%={sev}")
                if alerts:
                    print(f"  -> Deviations from optimal: {' | '.join(alerts)}")
                else:
                    ok_checked = sum(1 for p in si_params
                                     if p in df_si.columns and p in optimal
                                     and len(df_si[p].dropna()) > 0)
                    print(f"  -> All {ok_checked} params within ±5% of optimal")
        except Exception as e:
            print(f"  -> ERROR: {e}")
    else:
        print(f"  -> No si_params configured")

    print()


# ── 4. Run all ────────────────────────────────────────────────────
summary = []
for reg_id, label, cfg_json in REGISTRY:
    test_foundry(reg_id, label, cfg_json)
    cfg     = json.loads(cfg_json)
    db_name, line_id = _parse_label(label, cfg)
    summary.append((label, db_name, line_id))

print(SEP)
print(f"DONE — tested {len(summary)} foundries")
for label, db, lid in summary:
    print(f"  {label:<35}  db={db:<25}  line={lid}")
print(SEP)
