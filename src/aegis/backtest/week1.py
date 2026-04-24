"""Week 1 vertical-slice pipeline (spec §6 A+C end-to-end, Week 1 Day 6).

Orchestrates the spine:

    configs → build_panel → Momentum12m1m.compute → Parquet writes → ledger rows

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

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pandas as pd

from aegis.config import AegisConfig
from aegis.data.panel import build_panel
from aegis.features.momentum import Momentum12m1m
from aegis.ledger import (
    open_ledger,
    register_artifact,
    register_candidate,
    register_experiment,
)
from aegis.utils.git import current_git_sha
from aegis.utils.hashing import sha256_file

EXPERIMENT_NAME: str = "week1_vertical_slice"


@dataclass(frozen=True)
class Week1SliceResult:
    """Summary of one ``run_week1_slice`` invocation.

    The CLI formats this into a human-readable block; tests assert on it
    directly rather than poking at the ledger.
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


def run_week1_slice(
    cfg: AegisConfig,
    ledger_path: Path,
    sleep_between_calls: float = 12.5,
) -> Week1SliceResult:
    """Run the Week 1 vertical slice end-to-end.

    Args:
        cfg: Validated :class:`AegisConfig` — drives data paths, universe
            rules, and the config_hash stamped into every ledger row.
        ledger_path: SQLite path for the research ledger. Created if missing.
            ``Path(":memory:")`` is supported for tests.
        sleep_between_calls: Seconds between Polygon API calls. Default 12.5
            is free-tier safe. Paid-tier callers pass 0.

    Returns:
        :class:`Week1SliceResult` with UUIDs, paths, hashes, and row counts.
    """
    # 1. Pull + filter + write panel (Day 3 plumbing).
    panel_path = build_panel(cfg, sleep_between_calls=sleep_between_calls)
    panel = pd.read_parquet(panel_path)

    # data_snapshot_id is constant across all rows of the panel (Day 3 invariant).
    data_snapshot_id = str(panel["data_snapshot_id"].iloc[0])
    panel_rows = len(panel)

    # 2. Compute 12-1 momentum (Day 5).
    factor = Momentum12m1m()
    factor_out = factor.compute(panel)
    factor_valid_rows = int(factor_out["valid_flag"].sum())

    # 3. Write factor Parquet alongside the panel.
    factor_path = cfg.data.paths.processed / cfg.data.snapshot.factor_filename
    factor_path.parent.mkdir(parents=True, exist_ok=True)
    factor_out.to_parquet(factor_path, index=False)

    # 4. Hash both artifacts (bytes on disk, post-write).
    panel_checksum = sha256_file(panel_path)
    factor_checksum = sha256_file(factor_path)

    # 5. Register provenance in the ledger (Day 4).
    config_hash = cfg.content_hash()
    git_sha = current_git_sha()

    with open_ledger(ledger_path) as session:
        experiment_id = register_experiment(
            session,
            name=EXPERIMENT_NAME,
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

    return Week1SliceResult(
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
    )


__all__ = ["EXPERIMENT_NAME", "Week1SliceResult", "run_week1_slice"]
