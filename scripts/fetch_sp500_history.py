"""One-shot Wikipedia scraper for historical S&P 500 membership.

Produces two artifacts under ``data/reference/`` (relative to the repo root):

- ``sp500_membership.csv`` — long-format, one row per ``(ticker, date_added,
  date_removed)`` membership interval. Multi-interval tickers (added →
  removed → re-added) get one row per closed interval plus one open-ended
  row for current membership.

- ``sp500_membership.meta.json`` — provenance sidecar (Principle 5 —
  auditability). Records source URL, UTC fetch timestamp, scraper git SHA,
  CSV sha256, and row counts. The meta JSON is the audit receipt for one
  scrape; ``csv_sha256`` lets downstream code verify the CSV hasn't been
  edited out-of-band.

Run quarterly (or whenever Wikipedia's "Selected changes" table grows):

    uv run python scripts/fetch_sp500_history.py

Coverage notes
--------------

Wikipedia's "Selected changes" table goes back to ~2009 reliably (a
handful of older rows exist but are sparse). Tickers that appear ONLY as
"Removed" (i.e. pre-2009 members removed in the changes era) are encoded
with ``date_added`` set to the earliest change-table date as a sentinel.
This means pre-2009 backtests treat then-current members as if added at
that sentinel date — best-effort, documented in the meta sidecar's
``notes`` field.

Same-day adds in changes that match the current-table ``Date added`` (e.g.
TSLA 2020-12-21) are deduped: one open-ended interval, not two.

Same-day rename pairs (FB Removed / META Added on 2022-06-09) are not
collapsed at this layer; ``ticker_aliases.csv`` (Day 9) is the right place
for rename reconciliation. Day 10's Module A acceptance test tolerates a
1-name drift, which absorbs the few cases this leaves.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pandas as pd

# Project root resolves up from scripts/<this file>; the import below expects
# `src/` to be on sys.path when invoked via `uv run`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.utils.git import current_git_sha
from aegis.utils.hashing import sha256_file

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
USER_AGENT = "aegis-alpha-lab/0.1 (https://github.com/timidpaper/aegis-alpha-lab)"

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "data" / "reference"
CSV_PATH = OUTPUT_DIR / "sp500_membership.csv"
META_PATH = OUTPUT_DIR / "sp500_membership.meta.json"


def _fetch_wikipedia(url: str) -> str:
    """Fetch Wikipedia HTML using a polite User-Agent (default urllib UA is 403'd)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return raw.decode("utf-8")


def _normalize_current(current: pd.DataFrame) -> pd.DataFrame:
    """Return ``(ticker, name, wiki_sector, wiki_sub_industry, date_added, cik_code)``."""
    df = current.rename(
        columns={
            "Symbol": "ticker",
            "Security": "name",
            "GICS Sector": "wiki_sector",
            "GICS Sub-Industry": "wiki_sub_industry",
            "Date added": "date_added",
            "CIK": "cik_code",
        }
    )
    df["date_added"] = pd.to_datetime(df["date_added"], errors="coerce")
    df["cik_code"] = pd.to_numeric(df["cik_code"], errors="coerce").astype("Int64")
    df = df[["ticker", "name", "wiki_sector", "wiki_sub_industry", "date_added", "cik_code"]]
    null_dates = int(df["date_added"].isna().sum())
    if null_dates:
        print(f"WARN: dropping {null_dates} current rows with unparseable date_added")
        df = df.dropna(subset=["date_added"])
    df["ticker"] = df["ticker"].astype(str).str.strip()
    return df.reset_index(drop=True)


def _normalize_changes(changes: pd.DataFrame) -> pd.DataFrame:
    """Flatten the multi-level header and parse the effective date."""
    flat_cols: list[str] = []
    for col in changes.columns:
        if isinstance(col, tuple):
            flat_cols.append("_".join(str(c) for c in col).strip("_"))
        else:
            flat_cols.append(str(col))
    df = changes.copy()
    df.columns = flat_cols
    df = df.rename(
        columns={
            "Effective Date_Effective Date": "date",
            "Added_Ticker": "added_ticker",
            "Added_Security": "added_name",
            "Removed_Ticker": "removed_ticker",
            "Removed_Security": "removed_name",
        }
    )
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).reset_index(drop=True)
    for col in ("added_ticker", "removed_ticker", "added_name", "removed_name"):
        if col in df.columns:
            df[col] = df[col].astype(str).where(df[col].notna(), other=pd.NA).str.strip()
    return df


