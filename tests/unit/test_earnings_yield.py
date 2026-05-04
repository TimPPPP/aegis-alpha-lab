"""Day 17 acceptance tests for src/aegis/features/value.py::EarningsYield.

Polygon-free by construction. Drives EarningsYield from the engineered
``fundamentals_fixture`` × ``value_panel_fixture`` pair in ``tests/conftest.py``.
Each test pins exactly one behavior so a regression points at one defect.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from aegis.config import load_all
from aegis.features.base import (
    FactorContext,
    read_factor_diagnostics,
    write_factor_parquet,
)
from aegis.features.value import EarningsYield


def _ctx(fundamentals: pd.DataFrame) -> FactorContext:
    return FactorContext(fundamentals=fundamentals)


# --- 1. Formula correctness --------------------------------------------------
def test_earnings_yield_formula_correctness(
    value_panel_fixture: pd.DataFrame,
    fundamentals_fixture: pd.DataFrame,
) -> None:
    out = EarningsYield().compute(value_panel_fixture, context=_ctx(fundamentals_fixture))
    aapl = out[(out["ticker"] == "AAPL_X") & (out["date"] == date(2024, 12, 1))].iloc[0]
    # AAPL_X TTM net_income at 2024-12-01 = 10+20+30+40 = 100; mcap = 1000.
    assert aapl["raw_value"] == pytest.approx(0.10, abs=1e-12)
    assert aapl["valid_flag"] is True or aapl["valid_flag"]  # numpy bool
    assert aapl["invalid_reason"] is None or pd.isna(aapl["invalid_reason"])


# --- 2. Two formulas agree (split-adjustment equivalence) -------------------
def test_earnings_yield_two_formulas_agree(
    value_panel_fixture: pd.DataFrame,
    fundamentals_fixture: pd.DataFrame,
) -> None:
    """ttm_ni / mcap must equal ttm_ni / shares_out / adj_close within 1e-12,
    because mcap is shares_out × adj_close in the panel fixture."""
    out = EarningsYield().compute(value_panel_fixture, context=_ctx(fundamentals_fixture))
    aapl_panel = value_panel_fixture[
        (value_panel_fixture["ticker"] == "AAPL_X")
        & (value_panel_fixture["date"] == date(2024, 12, 1))
    ].iloc[0]
    aapl_factor = out[(out["ticker"] == "AAPL_X") & (out["date"] == date(2024, 12, 1))].iloc[0]
    direct = aapl_factor["raw_value"]
    indirect = 100.0 / aapl_panel["shares_out"] / aapl_panel["adj_close"]
    assert direct == pytest.approx(indirect, abs=1e-12)


# --- 3. Per-date z-score has mean 0 across valid tickers --------------------
def test_earnings_yield_per_date_zscore_mean_zero(
    value_panel_fixture: pd.DataFrame,
    fundamentals_fixture: pd.DataFrame,
) -> None:
    out = EarningsYield().compute(value_panel_fixture, context=_ctx(fundamentals_fixture))
    valid_2024_12_01 = out[(out["date"] == date(2024, 12, 1)) & out["valid_flag"]]
    # Three valid tickers on this date: AAPL_X, MSFT_X, MAR_FY_X.
    assert len(valid_2024_12_01) == 3
    assert valid_2024_12_01["zscore_value"].mean() == pytest.approx(0.0, abs=1e-12)


# --- 4. σ-algebra measurability (truncation-stability) ---------------------
def test_earnings_yield_is_filtration_measurable(
    value_panel_fixture: pd.DataFrame,
    fundamentals_fixture: pd.DataFrame,
) -> None:
    """Truncating fundamentals to filing_date < t must not change factor
    output at t. Mirrors the Day 5 momentum filtration test and the Day 16
    fundamentals_at filtration test — proves F_t-measurability."""
    t = date(2024, 12, 1)
    panel = value_panel_fixture[value_panel_fixture["date"] == t]
    full = EarningsYield().compute(panel, context=_ctx(fundamentals_fixture))
    truncated = fundamentals_fixture[fundamentals_fixture["filing_date"] < t]
    sliced = EarningsYield().compute(panel, context=_ctx(truncated))
    # Same shape, same per-row raw and zscore values for every (date, ticker).
    assert full.shape == sliced.shape
    merged = full.merge(sliced, on=["date", "ticker"], suffixes=("_full", "_sliced"))
    for col in ("raw_value", "winsorized_value", "zscore_value"):
        full_col = merged[f"{col}_full"]
        sliced_col = merged[f"{col}_sliced"]
        # Equal where both are finite; both NaN otherwise.
        both_nan = full_col.isna() & sliced_col.isna()
        equal = full_col == sliced_col
        assert (both_nan | equal).all(), f"{col} differs after truncation"


# --- 5. Requires fundamentals context ---------------------------------------
def test_earnings_yield_requires_fundamentals_context(
    value_panel_fixture: pd.DataFrame,
) -> None:
    factor = EarningsYield()
    with pytest.raises(ValueError, match=r"requires context\.fundamentals"):
        factor.compute(value_panel_fixture)
    with pytest.raises(ValueError, match=r"requires context\.fundamentals"):
        factor.compute(value_panel_fixture, context=FactorContext())


# --- 6. Missing fundamentals → invalid_reason ------------------------------
def test_earnings_yield_missing_fundamentals_marks_invalid_reason(
    value_panel_fixture: pd.DataFrame,
    fundamentals_fixture: pd.DataFrame,
) -> None:
    out = EarningsYield().compute(value_panel_fixture, context=_ctx(fundamentals_fixture))
    missing = out[out["ticker"] == "MISSING_X"].iloc[0]
    assert missing["valid_flag"] == False  # noqa: E712 — numpy bool
    assert missing["invalid_reason"] == "missing_fundamentals"


# --- 7. Insufficient quarters → invalid_reason -----------------------------
def test_earnings_yield_insufficient_quarters_marks_invalid_reason(
    value_panel_fixture: pd.DataFrame,
    fundamentals_fixture: pd.DataFrame,
) -> None:
    out = EarningsYield().compute(value_panel_fixture, context=_ctx(fundamentals_fixture))
    short = out[out["ticker"] == "SHORT_X"].iloc[0]
    assert short["invalid_reason"] == "insufficient_quarters"
    assert short["valid_flag"] == False  # noqa: E712


# --- 8. Zero / negative mcap → invalid_reason ------------------------------
def test_earnings_yield_zero_mcap_marks_invalid_reason(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    """Engineer a single AAPL_X row with mcap=0 -> invalid_denominator."""
    panel = pd.DataFrame(
        [
            {
                "date": date(2024, 12, 1),
                "ticker": "AAPL_X",
                "mcap": 0.0,
                "adj_close": 100.0,
                "shares_out": 0.0,
                "eligible_flag": True,
            }
        ]
    )
    out = EarningsYield().compute(panel, context=_ctx(fundamentals_fixture))
    row = out.iloc[0]
    assert row["invalid_reason"] == "invalid_denominator"
    assert row["valid_flag"] == False  # noqa: E712


# --- 9. Ambiguous CIK collapses to missing_fundamentals --------------------
def test_earnings_yield_ambiguous_cik_collapses_to_missing_fundamentals(
    value_panel_fixture: pd.DataFrame,
    fundamentals_fixture: pd.DataFrame,
) -> None:
    """REUSE_X has the same ticker registered to two different CIKs in the
    fundamentals fixture. Without panel-side CIK (Day 17 panel doesn't carry
    one — Day 18 plumbs that), the lookup is ambiguous and EarningsYield
    must conservatively map to invalid_reason='missing_fundamentals'."""
    out = EarningsYield().compute(value_panel_fixture, context=_ctx(fundamentals_fixture))
    reuse = out[out["ticker"] == "REUSE_X"].iloc[0]
    assert reuse["invalid_reason"] == "missing_fundamentals"
    assert reuse["valid_flag"] == False  # noqa: E712


# --- 10. Factor parquet shape now (rows, 10) -------------------------------
def test_factor_observation_invalid_reason_shape(
    value_panel_fixture: pd.DataFrame,
    fundamentals_fixture: pd.DataFrame,
) -> None:
    out = EarningsYield().compute(value_panel_fixture, context=_ctx(fundamentals_fixture))
    assert out.shape[1] == 10
    assert "invalid_reason" in out.columns


# --- 11. valid_flag does NOT encode universe ineligibility -----------------
def test_valid_flag_does_not_encode_universe_ineligible(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    """An eligible_flag=False panel row that produces math-valid numbers
    must have valid_flag=True, tradable_flag=False, invalid_reason=None.
    Universe-ineligibility lives in tradable_flag, never in invalid_reason."""
    panel = pd.DataFrame(
        [
            {
                "date": date(2024, 12, 1),
                "ticker": "AAPL_X",
                "mcap": 1000.0,
                "adj_close": 100.0,
                "shares_out": 10.0,
                "eligible_flag": False,  # universe ineligible
            },
            # Need ≥2 valid tickers on this date so the per-date z-score is finite.
            {
                "date": date(2024, 12, 1),
                "ticker": "MAR_FY_X",
                "mcap": 2000.0,
                "adj_close": 100.0,
                "shares_out": 20.0,
                "eligible_flag": True,
            },
        ]
    )
    out = EarningsYield().compute(panel, context=_ctx(fundamentals_fixture))
    aapl = out[out["ticker"] == "AAPL_X"].iloc[0]
    assert aapl["valid_flag"] == True  # noqa: E712 — math is valid
    assert aapl["tradable_flag"] == False  # noqa: E712 — universe ineligible
    assert aapl["invalid_reason"] is None or pd.isna(aapl["invalid_reason"])


# --- 12. Factor catalog contains both factors ------------------------------
def test_factor_catalog_contains_momentum_and_earnings_yield() -> None:
    cfg = load_all()
    names = {spec.name for spec in cfg.factors.factors}
    assert "mom_12_1" in names
    assert "earnings_yield" in names


# --- 13. Diagnostics round-trip via parquet metadata -----------------------
def test_earnings_yield_diagnostics_in_parquet_metadata(
    value_panel_fixture: pd.DataFrame,
    fundamentals_fixture: pd.DataFrame,
    tmp_path: Path,
) -> None:
    factor = EarningsYield()
    context = _ctx(fundamentals_fixture)
    out = factor.compute(value_panel_fixture, context=context)
    diag = factor.diagnostics(out, context=context)

    parquet_path = tmp_path / "earnings_yield.parquet"
    write_factor_parquet(out, parquet_path, diag)

    round_trip = read_factor_diagnostics(parquet_path)
    # Required keys for EarningsYield diagnostics
    assert "invalid_reason_counts" in round_trip
    counts = round_trip["invalid_reason_counts"]
    # Must include valid AAPL_X/MSFT_X/MAR_FY_X under "__valid__" and the
    # invalid statuses we engineered.
    assert "__valid__" in counts
    assert counts["__valid__"] >= 3  # AAPL_X, MSFT_X, MAR_FY_X
    assert counts.get("missing_fundamentals", 0) >= 2  # MISSING_X, REUSE_X
    assert counts.get("insufficient_quarters", 0) >= 1  # SHORT_X

    # Lag stats present (we have valid PIT-eligible rows).
    assert "latest_filing_lag_days" in round_trip
    assert "oldest_ttm_component_lag_days" in round_trip
    for key in ("latest_filing_lag_days", "oldest_ttm_component_lag_days"):
        for stat in ("median", "p90", "max", "n"):
            assert stat in round_trip[key]
