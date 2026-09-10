"""
Test bad_batch, sieve, prescription, and SI monitors against Munjalkiriu data.
Run: python test_monitors_munjal.py [1|2]   (line number, default 1)
"""
import json, sys, logging
from datetime import date, timedelta
import pandas as pd

sys.path.insert(0, '.')
logging.basicConfig(level=logging.WARNING)

LINE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 1
LABEL   = f"Munjalkiriu_sandman_L{LINE_ID}"
SEP     = "=" * 65

# ── Load config from DB ───────────────────────────────────────────
import pymysql as _pm
_reg = _pm.connect(host='localhost', port=3306, user='root',
                   password='Password@123', db='sandman_dev')
_cur = _reg.cursor()
_cur.execute("SELECT config_json FROM watchdog_si_config WHERE foundry_label=%s", (LABEL,))
row = _cur.fetchone()
_reg.close()
if not row:
    print(f"No config found for {LABEL}")
    sys.exit(1)

cfg = json.loads(row[0])
# Inject DB connection (not stored in registry config for this foundry)
cfg['database'] = {
    'host': 'localhost', 'port': 3306,
    'name': 'munjalkiriu_sandman',
    'user': 'root', 'password': 'Password@123',
    'pool_size': 3,
}
cfg['foundry_line_id'] = LINE_ID

print(f"Foundry : {LABEL}")
print(f"DB      : munjalkiriu_sandman  fl_id={LINE_ID}")

# ── Latest data date ─────────────────────────────────────────────
from watchdog.pipeline.db_connector import get_engine
from sqlalchemy import text as sa_text

engine = get_engine(cfg)
with engine.connect() as c:
    row = c.execute(sa_text(
        "SELECT MAX(DATE(date)) FROM additive WHERE deleted=0 AND foundry_line_id=:fl"
    ), {"fl": LINE_ID}).fetchone()
LATEST_DATE = str(row[0])
print(f"Latest data date: {LATEST_DATE}")
print()

# ══════════════════════════════════════════════════════════════════
# 1. BAD BATCH
# ══════════════════════════════════════════════════════════════════
print(SEP)
print("1. BAD BATCH")
print(SEP)

bb_cfg   = cfg.get('bad_batch_watchdog', {})
smc_col  = bb_cfg.get('smc_col',  'compactability_smc_pct')
cosp_col = bb_cfg.get('cosp_col', 'cosp_percentage_pct')
abs_warn = float(bb_cfg.get('abs_warn_thr', 2.0))
abs_crit = float(bb_cfg.get('abs_crit_thr', 3.0))
print(f"  smc_col={smc_col}  cosp_col={cosp_col}")
print(f"  thresholds: warn>{abs_warn}  crit>{abs_crit}")
print()

with engine.connect() as c:
    df_bb = pd.read_sql(sa_text(f"""
        SELECT a.pkey, a.component_id, DATE(a.date) as date, a.shift,
               a.`{smc_col}` as smc, a.`{cosp_col}` as cosp,
               g.name as group_name
        FROM additive a
        LEFT JOIN foundry_line_group_component gc
               ON gc.component_id = a.component_id AND gc.deleted=0
        LEFT JOIN foundry_line_group g
               ON g.pkey = gc.foundry_line_group_pkey
              AND g.deleted=0 AND g.foundry_line_pkey = :fl
        WHERE a.foundry_line_id = :fl AND a.deleted = 0
          AND DATE(a.date) = :dt
          AND a.`{smc_col}` IS NOT NULL AND a.`{cosp_col}` IS NOT NULL
        ORDER BY a.pkey
    """), c, params={"fl": LINE_ID, "dt": LATEST_DATE})

