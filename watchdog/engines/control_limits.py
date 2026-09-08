"""
engines/control_limits.py
--------------------------
LCL/UCL Deviation Engine.

Limit source priority: DB (fetch_control_limits) -> config["control_limits"] fallback.

Status values: "OK" | "Deviated LOW ?" | "Deviated HIGH ?" | "No Limit Set" | ""
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_PREFIXES = ("ps_", "con_", "add_", "pse_")


def compute_deviation_status(
    df:         pd.DataFrame,
    param_cols: list[str],
    config:     dict,
    db_limits:  Optional[dict] = None,
) -> pd.DataFrame:
    """Check each parameter against LCL/UCL. Returns DataFrame with dev_status/dev_value/dev_delta columns."""
    config_limits = config.get("control_limits", {})
    result = {}

    for col in param_cols:
        if col not in df.columns:
            continue

        base_col = _strip_prefix(col)

        # Resolve limits: DB first, config fallback
        lcl, ucl = _resolve_limits(base_col, db_limits, config_limits)

        values   = pd.to_numeric(df[col], errors="coerce")
        statuses = []
        deltas   = []

        for v in values:
            if pd.isna(v):
                statuses.append("")
                deltas.append(np.nan)
            elif lcl is None and ucl is None:
                statuses.append("No Limit Set")
                deltas.append(np.nan)
            elif lcl is not None and v < lcl:
                delta = round(v - lcl, 4)
                statuses.append(f"Deviated LOW {delta:+g}  (LCL={lcl})")
                deltas.append(delta)
            elif ucl is not None and v > ucl:
                delta = round(v - ucl, 4)
                statuses.append(f"Deviated HIGH {delta:+g}  (UCL={ucl})")
                deltas.append(delta)
            else:
                statuses.append("OK")
                deltas.append(0.0)

        result[f"dev_status_{col}"] = statuses
        result[f"dev_value_{col}"]  = values.values
        result[f"dev_delta_{col}"]  = deltas

    return pd.DataFrame(result, index=df.index)


def is_deviated(status: str) -> bool:
    """Return True if a deviation status string indicates an out-of-limit condition."""
    if not isinstance(status, str):
        return False
    return status.startswith("Deviated")


def get_deviated_params(dev_df: pd.DataFrame, row_idx: int) -> list[tuple[str, str]]:
    """
    Return [(param_name, status), ...] for all parameters that are
    deviating at the given row index.
    """
    out = []
    for col in dev_df.columns:
        if not col.startswith("dev_status_"):
            continue
        status = dev_df.at[row_idx, col]
        if is_deviated(status):
            param = col[len("dev_status_"):]
            out.append((param, status))
    return out


def log_active_limits(db_limits: Optional[dict], config: dict) -> None:
    """Print/log the effective LCL/UCL for every configured parameter."""
    config_limits = config.get("control_limits", {})
    display       = config.get("display_names", {})
    all_keys      = set(config_limits.keys())
    if db_limits:
        all_keys |= set(db_limits.keys())

    logger.info("Active control limits (DB overrides config where available):")
    for k in sorted(all_keys):
        lcl, ucl = _resolve_limits(k, db_limits, config_limits)
        source   = "DB"   if (db_limits and k in db_limits) else "config"
        label    = display.get(k, k)
        logger.info("  %-35s  LCL=%-10s  UCL=%-10s  [%s]",
                    label, lcl, ucl, source)


def _resolve_limits(
    base_col:      str,
    db_limits:     Optional[dict],
    config_limits: dict,
) -> tuple[Optional[float], Optional[float]]:
    if db_limits and base_col in db_limits:
        lim = db_limits[base_col]
        return lim.get("lcl"), lim.get("ucl")
    lim = config_limits.get(base_col, {})
    if lim:
        return lim.get("lcl"), lim.get("ucl")
    return None, None


def _strip_prefix(col: str) -> str:
    for p in _PREFIXES:
        if col.startswith(p):
            return col[len(p):]
    return col
