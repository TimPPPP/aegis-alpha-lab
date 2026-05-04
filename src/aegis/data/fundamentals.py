"""PIT lookup helpers over the Day 15 fundamentals snapshot (Week 3 Day 16).

Spec §4.1 σ-algebra measurability: every helper here filters on
``filing_date < as_of`` (strict). A report filed exactly on date ``t``
is NOT visible at ``t`` — it becomes visible at ``t+1``. This matches
the panel's t-1 close discipline.

Restatement handling: when a fiscal period is filed twice (10-Q/-K
amendment), both rows live in the frame. ``ttm_at`` and friends
de-duplicate by ``(fiscal_year, fiscal_quarter, period_end_date)`` per
ticker and keep the LATEST PIT-eligible filing per period.

All helpers are pure functions over a ``pd.DataFrame`` whose columns
match :data:`EXPECTED_COLUMNS` (a mirror of the scraper's column shape;
test_fundamentals_columns_match_scraper guards them against drift).
They are scalar (one ticker, one date) for clarity and testability —
vectorization belongs to factor-compute (Day 17 / Day 19).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

# Mirror of scripts.fetch_polygon_fundamentals.EXPECTED_COLUMNS. The two
# stay in sync via test_fundamentals_columns_match_scraper.
EXPECTED_COLUMNS: tuple[str, ...] = (
    "ticker",
    "cik",
    "filing_date",
    "period_end_date",
    "fiscal_year",
    "fiscal_quarter",
    "period_kind",
    "revenues",
    "net_income",
    "eps_basic",
    "eps_diluted",
    "weighted_avg_shares_basic",
    "weighted_avg_shares_diluted",
    "common_equity",
    "total_assets",
    "operating_cash_flow",
    "source_endpoints",
)

_DEDUP_KEY: tuple[str, ...] = ("fiscal_year", "fiscal_quarter", "period_end_date")


def load_fundamentals(path: str | Path) -> pd.DataFrame:
    """Read a fundamentals parquet and validate its column shape.

    Coerces ``filing_date`` and ``period_end_date`` to :class:`datetime.date`
    (parquet round-trip surfaces them as ``datetime64[ns]``). Does NOT
    instantiate :class:`FundamentalsRow` per-row — that's far too slow for
    a 12,800-row frame. Per-row Pydantic validation only happens in tests
    and engineered fixtures.
    """
    df = pd.read_parquet(path)
    if tuple(df.columns) != EXPECTED_COLUMNS:
        missing = sorted(set(EXPECTED_COLUMNS) - set(df.columns))
        extra = sorted(set(df.columns) - set(EXPECTED_COLUMNS))
        raise ValueError(
            f"fundamentals parquet column shape mismatch at {path}: missing={missing} extra={extra}"
        )
    for col in ("filing_date", "period_end_date"):
        df[col] = pd.to_datetime(df[col]).dt.date
    return df


def _ticker_slice(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    return df[df["ticker"] == ticker]


def _pit_quarterly_dedup(sub: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Filter to PIT-eligible quarterly rows for one ticker and de-dupe
    restatements by fiscal period (keeping the latest filing).

    Caller is responsible for restricting ``sub`` to a single ticker.
    """
    sub = sub[(sub["filing_date"] < as_of) & (sub["period_kind"] == "quarterly")]
    if sub.empty:
        return sub
    # Sort ascending by filing_date so drop_duplicates(keep="last") retains the
    # latest filing per fiscal period — this is how restatements collapse to
    # the most-recent filed value.
    sub = sub.sort_values("filing_date")
    return sub.drop_duplicates(subset=list(_DEDUP_KEY), keep="last")


def fundamentals_at(ticker: str, as_of: date, df: pd.DataFrame) -> pd.Series | None:
    """Latest row for ``ticker`` whose ``filing_date < as_of``.

    Spans every ``period_kind`` (quarterly / annual / TTM) — answers the
    question "what's the most recent fundamentals report this ticker filed
    before t?". Returns None if no PIT-eligible row exists for the ticker.
    """
    sub = _ticker_slice(df, ticker)
    if sub.empty:
        return None
    sub = sub[sub["filing_date"] < as_of]
    if sub.empty:
        return None
    return sub.sort_values("filing_date").iloc[-1]