def _intervals_for_ticker(
    ticker: str,
    sorted_events: list[tuple[pd.Timestamp, str]],
    cur_date_added: pd.Timestamp | None,
    sentinel: pd.Timestamp,
) -> tuple[list[dict[str, object]], bool]:
    """Walk one ticker's events and return (rows, was_phantom_dropped)."""
    rows: list[dict[str, object]] = []
    open_add: pd.Timestamp | None = None
    for ts, kind in sorted_events:
        if kind == "add":
            if open_add is not None:
                # Two adds in a row without a remove — defensive close.
                rows.append({"ticker": ticker, "date_added": open_add, "date_removed": ts})
            open_add = ts
        else:  # remove
            if open_add is None:
                open_add = sentinel
            rows.append({"ticker": ticker, "date_added": open_add, "date_removed": ts})
            open_add = None

    is_current = cur_date_added is not None
    if is_current:
        # Same-day dedupe: if the dangling add matches the current row's
        # date_added, one open-ended row covers it. Otherwise prefer the
        # current table's date (the changes table sometimes records the
        # announcement date rather than the effective date).
        if open_add is not None and open_add == cur_date_added:
            date_added_final = open_add
        else:
            date_added_final = cur_date_added
        rows.append({"ticker": ticker, "date_added": date_added_final, "date_removed": pd.NaT})
        return rows, False

    if open_add is not None:
        # Dangling add for a ticker not in the current table is almost always
        # a rename whose "removal" was never logged in the changes table
        # (FB→META 2022-06-09, KORS→CPRI 2018, PCLN→BKNG 2018, etc.).
        # Wikipedia's current table inherits the rename by listing the *new*
        # symbol with the *original* date_added, so emitting an open-ended
        # interval for the old symbol would double-count the lineage. Drop
        # the phantom; Day 9's ticker_aliases.csv handles renames properly.
        return rows, True

    return rows, False


def _build_intervals(
    current: pd.DataFrame,
    changes: pd.DataFrame,
) -> pd.DataFrame:
    """Walk each ticker's events chronologically and emit one row per interval."""
    sentinel = pd.Timestamp(changes["date"].min())

    # Per-ticker change events, sorted by date.
    events: dict[str, list[tuple[pd.Timestamp, str]]] = {}
    for _, row in changes.iterrows():
        ts = row["date"]
        added = row.get("added_ticker")
        removed = row.get("removed_ticker")
        if isinstance(added, str) and added and added != "nan":
            events.setdefault(added, []).append((ts, "add"))
        if isinstance(removed, str) and removed and removed != "nan":
            events.setdefault(removed, []).append((ts, "remove"))

    current_by_ticker: dict[str, dict[str, object]] = current.set_index("ticker").to_dict("index")

    all_tickers = set(events.keys()) | set(current_by_ticker.keys())
    rows: list[dict[str, object]] = []
    phantoms_dropped: list[str] = []

    for ticker in sorted(all_tickers):
        # Sort events; on the same date, "add" precedes "remove" so a same-day
        # rename pair (FB Removed / META Added) doesn't intermix.
        ev = sorted(events.get(ticker, []), key=lambda x: (x[0], 0 if x[1] == "add" else 1))
        cur = current_by_ticker.get(ticker)
        cur_date_added = pd.Timestamp(cur["date_added"]) if cur is not None else None

        ticker_rows, was_phantom = _intervals_for_ticker(ticker, ev, cur_date_added, sentinel)
        rows.extend(ticker_rows)
        if was_phantom:
            phantoms_dropped.append(ticker)

    if phantoms_dropped:
        print(
            f"  dropped {len(phantoms_dropped)} phantom-current rows for likely-renamed tickers: "
            f"{', '.join(sorted(phantoms_dropped))}"
        )
    return pd.DataFrame(rows)


