"""12-1 momentum factor (spec §8.1 representative formula, Week 1 Day 5).

    MOM_{i,t} = log( P_{i,t-21} / P_{i,t-252} )

Equivalently, ``log(P[t-21]) - log(P[t-252])`` — log-difference form. The
factor intentionally skips the most recent 21 trading days ("one month") to
exclude short-term reversal effects per Jegadeesh & Titman (1993).

σ-algebra measurability (spec §4.1): the factor at date t uses only
``adj_close`` at dates ≤ t-21. All operators preserve this by lag-only
construction.

Transform pipeline per spec §8 section intro:
    raw = log(P[t-21]) - log(P[t-252])
    winsorized = per-date 1%/99% percentile clip  (configs/universe.yaml)
    zscore     = per-date mean 0, std 1  (ddof=0)

Rows with insufficient history (< 253 observations for the ticker) return
raw=winsorized=zscore=NaN and ``valid_flag=False`` — see
:class:`aegis.features.base.FactorObservation` for the invariant.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
import pandas as pd

from aegis.features.base import Factor
from aegis.features.operators import winsorize_cross_section, zscore_cross_section
from aegis.utils.hashing import sha256_dataframe

# Required columns on the input panel.
_REQUIRED_COLUMNS: frozenset[str] = frozenset({"date", "ticker", "adj_close"})

# Output column order — matches FactorObservation schema.
_OUTPUT_COLUMNS: tuple[str, ...] = (
    "date",
    "ticker",
    "factor_name",
    "raw_value",
    "winsorized_value",
    "zscore_value",
    "valid_flag",
    "tradable_flag",
    "feature_snapshot_id",
)

# Short-horizon skip (21 trading days ≈ 1 month) + total lookback (252 ≈ 1 year).
_SHORT_SKIP_DAYS: int = 21
_LONG_LOOKBACK_DAYS: int = 252


class Momentum12m1m(Factor):
    """Jegadeesh & Titman 12-1 momentum, log-return form."""

    name: ClassVar[str] = "mom_12_1"
    formula: ClassVar[str] = "log(P[t-21] / P[t-252])"
    lookback_days: ClassVar[int] = _LONG_LOOKBACK_DAYS

    def compute(self, panel: pd.DataFrame) -> pd.DataFrame:
        missing = _REQUIRED_COLUMNS - set(panel.columns)
        if missing:
            raise ValueError(f"panel missing required columns for {self.name}: {sorted(missing)}")

        # Sort (ticker, date) so shifts are deterministic.
        df = panel.sort_values(["ticker", "date"]).reset_index(drop=True)

        # Raw factor: log(P[t-21] / P[t-252]) = log(P[t-21]) - log(P[t-252]).
        log_p = np.log(df["adj_close"])
        p_tm21 = log_p.groupby(df["ticker"], sort=False).shift(_SHORT_SKIP_DAYS)
        p_tm252 = log_p.groupby(df["ticker"], sort=False).shift(_LONG_LOOKBACK_DAYS)
        raw = (p_tm21 - p_tm252).astype("float64")

        # Build working frame for cross-sectional transforms.
        working = pd.DataFrame(
            {
                "date": df["date"].to_numpy(),
                "ticker": df["ticker"].to_numpy(),
                "raw_value": raw.to_numpy(),
            }
        )

        working["winsorized_value"] = winsorize_cross_section(working, "raw_value")
        working["zscore_value"] = zscore_cross_section(working, "winsorized_value")

        # valid_flag: true iff all three values are finite (non-NaN, non-inf).
        values = working[["raw_value", "winsorized_value", "zscore_value"]]
        working["valid_flag"] = np.isfinite(values.to_numpy()).all(axis=1)
        if "eligible_flag" in df.columns:
            eligible = df["eligible_flag"].fillna(False).astype(bool).to_numpy()
        else:
            eligible = np.ones(len(df), dtype=bool)
        working["tradable_flag"] = working["valid_flag"].to_numpy() & eligible

        # feature_snapshot_id: deterministic hash of factor values and signal availability.
        snapshot = sha256_dataframe(
            working[
                [
                    "date",
                    "ticker",
                    "raw_value",
                    "winsorized_value",
                    "zscore_value",
                    "valid_flag",
                    "tradable_flag",
                ]
            ]
        )
        working["feature_snapshot_id"] = snapshot
        working["factor_name"] = self.name

        return working.loc[:, list(_OUTPUT_COLUMNS)].reset_index(drop=True)


__all__ = ["Momentum12m1m"]
