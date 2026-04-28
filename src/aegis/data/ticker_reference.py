"""Per-ticker tradability cache + rename reconciliation (Week 2 Day 9).

Composes with :mod:`aegis.data.index_membership` (Day 8). The Day 8 layer
answers "was this ticker in the S&P 500 on date t?"; this module answers
"was this *specific* ticker actually listed and trading on date t, or had
it already delisted?". Together they form the survivorship-bias-reduced
universe filter that Day 10 will compose into ``build_panel_for_date``.

Spec compliance
---------------

- **§4.1 σ-algebra measurability.** :func:`is_active_on` is filtration-
  measurable: ``(list_date <= t) & (delisted_date.isna() | (t < delisted_date))``.
  Future delistings are excluded from the active set by the predicate, not
  by the data layout. Same boundary semantics as
  :func:`aegis.data.index_membership.active_on`.

- **§7 Sample filters.** This module ships only the *per-ticker tradability*
  gate. The §7 common-share / exchange / price / history filters live in
  :func:`aegis.data.universe.build_universe_flags` and compose downstream.

- **Principle 5 — Auditability.** ``ticker_metadata.parquet`` is regenerated
  from Polygon by :mod:`scripts.fetch_polygon_ticker_reference`; a sibling
  ``ticker_metadata.meta.json`` records source URL, fetch timestamp, and
  parquet sha256. ``ticker_aliases.csv`` is checked into git (small, curated).

Coverage caveats
----------------

- ``ticker_aliases.csv`` is small by design (~10–20 entries, capped). Only
  high-impact renames among S&P 500 members are seeded. Acquisition-driven
  ticker terminations are NOT aliases (the lineage ends; the acquirer is a
  different entity). ADRs are filtered upstream by §7's common-share gate.

- ``canonicalize_ticker`` resolves a symbol to whatever it was called *as
  of* a specific date — useful for both "what was ANTM in 2018?" (returns
  ``ANTM``) and "what is ANTM today?" (pass ``date=today`` → returns
  ``ELV``). Independent of S&P 500 membership.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from aegis.data.index_membership import active_on

METADATA_COLUMNS: tuple[str, ...] = (
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

ALIAS_COLUMNS: tuple[str, ...] = (
    "canonical_ticker",
    "alias",
    "effective_from",
    "effective_to",
    "note",
)

DROP_METADATA_MISSING = "metadata_missing"
DROP_NOT_ACTIVE_ON_DATE = "not_active_on_date"


@dataclass(frozen=True)
class ResolvedUniverse:
    """Tradable ticker list resolved from date-aware membership.

    ``requested_count`` is the raw S&P membership count before symbol
    reconciliation and tradability checks. ``tickers`` is the deterministic
    Polygon query list after alias resolution and metadata gating.
    ``dropped`` records fail-closed decisions as ``(membership_ticker,
    reason)`` for auditability.
    """

    tickers: tuple[str, ...]
    requested_count: int
    dropped: tuple[tuple[str, str], ...]


def load_ticker_metadata(path: Path) -> pd.DataFrame:
    """Load the Polygon-derived ticker reference Parquet.

    Validates that the columns match :data:`METADATA_COLUMNS` exactly.
    ``list_date`` and ``delisted_date`` are returned as ``datetime64[ns]``
    with NaT for unknown / still-listed.
    """
    df = pd.read_parquet(path)
    actual = tuple(df.columns)
    if actual != METADATA_COLUMNS:
        raise ValueError(
            f"ticker_metadata header mismatch: expected {METADATA_COLUMNS}, got {actual}"
        )
    # parquet round-trips datetimes; ensure they're proper Timestamps.
    for col in ("list_date", "delisted_date"):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def load_ticker_aliases(path: Path) -> pd.DataFrame:
    """Load the hand-curated ticker-rename CSV.

    Validates columns. Empty cells in ``effective_from`` / ``effective_to``
    parse to NaT — meaning "from the beginning" / "still active",
    respectively.
    """
    df = pd.read_csv(path, parse_dates=["effective_from", "effective_to"])
    actual = tuple(df.columns)
    if actual != ALIAS_COLUMNS:
        raise ValueError(f"ticker_aliases header mismatch: expected {ALIAS_COLUMNS}, got {actual}")
    df["note"] = df["note"].fillna("").astype(str)
    return df


def is_active_on(
    ticker: str,
    check_date: date,
    metadata: pd.DataFrame,
) -> bool:
    """Was ``ticker`` listed and trading on ``check_date``?

    Boundary semantics match :func:`aegis.data.index_membership.active_on`:
    inclusive at ``list_date``, exclusive at ``delisted_date``. Per spec
    §4.1, the predicate uses only F_t-measurable rows; future-dated
    list/delist events are filtered out by the predicate.

    Returns False for unknown tickers (not in ``metadata``) so the gate
    fails closed — better to drop a name than to silently include
    something we don't have provenance for.
    """
    ts = pd.Timestamp(check_date)
    rows = metadata[metadata["ticker"] == ticker]
    if rows.empty:
        return False
    # Any row whose interval contains ts qualifies. Polygon gives one row
    # per ticker, so this is effectively a single-row check, but the
    # any() form is robust to future schema changes.
    listed_ok = rows["list_date"].isna() | (rows["list_date"] <= ts)
    not_delisted = rows["delisted_date"].isna() | (ts < rows["delisted_date"])
    return bool((listed_ok & not_delisted).any())


def canonicalize_ticker(
    ticker: str,
    check_date: date,
    aliases: pd.DataFrame,
) -> str:
    """Return the symbol the ``ticker``'s lineage was traded under on ``check_date``.

    Walks the alias chain forward up to ``check_date`` and returns whichever
    name was active then. Examples:

    - ``canonicalize_ticker("FB", date(2018, 6, 15), aliases) == "FB"``
      (the FB→META rename happened 2022-06-09, so on 2018-06-15 the
      symbol was still FB).
    - ``canonicalize_ticker("FB", date(2023, 1, 1), aliases) == "META"``.
    - ``canonicalize_ticker("WLP", date(2018, 6, 15), aliases) == "ANTM"``
      (chained: WLP→ANTM in 2014, ANTM→ELV in 2022).
    - ``canonicalize_ticker("AAPL", any_date, aliases) == "AAPL"``
      (no alias rows; identity).

    The function is *symbol-resolution*, not S&P-membership: a ticker can
    be canonicalized even if neither the alias nor the canonical is
    currently in the index.
    """
    ts = pd.Timestamp(check_date)

    # Step 1: find the canonical for `ticker`. The ticker may itself be a
    # canonical (no row with alias=ticker) or an alias (one or more rows).
    # All rows with alias=ticker share the same canonical (one entity).
    alias_rows = aliases[aliases["alias"] == ticker]
    canonical = ticker if alias_rows.empty else str(alias_rows["canonical_ticker"].iloc[0])

    # Step 2: among all rows for this canonical, find the one whose
    # interval contains check_date. NaT effective_from is treated as
    # -infinity; NaT effective_to as +infinity.
    canonical_rows = aliases[aliases["canonical_ticker"] == canonical]
    if canonical_rows.empty:
        return canonical

    from_ok = canonical_rows["effective_from"].isna() | (canonical_rows["effective_from"] <= ts)
    to_ok = canonical_rows["effective_to"].isna() | (ts < canonical_rows["effective_to"])
    matches = canonical_rows[from_ok & to_ok]

    if matches.empty:
        # date is in the "current" period (after the latest aliased rename);
        # the canonical itself is the active symbol.
        return canonical
    if len(matches) > 1:
        # Overlapping intervals — alias table is malformed. Fail loudly.
        raise ValueError(
            f"canonicalize_ticker({ticker!r}, {check_date}): overlapping alias "
            f"intervals: {matches[['alias', 'effective_from', 'effective_to']].to_dict('records')}"
        )
    return str(matches["alias"].iloc[0])


def sector_for(ticker: str, metadata: pd.DataFrame) -> str | None:
    """Return Polygon's SIC description for ``ticker`` (or None if absent).

    This is a thin lookup, not a sector mapping. Week 3's sector-proxy
    enrichment will translate SIC codes → coarse sectors and rename the
    panel column from ``gics_sector`` to ``sector_proxy``. Day 9 just
    surfaces the raw SIC string.
    """
    rows = metadata[metadata["ticker"] == ticker]
    if rows.empty:
        return None
    val = rows["sic_description"].iloc[0]
    if pd.isna(val):
        return None
    return str(val)


def resolve_sp500_universe_for_date(
    sample_date: date,
    membership: pd.DataFrame,
    metadata: pd.DataFrame,
    aliases: pd.DataFrame,
) -> ResolvedUniverse:
    """Resolve S&P 500 membership into a date-tradable Polygon ticker list.

    Resolution order:
    1. ``active_on(sample_date, membership)`` supplies the raw index names.
    2. ``canonicalize_ticker`` maps current/canonical symbols back to the
       symbol traded on ``sample_date`` where a continuing lineage has a
       known ticker rename.
    3. ``is_active_on`` verifies the resolved ticker was listed on the date.

    Missing metadata, future-listed tickers, delisted tickers, and ambiguous
    ticker reuse all fail closed. If two membership rows collapse to the same
    resolved ticker, the alias table is ambiguous and the function raises
    rather than silently double-counting a security.
    """
    requested = tuple(sorted(active_on(sample_date, membership)))
    resolved_by_source: dict[str, str] = {}
    source_by_resolved: dict[str, str] = {}
    dropped: list[tuple[str, str]] = []

    metadata_tickers = set(metadata["ticker"].astype(str))

    for ticker in requested:
        resolved = canonicalize_ticker(ticker, sample_date, aliases)
        if resolved not in metadata_tickers:
            dropped.append((ticker, f"{DROP_METADATA_MISSING}:{resolved}"))
            continue
        if not is_active_on(resolved, sample_date, metadata):
            dropped.append((ticker, f"{DROP_NOT_ACTIVE_ON_DATE}:{resolved}"))
            continue

        prior_source = source_by_resolved.get(resolved)
        if prior_source is not None and prior_source != ticker:
            raise ValueError(
                "alias collision while resolving S&P 500 universe for "
                f"{sample_date}: {prior_source!r} and {ticker!r} both resolve to {resolved!r}"
            )
        source_by_resolved[resolved] = ticker
        resolved_by_source[ticker] = resolved

    return ResolvedUniverse(
        tickers=tuple(sorted(resolved_by_source.values())),
        requested_count=len(requested),
        dropped=tuple(sorted(dropped)),
    )


__all__ = [
    "ALIAS_COLUMNS",
    "DROP_METADATA_MISSING",
    "DROP_NOT_ACTIVE_ON_DATE",
    "METADATA_COLUMNS",
    "ResolvedUniverse",
    "canonicalize_ticker",
    "is_active_on",
    "load_ticker_aliases",
    "load_ticker_metadata",
    "resolve_sp500_universe_for_date",
    "sector_for",
]
