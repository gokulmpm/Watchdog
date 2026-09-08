"""
engines/pct_change_engine.py
-----------------------------
Percentage-of-Change Engine.
Computes pct_change = (current ? previous) / |previous| x 100 per period.
Sets a flag when |pct_change| > pct_change_warning (config default 5.0%).
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_pct_change(
    df: pd.DataFrame,
    param_cols: list[str],
    config: dict,
) -> pd.DataFrame:
    """Compute % change vs previous period. Returns DataFrame with pct_{col} and pct_flag_{col} columns."""
    warn_thr = float(config.get("pct_change_warning", 5.0))
    result   = {}

    for col in param_cols:
        if col not in df.columns:
            continue

        vals    = pd.to_numeric(df[col], errors="coerce").values.astype(float)
        prev    = np.roll(vals, 1)
        prev[0] = np.nan

        with np.errstate(invalid="ignore", divide="ignore"):
            pct = np.where(
                (np.abs(prev) < 1e-10) | np.isnan(prev) | np.isnan(vals),
                np.nan,
                (vals - prev) / np.abs(prev) * 100.0
            )

        flags = np.where(
            np.isnan(pct),
            0,
            (np.abs(pct) > warn_thr).astype(int)
        )

        result[f"pct_{col}"]      = np.round(pct,  2)
        result[f"pct_flag_{col}"] = flags.astype(int)

    return pd.DataFrame(result, index=df.index)


def get_high_change_params(pct_df: pd.DataFrame, row_idx: int) -> list[tuple[str, float]]:
    """
    Return [(param_name, pct_change), ...] for all parameters with a
    high % change flag at the given row index.
    """
    out = []
    for col in pct_df.columns:
        if not col.startswith("pct_flag_"):
            continue
        flag = pct_df.at[row_idx, col]
        if flag == 1:
            param    = col[len("pct_flag_"):]
            pct_col  = f"pct_{param}"
            pct_val  = pct_df.at[row_idx, pct_col] if pct_col in pct_df.columns else np.nan
            out.append((param, float(pct_val)))
    return out
