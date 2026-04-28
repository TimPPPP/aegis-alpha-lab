"""Day 6 unit coverage for the Week 1 vertical-slice pipeline.

Polygon-free by construction: monkey-patches ``build_panel`` inside
:mod:`aegis.backtest.week1` so the loader isn't hit. The rest of the
pipeline — factor compute, Parquet writes, ledger inserts — runs for real
against the engineered ``stock_daily_panel`` fixture and an on-disk
SQLite.

The ``pipeline_fixture`` and ``ledger_snapshot`` helpers live in
``tests/conftest.py`` (lifted there in Week 2 Day 12 so ``test_ledger``
can consume the same fixture for verify-mode replay tests).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from aegis.backtest.week1 import Week1SliceResult, run_week1_slice
from aegis.config import AegisConfig
from aegis.utils.hashing import sha256_file
from tests.conftest import FAKE_GIT_SHA, ledger_snapshot


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

    snap = ledger_snapshot(ledger_path)
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

    snap = ledger_snapshot(ledger_path)
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

    snap = ledger_snapshot(ledger_path)
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

    snap = ledger_snapshot(ledger_path)
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

    snap = ledger_snapshot(ledger_path)
    cand = snap.candidates[0]

    assert cand["data_snapshot_id"] == panel_snapshot
    assert result.data_snapshot_id == panel_snapshot