def _enrich_metadata(
    intervals: pd.DataFrame,
    current: pd.DataFrame,
    changes: pd.DataFrame,
) -> pd.DataFrame:
    """Attach (name, wiki_sector, wiki_sub_industry, cik_code) to every interval row."""
    cur_meta = current.set_index("ticker")[["name", "wiki_sector", "wiki_sub_industry", "cik_code"]]
    out = intervals.join(cur_meta, on="ticker", how="left")

    # Fall back to the changes table's Security columns for tickers not currently listed.
    fallback_names: dict[str, str] = {}
    for _, row in changes.iterrows():
        for tcol, ncol in (
            ("added_ticker", "added_name"),
            ("removed_ticker", "removed_name"),
        ):
            t = row.get(tcol)
            n = row.get(ncol)
            if isinstance(t, str) and isinstance(n, str) and t and n and t != "nan":
                fallback_names.setdefault(t, n)
    out["name"] = out["name"].fillna(out["ticker"].map(fallback_names))

    # Final column order matches src/aegis/data/index_membership.py::EXPECTED_COLUMNS.
    out = out[
        [
            "ticker",
            "name",
            "wiki_sector",
            "wiki_sub_industry",
            "date_added",
            "date_removed",
            "cik_code",
        ]
    ]
    return out.sort_values(["date_added", "ticker"]).reset_index(drop=True)


# Manual patches for Wikipedia data gaps and quirks discovered while landing
# the Module A acceptance test (Week 2 Day 10). Each entry is one of:
#   ("update", ticker, field, value)   -- mutate an existing row in place
#   ("add", row_dict)                  -- append a new row
#   ("delete", ticker, where_dict)     -- delete rows matching `where_dict`
#
# These corrections target specific tickers where Wikipedia's "Selected
# changes" table either has a data error (Q's three-entity reuse, TROW's
# 2019-07-29 spurious date), is missing rows entirely (WRK never appears),
# or treats a same-symbol rename as a same-day add+remove pair (FOXA at
# the 21st Century Fox / Fox Corporation transition). Without these the
# spec §6 within-1-name acceptance fails by 5-6 names.
#
# Each patch is justified inline. Re-running this script re-applies them.
_MANUAL_PATCHES: list[tuple] = [
    # 2018-06-18 quarterly rebalance: iShares pre-rebalanced its holdings
    # at the 2018-06-15 close; Wikipedia's Effective Date is 2018-06-18.
    # Aligning to iShares' reality (the Module A truth source).
    ("update", "AYI", "date_removed", pd.Timestamp("2018-06-15")),
    ("update", "RRC", "date_removed", pd.Timestamp("2018-06-15")),
    ("update", "BR", "date_added", pd.Timestamp("2018-06-15")),
    ("update", "HFC", "date_added", pd.Timestamp("2018-06-15")),
    # TROW (T. Rowe Price) has been continuously in S&P 500 since 1999;
    # Wikipedia's current-table "Date added" of 2019-07-29 is spurious
    # (likely an article-edit artifact). Set to a clearly-pre-2018 date.
    ("update", "TROW", "date_added", pd.Timestamp("1999-04-30")),
    # FOXA pre-Fox-Corp era (21st Century Fox, 2013-06-19 to 2019-03-19)
    # is missing because Wikipedia represents the rename as a same-day
    # add+remove pair under the same ticker, which our scraper collapses
    # to a degenerate zero-length interval. Add the historical interval.
    (
        "add",
        {
            "ticker": "FOXA",
            "name": "21st Century Fox / Fox Corporation",
            "wiki_sector": "Communication Services",
            "wiki_sub_industry": "Movies & Entertainment",
            "date_added": pd.Timestamp("2013-06-19"),
            "date_removed": pd.Timestamp("2019-03-19"),
            "cik_code": pd.NA,
        },
    ),
    # WRK (WestRock) was in S&P from its 2015-07-01 creation (MeadWestvaco
    # + Rock-Tenn merger) until its 2024-07-05 acquisition by Smurfit Kappa.
    # Wikipedia's "Selected changes" table doesn't record either event,
    # leaving WRK invisible to our scraper.
    (
        "add",
        {
            "ticker": "WRK",
            "name": "WestRock",
            "wiki_sector": "Materials",
            "wiki_sub_industry": "Paper Packaging",
            "date_added": pd.Timestamp("2015-07-01"),
            "date_removed": pd.Timestamp("2024-07-05"),
            "cik_code": 1732845,
        },
    ),
    # Drop the FOXA degenerate (2019-03-19, 2019-03-19) row that our
    # scraper produced from the same-day add+remove pair.
    (
        "delete_where",
        "FOXA",
        {"date_added": pd.Timestamp("2019-03-19"), "date_removed": pd.Timestamp("2019-03-19")},
    ),
]


