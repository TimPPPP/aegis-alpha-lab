"""Shared factor-slice helper for the Week 1 / Week 2 backtest commands.

``run_week1_slice`` (Week 1 Day 6) and ``run_full_slice`` (Week 2 Day 13)
overlap on ~80% of their work — pull a panel for some ticker list, compute
``Momentum12m1m``, write both Parquets, and stamp three ledger rows. The
only differences are the ticker source (a hardcoded blue-chip tuple vs a
date-aware S&P 500 reconstruction) and the experiment-name string.

Lifting that 80% into ``_run_factor_slice`` keeps both callers as 5-line
wrappers and removes the divergent-twin maintenance hazard. The leading
underscore signals "package-internal helper, not part of the public API".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import UUID

import pandas as pd

from aegis.config import AegisConfig
from aegis.data.panel import build_panel
from aegis.features.base import FactorContext, write_factor_parquet
from aegis.features.momentum import Momentum12m1m
from aegis.ledger import (
    open_ledger,
    register_artifact,
    register_candidate,
    register_experiment,
)
from aegis.utils.git import current_git_sha
from aegis.utils.hashing import sha256_file


@dataclass(frozen=True)
class SliceResult:
    """Provenance bundle returned by one factor-slice invocation.

    Identical shape to the legacy ``Week1SliceResult`` (which is now a type
    alias for ``SliceResult``). Carries every UUID, path, and hash callers
    or downstream verify-mode replay calls might need.
    """

    experiment_id: UUID
    candidate_id: UUID
    panel_artifact_id: UUID
    factor_artifact_id: UUID
    panel_path: Path
    factor_path: Path
    panel_checksum: str
    factor_checksum: str
    data_snapshot_id: str
    config_hash: str
    git_sha: str
    panel_rows: int
    factor_valid_rows: int
    factor_tradable_rows: int


def _run_factor_slice(
    cfg: AegisConfig,
    ledger_path: Path,
    *,
    tickers: Sequence[str],
    experiment_name: str,
    sleep_between_calls: float,
    panel_filename: str | None = None,
    factor_filename: str | None = None,
    metadata_as_of: date | None = None,
    require_all_tickers: bool = False,
) -> SliceResult:
    """Build panel for ``tickers``, compute mom_12_1, write artifacts, ledger.

    Internal — callers are :func:`aegis.backtest.week1.run_week1_slice` and
    :func:`aegis.backtest.full.run_full_slice`. The function is intentionally
    side-effectful: writes two Parquets to ``cfg.data.paths.processed`` and
    appends three rows to the ledger SQLite at ``ledger_path``.

    ``panel_filename`` and ``factor_filename`` default to the
    ``cfg.data.snapshot.*`` values (Week 1's filenames), but Week 2's
    full-universe slice overrides them so the two slices' artifacts don't
    collide on disk and break each other's ledger checksums.
    """
    panel_basename = panel_filename or cfg.data.snapshot.panel_filename
    factor_basename = factor_filename or cfg.data.snapshot.factor_filename

    # 1. Pull + filter + write panel (Day 3 plumbing; tickers parameter from Day 10).
    panel_path = build_panel(
        cfg,
        tickers=list(tickers),
        sleep_between_calls=sleep_between_calls,
        panel_filename=panel_basename,
        metadata_as_of=metadata_as_of,
        require_all_tickers=require_all_tickers,
    )
    panel = pd.read_parquet(panel_path)
    data_snapshot_id = str(panel["data_snapshot_id"].iloc[0])
    panel_rows = len(panel)

    # 2. Compute 12-1 momentum (Day 5).
    factor = Momentum12m1m()
    context = FactorContext()
    factor_out = factor.compute(panel, context=context)
    factor_valid_rows = int(factor_out["valid_flag"].sum())
    factor_tradable_rows = int(factor_out["tradable_flag"].sum())

    # 3. Write factor Parquet alongside the panel, embedding per-factor
    # diagnostics in pyarrow schema metadata (Day 17).
    factor_path = cfg.data.paths.processed / factor_basename
    factor_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics = factor.diagnostics(factor_out, context=context)
    write_factor_parquet(factor_out, factor_path, diagnostics)

    # 4. Hash both artifacts (bytes on disk, post-write).
    panel_checksum = sha256_file(panel_path)
    factor_checksum = sha256_file(factor_path)

    # 5. Register provenance in the ledger (Day 4).
    config_hash = cfg.content_hash()
    git_sha = current_git_sha(require_clean=True)

    with open_ledger(ledger_path) as session:
        experiment_id = register_experiment(
            session,
            name=experiment_name,
            config_hash=config_hash,
            git_sha=git_sha,
        )
        candidate_id = register_candidate(
            session,
            experiment_id=experiment_id,
            candidate_name=factor.name,
            formula_string=factor.formula,
            data_snapshot_id=data_snapshot_id,
            status="computed",
        )
        panel_artifact_id = register_artifact(
            session,
            candidate_id=candidate_id,
            artifact_type="panel",
            path=panel_path,
            checksum=panel_checksum,
        )
        factor_artifact_id = register_artifact(
            session,
            candidate_id=candidate_id,
            artifact_type="factor",
            path=factor_path,
            checksum=factor_checksum,
        )

    return SliceResult(
        experiment_id=experiment_id,
        candidate_id=candidate_id,
        panel_artifact_id=panel_artifact_id,
        factor_artifact_id=factor_artifact_id,
        panel_path=panel_path,
        factor_path=factor_path,
        panel_checksum=panel_checksum,
        factor_checksum=factor_checksum,
        data_snapshot_id=data_snapshot_id,
        config_hash=config_hash,
        git_sha=git_sha,
        panel_rows=panel_rows,
        factor_valid_rows=factor_valid_rows,
        factor_tradable_rows=factor_tradable_rows,
    )


__all__ = ["SliceResult", "_run_factor_slice"]
