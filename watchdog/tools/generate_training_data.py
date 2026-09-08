"""
generate_training_data.py
--------------------------
Generates fine-tuning training data by:
  1. Fetching historical alerts from watchdog_alerts DB
  2. Calling Groq (free) to produce high-quality root_cause + recommendation
  3. Saving as JSONL ready for Unsloth fine-tuning

Usage:
    python -m watchdog.tools.generate_training_data --output training_data.jsonl --limit 500
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an expert in foundry sand preparation monitoring. You receive alert data
from a Sand Index (SI) monitoring system and produce a concise diagnosis.

Domain knowledge:
- Sand properties: active_clay, compactibility, GCS, GFN/AFS, moisture,
  permeability, LOI, volatile_matter, inert_fines, shear_strength, split_strength.
- Additives: bentonite (raises active clay), coal dust/LCA (raises LOI/volatile matter),
  fresh silica sand (controls GFN), water (affects moisture/compactibility).
- Drift = sustained multi-shift trend. Variance = batch-to-batch instability.
- Deviation = outside LCL/UCL control limits.

Respond ONLY with a JSON object:
{
  "root_cause": "1-2 sentences — what is wrong and why, naming specific parameters and direction",
  "recommendation": "1-2 sentences — what the operator should do right now, specific and actionable"
}"""