print(f"  Batches fetched for {LATEST_DATE}: {len(df_bb)}")
if not df_bb.empty:
    df_bb['diff'] = (df_bb['smc'] - df_bb['cosp']).abs()
    ok_count   = (df_bb['diff'] <= 1.0).sum()
    warn_count = ((df_bb['diff'] > 1.0) & (df_bb['diff'] <= abs_crit)).sum()
    crit_count = (df_bb['diff'] > abs_crit).sum()
    print(f"  Results: OK={ok_count}  WARNING={warn_count}  CRITICAL={crit_count}  (total={len(df_bb)})")

    bad = df_bb[df_bb['diff'] > abs_warn].sort_values('diff', ascending=False)
    if not bad.empty:
        print(f"\n  Worst bad batches (diff > {abs_warn}):")
        print(f"  {'Component':<16}  {'Group':<12}  {'Shift':<5}  {'SMC':>7}  {'COSP':>7}  {'Diff':>7}  Status")
        for _, r in bad.head(10).iterrows():
            sev = 'CRITICAL' if r['diff'] > abs_crit else 'WARNING'
            print(f"  {str(r['component_id']):<16}  {str(r.get('group_name','--')):<12}  {str(r['shift']):<5}  {r['smc']:>7.2f}  {r['cosp']:>7.2f}  {r['diff']:>7.2f}  {sev}")
    else:
        print("  All batches within threshold.")
else:
    print("  No batches found for this date.")

# ══════════════════════════════════════════════════════════════════
# 2. PRESCRIPTION
# ══════════════════════════════════════════════════════════════════
print()
print(SEP)
print("2. PRESCRIPTION")
print(SEP)

pw_cfg      = cfg.get('prescription_watchdog', {})
monitored   = pw_cfg.get('monitored_params', [])
tolerance   = float(pw_cfg.get('tolerance_pct', 3.0))
print(f"  Monitored params: {monitored}")
print(f"  Tolerance: {tolerance}%")
print()

if monitored:
    actual_cols_map = {p: f"{p}_actual" for p in monitored}
    with engine.connect() as c:
        # Check which actual cols exist
        desc = c.execute(sa_text("DESCRIBE `additive`")).fetchall()
        db_cols = {r[0] for r in desc}
        sel_cols = [f"a.`{v}`" for v in actual_cols_map.values() if v in db_cols]
        if sel_cols:
            df_presc = pd.read_sql(sa_text(f"""
                SELECT DATE(a.date) as date, a.shift, a.component_id,
                       {', '.join(sel_cols)}
                FROM additive a
                WHERE a.foundry_line_id = :fl AND a.deleted = 0
                  AND DATE(a.date) = :dt
                ORDER BY a.pkey
            """), c, params={"fl": LINE_ID, "dt": LATEST_DATE})

            print(f"  Rows: {len(df_presc)}")
            print(f"  {'Param':<20}  {'Non-null':>8}  {'Mean':>10}  {'Min':>8}  {'Max':>8}")
            for p in monitored:
                col = f"{p}_actual"
                if col in df_presc.columns:
                    s = df_presc[col].dropna()
                    if len(s):
                        print(f"  {p:<20}  {len(s):>8}  {s.mean():>10.3f}  {s.min():>8.3f}  {s.max():>8.3f}")
                    else:
                        print(f"  {p:<20}  {'NO DATA':>8}")
        else:
            print("  No matching actual columns found in additive table.")
else:
    print("  prescription_watchdog.monitored_params not configured.")

# ══════════════════════════════════════════════════════════════════
# 3. SIEVE
# ══════════════════════════════════════════════════════════════════
print()
print(SEP)
print("3. SIEVE")
print(SEP)

sv_cfg   = cfg.get('sieve_watchdog', {})
ok_thr   = float(sv_cfg.get('ok_thr',      1.0))
warn_thr = float(sv_cfg.get('warn_thr',    3.0))
crit_thr = float(sv_cfg.get('critical_thr',5.0))
band_thr = sv_cfg.get('band_thresholds', {})
print(f"  Thresholds: ok<{ok_thr}%  warn<{warn_thr}%  crit>{crit_thr}%")
print(f"  Per-band overrides: {band_thr}")
print()

with engine.connect() as c:
    df_sv = pd.read_sql(sa_text("""
        SELECT b.sieve_id as pkey, DATE(b.date) as date, b.shift,
               b.band_type, b.value as percentage, b.sand_type
        FROM sieve_band_data b
        WHERE b.deleted = 0 AND b.sand_type = 1
          AND b.foundry_line_id = :fl
        ORDER BY b.sieve_id DESC
        LIMIT 80
    """), c, params={"fl": LINE_ID})

if df_sv.empty:
    print("  No sieve data found.")
