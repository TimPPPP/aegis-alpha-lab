"""Polygon.io smoke test — marked ``@pytest.mark.polygon``, skipped in CI.

Runs locally when ``POLYGON_API_KEY`` is set in the environment (loaded from
``.env`` by conftest). Pulls ~15 rows of real Polygon daily bars for three
well-known large-caps over 5 trading days and asserts the loader's output
contract.

Date window constraint: Polygon's free tier only serves ~2 years of
history, so the window is anchored ~90 days before "today" (2026-04-23) to
stay safely inside it. When the calendar rolls forward and the test
eventually starts failing with NOT_AUTHORIZED, bump ``_SMOKE_WINDOW_END``.

Free-tier rate-limit: the loader sleeps between calls (12.5s default) to
stay under the 5-calls/min limit. Expect ~2 minutes wall-time for this
test on the free tier.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from aegis.data.polygon_loader import OUTPUT_COLUMNS, load_polygon_daily

_SMOKE_TICKERS: list[str] = ["AAPL", "MSFT", "IBM"]

# Recent trading week well inside the free-tier 2-year window (as of 2026-04-23).
_SMOKE_WINDOW_START = date(2026, 1, 5)  # Mon
_SMOKE_WINDOW_END = date(2026, 1, 9)  # Fri  (5 trading days)


@pytest.mark.polygon
@pytest.mark.skipif(
    not os.environ.get("POLYGON_API_KEY"),
    reason="POLYGON_API_KEY not set; skipping live-Polygon smoke test",
)
def test_polygon_smoke_pull_three_stocks_one_week() -> None:
    df = load_polygon_daily(
        tickers=_SMOKE_TICKERS,
        start=_SMOKE_WINDOW_START,
        end=_SMOKE_WINDOW_END,
    )

    # ~5 trading days × 3 stocks = ~15 rows
    assert len(df) >= 10, f"expected ~15 rows, got {len(df)}"

    # Column contract
    assert set(df.columns) == set(OUTPUT_COLUMNS)

    # Value sanity
    assert df["exchange"].isin(["NYSE", "AMEX", "NASDAQ"]).all()
    assert (df["raw_close"] > 0).all()
    assert (df["adj_close"] > 0).all()
    assert (df["shares_out"] > 0).all()
    assert (df["mcap"] > 0).all()
    assert df["ticker"].isin(_SMOKE_TICKERS).all()
    # AAPL, MSFT, IBM are all common stock.
    assert (df["ticker_type"] == "CS").all()
    assert df["is_common_share"].all()

    # No duplicate (date, ticker) rows
    assert not df.duplicated(subset=["date", "ticker"]).any()