def fetch_alerts(config: dict, limit: int) -> list[dict]:
    """Fetch SI alerts with non-STABLE status from watchdog_alerts."""
    from watchdog.pipeline.db_connector import get_engine
    from sqlalchemy import text

    engine = get_engine(config)
    sql = text("""
        SELECT id, alert_level, si_score, root_cause, recommendation, params_json
        FROM   watchdog_alerts
        WHERE  alert_type  = 'SI'
          AND  params_json IS NOT NULL
          AND  params_json != '[]'
          AND  si_score    > 5
        ORDER  BY updated_at DESC
        LIMIT  :n
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"n": limit}).mappings().fetchall()
    logger.info("Fetched %d alerts from DB", len(rows))
    return [dict(r) for r in rows]


def build_input_text(row: dict) -> str:
    """Convert a DB alert row into the model input prompt."""
    params = row.get("params_json") or []
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = []

    lines = [
        f"Alert level: {row.get('alert_level', '')}  |  SI score: {row.get('si_score', 0):.1f}/100",
        "\nNon-stable parameters:",
    ]
    for p in params:
        if p.get("alert_level", "STABLE") == "STABLE":
            continue
        parts = [f"  {p.get('label', p.get('param', ''))}:  {p.get('alert_level', '')}"]
        if p.get("drift_label") and p["drift_label"] != "STABLE":
            parts.append(f"drift={p['drift_label']}")
        if p.get("var_label") and p["var_label"] != "STABLE":
            parts.append(f"var={p['var_label']}")
        if p.get("raw_value") is not None:
            parts.append(f"value={p['raw_value']:.3g}")
        if p.get("pct_change") is not None:
            parts.append(f"Δ={p['pct_change']:+.1f}%")
        if p.get("deviation") and p["deviation"] not in ("OK", ""):
            parts.append(f"({p['deviation']})")
        lines.append("  ".join(parts))

    return "\n".join(lines)


def call_groq(groq_key: str, input_text: str, retries: int = 3) -> dict | None:
    """Call Groq to generate root_cause + recommendation."""
    from groq import Groq, RateLimitError

    client = Groq(api_key=groq_key)

    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=300,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": input_text},
                ],
            )
            text = resp.choices[0].message.content or ""
            # Strip markdown fences
            raw = text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            data = json.loads(raw)
            if "root_cause" in data and "recommendation" in data:
                return data
        except RateLimitError:
            wait = 2 ** attempt
            logger.warning("Rate limited — waiting %ds", wait)
            time.sleep(wait)
        except Exception as exc:
            logger.warning("Groq call failed (attempt %d): %s", attempt + 1, exc)
            time.sleep(1)
    return None


def build_training_example(input_text: str, output: dict) -> dict:
    """Format as Alpaca/Unsloth instruction-tuning example."""
    return {
        "instruction": _SYSTEM_PROMPT,
        "input"      : input_text,
        "output"     : json.dumps(output, ensure_ascii=False),
    }


def _augment_with_synthetic(real_alerts: list[dict], target: int = 300) -> list[dict]:
    """
    When real DB alerts are fewer than target, create synthetic variations
    by randomly perturbing parameter values and alert levels.
    This prevents overfitting on small datasets.
    """
    import random, copy
    if len(real_alerts) >= target:
        return real_alerts

    _PARAMS = [
        ("active_clay",     "Active Clay",      8.45, 0.3, "bentonite"),
        ("compactibility",  "Compactibility",   40.0, 2.0, "water/bentonite"),
        ("moisture",        "Moisture",          3.4, 0.3, "water dosing"),
        ("gcs",             "Green Compression", 2140, 80, "active clay level"),
        ("permeability",    "Permeability",       94, 5,   "moisture/clay ratio"),
        ("loi",             "LOI",               5.36, 0.3, "coal dust/LCA"),
        ("volatile_matter", "Volatile Matter",   2.9, 0.2, "coal dust/LCA"),
        ("gfn_afs",         "GFN / AFS",         65.3, 1.5, "fresh silica sand"),
        ("inert_fines",     "Inert Fines",        2.6, 0.2, "return sand ratio"),
    ]
    _ALERTS  = ["WATCH", "ELEVATED", "ALERT", "CRITICAL"]
    _DRIFTS  = ["STABLE", "SLIGHT DRIFT", "STRONG DRIFT", "SLIGHT TREND", "STRONG TREND"]
    _VARS    = ["STABLE", "WATCH", "ELEVATED", "HIGH VAR"]

    synthetic = []
    needed    = target - len(real_alerts)

    for _ in range(needed):
        # Pick 1-4 random parameters to flag
        n_params = random.randint(1, 4)
        chosen   = random.sample(_PARAMS, n_params)
        params_json = []

        for col, label, target_val, sigma, additive in chosen:
            direction   = random.choice([-1, 1])
            magnitude   = random.uniform(0.5, 2.5)
            actual_val  = round(target_val + direction * magnitude * sigma, 3)
            pct_change  = round((actual_val - target_val) / target_val * 100, 2)
            alert_level = random.choice(_ALERTS)
            drift_label = random.choice(_DRIFTS)
            var_label   = random.choice(_VARS)
            lcl = round(target_val - 2 * sigma, 3)
            ucl = round(target_val + 2 * sigma, 3)
            deviation = ""
            if actual_val < lcl:
                deviation = f"Deviated LOW {round(actual_val - lcl, 3)} (LCL={lcl})"
            elif actual_val > ucl:
                deviation = f"Deviated HIGH +{round(actual_val - ucl, 3)} (UCL={ucl})"

            params_json.append({
                "param"      : f"ps_{col}",
                "label"      : label,
                "alert_level": alert_level,
                "drift_label": drift_label,
                "var_label"  : var_label,
                "raw_value"  : actual_val,
                "pct_change" : pct_change,
                "deviation"  : deviation,
            })

        worst = max(params_json, key=lambda p: _ALERTS.index(p["alert_level"])
                    if p["alert_level"] in _ALERTS else 0)
        overall_level = worst["alert_level"]
        si_score = {"WATCH": random.uniform(25, 48), "ELEVATED": random.uniform(35, 60),
                    "ALERT": random.uniform(55, 75), "CRITICAL": random.uniform(70, 95)}.get(
                    overall_level, 30)

        synthetic.append({
            "id"          : f"synthetic_{_}",
            "alert_level" : overall_level,
            "si_score"    : round(si_score, 1),
            "root_cause"  : "",   # will be generated by Groq
            "recommendation": "",
            "params_json" : json.dumps(params_json),
        })

    combined = real_alerts + synthetic
    random.shuffle(combined)
    logger.info("Added %d synthetic examples (total: %d)", len(synthetic), len(combined))
    return combined


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="watchdog/config/watchdog_config.json")
    parser.add_argument("--output", default="training_data.jsonl")
    parser.add_argument("--limit",  type=int, default=500)
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)

    groq_key = config.get("groq_api_key") or ""
    if not groq_key:
        logger.error("groq_api_key not set in config — cannot generate training data")
        sys.exit(1)

    alerts = fetch_alerts(config, args.limit)
    if not alerts:
        logger.error("No alerts found in DB")
        sys.exit(1)

    logger.info("Found %d real alerts — augmenting with synthetic variations...", len(alerts))
    alerts = _augment_with_synthetic(alerts, target=max(args.limit, 300))
    logger.info("Total after augmentation: %d examples", len(alerts))

    out_path = Path(args.output)
    written = 0

    with out_path.open("w", encoding="utf-8") as f:
        for i, row in enumerate(alerts):
            input_text = build_input_text(row)
            if not input_text.strip():
                continue

            logger.info("[%d/%d] Generating for alert #%s ...", i + 1, len(alerts), row["id"])
            output = call_groq(groq_key, input_text)

            if output:
                example = build_training_example(input_text, output)
                f.write(json.dumps(example, ensure_ascii=False) + "\n")
                written += 1
                logger.info("  root_cause: %s", output["root_cause"][:80])
            else:
                logger.warning("  Skipped alert #%s — no valid response", row["id"])

            # Groq free tier: ~30 req/min — small delay to stay within limit
            time.sleep(2)

    logger.info("Done — %d training examples written to %s", written, out_path)
    print(f"\n✓ Training data ready: {out_path}  ({written} examples)")
    print("Next step: upload to Google Colab and run fine-tuning with Unsloth")


if __name__ == "__main__":
    main()