else:
    pkeys = df_sv['pkey'].unique()
    latest_pkey = pkeys[0]
    prev_pkey   = pkeys[1] if len(pkeys) > 1 else None
    curr_bands  = df_sv[df_sv['pkey'] == latest_pkey].set_index('band_type')['percentage']
    latest_date = df_sv[df_sv['pkey'] == latest_pkey]['date'].iloc[0]

    print(f"  Latest sieve: pkey={latest_pkey}  date={latest_date}")
    print(f"  Bands: {dict(curr_bands.round(2))}")

    if prev_pkey is not None:
        prev_bands = df_sv[df_sv['pkey'] == prev_pkey].set_index('band_type')['percentage']
        prev_date  = df_sv[df_sv['pkey'] == prev_pkey]['date'].iloc[0]
        print(f"  Previous sieve: pkey={prev_pkey}  date={prev_date}")
        print()
        print(f"  {'Band':<10}  {'Prev':>8}  {'Curr':>8}  {'Delta':>8}  {'Chg%':>7}  Status")
        any_alert = False
        for band in sorted(set(curr_bands.index) & set(prev_bands.index)):
            prev_v = float(prev_bands[band])
            curr_v = float(curr_bands[band])
            if prev_v == 0:
                continue
            chg = abs((curr_v - prev_v) / prev_v * 100)
            sev = 'OK' if chg <= ok_thr else ('WARNING' if chg <= warn_thr else 'CRITICAL')
            if sev != 'OK':
                any_alert = True
            flag = ' <--' if sev != 'OK' else ''
            print(f"  {str(band):<10}  {prev_v:>8.2f}  {curr_v:>8.2f}  {(curr_v-prev_v):>+8.2f}  {chg:>6.1f}%  {sev}{flag}")
        if not any_alert:
            print("  All bands within threshold.")
    else:
        print("  Only one sieve entry — cannot compare.")

# ══════════════════════════════════════════════════════════════════
# 4. SI — Sand Index
# ══════════════════════════════════════════════════════════════════
print()
print(SEP)
print("4. SI — Sand Index")
print(SEP)

si_params = cfg.get('si_params', {}).get('params', [])
optimal   = cfg.get('optimal_values', {})
print(f"  Monitored params: {si_params[:8]}{'...' if len(si_params)>8 else ''}")
print()

from watchdog.pipeline.data_fetcher import fetch_prepared_sand

cfg_si = dict(cfg)
cfg_si['parameters'] = dict(cfg.get('parameters', {}))
cfg_si['parameters']['prepared_sand'] = si_params

start_dt = date.fromisoformat(LATEST_DATE) - timedelta(days=7)
end_dt   = date.fromisoformat(LATEST_DATE)
print(f"  Fetching SI data: {start_dt} to {end_dt} ...")

df_si = fetch_prepared_sand(cfg_si, start_date=start_dt, end_date=end_dt)
print(f"  Rows fetched: {len(df_si)}")

if not df_si.empty and si_params:
    print()
    print(f"  {'Parameter':<30}  {'Non-null':>8}  {'Mean':>10}  {'Std':>10}  {'Min':>8}  {'Max':>8}")
    for p in si_params:
        if p in df_si.columns:
            col = df_si[p].dropna()
            if len(col) > 0:
                print(f"  {p:<30}  {len(col):>8}  {col.mean():>10.3f}  {col.std():>10.3f}  {col.min():>8.3f}  {col.max():>8.3f}")
            else:
                print(f"  {p:<30}  {'NO DATA':>8}")

    if optimal:
        print()
        print(f"  Deviation from optimal ({len(optimal)} params configured):")
        print(f"  {'Parameter':<30}  {'Optimal':>8}  {'Actual':>8}  {'Dev%':>8}  Status")
        for p in si_params:
            if p in df_si.columns and p in optimal:
                col = df_si[p].dropna()
                if len(col) > 0:
                    actual  = col.mean()
                    opt_val = float(optimal[p])
                    if opt_val:
                        dev = (actual - opt_val) / opt_val * 100
                        sev = 'OK' if abs(dev) < 5 else ('WATCH' if abs(dev) < 10 else 'ALERT')
                        flag = ' <--' if sev != 'OK' else ''
                        print(f"  {p:<30}  {opt_val:>8.3f}  {actual:>8.3f}  {dev:>+7.1f}%  {sev}{flag}")
    else:
        print("\n  No optimal_values configured.")
elif df_si.empty:
    print("  No SI data for this date range.")

print()
print(SEP)
print(f"Test complete — {LABEL}")
print(SEP)
