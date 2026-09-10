"""
Test bad_batch, sieve, and SI monitors against real GPI data.
Run: python test_monitors.py
"""
import json, sys, logging
from datetime import date, timedelta
import pandas as pd

sys.path.insert(0, '.')
logging.basicConfig(level=logging.WARNING)  # suppress INFO noise

with open('watchdog/config/caspro_sandman_L1.json', encoding='utf-8') as f:
    cfg = json.load(f)
with open('watchdog/config/watchdog_config.json', encoding='utf-8') as f:
    cfg.update({k: v for k, v in json.load(f).items() if k not in cfg})
cfg['foundry_line_id'] = 1

SEP = "=" * 65

# ── shared: find latest data date from additive ───────────────────
import pymysql
conn = pymysql.connect(host='localhost', port=3306, user='root',
                       password='Password@123', db='caspro_sandman')
cur = conn.cursor()
cur.execute("SELECT MAX(DATE(date)) FROM additive WHERE deleted=0 AND foundry_line_id=1")
LATEST_DATE = str(cur.fetchone()[0])
conn.close()
print(f"Latest data date: {LATEST_DATE}")
print()

# ══════════════════════════════════════════════════════════════════
# 1. BAD BATCH
# ══════════════════════════════════════════════════════════════════
print(SEP)
print("1. BAD BATCH")
print(SEP)

from watchdog.bad_batch_monitor import run_check as bb_run_check

bb_cfg   = cfg.get('bad_batch_watchdog', {})
smc_col  = bb_cfg.get('smc_col',  'compactability_smc_pct')
cosp_col = bb_cfg.get('cosp_col', 'cosp_percentage_pct')
mode     = bb_cfg.get('detection_mode', 'db')
print(f"  smc_col  : {smc_col}")
print(f"  cosp_col : {cosp_col}")
print(f"  mode     : {mode}")
print(f"  thresholds: abs_ok={bb_cfg.get('abs_ok_thr')} abs_warn={bb_cfg.get('abs_warn_thr')} abs_crit={bb_cfg.get('abs_crit_thr')}")
print(f"  Testing date: {LATEST_DATE}")
print()

# Manual detection from additive table directly
from watchdog.pipeline.db_connector import get_engine
from sqlalchemy import text as sa_text
import pandas as pd

engine = get_engine(cfg)
with engine.connect() as c:
    df_bb = pd.read_sql(sa_text(f"""
        SELECT a.pkey, a.component_id, DATE(a.date) as date, a.shift,
               a.timestamp as batch_time,
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
    """), c, params={"fl": 1, "dt": LATEST_DATE})

print(f"  Batches fetched for {LATEST_DATE}: {len(df_bb)}")
if not df_bb.empty:
    df_bb['diff'] = (df_bb['smc'] - df_bb['cosp']).abs()
    abs_warn = float(bb_cfg.get('abs_warn_thr', 2.0))
    abs_crit = float(bb_cfg.get('abs_crit_thr', 3.0))

    ok_count   = (df_bb['diff'] <= 1.0).sum()
    warn_count = ((df_bb['diff'] > 1.0) & (df_bb['diff'] <= abs_crit)).sum()
    crit_count = (df_bb['diff'] > abs_crit).sum()
    print(f"  Results  : OK={ok_count}  WARNING={warn_count}  CRITICAL={crit_count}  (total={len(df_bb)})")

    bad = df_bb[df_bb['diff'] > abs_warn].sort_values('diff', ascending=False)
    if not bad.empty:
        print(f"\n  Bad batches (diff > {abs_warn}):")
        print(f"  {'Component':<16}  {'Group':<12}  {'Shift':<5}  {'SMC':>7}  {'COSP':>7}  {'Diff':>7}  Status")
        for _, r in bad.head(10).iterrows():
            sev = 'CRITICAL' if r['diff'] > abs_crit else 'WARNING'
            print(f"  {str(r['component_id']):<16}  {str(r.get('group_name','--')):<12}  {str(r['shift']):<5}  {r['smc']:>7.2f}  {r['cosp']:>7.2f}  {r['diff']:>7.2f}  {sev}")
    else:
        print("  All batches within threshold — no bad batches.")
else:
    print("  No batches found for this date.")

# ══════════════════════════════════════════════════════════════════
# 2. SIEVE
# ══════════════════════════════════════════════════════════════════
print()
print(SEP)
print("2. SIEVE")
print(SEP)

from watchdog.sieve_monitor import run_check as sv_run_check

sv_cfg   = cfg.get('sieve_watchdog', {})
ok_thr   = float(sv_cfg.get('ok_thr',       1.0))
warn_thr = float(sv_cfg.get('warn_thr',      3.0))
crit_thr = float(sv_cfg.get('critical_thr',  5.0))
band_thr = sv_cfg.get('band_thresholds', {})
print(f"  Thresholds: ok<{ok_thr}%  warn<{warn_thr}%  crit>{crit_thr}%")
print(f"  Per-band overrides: {band_thr}")

with engine.connect() as c:
    # Latest sieve entries — sieve_band_data uses 'value' not 'percentage'
    df_sv = pd.read_sql(sa_text("""
        SELECT b.sieve_id as pkey, DATE(b.date) as date, b.shift,
               b.band_type, b.value as percentage, b.sand_type
        FROM sieve_band_data b
        WHERE b.deleted = 0 AND b.sand_type = 1
          AND b.foundry_line_id = :fl
        ORDER BY b.sieve_id DESC
        LIMIT 60
    """), c, params={"fl": 1})