def ttm_at(ticker: str, as_of: date, df: pd.DataFrame, field: str) -> float | None:
    """Sum of ``field`` across the latest 4 PIT-eligible quarterly rows.

    Filters to ``period_kind=='quarterly'`` (annual and TTM rows from
    Polygon are ignored). De-duplicates restatements by fiscal period
    keeping the latest filing. Returns None if fewer than 4 PIT-eligible
    quarterly periods, OR if any of the 4 selected rows has
    ``df[field]`` missing.
    """
    sub = _pit_quarterly_dedup(_ticker_slice(df, ticker), as_of)
    if len(sub) < 4:
        return None
    sub = sub.sort_values("period_end_date").tail(4)
    values = sub[field]
    if values.isna().any():
        return None
    return float(values.sum())


def ttm_with_status(
    ticker: str, as_of: date, df: pd.DataFrame, field: str
) -> tuple[float | None, str | None]:
    """Same compute as :func:`ttm_at` but disambiguates the None case.

    Returns one of:
      * ``(value, None)``                       — valid TTM
      * ``(None, "missing_fundamentals")``      — ticker has zero PIT-eligible
        quarterly rows (either ticker absent or only annual/TTM rows present)
      * ``(None, "insufficient_quarters")``     — 1-3 PIT-eligible quarterly
        periods after restatement de-dupe (fewer than 4)
      * ``(None, "missing_field_value")``       — 4 quarterlies present but at
        least one has ``df[field]`` missing (None / NaN)

    Day 17's ``EarningsYield.compute`` consumes this directly to populate
    ``invalid_reason`` without re-walking the frame.
    """
    sub = _pit_quarterly_dedup(_ticker_slice(df, ticker), as_of)
    n = len(sub)
    if n == 0:
        return None, "missing_fundamentals"
    if n < 4:
        return None, "insufficient_quarters"
    sub = sub.sort_values("period_end_date").tail(4)
    values = sub[field]
    if values.isna().any():
        return None, "missing_field_value"
    return float(values.sum()), None


def coverage_window(start: date, end: date, df: pd.DataFrame) -> pd.DataFrame:
    """Long-format ``(date, ticker, has_pit_fundamentals)`` over [start, end].

    Calendar-day granular (every day from ``start`` to ``end``, inclusive).
    Trading-day filtering is the caller's responsibility (typically by
    joining to the panel's ``date`` column on Day 17/19).
    """
    if start > end:
        raise ValueError(f"start={start} after end={end}")
    dates = pd.date_range(start, end, freq="D").date
    tickers = sorted(df["ticker"].unique())
    rows: list[dict[str, object]] = []
    for d in dates:
        for ticker in tickers:
            rows.append(
                {
                    "date": d,
                    "ticker": ticker,
                    "has_pit_fundamentals": fundamentals_at(ticker, d, df) is not None,
                }
            )
    return pd.DataFrame(rows, columns=["date", "ticker", "has_pit_fundamentals"])


def latest_filing_lag_days(ticker: str, as_of: date, df: pd.DataFrame) -> int | None:
    """``(as_of - filing_date).days`` for the row :func:`fundamentals_at`
    would return. None if no PIT-eligible row exists.

    Used by ``EarningsYield.diagnostics`` for "freshness of latest filing"
    reporting (median / p90 / max across the panel).
    """
    row = fundamentals_at(ticker, as_of, df)
    if row is None:
        return None
    return int((as_of - row["filing_date"]).days)


def oldest_ttm_component_lag_days(ticker: str, as_of: date, df: pd.DataFrame) -> int | None:
    """``(as_of - oldest_filing_date_among_ttm_components).days``.

    None if :func:`ttm_at` would return None (i.e. fewer than 4 PIT-eligible
    quarterlies). Captures TTM staleness vs. just latest-filing staleness —
    catches restated-quarter scenarios where the *latest* filing is fresh
    but a Q in the middle of TTM is months stale.
    """
    sub = _pit_quarterly_dedup(_ticker_slice(df, ticker), as_of)
    if len(sub) < 4:
        return None
    sub = sub.sort_values("period_end_date").tail(4)
    oldest_filing = sub["filing_date"].min()
    return int((as_of - oldest_filing).days)


__all__ = [
    "EXPECTED_COLUMNS",
    "coverage_window",
    "fundamentals_at",
    "latest_filing_lag_days",
    "load_fundamentals",
    "oldest_ttm_component_lag_days",
    "ttm_at",
    "ttm_with_status",
]
