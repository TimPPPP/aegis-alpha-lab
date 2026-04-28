"""Week 1 vertical-slice pipeline (spec §6 A+C end-to-end, Week 1 Day 6).

Thin wrapper around :func:`aegis.backtest._common._run_factor_slice` that
pins the Week 1 ticker universe (:data:`aegis.data.panel.WEEK1_TICKERS`)
and the experiment name. The Week 2 Day 13 ``run_full_slice`` consumes the
same helper with a date-aware S&P 500 universe.

After ``run_week1_slice`` returns, the filesystem has:
  * ``data/processed/daily_panel_week1.parquet`` (Module A output)
  * ``data/processed/factor_mom_12_1_week1.parquet`` (Module C output)

And the ledger has three rows:
  * one ``experiments`` row stamped with ``(config_hash, git_sha)``
  * one ``candidates`` row (status ``"computed"``, linked to the experiment)
  * two ``artifacts`` rows (panel + factor, linked to the candidate)

Re-runs append new experiment/candidate/artifact rows — the ledger is
append-only per spec principle 5. Re-running with unchanged code + config +
Polygon snapshot produces identical ``config_hash`` and ``data_snapshot_id``
values across runs, but unique UUIDs + timestamps.
"""

from __future__ import annotations

from pathlib import Path

from aegis.backtest._common import SliceResult, _run_factor_slice
from aegis.config import AegisConfig
from aegis.data.panel import WEEK1_TICKERS

EXPERIMENT_NAME: str = "week1_vertical_slice"

# Backward-compat alias: Week 1 callers / tests imported ``Week1SliceResult``
# directly. The shape is unchanged — both names point at the same dataclass.
Week1SliceResult = SliceResult


def run_week1_slice(
    cfg: AegisConfig,
    ledger_path: Path,
    sleep_between_calls: float = 12.5,
) -> SliceResult:
    """Run the Week 1 vertical slice end-to-end.

    Args:
        cfg: Validated :class:`AegisConfig` — drives data paths, universe
            rules, and the config_hash stamped into every ledger row.
        ledger_path: SQLite path for the research ledger. Created if missing.
            ``Path(":memory:")`` is supported for tests.
        sleep_between_calls: Seconds between Polygon API calls. Default 12.5
            is free-tier safe. Paid-tier callers pass 0.

    Returns:
        :class:`SliceResult` with UUIDs, paths, hashes, and row counts.
    """
    return _run_factor_slice(
        cfg,
        ledger_path,
        tickers=WEEK1_TICKERS,
        experiment_name=EXPERIMENT_NAME,
        sleep_between_calls=sleep_between_calls,
    )


__all__ = ["EXPERIMENT_NAME", "SliceResult", "Week1SliceResult", "run_week1_slice"]
