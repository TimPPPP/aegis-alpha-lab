"""Polygon v1 fundamentals scraper for the Week 3 Day 15 fundamentals snapshot.

Builds ``data/reference/fundamentals.parquet`` (gitignored, regenerable) with
one row per fiscal period per S&P 500 entity over the last ~5 years. Pulls
from Polygon's split v1 financial-statement endpoints (NOT the deprecated
``/vX/reference/financials``):

  - ``list_financials_income_statements``
  - ``list_financials_balance_sheets``
  - ``list_financials_cash_flow_statements``

Entitlement
-----------
These endpoints require Polygon's **Financials & Ratios Expansion** or
**Stocks Advanced** plan. **Stocks Starter is not sufficient.** Run the
preflight first:

    uv run python scripts/fetch_polygon_fundamentals.py --preflight-only

If any endpoint reports ``forbidden``, the script exits with code 2 and a
clear message. The full fetch raises :class:`EntitlementError` rather than
silently producing a partial snapshot.

Strategy
--------
``sp500_membership.csv`` × ``ticker_metadata.parquet`` is the source of truth
for the universe. We prefer ``cik=`` queries when CIK is present (CIK is
unambiguous; ticker symbols are reusable on Polygon's side). For ex-members
we cap ``filing_date_lt`` to ``date_removed + 30d`` to dodge ticker-reuse
pollution.

Each entity gets three API calls (one per endpoint). Per-period rows are
merged by ``(cik or ticker, fiscal_year, fiscal_quarter, period_kind,
period_end_date)`` and ``source_endpoints`` records which of the three
endpoints contributed.

PIT discipline (spec §4.1): every output row stores both ``filing_date``
(when the report became publicly available) and ``period_end_date`` (the
as-of period the report describes). Day 16's lookup helpers slice on
``filing_date < t``, never on ``period_end_date``.

Auth: reads ``POLYGON_API_KEY`` from env / ``.env``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.utils.dotenv import load_dotenv_if_present
from aegis.utils.git import current_git_sha
from aegis.utils.hashing import sha256_file

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "data" / "reference"
MEMBERSHIP_CSV = OUTPUT_DIR / "sp500_membership.csv"
METADATA_PARQUET = OUTPUT_DIR / "ticker_metadata.parquet"
PARQUET_PATH = OUTPUT_DIR / "fundamentals.parquet"
META_PATH = OUTPUT_DIR / "fundamentals.meta.json"

# Polygon Starter is 100 calls/min — 0.6s sleep stays under the ceiling
# with ~50% headroom. On Stocks Advanced (1000+/min) drop to 0.05.
SLEEP_BETWEEN_CALLS = 0.6

LOOKBACK_YEARS_DEFAULT = 5

# 4 quarterlies + 1 annual + ≤4 TTM rows per fiscal year × 5y ≈ ≤45 rows
# per entity per endpoint. 50 is comfortable headroom.
PER_ENTITY_REPORT_LIMIT = 50

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

# Per-endpoint Polygon-field → ours mapping. Keys are the only attributes
# we extract from each response object beyond the shared period/filing keys.
INCOME_FIELD_MAP: dict[str, str] = {
    "revenue": "revenues",
    "net_income_loss_attributable_common_shareholders": "net_income",
    "basic_earnings_per_share": "eps_basic",
    "diluted_earnings_per_share": "eps_diluted",
    "basic_shares_outstanding": "weighted_avg_shares_basic",
    "diluted_shares_outstanding": "weighted_avg_shares_diluted",
}
BALANCE_FIELD_MAP: dict[str, str] = {
    "total_equity_attributable_to_parent": "common_equity",
    "total_assets": "total_assets",
}
CASHFLOW_FIELD_MAP: dict[str, str] = {
    "net_cash_from_operating_activities": "operating_cash_flow",
}

VALID_PERIOD_KINDS = ("quarterly", "annual", "trailing_twelve_months")


class EntitlementError(RuntimeError):
    """Raised when Polygon returns NOT_AUTHORIZED for the v1 financials endpoints.

    Indicates the API key does not include Financials & Ratios Expansion or
    Stocks Advanced. Day 15 cannot generate live fundamentals without it.
    """


def _is_not_authorized(exc: BaseException) -> bool:
    """Identify Polygon's NOT_AUTHORIZED response shape from any exception."""
    s = str(exc)
    return "NOT_AUTHORIZED" in s or "not entitled" in s.lower()


def _parse_iso_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _format_cik(cik: Any) -> str | None:
    """Normalize a CIK to Polygon's expected zero-padded 10-char string form."""
    if cik is None:
        return None
    if isinstance(cik, float) and pd.isna(cik):
        return None
    try:
        return f"{int(cik):010d}"
    except (TypeError, ValueError):
        return None


