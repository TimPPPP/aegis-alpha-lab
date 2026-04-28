"""Build per-date ground-truth fixtures for the Module A acceptance test.

Source: iShares Core S&P 500 ETF (IVV) historical holdings, served directly
by iShares' CDN at:

    https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/...
        ?fileType=csv&fileName=IVV_holdings&dataType=fund&asOfDate=YYYYMMDD

The CSV has 9 metadata rows then a normal table whose ``Ticker`` column is
the symbol used on that trading day (e.g. ``FB`` on 2018-06-15, not
``META``). The fixture stores those raw historical symbols verbatim — the
acceptance test reconciles them against our reconstruction by mapping the
reconstructed canonical symbols *backward* through
:func:`aegis.data.ticker_reference.canonicalize_ticker`. Keeping the
fixture pristine means: re-running this script produces a byte-identical
file, and the rename-handling logic lives entirely in
``data/reference/ticker_aliases.csv``.

Usage:

    uv run python scripts/build_sp500_groundtruth.py --date 2018-06-15
    uv run python scripts/build_sp500_groundtruth.py --date 2021-01-04

Writes ``tests/fixtures/sp500_<YYYYMMDD>.txt`` (one ticker per line, sorted,
with a header comment block documenting source + fetch timestamp).

The script is one-shot. The fixture files are checked into git; this script
is preserved for reproducibility / audit but isn't required to be re-run on
every clone.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from datetime import date, datetime
from io import StringIO
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

IVV_URL = (
    "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IVV_holdings"
    "&dataType=fund&asOfDate={asof}"
)
USER_AGENT = "aegis-alpha-lab/0.1 (https://github.com/timidpaper/aegis-alpha-lab)"


def _fetch_ivv_csv(asof: date) -> str:
    url = IVV_URL.format(asof=asof.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8-sig")


def _parse_ivv(csv_text: str) -> pd.DataFrame:
    """Skip iShares' 9-row metadata preamble; return a normal DataFrame."""
    # The header block ends with a non-breaking-space line followed by the
    # real header. ``skiprows=9`` lands on the Ticker/Name/... header row.
    df = pd.read_csv(StringIO(csv_text), skiprows=9, dtype={"Ticker": str})
    # iShares trails the holdings with a small block of non-equity rows
    # (cash placeholders, FX). Keep only Equity Asset Class entries.
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].astype(str).str.strip() == "Equity"]
    df = df[df["Ticker"].notna()]
    df["Ticker"] = df["Ticker"].astype(str).str.strip()
    df = df[df["Ticker"] != ""]
    df = df[~df["Ticker"].str.contains("-", na=False)]  # drop e.g. "MSFT-"
    return df.reset_index(drop=True)


def _write_fixture(
    out_path: Path,
    asof: date,
    tickers: set[str],
    *,
    source_url: str,
    fetched_at: datetime,
) -> None:
    lines = [
        f"# S&P 500 constituents as of {asof.isoformat()} (raw iShares historical symbols)",
        "# source: iShares Core S&P 500 ETF (IVV) holdings",
        f"# url: {source_url}",
        f"# fetched: {fetched_at.isoformat(timespec='seconds')}Z",
        f"# count: {len(tickers)}",
    ]
    lines.extend(sorted(tickers))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD trading day")
    args = parser.parse_args()

    asof = date.fromisoformat(args.date)
    print(f"Fetching iShares IVV holdings as of {asof}…")
    csv_text = _fetch_ivv_csv(asof)
    print(f"  received {len(csv_text)} chars")

    raw = _parse_ivv(csv_text)
    print(f"  parsed {len(raw)} equity rows")

    tickers = set(raw["Ticker"].tolist())
    print(f"  {len(tickers)} unique symbols")

    out_path = FIXTURES_DIR / f"sp500_{asof.strftime('%Y%m%d')}.txt"
    source_url = IVV_URL.format(asof=asof.strftime("%Y%m%d"))
    _write_fixture(
        out_path,
        asof,
        tickers,
        source_url=source_url,
        fetched_at=datetime.utcnow(),
    )
    print(f"Wrote {out_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