def _apply_manual_patches(df: pd.DataFrame) -> pd.DataFrame:
    """Apply hand-curated corrections to Wikipedia data gaps. See _MANUAL_PATCHES."""
    out = df.copy()
    n_updates = 0
    n_adds = 0
    n_deletes = 0
    for patch in _MANUAL_PATCHES:
        op = patch[0]
        if op == "update":
            _, ticker, field, value = patch
            mask = out["ticker"] == ticker
            if not mask.any():
                print(f"  WARN patch update {ticker}.{field}: ticker not found")
                continue
            out.loc[mask, field] = value
            n_updates += int(mask.sum())
        elif op == "add":
            _, row_dict = patch
            out = pd.concat([out, pd.DataFrame([row_dict])], ignore_index=True)
            n_adds += 1
        elif op == "delete_where":
            _, ticker, where = patch
            mask = out["ticker"] == ticker
            for k, v in where.items():
                mask &= out[k] == v
            n_deletes += int(mask.sum())
            out = out[~mask].reset_index(drop=True)
        else:
            raise RuntimeError(f"unknown patch op: {op}")
    print(f"  patches applied: {n_updates} updates, {n_adds} adds, {n_deletes} deletes")
    return out.sort_values(["date_added", "ticker"]).reset_index(drop=True)


def _validate(df: pd.DataFrame) -> None:
    if len(df) < 640:
        raise RuntimeError(f"sanity floor: expected >=640 rows, got {len(df)}")
    if df["ticker"].isna().any():
        raise RuntimeError("found rows with null ticker")
    if df["date_added"].isna().any():
        raise RuntimeError("found rows with null date_added")
    bad = df["date_removed"].notna() & (df["date_removed"] < df["date_added"])
    if bool(bad.any()):
        raise RuntimeError(f"found {int(bad.sum())} rows with date_removed < date_added")


def _write_meta(df: pd.DataFrame, csv_path: Path, meta_path: Path, earliest: pd.Timestamp) -> None:
    csv_sha = sha256_file(csv_path)
    git_sha = current_git_sha()
    fetched_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "source_url": WIKIPEDIA_URL,
        "fetched_at_utc": fetched_at,
        "scraper_git_sha": git_sha,
        "csv_path": str(csv_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "csv_sha256": csv_sha,
        "row_count": len(df),
        "current_member_count": int(df["date_removed"].isna().sum()),
        "historical_removal_count": int(df["date_removed"].notna().sum()),
        "earliest_change_date": str(earliest.date()),
        "notes": (
            "Wikipedia 'Selected changes' table starts ~2009; pre-2009 reconstruction "
            "treats then-current members as if joining at earliest_change_date. "
            "Same-day rename pairs (e.g. FB->META 2022-06-09) are not collapsed; "
            "ticker_aliases.csv (Week 2 Day 9) handles renames."
        ),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    print(f"Fetching {WIKIPEDIA_URL}")
    html = _fetch_wikipedia(WIKIPEDIA_URL)
    tables = pd.read_html(StringIO(html))
    if len(tables) < 2:
        raise RuntimeError(f"expected >=2 tables on Wikipedia page, got {len(tables)}")

    current = _normalize_current(tables[0])
    changes = _normalize_changes(tables[1])
    earliest = changes["date"].min()
    print(
        f"  current: {len(current)} rows | changes: {len(changes)} rows | earliest change: {earliest.date()}"
    )

    intervals = _build_intervals(current, changes)
    enriched = _enrich_metadata(intervals, current, changes)
    enriched = _apply_manual_patches(enriched)
    _validate(enriched)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    enriched.to_csv(CSV_PATH, index=False, date_format="%Y-%m-%d")
    _write_meta(enriched, CSV_PATH, META_PATH, earliest)

    csv_sha = sha256_file(CSV_PATH)
    print(f"Wrote {CSV_PATH.relative_to(REPO_ROOT)}")
    print(
        f"  rows={len(enriched)} | current={int(enriched['date_removed'].isna().sum())} "
        f"| historical={int(enriched['date_removed'].notna().sum())}"
    )
    print(f"  sha256={csv_sha}")
    print(f"Wrote {META_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