def _entitlement_preflight(client: Any) -> dict[str, str]:
    """Probe each of the 3 v1 financial statement endpoints with one ticker.

    Returns a dict ``{endpoint_name: 'ok' | 'forbidden' | 'unknown_error: <msg>'}``.
    Raises nothing — caller decides whether to continue.
    """
    probes = (
        ("income_statements", client.list_financials_income_statements),
        ("balance_sheets", client.list_financials_balance_sheets),
        ("cash_flow_statements", client.list_financials_cash_flow_statements),
    )
    results: dict[str, str] = {}
    for name, fn in probes:
        try:
            list(fn(tickers="AAPL", limit=1))
            results[name] = "ok"
        except Exception as exc:
            if _is_not_authorized(exc):
                results[name] = "forbidden"
            else:
                results[name] = f"unknown_error: {type(exc).__name__}: {str(exc)[:120]}"
    return results


def _build_universe(membership_csv: Path, metadata_parquet: Path) -> list[dict[str, Any]]:
    """Return ``[{ticker, cik, ref_date}]`` for each S&P 500 entity.

    ``ref_date`` is None for currently-active members; for ex-members it's
    the latest ``date_removed`` from sp500_membership.csv. Used as the
    upper bound for ``filing_date_lt`` to dodge ticker-reuse pollution.
    """
    members = pd.read_csv(membership_csv, parse_dates=["date_added", "date_removed"])
    metadata = pd.read_parquet(metadata_parquet)
    cik_map: dict[str, Any] = dict(zip(metadata["ticker"], metadata["cik"], strict=False))

    out: list[dict[str, Any]] = []
    for ticker_obj, group in members.groupby("ticker"):
        ticker = str(ticker_obj)
        currently_active = bool(group["date_removed"].isna().any())
        if currently_active:
            ref_date: date | None = None
        else:
            max_removed = group["date_removed"].max()
            ref_date = pd.Timestamp(max_removed).date() if pd.notna(max_removed) else None
        out.append({"ticker": ticker, "cik": cik_map.get(ticker), "ref_date": ref_date})
    return sorted(out, key=lambda r: r["ticker"])


def _fetch_endpoint(
    fn: Any,
    *,
    cik: str | None,
    ticker: str,
    filing_date_gte: date,
    filing_date_lt: date | None,
    limit: int,
) -> tuple[list[Any], int, str | None]:
    """Pull one endpoint for one entity. Prefer ``cik=``, fall back to ``tickers=``.

    Returns ``(rows, api_calls_made, error_or_none)``.
    """
    kwargs: dict[str, Any] = {
        "filing_date_gte": filing_date_gte.isoformat(),
        "limit": limit,
    }
    if filing_date_lt is not None:
        kwargs["filing_date_lt"] = filing_date_lt.isoformat()
    if cik is not None:
        kwargs["cik"] = cik
    else:
        kwargs["tickers"] = ticker

    try:
        rows = list(fn(**kwargs))
        return rows, 1, None
    except Exception as exc:
        return [], 1, f"{type(exc).__name__}: {str(exc)[:120]}"


def _project_row(
    raw: Any,
    *,
    field_map: dict[str, str],
    primary_ticker: str,
    endpoint_tag: str,
) -> dict[str, Any]:
    """Translate one Polygon response object into a partial FundamentalsRow dict.

    Uses the endpoint-specific ``field_map`` for the value columns; shared
    keys (ticker, cik, filing/period dates, fiscal year/quarter, period_kind)
    are extracted directly. Missing fields → None.
    """
    tickers = getattr(raw, "tickers", None) or []
    if primary_ticker in tickers:
        ticker = primary_ticker
    elif tickers:
        ticker = tickers[0]
    else:
        ticker = primary_ticker

    cik_raw = getattr(raw, "cik", None)
    try:
        cik: int | None = int(cik_raw) if cik_raw not in (None, "") else None
    except (TypeError, ValueError):
        cik = None

    timeframe = getattr(raw, "timeframe", None)
    period_kind = timeframe if timeframe in VALID_PERIOD_KINDS else None

    fy_raw = getattr(raw, "fiscal_year", None)
    fiscal_year = int(fy_raw) if fy_raw is not None else None
    fq_raw = getattr(raw, "fiscal_quarter", None)
    fiscal_quarter = int(fq_raw) if fq_raw is not None else None

    out: dict[str, Any] = {
        "ticker": ticker,
        "cik": cik,
        "filing_date": _parse_iso_date(getattr(raw, "filing_date", None)),
        "period_end_date": _parse_iso_date(getattr(raw, "period_end", None)),
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "period_kind": period_kind,
        "_endpoint_tag": endpoint_tag,
    }
    for src, dst in field_map.items():
        out[dst] = getattr(raw, src, None)
    return out


