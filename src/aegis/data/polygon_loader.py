"""Polygon.io daily-bars loader (spec §7, Week 1 Day 2 — pivoted 2026-04-23).

Replaces the CRSP/WRDS loader after Rice denied WRDS access. Pure raw-in /
raw-out pull: no filtering, no return computation. Eligibility decisions are
made downstream by :mod:`aegis.data.universe`.

Normalizations applied here:
  * Polygon's primary-exchange MIC codes → friendly strings: ``XNYS→"NYSE"``,
    ``XASE→"AMEX"``, ``XNAS→"NASDAQ"``, others → None (which makes the row
    fail ``exchange_ok`` downstream — that's the desired behavior).
  * Polygon's ``adjusted=True`` aggregate returns split/dividend-adjusted
    prices directly. We carry both ``adj_close`` (from the adjusted pull)
    and ``raw_close`` (from an unadjusted pull) so downstream code has both.
  * ``mcap = raw_close * shares_out`` using the ticker-details snapshot's
    ``weighted_shares_outstanding`` at pull time. This is a known
    approximation — for PIT-accurate mcap we'd pull ticker details with
    ``?date=`` per trading day, which is out of scope for the Week 1 slice.
  * ``ticker_type`` comes from the ticker-details endpoint. We map Polygon's
    string ("CS", "PFD", "ETF", …) onto our :data:`aegis.data.schema.TickerType`
    Literal. Unknown codes fall into "OTHER".

Rate-limit handling: the free tier permits 5 calls/min. For N tickers we
need N aggs calls + N ticker-details calls = 2N calls. The loader sleeps
between calls to stay under the limit unless ``sleep_between_calls`` is set
to 0 by a caller who knows they're on a paid tier.

Auth: reads ``POLYGON_API_KEY`` from env. Pass ``api_key=`` explicitly to
override (useful in tests and CI).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from aegis.data.schema import ExchangeCode, TickerType

if TYPE_CHECKING:
    from polygon import RESTClient

# MIC code → friendly exchange name. Anything unmapped returns None,
# which the universe filter rejects via exchange_ok.
MIC_TO_EXCHANGE: dict[str, ExchangeCode] = {
    "XNYS": "NYSE",
    "XASE": "AMEX",
    "XNAS": "NASDAQ",
}

# Polygon ticker_type string → our TickerType Literal. Values not in the
# map fall into "OTHER".
_TICKER_TYPE_MAP: dict[str, TickerType] = {
    "CS": "CS",
    "PFD": "PFD",
    "ETF": "ETF",
    "ETN": "ETN",
    "ADRC": "ADRC",
    "ADRP": "ADRP",
    "UNIT": "UNIT",
    "WARRANT": "WARRANT",
    "RIGHT": "RIGHT",
    "FUND": "FUND",
    "SP": "SP",
}

OUTPUT_COLUMNS: tuple[str, ...] = (
    "date",
    "ticker",
    "exchange",
    "ticker_type",
    "is_common_share",
    "raw_close",
    "adj_close",
    "volume",
    "shares_out",
    "mcap",
)

# Free-tier: 5 calls/min → 12 seconds between calls keeps us under the cap.
_DEFAULT_SLEEP_S: float = 12.5


@dataclass(frozen=True)
class _TickerMeta:
    """Static-ish metadata for one ticker (type, exchange, shares out)."""

    ticker: str
    ticker_type: TickerType
    exchange: ExchangeCode | None
    shares_out: float


def load_polygon_daily(
    tickers: Sequence[str],
    start: date,
    end: date,
    client: RESTClient | None = None,
    api_key: str | None = None,
    sleep_between_calls: float = _DEFAULT_SLEEP_S,
    metadata_as_of: date | None = None,
) -> pd.DataFrame:
    """Pull daily bars for the given tickers over [start, end].

    Args:
        tickers: List of Polygon ticker symbols (e.g. ``["AAPL", "MSFT"]``).
        start: First trading date (inclusive).
        end: Last trading date (inclusive).
        client: Optional existing ``polygon.RESTClient`` (for tests/reuse).
            If None, constructs one using ``api_key`` or ``POLYGON_API_KEY``.
        api_key: Override the ``POLYGON_API_KEY`` env var. Mostly for tests.
        sleep_between_calls: Seconds to sleep between Polygon API calls.
            Defaults to 12.5 (free-tier friendly). Paid-tier users set to 0.
        metadata_as_of: Optional date passed to Polygon ticker-details calls.
            Historical universe runs use this to avoid classifying a renamed
            or delisted symbol using today's ticker state.

    Returns:
        Long-format DataFrame with columns listed in :data:`OUTPUT_COLUMNS`.
        One row per (date, ticker). ``adj_close`` is population-adjusted by
        Polygon; downstream code can compute ret_1d from it directly.
    """
    if end < start:
        raise ValueError(f"end ({end}) must be >= start ({start})")
    if not tickers:
        return _empty_frame()

    conn = client if client is not None else _make_client(api_key)

    # Pull ticker metadata once per ticker (single call each), then the
    # per-ticker adjusted aggs over the window. Tolerant per-ticker error
    # handling: a NOT_FOUND on either call (delisted ticker, renamed ticker,
    # Polygon coverage gap) skips that ticker with a logged warning so the
    # other ~495 tickers in a Day 13 widened-universe run still produce a
    # panel. The locked Week 2 plan accepts "~500 tickers" as the live-run
    # acceptance target, not "exactly 500".
    frames: list[pd.DataFrame] = []
    skipped: list[tuple[str, str]] = []
    for i, ticker in enumerate(tickers):
        if i > 0 and sleep_between_calls > 0:
            time.sleep(sleep_between_calls)
        try:
            meta = _fetch_ticker_meta(conn, ticker, metadata_as_of=metadata_as_of)
        except Exception as e:
            skipped.append((ticker, f"meta: {type(e).__name__}: {str(e)[:80]}"))
            continue

        if sleep_between_calls > 0:
            time.sleep(sleep_between_calls)
        try:
            aggs = _fetch_daily_bars(conn, ticker, start, end, sleep_between_calls)
        except Exception as e:
            skipped.append((ticker, f"aggs: {type(e).__name__}: {str(e)[:80]}"))
            continue

        if aggs.empty:
            continue

        frames.append(_assemble_ticker_frame(aggs, meta))

    if skipped:
        print(
            f"  skipped {len(skipped)} tickers (Polygon errors): "
            f"{', '.join(t for t, _ in skipped[:10])}"
            f"{' …' if len(skipped) > 10 else ''}"
        )

    if not frames:
        return _empty_frame()

    out = pd.concat(frames, ignore_index=True)
    out = out.loc[:, list(OUTPUT_COLUMNS)].reset_index(drop=True)

    # Invariant: one row per (date, ticker).
    dupes = out.duplicated(subset=["date", "ticker"]).sum()
    if dupes:
        raise RuntimeError(f"Unexpected {dupes} duplicate (date, ticker) rows")

    return out


def _make_client(api_key: str | None) -> RESTClient:
    """Construct a polygon.RESTClient, falling back to POLYGON_API_KEY env."""
    key = api_key or os.environ.get("POLYGON_API_KEY")
    if not key:
        raise RuntimeError(
            "POLYGON_API_KEY is not set. Put your key in .env (see .env.example) "
            "or pass api_key=... explicitly."
        )
    from polygon import RESTClient  # import lazily to keep unit tests deps-free

    return RESTClient(key)


def _fetch_ticker_meta(
    client: RESTClient,
    ticker: str,
    *,
    metadata_as_of: date | None = None,
) -> _TickerMeta:
    """Pull (ticker_type, primary_exchange, weighted_shares_outstanding)."""
    if metadata_as_of is None:
        details = client.get_ticker_details(ticker)
    else:
        details = client.get_ticker_details(ticker, date=metadata_as_of.isoformat())
    raw_type = getattr(details, "type", None) or "OTHER"
    raw_mic = getattr(details, "primary_exchange", None) or ""
    raw_shares = getattr(details, "weighted_shares_outstanding", None) or 0.0

    ticker_type = _TICKER_TYPE_MAP.get(raw_type, "OTHER")
    exchange: ExchangeCode | None = MIC_TO_EXCHANGE.get(raw_mic)

    return _TickerMeta(
        ticker=ticker,
        ticker_type=ticker_type,
        exchange=exchange,
        shares_out=float(raw_shares),
    )


def _fetch_daily_bars(
    client: RESTClient,
    ticker: str,
    start: date,
    end: date,
    sleep_between_calls: float = 0.0,
) -> pd.DataFrame:
    """Return a DataFrame with columns: date, raw_close, adj_close, volume.

    Calls Polygon twice per ticker: once with ``adjusted=True`` (split/div
    adjusted), once unadjusted. We need both — adj_close for return
    computation, raw_close for the universe price_ok rule.

    ``sleep_between_calls`` (seconds) is waited between the two calls to
    stay under the free-tier rate limit. Pass 0 on paid tiers.
    """
    adjusted_bars = list(
        client.list_aggs(
            ticker=ticker,
            multiplier=1,
            timespan="day",
            from_=start.isoformat(),
            to=end.isoformat(),
            adjusted=True,
            limit=50_000,
        )
    )

    if sleep_between_calls > 0:
        time.sleep(sleep_between_calls)

    raw_bars = list(
        client.list_aggs(
            ticker=ticker,
            multiplier=1,
            timespan="day",
            from_=start.isoformat(),
            to=end.isoformat(),
            adjusted=False,
            limit=50_000,
        )
    )

    if not adjusted_bars or not raw_bars:
        return pd.DataFrame()

    adj_df = _bars_to_df(adjusted_bars).rename(columns={"close": "adj_close"})
    raw_df = _bars_to_df(raw_bars).rename(columns={"close": "raw_close"})

    merged = adj_df[["date", "adj_close"]].merge(
        raw_df[["date", "raw_close", "volume"]],
        on="date",
        how="inner",
    )
    return merged


def _bars_to_df(bars: Iterable[Any]) -> pd.DataFrame:
    """Convert an iterable of polygon.rest.models.Agg → DataFrame.

    The Polygon ``Agg`` model exposes ``.timestamp`` (ms since epoch),
    ``.close``, and ``.volume`` as attributes. The polygon-api-client
    package ships no type stubs, so we type the iterable as ``Any`` and
    cast where stricter types help readability.
    """
    rows = []
    for bar in bars:
        ts_ms = cast(int, bar.timestamp)
        rows.append(
            {
                "date": pd.Timestamp(ts_ms, unit="ms").normalize().to_pydatetime().date(),
                "close": float(bar.close),
                "volume": float(bar.volume),
            }
        )
    return pd.DataFrame(rows)


def _assemble_ticker_frame(aggs: pd.DataFrame, meta: _TickerMeta) -> pd.DataFrame:
    """Join per-date bars with the ticker's static metadata into an output frame."""
    n = len(aggs)
    df = aggs.copy()
    df["ticker"] = meta.ticker
    df["exchange"] = pd.array([meta.exchange] * n, dtype="string")
    df["ticker_type"] = pd.array([meta.ticker_type] * n, dtype="string")
    df["is_common_share"] = meta.ticker_type == "CS"
    df["shares_out"] = meta.shares_out
    df["mcap"] = df["raw_close"] * df["shares_out"]
    return df


def _empty_frame() -> pd.DataFrame:
    """Empty DataFrame with the canonical output dtype layout."""
    dtypes: dict[str, str] = {
        "date": "datetime64[ns]",
        "ticker": "string",
        "exchange": "string",
        "ticker_type": "string",
        "is_common_share": "bool",
        "raw_close": "float64",
        "adj_close": "float64",
        "volume": "float64",
        "shares_out": "float64",
        "mcap": "float64",
    }
    return pd.DataFrame({col: pd.Series(dtype=dtypes[col]) for col in OUTPUT_COLUMNS})


__all__ = ["MIC_TO_EXCHANGE", "OUTPUT_COLUMNS", "load_polygon_daily"]
