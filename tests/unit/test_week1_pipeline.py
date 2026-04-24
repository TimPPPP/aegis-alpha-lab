"""Day 6 unit coverage for the Week 1 vertical-slice pipeline.

Polygon-free by construction: monkey-patches ``build_panel`` inside
:mod:`aegis.backtest.week1` so the loader isn't hit. The rest of the
pipeline — factor compute, Parquet writes, ledger inserts — runs for real
against the engineered ``stock_daily_panel`` fixture and an on-disk
SQLite.

Implementation note: SQLAlchemy ORM instances become detached when their
session closes, so each test pulls plain values out of the session inside
the ``with open_ledger(...)`` block and asserts on those afterward.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from aegis.backtest import week1 as week1_module
from aegis.backtest.week1 import Week1SliceResult, run_week1_slice
from aegis.config import AegisConfig, load_all
from aegis.data.panel import _finalize_panel
from aegis.ledger.schema import Artifact, Candidate, Experiment
from aegis.ledger.store import open_ledger
from aegis.utils.hashing import sha256_file

# Deterministic git SHA stamped by the pipeline in tests (no subprocess).
FAKE_GIT_SHA: str = "a1b2c3d4e5f6"


@dataclass(frozen=True)
class _LedgerSnapshot:
    """Plain-value snapshot of the ledger at one moment, safe to use post-session."""

    experiments: list[dict[str, str]]
    candidates: list[dict[str, str]]
    artifacts: list[dict[str, str]]


def _snapshot(ledger_path: Path) -> _LedgerSnapshot:
    """Read every row into a dict, avoiding DetachedInstanceError."""
    with open_ledger(ledger_path) as session:
        experiments = [
            {
                "experiment_id": e.experiment_id,
                "name": e.name,
                "config_hash": e.config_hash,
                "git_sha": e.git_sha,
            }
            for e in session.execute(select(Experiment)).scalars().all()
        ]
        candidates = [
            {
                "candidate_id": c.candidate_id,
                "experiment_id": c.experiment_id,
                "candidate_name": c.candidate_name,
                "candidate_type": c.candidate_type,
                "formula_string": c.formula_string,
                "data_snapshot_id": c.data_snapshot_id,
                "status": c.status,
            }
            for c in session.execute(select(Candidate)).scalars().all()
        ]
        artifacts = [
            {
                "artifact_id": a.artifact_id,
                "candidate_id": a.candidate_id,
                "artifact_type": a.artifact_type,
                "path": a.path,
                "checksum": a.checksum,
            }
            for a in session.execute(select(Artifact)).scalars().all()
        ]
    return _LedgerSnapshot(experiments=experiments, candidates=candidates, artifacts=artifacts)


@pytest.fixture
def pipeline_fixture(
    stock_daily_panel: pd.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AegisConfig, Path]:
    """Isolated pipeline invocation: redirected paths, mocked loader + git."""
    cfg = load_all()

    test_cfg = cfg.model_copy(
        update={
            "data": cfg.data.model_copy(
                update={
                    "paths": cfg.data.paths.model_copy(update={"processed": tmp_path}),
                }
            )
        }
    )

    # Pre-finalize the fixture through Day 3's pipeline → writes the panel
    # Parquet exactly where build_panel would write it.
    finalized = _finalize_panel(stock_daily_panel, test_cfg)
    panel_path = tmp_path / test_cfg.data.snapshot.panel_filename
    finalized.to_parquet(panel_path, index=False)

    def _fake_build_panel(cfg: AegisConfig, *, sleep_between_calls: float = 0.0) -> Path:
        return panel_path

    monkeypatch.setattr(week1_module, "build_panel", _fake_build_panel)
    monkeypatch.setattr(week1_module, "current_git_sha", lambda: FAKE_GIT_SHA)

    ledger_path = tmp_path / "ledger.sqlite"
    return test_cfg, ledger_path


# --- Coverage tests ----------------------------------------------------------
def test_run_week1_slice_writes_both_parquets(
    pipeline_fixture: tuple[AegisConfig, Path],
) -> None:
    cfg, ledger_path = pipeline_fixture
    result = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    assert isinstance(result, Week1SliceResult)
    assert result.panel_path.exists()
    assert result.factor_path.exists()
    assert result.panel_path.name == cfg.data.snapshot.panel_filename
    assert result.factor_path.name == cfg.data.snapshot.factor_filename


def test_run_week1_slice_registers_one_experiment_one_candidate_two_artifacts(
    pipeline_fixture: tuple[AegisConfig, Path],
) -> None:
    cfg, ledger_path = pipeline_fixture
    run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    snap = _snapshot(ledger_path)
    assert len(snap.experiments) == 1
    assert len(snap.candidates) == 1
    assert len(snap.artifacts) == 2

    # FK linkage: candidate → experiment; artifacts → candidate.
    assert snap.candidates[0]["experiment_id"] == snap.experiments[0]["experiment_id"]
    assert {a["candidate_id"] for a in snap.artifacts} == {snap.candidates[0]["candidate_id"]}

    # Two distinct artifact_types (panel + factor)
    assert {a["artifact_type"] for a in snap.artifacts} == {"panel", "factor"}


def test_ledger_rows_carry_config_hash_and_git_sha(
    pipeline_fixture: tuple[AegisConfig, Path],
) -> None:
    cfg, ledger_path = pipeline_fixture
    result = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    snap = _snapshot(ledger_path)
    exp = snap.experiments[0]
    cand = snap.candidates[0]

    assert exp["config_hash"] == cfg.content_hash()
    assert exp["config_hash"] == result.config_hash
    assert exp["git_sha"] == FAKE_GIT_SHA
    assert exp["name"] == "week1_vertical_slice"

    assert cand["candidate_name"] == "mom_12_1"
    assert cand["formula_string"] == "log(P[t-21] / P[t-252])"
    assert cand["status"] == "computed"
    assert cand["candidate_type"] == "deterministic_factor"


def test_artifact_checksums_match_sha256_of_files_on_disk(
    pipeline_fixture: tuple[AegisConfig, Path],
) -> None:
    cfg, ledger_path = pipeline_fixture
    result = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    panel_hash_on_disk = sha256_file(result.panel_path)
    factor_hash_on_disk = sha256_file(result.factor_path)

    assert result.panel_checksum == panel_hash_on_disk
    assert result.factor_checksum == factor_hash_on_disk

    snap = _snapshot(ledger_path)
    checksums = {a["artifact_type"]: a["checksum"] for a in snap.artifacts}
    assert checksums["panel"] == panel_hash_on_disk
    assert checksums["factor"] == factor_hash_on_disk


def test_rerun_produces_new_experiment_row_but_same_config_hash(
    pipeline_fixture: tuple[AegisConfig, Path],
) -> None:
    cfg, ledger_path = pipeline_fixture
    first = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)
    second = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    # Different UUIDs — each run is a distinct experiment.
    assert first.experiment_id != second.experiment_id
    assert first.candidate_id != second.candidate_id

    # But same research identity + same data identity.
    assert first.config_hash == second.config_hash
    assert first.data_snapshot_id == second.data_snapshot_id

    snap = _snapshot(ledger_path)
    assert len(snap.experiments) == 2
    assert len(snap.candidates) == 2
    assert len(snap.artifacts) == 4

    # Both experiments share the config_hash (no silent drift).
    assert {e["config_hash"] for e in snap.experiments} == {first.config_hash}


def test_candidate_data_snapshot_id_matches_panel_column(
    pipeline_fixture: tuple[AegisConfig, Path],
) -> None:
    cfg, ledger_path = pipeline_fixture
    result = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    panel = pd.read_parquet(result.panel_path)
    # Day 3 invariant: data_snapshot_id is constant per panel.
    panel_snapshot = panel["data_snapshot_id"].iloc[0]
    assert (panel["data_snapshot_id"] == panel_snapshot).all()

    snap = _snapshot(ledger_path)
    cand = snap.candidates[0]

    assert cand["data_snapshot_id"] == panel_snapshot
    assert result.data_snapshot_id == panel_snapshot