def _merge_three_endpoints(
    income_rows: list[dict[str, Any]],
    balance_rows: list[dict[str, Any]],
    cashflow_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge per-endpoint projected rows by fiscal-period key.

    Output: one row per ``(cik or ticker, fiscal_year, fiscal_quarter,
    period_kind, period_end_date)`` with ``source_endpoints`` recording the
    endpoint tags that contributed and value columns unioned (first non-None
    wins, except ``filing_date`` which takes the LATEST across the three —
    rare 10-Q amendments file separately for each statement type).
    """

    def _key(r: dict[str, Any]) -> tuple[Any, ...]:
        return (
            r.get("cik") or r.get("ticker"),
            r.get("fiscal_year"),
            r.get("fiscal_quarter"),
            r.get("period_kind"),
            r.get("period_end_date"),
        )

    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for batch in (income_rows, balance_rows, cashflow_rows):
        for r in batch:
            k = _key(r)
            if k not in merged:
                merged[k] = dict.fromkeys(EXPECTED_COLUMNS)
                merged[k]["source_endpoints"] = ()
            tgt = merged[k]
            for col, val in r.items():
                if col == "_endpoint_tag":
                    continue
                if col == "filing_date":
                    current = tgt.get("filing_date")
                    if val is not None and (current is None or val > current):
                        tgt[col] = val
                elif tgt.get(col) is None:
                    tgt[col] = val
            tag = r.get("_endpoint_tag")
            if tag is not None and tag not in tgt["source_endpoints"]:
                tgt["source_endpoints"] = (*tgt["source_endpoints"], tag)
    return list(merged.values())


def _validate(df: pd.DataFrame, *, enforce_sanity_floors: bool = True) -> None:
    if tuple(df.columns) != EXPECTED_COLUMNS:
        raise RuntimeError(f"column mismatch: {tuple(df.columns)} != {EXPECTED_COLUMNS}")
    for col in ("ticker", "filing_date", "period_end_date", "period_kind"):
        if df[col].isna().any():
            raise RuntimeError(f"found rows with null {col}")
    if not enforce_sanity_floors:
        return
    if len(df) < 10_000:
        raise RuntimeError(f"sanity floor: expected >=10,000 rows, got {len(df)}")
    unique_tickers = int(df["ticker"].nunique())
    if unique_tickers < 500:
        raise RuntimeError(f"sanity floor: expected >=500 unique tickers, got {unique_tickers}")


def _write_meta(
    df: pd.DataFrame,
    *,
    parquet_path: Path,
    meta_path: Path,
    api_calls: int,
    universe_size: int,
    coverage_failed: list[dict[str, Any]],
    entitlement_preflight: dict[str, str],
    lookback_years: int,
) -> None:
    meta = {
        "source_urls": [
            "https://api.polygon.io/stocks/financials/v1/income-statements",
            "https://api.polygon.io/stocks/financials/v1/balance-sheets",
            "https://api.polygon.io/stocks/financials/v1/cash-flow-statements",
        ],
        "fetched_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scraper_git_sha": current_git_sha(),
        "parquet_path": str(parquet_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "parquet_sha256": sha256_file(parquet_path),
        "row_count": len(df),
        "unique_ticker_count": int(df["ticker"].nunique()),
        "universe_size": universe_size,
        "lookback_years": lookback_years,
        "api_calls_made": api_calls,
        "entitlement_preflight_result": entitlement_preflight,
        "coverage_failed_count": len(coverage_failed),
        "sample_coverage_failed": coverage_failed[:10],
        "notes": (
            "Pulled via Polygon v1 split financial-statement endpoints (income / "
            "balance sheet / cash flow). CIK preferred over ticker. PIT discipline: "
            "filing_date is the t-1 visibility boundary; period_end_date is the "
            "as-of period the report describes."
        ),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def _print_preflight(preflight: dict[str, str]) -> None:
    print("Polygon v1 financials entitlement preflight:")
    for ep, status in preflight.items():
        print(f"  {ep}: {status}")


def _run_full_fetch(
    client: Any,
    *,
    universe: list[dict[str, Any]],
    lookback_years: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Iterate every entity × 3 endpoints, merging into FundamentalsRow dicts.

    Returns (merged_rows, coverage_failed, api_calls_made).
    """
    filing_date_gte = date.today() - timedelta(days=365 * lookback_years)
    print(f"Lookback: filing_date_gte = {filing_date_gte.isoformat()}")

    rows: list[dict[str, Any]] = []
    coverage_failed: list[dict[str, Any]] = []
    api_calls = 0
    t0 = time.time()

    endpoints = (
        ("income_statements", client.list_financials_income_statements, INCOME_FIELD_MAP),
        ("balance_sheets", client.list_financials_balance_sheets, BALANCE_FIELD_MAP),
        (
            "cash_flow_statements",
            client.list_financials_cash_flow_statements,
            CASHFLOW_FIELD_MAP,
        ),
    )

    for i, entity in enumerate(universe, 1):
        ticker = entity["ticker"]
        cik_str = _format_cik(entity["cik"])
        ref_date = entity["ref_date"]
        filing_date_lt = ref_date + timedelta(days=30) if ref_date is not None else None

        per_endpoint_rows: dict[str, list[dict[str, Any]]] = {}
        per_endpoint_errors: dict[str, str] = {}
        for ep_name, fn, field_map in endpoints:
            raw_rows, calls, err = _fetch_endpoint(
                fn,
                cik=cik_str,
                ticker=ticker,
                filing_date_gte=filing_date_gte,
                filing_date_lt=filing_date_lt,
                limit=PER_ENTITY_REPORT_LIMIT,
            )
            api_calls += calls
            if err is not None:
                per_endpoint_errors[ep_name] = err
                per_endpoint_rows[ep_name] = []
            else:
                per_endpoint_rows[ep_name] = [
                    _project_row(
                        r,
                        field_map=field_map,
                        primary_ticker=ticker,
                        endpoint_tag=ep_name,
                    )
                    for r in raw_rows
                ]
            time.sleep(SLEEP_BETWEEN_CALLS)

        merged = _merge_three_endpoints(
            per_endpoint_rows["income_statements"],
            per_endpoint_rows["balance_sheets"],
            per_endpoint_rows["cash_flow_statements"],
        )
        if not merged:
            coverage_failed.append(
                {
                    "ticker": ticker,
                    "cik": cik_str,
                    "errors": per_endpoint_errors or "no_rows_returned",
                }
            )
        else:
            rows.extend(merged)

        if i % 25 == 0 or i == len(universe):
            elapsed = time.time() - t0
            print(
                f"  [{i}/{len(universe)}] elapsed={elapsed:5.1f}s "
                f"rows={len(rows)} failed={len(coverage_failed)} api_calls={api_calls}"
            )

    return rows, coverage_failed, api_calls


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Probe entitlement only; do not pull the full snapshot.",
    )
    parser.add_argument(
        "--limit-tickers",
        type=int,
        default=None,
        help="Cap the universe to the first N tickers (smoke-test convenience).",
    )
    parser.add_argument(
        "--lookback-years",
        type=int,
        default=LOOKBACK_YEARS_DEFAULT,
        help="Pull filings whose filing_date is within this many years.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    load_dotenv_if_present(REPO_ROOT)
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        raise RuntimeError("POLYGON_API_KEY not set. Put your key in .env (see .env.example).")

    from polygon import RESTClient

    client = RESTClient(api_key)

    preflight = _entitlement_preflight(client)
    _print_preflight(preflight)

    forbidden = [ep for ep, s in preflight.items() if s == "forbidden"]
    if forbidden:
        print(
            f"\nENTITLEMENT MISSING for: {', '.join(forbidden)}.\n"
            "These endpoints require Polygon's Financials & Ratios Expansion or "
            "Stocks Advanced plan. Stocks Starter is not sufficient.\n"
            "Day 15 live fetch is blocked until entitlement is granted."
        )
        if args.preflight_only:
            return 2
        raise EntitlementError(f"Cannot proceed with full fetch; forbidden endpoints: {forbidden}")

    if args.preflight_only:
        print("\nPreflight passed. Re-run without --preflight-only for the full fetch.")
        return 0

    universe = _build_universe(MEMBERSHIP_CSV, METADATA_PARQUET)
    if args.limit_tickers is not None:
        universe = universe[: args.limit_tickers]
    print(f"\nUniverse: {len(universe)} entities")

    rows, coverage_failed, api_calls = _run_full_fetch(
        client, universe=universe, lookback_years=args.lookback_years
    )

    df = pd.DataFrame(rows, columns=list(EXPECTED_COLUMNS))
    df = df.dropna(subset=["ticker", "filing_date", "period_end_date", "period_kind"])
    df = df.sort_values(["ticker", "filing_date"]).reset_index(drop=True)
    _validate(df, enforce_sanity_floors=args.limit_tickers is None)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(PARQUET_PATH, index=False)
    _write_meta(
        df,
        parquet_path=PARQUET_PATH,
        meta_path=META_PATH,
        api_calls=api_calls,
        universe_size=len(universe),
        coverage_failed=coverage_failed,
        entitlement_preflight=preflight,
        lookback_years=args.lookback_years,
    )

    sha = sha256_file(PARQUET_PATH)
    print(f"\nWrote {PARQUET_PATH.relative_to(REPO_ROOT)}")
    print(f"  rows={len(df)} unique_tickers={df['ticker'].nunique()}")
    print(f"  sha256={sha}")
    print(f"  api_calls_made={api_calls}")
    print(f"  coverage_failed_count={len(coverage_failed)}")
    print(f"Wrote {META_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