if df_sv.empty:
    print("  No sieve data found.")
else:
    sieve_pkeys = df_sv['pkey'].unique()
    latest_pkey = sieve_pkeys[0]
    prev_pkey   = sieve_pkeys[1] if len(sieve_pkeys) > 1 else None

    curr_bands = df_sv[df_sv['pkey'] == latest_pkey].set_index('band_type')['percentage']
    latest_date = df_sv[df_sv['pkey'] == latest_pkey]['date'].iloc[0]
    print(f"  Latest sieve: pkey={latest_pkey}  date={latest_date}")
    print(f"  Bands: {dict(curr_bands.round(2))}")

    if prev_pkey is not None:
        prev_bands  = df_sv[df_sv['pkey'] == prev_pkey].set_index('band_type')['percentage']
        prev_date   = df_sv[df_sv['pkey'] == prev_pkey]['date'].iloc[0]
        print(f"  Previous sieve: pkey={prev_pkey}  date={prev_date}")
        print()
        print(f"  Band % change (curr vs prev):")
        print(f"  {'Band':<10}  {'Prev':>8}  {'Curr':>8}  {'Delta':>8}  {'Chg%':>7}  Status")
        any_alert = False
        for band in sorted(set(curr_bands.index) & set(prev_bands.index)):
            prev_v = float(prev_bands[band])
            curr_v = float(curr_bands[band])
            if prev_v == 0:
                continue
            delta  = curr_v - prev_v
            chg    = abs(delta / prev_v * 100)
            bt     = band_thr.get(str(band), crit_thr)
            sev    = 'OK' if chg <= ok_thr else ('WARNING' if chg <= warn_thr else 'CRITICAL')
            if sev != 'OK':
                any_alert = True
            flag = ' <--' if sev != 'OK' else ''
            print(f"  {str(band):<10}  {prev_v:>8.2f}  {curr_v:>8.2f}  {delta:>+8.2f}  {chg:>6.1f}%  {sev}{flag}")
        if not any_alert:
            print("  All bands within threshold.")
    else:
        print("  Only one sieve entry — no previous to compare.")

# ══════════════════════════════════════════════════════════════════
# 3. SI — Sand Index
# ══════════════════════════════════════════════════════════════════
print()
print(SEP)
print("3. SI — Sand Index")
print(SEP)

si_params = cfg.get('si_params', {}).get('params', [])
engines   = cfg.get('engines', {})
print(f"  Monitored params  : {si_params[:8]}{'...' if len(si_params)>8 else ''}")
print(f"  Aggregation mode  : {cfg.get('aggregation', {}).get('mode', 'window')}")
print(f"  Report window     : {cfg.get('report_window', 10)} batches")
print()

from watchdog.pipeline.data_fetcher import fetch_prepared_sand

# SI monitor uses si_params.params directly; inject into parameters.prepared_sand for fetch
cfg_si = dict(cfg)
cfg_si["parameters"] = dict(cfg.get("parameters", {}))
cfg_si["parameters"]["prepared_sand"] = si_params  # use the monitored param list

start_dt = date.fromisoformat(LATEST_DATE) - timedelta(days=7)
end_dt   = date.fromisoformat(LATEST_DATE)
print(f"  Fetching SI data: {start_dt} to {end_dt} ...")

df_si = fetch_prepared_sand(cfg_si, start_date=start_dt, end_date=end_dt)
print(f"  Rows fetched: {len(df_si)}")

if not df_si.empty and si_params:
    print(f"  Columns available: {list(df_si.columns[:6])}...")
    print()
    print(f"  {'Parameter':<30}  {'Non-null':>8}  {'Mean':>10}  {'Std':>10}  {'Min':>8}  {'Max':>8}")
    for p in si_params[:12]:
        if p in df_si.columns:
            col = df_si[p].dropna()
            if len(col) > 0:
                print(f"  {p:<30}  {len(col):>8}  {col.mean():>10.3f}  {col.std():>10.3f}  {col.min():>8.3f}  {col.max():>8.3f}")
            else:
                print(f"  {p:<30}  {'NO DATA':>8}")

    # Run z-score check against baseline
    baseline_cfg = cfg.get('baseline', {})
    optimal      = cfg.get('optimal_values', {})
    print()
    print(f"  Baseline: {baseline_cfg.get('start_date')} to {baseline_cfg.get('end_date')}")
    if optimal:
        print(f"  Optimal values configured: {len(optimal)} params")
        print()
        print(f"  Deviation from optimal:")
        print(f"  {'Parameter':<30}  {'Optimal':>8}  {'Actual':>8}  {'Dev%':>8}  Status")
        for p in si_params[:12]:
            if p in df_si.columns and p in optimal:
                col = df_si[p].dropna()
                if len(col) > 0:
                    actual  = col.mean()
                    opt_val = float(optimal[p])
                    if opt_val:
                        dev = (actual - opt_val) / opt_val * 100
                        sev = 'OK' if abs(dev) < 5 else ('WATCH' if abs(dev) < 10 else 'ALERT')
                        print(f"  {p:<30}  {opt_val:>8.3f}  {actual:>8.3f}  {dev:>+7.1f}%  {sev}")
    else:
        print("  No optimal_values configured in config — set them in the dashboard.")
elif df_si.empty:
    print("  No SI data returned for date range.")

print()
print(SEP)
print("Test complete.")
print(SEP)
