"""Polygon ticker-reference scraper for the Day-9 tradability cache.

Builds ``data/reference/ticker_metadata.parquet`` (gitignored) with one row
per ticker that has ever appeared in ``data/reference/sp500_membership.csv``.
Columns:

    ticker, name, primary_exchange, ticker_type, list_date, delisted_date,
    sic_code, sic_description, cik

Plus a sibling provenance sidecar ``ticker_metadata.meta.json`` recording
source URL, fetch timestamp, scraper git SHA, parquet sha256, row counts,
delisted-row count, and Polygon API call totals (Principle 5 — auditability).

Strategy
--------
``sp500_membership.csv`` is the source of truth for **which tickers were
S&P 500 members and when they were removed**. We use it to drive the
Polygon queries — *not* Polygon's bulk inactive-ticker listing, because
Polygon-side ticker symbols get re-used (the modern ``MON`` ticker is a
2021-listed entity unrelated to the original Monsanto, which is what
*we* care about; the bulk list would return the wrong entity).

For each unique ticker that ever appeared in ``sp500_membership.csv``:

1. If the ticker has any open membership interval (currently in S&P):
   call ``get_ticker_details(ticker)`` (no date). Sets ``delisted_date``
   to None. If Polygon returns ``NOT_FOUND``, the ticker is logged and
   skipped — extremely rare, indicates the membership CSV is ahead of
   Polygon.

2. Otherwise (was in S&P, then removed): use the **maximum**
   ``date_removed`` across the ticker's intervals as the delisting
   boundary. Call ``get_ticker_details(ticker, date=<date_removed -
   30 days>)`` to fetch static fields from the in-S&P era — this avoids
   ticker-reuse pollution. Set ``delisted_date`` directly from
   ``sp500_membership.date_removed`` (canonical for our purposes).

3. Tolerate per-ticker failures by recording the ticker in a failures
   list and continuing. The provenance sidecar logs the failure count
   and a sample.

Run quarterly (or whenever the universe shifts):

    uv run python scripts/fetch_polygon_ticker_reference.py

On Polygon Starter (100 calls/min) this takes ~8–12 min for ~640
universe tickers (one ticker-detail call each).

Auth: reads ``POLYGON_API_KEY`` from env / ``.env``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
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
PARQUET_PATH = OUTPUT_DIR / "ticker_metadata.parquet"
META_PATH = OUTPUT_DIR / "ticker_metadata.meta.json"

# Polygon Starter is 100 calls/min — 0.6s sleep stays under the ceiling
# with ~50% headroom. Override at the top of `main()` for paid tiers.
SLEEP_BETWEEN_CALLS = 0.6

EXPECTED_COLUMNS = (
    "ticker",
    "name",
    "primary_exchange",
    "ticker_type",
    "list_date",
    "delisted_date",
    "sic_code",
    "sic_description",
    "cik",
)


def _build_sp500_map(membership_csv: Path) -> dict[str, pd.Timestamp | None]:
    """ticker → latest date_removed (NaT-equivalent None if any open interval).

    None means "currently a member; treat as active in Polygon-land".
    A pd.Timestamp means "removed from S&P; use this as the delisting
    boundary and as the reference date for historical Polygon queries".
    """
    df = pd.read_csv(membership_csv, parse_dates=["date_added", "date_removed"])
    out: dict[str, pd.Timestamp | None] = {}
    for ticker, group in df.groupby("ticker"):
        if group["date_removed"].isna().any():
            out[str(ticker)] = None  # has at least one open interval
        else:
            out[str(ticker)] = pd.Timestamp(group["date_removed"].max())
    return out


def _coerce_date(value: Any) -> pd.Timestamp | None:
    if value is None or value == "" or pd.isna(value):
        return None
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    # Strip time component and timezone to keep the parquet schema clean.
    return pd.Timestamp(ts.date())


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip().lstrip("0") or "0")
    except (TypeError, ValueError):
        return None


def _row_from_details(
    details: Any,
    *,
    delisted_utc_override: str | None = None,
) -> dict[str, Any]:
    """Translate a Polygon TickerDetails response into our schema."""
    delisted_raw = delisted_utc_override or getattr(details, "delisted_utc", None)
    return {
        "ticker": getattr(details, "ticker", None),
        "name": getattr(details, "name", None),
        "primary_exchange": getattr(details, "primary_exchange", None),
        "ticker_type": getattr(details, "type", None),
        "list_date": _coerce_date(getattr(details, "list_date", None)),
        "delisted_date": _coerce_date(delisted_raw),
        "sic_code": getattr(details, "sic_code", None),
        "sic_description": getattr(details, "sic_description", None),
        "cik": _coerce_int(getattr(details, "cik", None)),
    }


def _fetch_one(
    client: Any,
    ticker: str,
    sp500_date_removed: pd.Timestamp | None,
) -> tuple[dict[str, Any] | None, int]:
    """Return (row_dict, api_calls_made). Tolerates per-ticker failures.

    ``sp500_date_removed`` is None if the ticker is currently a member
    (use the active path), or a pd.Timestamp of its S&P removal date
    (use the historical path with date=<removed - 30 days>).
    """
    calls = 0

    if sp500_date_removed is None:
        # Active path: ticker is a current S&P member.
        try:
            details = client.get_ticker_details(ticker)
            calls += 1
            return _row_from_details(details), calls
        except Exception as e:
            print(f"  WARN active   {ticker}: {type(e).__name__}: {e}")
            return None, calls

    # Historical path: query inside the in-S&P period to dodge ticker-reuse.
    ref_date = (sp500_date_removed - pd.Timedelta(days=30)).date()
    delisted_iso = sp500_date_removed.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        details = client.get_ticker_details(ticker, date=ref_date.isoformat())
        calls += 1
        return _row_from_details(details, delisted_utc_override=delisted_iso), calls
    except Exception as e:
        print(f"  WARN delisted {ticker} (ref={ref_date}): {type(e).__name__}: {e}")
        return None, calls


def _validate(df: pd.DataFrame) -> None:
    if tuple(df.columns) != EXPECTED_COLUMNS:
        raise RuntimeError(f"column mismatch: {tuple(df.columns)} != {EXPECTED_COLUMNS}")
    if df["ticker"].isna().any():
        raise RuntimeError("found rows with null ticker")
    if df["ticker"].duplicated().any():
        dupes = df.loc[df["ticker"].duplicated(), "ticker"].tolist()
        raise RuntimeError(f"found duplicate ticker rows: {dupes}")
    delisted_count = int(df["delisted_date"].notna().sum())
    if delisted_count < 50:
        raise RuntimeError(f"sanity floor: expected >=50 delisted tickers, got {delisted_count}")
    if len(df) < 500:
        raise RuntimeError(f"sanity floor: expected >=500 rows, got {len(df)}")


def _write_meta(
    df: pd.DataFrame,
    *,
    parquet_path: Path,
    meta_path: Path,
    api_calls: int,
    universe_size: int,
    sample_failed: list[str],
) -> None:
    meta = {
        "source_url": "https://api.polygon.io/v3/reference/tickers",
        "fetched_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scraper_git_sha": current_git_sha(),
        "parquet_path": str(parquet_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "parquet_sha256": sha256_file(parquet_path),
        "universe_size": universe_size,
        "row_count": len(df),
        "current_count": int(df["delisted_date"].isna().sum()),
        "delisted_count": int(df["delisted_date"].notna().sum()),
        "api_calls_made": api_calls,
        "sample_failed_tickers": sample_failed[:10],
        "notes": (
            "delisted tickers fetched via list_tickers(active=False) for "
            "delisted_utc, then get_ticker_details(ticker, date=<1y before>) "
            "for static fields. Per-ticker failures recorded as null fields."
        ),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    load_dotenv_if_present(REPO_ROOT)
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        raise RuntimeError("POLYGON_API_KEY not set. Put your key in .env (see .env.example).")

    from polygon import RESTClient

    client = RESTClient(api_key)

    sp500_map = _build_sp500_map(MEMBERSHIP_CSV)
    universe = sorted(sp500_map.keys())
    universe_active = sum(1 for v in sp500_map.values() if v is None)
    universe_delisted = len(universe) - universe_active
    print(
        f"Universe: {len(universe)} unique tickers "
        f"(active in S&P: {universe_active}, removed from S&P: {universe_delisted})"
    )

    print(f"Fetching ticker details (~{SLEEP_BETWEEN_CALLS}s/call)…")
    rows: list[dict[str, Any]] = []
    failed: list[str] = []
    api_calls = 0
    t0 = time.time()
    for i, ticker in enumerate(universe, 1):
        row, calls = _fetch_one(client, ticker, sp500_map[ticker])
        api_calls += calls
        if row is None:
            failed.append(ticker)
        else:
            rows.append(row)
        if i % 50 == 0 or i == len(universe):
            elapsed = time.time() - t0
            print(
                f"  [{i}/{len(universe)}] elapsed={elapsed:5.1f}s "
                f"got={len(rows)} failed={len(failed)} api_calls={api_calls}"
            )
        time.sleep(SLEEP_BETWEEN_CALLS)

    df = pd.DataFrame(rows, columns=list(EXPECTED_COLUMNS))
    df = df.sort_values("ticker").reset_index(drop=True)
    _validate(df)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(PARQUET_PATH, index=False)
    _write_meta(
        df,
        parquet_path=PARQUET_PATH,
        meta_path=META_PATH,
        api_calls=api_calls,
        universe_size=len(universe),
        sample_failed=failed,
    )

    sha = sha256_file(PARQUET_PATH)
    print(f"\nWrote {PARQUET_PATH.relative_to(REPO_ROOT)}")
    print(
        f"  rows={len(df)} | current={int(df['delisted_date'].isna().sum())} "
        f"| delisted={int(df['delisted_date'].notna().sum())}"
    )
    print(f"  sha256={sha}")
    print(f"  api_calls_made={api_calls}")
    if failed:
        print(
            f"  failed ({len(failed)}): {', '.join(failed[:10])}{' …' if len(failed) > 10 else ''}"
        )
    print(f"Wrote {META_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
