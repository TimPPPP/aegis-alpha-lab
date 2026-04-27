"""Date-aware S&P 500 index membership reconstructed from Wikipedia history.

Spec compliance
---------------

- **§4.1 σ-algebra measurability.** :func:`active_on` is filtration-
  measurable: the predicate is ``(date_added <= t) & (date_removed.isna()
  | (t < date_removed))``, which references only the date ``t`` and rows
  whose ``date_added`` is in ``F_t``. Future additions/removals are
  filtered out by the predicate, even though the underlying CSV records
  them. The function is therefore safe to call inside a backtest loop
  without leaking forward information.

- **§6 Module A acceptance.** ``active_on(t, …) == published constituents
  at t`` within a 1-name tolerance. Day 10 of Week 2 flips the
  corresponding xfail in :mod:`tests.unit.test_panel` against two
  ground-truth dates.

- **§7 Sample filters.** This module ships only the *index-membership*
  gate. The §7 sample filters (common shares, exchange, price ≥ $5,
  ≥252-day history) live in :func:`aegis.data.universe.build_universe_flags`
  and compose downstream. The eventual composition order is
  ``membership ∩ common-share ∩ exchange ∩ price ∩ history``.

- **Principle 5 — Auditability.** The CSV is checked into git; the
  sidecar ``sp500_membership.meta.json`` records source URL, fetch
  timestamp, scraper git SHA, and the CSV's sha256. Re-running the
  scraper without Wikipedia changes yields a byte-identical CSV.

Coverage caveats
----------------

- Wikipedia's "Selected changes" table starts ≈2009. Pre-2009
  reconstruction silently treats then-current members as if they joined
  at the start of the table. Backtests over 2000–2008 are best-effort.

- Universe scope is S&P 500 (~500 large-caps), narrower than the spec §7
  target of ~3,000 common-share names. This is a Week 2 stepping-stone;
  a broader universe is a later-week deliverable that composes by
  adding additional membership tables (e.g. Russell 1000) on top of the
  same primitive.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

EXPECTED_COLUMNS: tuple[str, ...] = (
    "ticker",
    "name",
    "wiki_sector",
    "wiki_sub_industry",
    "date_added",
    "date_removed",
    "cik_code",
)


def load_sp500_membership(path: Path) -> pd.DataFrame:
    """Load the historical S&P 500 membership CSV.

    Parses ``date_added`` and ``date_removed`` as ``datetime64[ns]`` (NaT
    for current members). Validates the column header against
    :data:`EXPECTED_COLUMNS`.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    ValueError
        If the CSV header does not match :data:`EXPECTED_COLUMNS` exactly.
    """
    df = pd.read_csv(path, parse_dates=["date_added", "date_removed"])
    actual = tuple(df.columns)
    if actual != EXPECTED_COLUMNS:
        raise ValueError(
            f"sp500_membership.csv header mismatch: expected {EXPECTED_COLUMNS}, got {actual}"
        )
    return df


def active_on(check_date: date, membership: pd.DataFrame) -> set[str]:
    """Return the set of S&P 500 tickers active on ``check_date``.

    A ticker is active iff at least one of its membership intervals
    contains ``check_date``: ``date_added <= check_date`` and either
    ``date_removed`` is null (still a member) or
    ``check_date < date_removed`` (the removal date is the first day the
    ticker is *no longer* a member).

    Per spec §4.1, the predicate uses only information measurable with
    respect to ``F_{check_date}``: future additions and future removals
    are excluded by the inequalities, never by the data layout.
    """
    ts = pd.Timestamp(check_date)
    mask = (membership["date_added"] <= ts) & (
        membership["date_removed"].isna() | (ts < membership["date_removed"])
    )
    return set(membership.loc[mask, "ticker"].astype(str))


def membership_window(
    start: date,
    end: date,
    membership: pd.DataFrame,
) -> pd.DataFrame:
    """Return a long-format DataFrame of (date, ticker) over a trading-day range.

    ``start`` and ``end`` are inclusive. Only business days are emitted
    (``pd.bdate_range``); the underlying ``active_on`` predicate is
    evaluated per date. Useful for joining onto the daily panel to
    restrict it to date-aware S&P 500 membership.

    Returns a frame with columns ``["date", "ticker"]``, sorted
    deterministically by ``(date, ticker)``.
    """
    if end < start:
        raise ValueError(f"membership_window: end ({end}) precedes start ({start})")

    rows: list[pd.DataFrame] = []
    for ts in pd.bdate_range(start, end):
        py_date: date = ts.date()
        tickers = active_on(py_date, membership)
        if not tickers:
            continue
        rows.append(
            pd.DataFrame(
                {"date": pd.Timestamp(py_date), "ticker": sorted(tickers)},
            )
        )
    if not rows:
        return pd.DataFrame(
            {"date": pd.Series(dtype="datetime64[ns]"), "ticker": pd.Series(dtype="object")}
        )
    out = pd.concat(rows, ignore_index=True)
    return out.sort_values(["date", "ticker"]).reset_index(drop=True)


__all__ = [
    "EXPECTED_COLUMNS",
    "active_on",
    "load_sp500_membership",
    "membership_window",
]
