"""Value-family factors (Week 3 Day 17, spec §8.1 first input).

Week 3 ships ``EarningsYield`` only. Week 4 will add ``BookYield``,
``SalesYield``, ``CashFlowYield``, and the four-input ``ValueComposite``
on top of the same plumbing.

PIT discipline (spec §4.1): the factor at date t reads only fundamentals
whose ``filing_date < t`` via :mod:`aegis.data.fundamentals`. The
σ-algebra truncation-stability test pins this.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from aegis.data.fundamentals import (
    latest_filing_lag_days,
    oldest_ttm_component_lag_days,
    ttm_with_status,
)
from aegis.features.base import Factor, FactorContext
from aegis.features.operators import winsorize_cross_section, zscore_cross_section
from aegis.utils.hashing import sha256_dataframe

_REQUIRED_PANEL_COLUMNS: frozenset[str] = frozenset({"date", "ticker", "mcap"})

# Output column order — matches FactorObservation (10 cols as of Day 17).
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
    "invalid_reason",
)

# `ttm_with_status` returns one of these as its second element when the TTM
# cannot be computed. ``EarningsYield`` maps each to an `invalid_reason`.
# Conservative collapse: ambiguous_cik (Day 16 hardening) and
# missing_field_value (Day 16) both fold into "missing_fundamentals" because
# the factor has no panel-side CIK to disambiguate (Day 18 lands that) and
# downstream consumers only need to know the factor is unusable.
_TTM_STATUS_TO_INVALID_REASON: dict[str, str] = {
    "missing_fundamentals": "missing_fundamentals",
    "missing_field_value": "missing_fundamentals",
    "ambiguous_cik": "missing_fundamentals",
    "insufficient_quarters": "insufficient_quarters",
}


def _positive_finite_float(value: Any) -> float | None:
    if pd.isna(value):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


class EarningsYield(Factor):
    """Trailing-twelve-months net income over current market cap (E/P)."""

    name: ClassVar[str] = "earnings_yield"
    formula: ClassVar[str] = "ttm_net_income / mcap (E/P)"
    # Need ~365 calendar days to accumulate 4 quarterly reports.
    lookback_days: ClassVar[int] = 365

    def compute(
        self,
        panel: pd.DataFrame,
        *,
        context: FactorContext | None = None,
    ) -> pd.DataFrame:
        if context is None or context.fundamentals is None:
            raise ValueError(f"{self.name}.compute requires context.fundamentals; got None")
        missing = _REQUIRED_PANEL_COLUMNS - set(panel.columns)
        if missing:
            raise ValueError(f"panel missing required columns for {self.name}: {sorted(missing)}")
        fundamentals = context.fundamentals

        # Pre-slice fundamentals by ticker once — avoids per-row full-frame
        # filters (would be ~229k full-frame scans on the live full-slice).
        funds_by_ticker: dict[str, pd.DataFrame] = {
            str(t): g for t, g in fundamentals.groupby("ticker", sort=False)
        }

        # Sort (ticker, date) so output is deterministic.
        df = panel.sort_values(["ticker", "date"]).reset_index(drop=True)

        raw_values: list[float] = []
        invalid_reasons: list[str | None] = []

        for row in df.itertuples(index=False):
            invalid_reason, raw = self._compute_row(row, funds_by_ticker)
            raw_values.append(raw)
            invalid_reasons.append(invalid_reason)

        working = pd.DataFrame(
            {
                "date": df["date"].to_numpy(),
                "ticker": df["ticker"].to_numpy(),
                "raw_value": np.array(raw_values, dtype="float64"),
                "invalid_reason": invalid_reasons,
            }
        )

        # Cross-sectional 1%/99% winsorize then z-score (per date).
        working["winsorized_value"] = winsorize_cross_section(working, "raw_value")
        working["zscore_value"] = zscore_cross_section(working, "winsorized_value")

        values = working[["raw_value", "winsorized_value", "zscore_value"]]
        finite = np.isfinite(values.to_numpy()).all(axis=1)
        # invalid_reason wins: a row marked invalid stays invalid even if the
        # winsorize/zscore happened to produce finite values from a NaN raw.
        explicit_invalid = working["invalid_reason"].notna().to_numpy()
        valid = finite & ~explicit_invalid
        working["valid_flag"] = valid

        # If the cross-sectional ops introduced new NaNs (rare: e.g. only one
        # eligible ticker on a date so std=0), backfill invalid_reason.
        new_invalid_mask = (~finite) & ~explicit_invalid
        if new_invalid_mask.any():
            working.loc[new_invalid_mask, "invalid_reason"] = "raw_factor_nan"

        if "eligible_flag" in df.columns:
            eligible = df["eligible_flag"].fillna(False).astype(bool).to_numpy()
        else:
            eligible = np.ones(len(df), dtype=bool)
        working["tradable_flag"] = valid & eligible

        # feature_snapshot_id: deterministic over the rendered factor values.
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
                    "invalid_reason",
                ]
            ]
        )
        working["feature_snapshot_id"] = snapshot
        working["factor_name"] = self.name

        return working.loc[:, list(_OUTPUT_COLUMNS)].reset_index(drop=True)

    def _compute_row(
        self,
        row: Any,
        funds_by_ticker: dict[str, pd.DataFrame],
    ) -> tuple[str | None, float]:
        """Return (invalid_reason, raw_value) for one panel row."""
        ticker = row.ticker
        funds_sub = funds_by_ticker.get(ticker)
        if funds_sub is None:
            return "missing_fundamentals", float("nan")

        ttm_ni, status = ttm_with_status(ticker, row.date, funds_sub, "net_income")
        if status is not None:
            mapped = _TTM_STATUS_TO_INVALID_REASON.get(status, "missing_fundamentals")
            return mapped, float("nan")

        mcap_value = _positive_finite_float(row.mcap)
        if mcap_value is None:
            return "invalid_denominator", float("nan")

        assert ttm_ni is not None  # status was None ⟹ ttm_ni is non-None
        raw = ttm_ni / mcap_value
        if not math.isfinite(raw):
            return "raw_factor_nan", float("nan")
        return None, raw

    def diagnostics(
        self,
        factor_out: pd.DataFrame,
        *,
        context: FactorContext | None = None,
    ) -> dict[str, Any]:
        diag: dict[str, Any] = {
            "invalid_reason_counts": (
                factor_out["invalid_reason"].fillna("__valid__").value_counts().to_dict()
            ),
        }
        if context is None or context.fundamentals is None:
            return diag

        fundamentals = context.fundamentals
        if factor_out.empty:
            return diag
        funds_by_ticker: dict[str, pd.DataFrame] = {
            str(t): g for t, g in fundamentals.groupby("ticker", sort=False)
        }

        latest_lags: list[int] = []
        ttm_lags: list[int] = []
        for d, t in zip(factor_out["date"], factor_out["ticker"], strict=False):
            funds_sub = funds_by_ticker.get(str(t))
            if funds_sub is None:
                continue
            ll = latest_filing_lag_days(t, d, funds_sub)
            tl = oldest_ttm_component_lag_days(t, d, funds_sub)
            if ll is not None:
                latest_lags.append(ll)
            if tl is not None:
                ttm_lags.append(tl)

        if latest_lags:
            diag["latest_filing_lag_days"] = {
                "median": int(np.median(latest_lags)),
                "p90": int(np.percentile(latest_lags, 90)),
                "max": int(max(latest_lags)),
                "n": len(latest_lags),
            }
        if ttm_lags:
            diag["oldest_ttm_component_lag_days"] = {
                "median": int(np.median(ttm_lags)),
                "p90": int(np.percentile(ttm_lags, 90)),
                "max": int(max(ttm_lags)),
                "n": len(ttm_lags),
            }
        return diag


__all__ = ["EarningsYield"]
