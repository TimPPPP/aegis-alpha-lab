"""Day 16 acceptance tests for src/aegis/data/fundamentals.py.

Drives the 8 helpers from the engineered ``fundamentals_fixture`` (see
``tests/conftest.py``). Polygon-free by construction. Each test pins one
behavior so a regression points at exactly one defect.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from aegis.data.fundamentals import (
    EXPECTED_COLUMNS,
    coverage_window,
    fundamentals_at,
    latest_filing_lag_days,
    load_fundamentals,
    oldest_ttm_component_lag_days,
    ttm_at,
    ttm_with_status,
)


# --- 1. PIT discipline: latest filed before as_of ----------------------------
def test_fundamentals_at_returns_latest_filed_before_date(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    # AAPL_X Q1 filed 2024-01-25, Q2 filed 2024-04-25.
    # At as_of=2024-07-01: Q1 and Q2 are PIT-eligible. Latest filing -> Q2.
    row = fundamentals_at("AAPL_X", date(2024, 7, 1), fundamentals_fixture)
    assert row is not None
    assert row["fiscal_quarter"] == 2
    assert row["period_end_date"] == date(2024, 3, 30)


# --- 2. period_end_date never used for visibility (PIT discipline) -----------
def test_fundamentals_at_ignores_period_end_date_for_visibility(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    # AAPL_X Q3 has period_end_date=2024-06-29 (in the past at as_of=2024-07-01)
    # but filing_date=2024-07-25 (still in the future at as_of=2024-07-01).
    # Q3 must NOT be visible — Q2 is the latest visible row.
    row = fundamentals_at("AAPL_X", date(2024, 7, 1), fundamentals_fixture)
    assert row is not None
    assert row["fiscal_quarter"] == 2  # not 3, even though Q3's period ended


# --- 3. Strict filing_date < as_of ------------------------------------------
def test_fundamentals_lookup_uses_strict_filing_date_before_t(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    # AAPL_X Q1 filed exactly on 2024-01-25.
    # Spec §4.1: a report filed at end-of-day t becomes visible at t+1, not t.
    # So at as_of=2024-01-25, Q1 must be invisible; at 2024-01-26, visible.
    assert fundamentals_at("AAPL_X", date(2024, 1, 25), fundamentals_fixture) is None
    row = fundamentals_at("AAPL_X", date(2024, 1, 26), fundamentals_fixture)
    assert row is not None
    assert row["fiscal_quarter"] == 1


# --- 4. σ-algebra measurability: truncation-stability ------------------------
def test_fundamentals_at_is_filtration_measurable(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    # Truncate the frame to filing_date < t. The helper's output at t must
    # be identical whether it sees the truncated or full frame — proves
    # F_t-measurability (no future data leakage).
    t = date(2024, 7, 1)
    truncated = fundamentals_fixture[fundamentals_fixture["filing_date"] < t]
    full_row = fundamentals_at("AAPL_X", t, fundamentals_fixture)
    sliced_row = fundamentals_at("AAPL_X", t, truncated)
    assert full_row is not None and sliced_row is not None
    for col in EXPECTED_COLUMNS:
        a, b = full_row[col], sliced_row[col]
        if isinstance(a, tuple) or isinstance(b, tuple):
            assert tuple(a) == tuple(b)
        elif pd.isna(a) and pd.isna(b):
            continue
        else:
            assert a == b


# --- 5. None for unknown / pre-history --------------------------------------
def test_fundamentals_at_returns_none_for_unknown_ticker_or_no_pit_rows(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    df = fundamentals_fixture
    # MISSING_X is absent from the fixture entirely.
    assert fundamentals_at("MISSING_X", date(2025, 1, 1), df) is None
    # Unknown ticker: no error, just None.
    assert fundamentals_at("ZZZZ", date(2025, 1, 1), df) is None
    # AAPL_X exists but at as_of=2024-01-01, no row has filing_date < 2024-01-01.
    assert fundamentals_at("AAPL_X", date(2024, 1, 1), df) is None


# --- 6. ttm_at sums the latest 4 quarterlies --------------------------------
def test_ttm_at_sums_exactly_four_quarterly_reports(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    df = fundamentals_fixture
    # AAPL_X revenues = [100, 200, 300, 400, 500, 600, 700, 800] across Q1..Q8.
    # After Q4 filed (2024-10-25), at as_of=2024-12-01: latest 4 quarters
    # by period_end_date are Q1..Q4 -> 100+200+300+400 = 1000.
    assert ttm_at("AAPL_X", date(2024, 12, 1), df, "revenues") == 1000.0
    # After Q5 filed (2025-01-25), at as_of=2025-02-01: Q2..Q5 -> 200+300+400+500 = 1400.
    assert ttm_at("AAPL_X", date(2025, 2, 1), df, "revenues") == 1400.0


# --- 7. Restatement de-dupe by fiscal period --------------------------------
def test_ttm_dedupes_restatement_by_period_then_latest_filing(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    df = fundamentals_fixture
    # MSFT_X FY24-Q3 has two filings:
    #   original    revenues=300, filing_date=2024-04-25
    #   restatement revenues=350, filing_date=2024-05-25 (later)
    # At as_of=2024-08-01, latest 4 PIT-eligible quarters by period_end are
    # FY24-Q1 (100) + Q2 (200) + Q3-restatement (350) + Q4 (400) = 1050.
    # If de-dupe were broken (kept original), result would be 1000.
    # If de-dupe were broken (kept BOTH), result would explode in either
    # direction (uniqueness violation in tail(4)).
    assert ttm_at("MSFT_X", date(2024, 8, 1), df, "revenues") == 1050.0


# --- 8. ttm_at returns None when fewer than 4 quarters ----------------------
def test_ttm_at_returns_none_when_fewer_than_four_quarters_pit_available(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    # SHORT_X has 2 quarterlies; both PIT-eligible at as_of=2025-01-01.
    # 2 < 4 -> ttm_at returns None.
    assert ttm_at("SHORT_X", date(2025, 1, 1), fundamentals_fixture, "revenues") is None


def test_ttm_requires_four_consecutive_fiscal_quarters(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    df = fundamentals_fixture
    # GAP_X has four PIT-eligible quarterlies, but FY24-Q3 is missing:
    # FY24-Q1, FY24-Q2, FY24-Q4, FY25-Q1. This is not a valid TTM window.
    as_of = date(2025, 3, 1)
    assert ttm_at("GAP_X", as_of, df, "revenues") is None
    assert ttm_with_status("GAP_X", as_of, df, "revenues") == (
        None,
        "insufficient_quarters",
    )
    assert oldest_ttm_component_lag_days("GAP_X", as_of, df) is None


# --- 9. Fiscal-year boundary: TTM crosses fiscal years correctly ------------
def test_ttm_at_handles_fiscal_year_boundary(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    df = fundamentals_fixture
    # MAR_FY_X has March FY-end. At as_of=2024-12-01 the PIT-eligible
    # quarterlies are FY24-Q1..Q4 + FY25-Q1..Q2 (6 total). Latest 4 by
    # period_end_date are FY24-Q3 (300) + FY24-Q4 (400) + FY25-Q1 (500) +
    # FY25-Q2 (600) = 1800. Crosses fiscal years (FY24 -> FY25).
    assert ttm_at("MAR_FY_X", date(2024, 12, 1), df, "revenues") == 1800.0


# --- 10. ttm_with_status: missing vs insufficient ---------------------------
def test_ttm_with_status_distinguishes_missing_from_insufficient(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    df = fundamentals_fixture
    # MISSING_X has no rows at all.
    assert ttm_with_status("MISSING_X", date(2025, 1, 1), df, "revenues") == (
        None,
        "missing_fundamentals",
    )
    # SHORT_X has 2 PIT-eligible quarterlies.
    assert ttm_with_status("SHORT_X", date(2025, 1, 1), df, "revenues") == (
        None,
        "insufficient_quarters",
    )
    # AAPL_X: happy path.
    val, status = ttm_with_status("AAPL_X", date(2024, 12, 1), df, "revenues")
    assert val == 1000.0
    assert status is None


# --- 11. ttm_with_status: missing_field_value -------------------------------
def test_ttm_with_status_distinguishes_missing_field_value(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    # SPARSE_X has 4 PIT-eligible quarterlies but Q2 has revenues=None.
    val, status = ttm_with_status("SPARSE_X", date(2025, 1, 1), fundamentals_fixture, "revenues")
    assert val is None
    assert status == "missing_field_value"
    # Same SPARSE_X is fine for net_income (no nulls there).
    val, status = ttm_with_status("SPARSE_X", date(2025, 1, 1), fundamentals_fixture, "net_income")
    assert val == 100.0  # 10+20+30+40
    assert status is None


def test_ttm_requires_cik_when_ticker_history_is_ambiguous(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    df = fundamentals_fixture
    as_of = date(2025, 1, 1)
    assert fundamentals_at("REUSE_X", as_of, df) is None
    assert latest_filing_lag_days("REUSE_X", as_of, df) is None
    assert ttm_at("REUSE_X", as_of, df, "revenues") is None
    assert ttm_with_status("REUSE_X", as_of, df, "revenues") == (
        None,
        "ambiguous_cik",
    )

    assert ttm_at("REUSE_X", as_of, df, "revenues", cik=22222) == 1000.0
    assert ttm_at("REUSE_X", as_of, df, "revenues", cik="0000022222") == 1000.0

    row = fundamentals_at("REUSE_X", as_of, df, cik=11111)
    assert row is not None
    assert row["cik"] == 11111
    assert row["revenues"] == 40.0
    assert (
        latest_filing_lag_days("REUSE_X", as_of, df, cik=11111) == (as_of - date(2024, 10, 31)).days
    )


# --- 12. latest_filing_lag_days ---------------------------------------------
def test_latest_filing_lag_days_basic(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    df = fundamentals_fixture
    # AAPL_X Q1 filed 2024-01-25. At as_of=2024-03-25 (60 days later) Q2
    # hasn't filed yet (filing_date=2024-04-25 >= 2024-03-25), so the
    # latest visible filing is still Q1. lag = (2024-03-25 - 2024-01-25) = 60.
    assert latest_filing_lag_days("AAPL_X", date(2024, 3, 25), df) == 60
    # MISSING_X has no rows.
    assert latest_filing_lag_days("MISSING_X", date(2024, 3, 25), df) is None


# --- 13. oldest_ttm_component_lag_days --------------------------------------
def test_oldest_ttm_component_lag_days_returns_age_of_oldest_quarter(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    df = fundamentals_fixture
    # AAPL_X at as_of=2024-12-01: latest 4 quarters' filings are
    # Q1=2024-01-25, Q2=2024-04-25, Q3=2024-07-25, Q4=2024-10-25.
    # Oldest is Q1 (2024-01-25). lag = (2024-12-01 - 2024-01-25).days.
    expected = (date(2024, 12, 1) - date(2024, 1, 25)).days
    assert oldest_ttm_component_lag_days("AAPL_X", date(2024, 12, 1), df) == expected
    # SHORT_X has < 4 quarters -> None (matches ttm_at semantics).
    assert oldest_ttm_component_lag_days("SHORT_X", date(2025, 1, 1), df) is None


# --- 14. load_fundamentals validates column shape ---------------------------
def test_load_fundamentals_validates_column_shape(tmp_path: Path) -> None:
    bad_df = pd.DataFrame({"ticker": ["X"], "cik": [1], "filing_date": [date(2024, 1, 1)]})
    bad_path = tmp_path / "bad.parquet"
    bad_df.to_parquet(bad_path, index=False)
    with pytest.raises(ValueError, match="column shape mismatch"):
        load_fundamentals(bad_path)


# --- 15. EXPECTED_COLUMNS sync between module and scraper -------------------
def test_fundamentals_columns_match_scraper() -> None:
    """Drift guard: src/aegis/data/fundamentals.py and the scraper must
    agree on the column shape, or the live parquet won't load."""
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root / "scripts"))
    import fetch_polygon_fundamentals as ff

    assert EXPECTED_COLUMNS == ff.EXPECTED_COLUMNS


# --- 16. coverage_window basic shape (free with the helpers, useful smoke) --
def test_coverage_window_returns_long_format_per_date_per_ticker(
    fundamentals_fixture: pd.DataFrame,
) -> None:
    cw = coverage_window(date(2024, 1, 1), date(2024, 1, 3), fundamentals_fixture)
    assert tuple(cw.columns) == ("date", "ticker", "has_pit_fundamentals")
    assert len(cw) == 3 * fundamentals_fixture["ticker"].nunique()
    # On 2024-01-01, no AAPL_X filing has happened yet (Q1 filed 2024-01-25).
    aapl_jan1 = cw[(cw["date"] == date(2024, 1, 1)) & (cw["ticker"] == "AAPL_X")]
    assert aapl_jan1["has_pit_fundamentals"].iloc[0] is False or (
        aapl_jan1["has_pit_fundamentals"].iloc[0] == False  # numpy bool fallback # noqa: E712
    )
