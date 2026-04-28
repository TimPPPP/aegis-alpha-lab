"""Widened-universe backtest slice (Week 2 Day 13).

Composes Day 8's date-aware S&P 500 membership reconstruction with the
existing Week 1 factor pipeline:

    sp500_membership.csv → active_on(sample_date) → ~500 tickers
        → build_panel(tickers) → Momentum12m1m → ledger writes

The experiment row is named ``week2_full_universe_<YYYY-MM-DD>`` so a
glance at the ledger tells you which sample-date-anchored universe the
slice covers. Re-runs append; the ledger is append-only.

Polygon Starter (100 calls/min) handles the ~1500-call live run for ~500
tickers in 15-25 minutes. The CLI uses 0.6s sleep by default (50% of the
ceiling); ``--fast`` drops sleep to 0 for users on a higher tier.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from aegis.backtest._common import SliceResult, _run_factor_slice
from aegis.config import AegisConfig
from aegis.data.index_membership import load_sp500_membership
from aegis.data.ticker_reference import (
    load_ticker_aliases,
    load_ticker_metadata,
    resolve_sp500_universe_for_date,
)

EXPERIMENT_NAME_PREFIX: str = "week2_full_universe"


def run_full_slice(
    cfg: AegisConfig,
    ledger_path: Path,
    sample_date: date,
    *,
    sleep_between_calls: float = 12.5,
) -> SliceResult:
    """Run a Week 1-style slice over the S&P 500 universe active on ``sample_date``.

    The sample date drives the membership cutoff only — the panel itself
    spans ``cfg.data.date_range`` (e.g. 2024-06-01 → 2026-03-31), pulled
    against the ~500 tickers active on the cutoff. Day 13's live run
    targets ``sample_date=2025-06-15``.

    Args:
        cfg: Validated :class:`AegisConfig`. Reads
            ``cfg.data.paths.reference`` for the membership CSV location
            and ``cfg.data.paths.processed`` for Parquet output.
        ledger_path: SQLite path for the research ledger. Created if missing.
        sample_date: Trading day used to anchor the S&P 500 universe via
            :func:`aegis.data.index_membership.active_on`. The membership
            reconstruction must cover this date (Wikipedia ``Selected
            changes`` post-2009 + manual patches).
        sleep_between_calls: Seconds between Polygon API calls. Default
            12.5 is free-tier safe; the CLI passes 0.6 (Polygon Starter).

    Returns:
        :class:`SliceResult` with UUIDs, paths, hashes, and row counts.

    Raises:
        RuntimeError: If ``active_on(sample_date, …)`` returns 0 tickers
            (e.g. the membership CSV doesn't cover ``sample_date``).
    """
    reference_path = cfg.data.paths.reference
    membership_path = reference_path / "sp500_membership.csv"
    metadata_path = reference_path / "ticker_metadata.parquet"
    aliases_path = reference_path / "ticker_aliases.csv"

    membership = load_sp500_membership(membership_path)
    metadata = load_ticker_metadata(metadata_path)
    aliases = load_ticker_aliases(aliases_path)
    universe = resolve_sp500_universe_for_date(sample_date, membership, metadata, aliases)
    if not universe.tickers:
        raise RuntimeError(
            f"resolve_sp500_universe_for_date({sample_date}) returned 0 tradable "
            f"tickers from {membership_path}, {metadata_path}, and {aliases_path}"
        )

    experiment_name = f"{EXPERIMENT_NAME_PREFIX}_{sample_date.isoformat()}"

    # Date-tagged filenames so the full-universe artifacts don't overwrite
    # Week 1's at ``daily_panel_week1.parquet`` (which would invalidate the
    # Week 1 ledger row's recorded checksums).
    iso = sample_date.isoformat()
    panel_filename = f"daily_panel_full_{iso}.parquet"
    factor_filename = f"factor_mom_12_1_full_{iso}.parquet"

    return _run_factor_slice(
        cfg,
        ledger_path,
        tickers=universe.tickers,
        experiment_name=experiment_name,
        sleep_between_calls=sleep_between_calls,
        panel_filename=panel_filename,
        factor_filename=factor_filename,
        metadata_as_of=sample_date,
    )


__all__ = ["EXPERIMENT_NAME_PREFIX", "run_full_slice"]
