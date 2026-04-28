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
import pytest

from aegis.backtest import _common as backtest_common_module
from aegis.backtest.week1 import Week1SliceResult, run_week1_slice
from aegis.config import AegisConfig, load_all
from aegis.data.panel import _finalize_panel
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


def test_run_week1_slice_requires_all_fixed_tickers(
    stock_daily_panel: pd.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 8-name Week 1 smoke universe must fail loudly on missing tickers."""
    base_cfg = load_all()
    cfg = base_cfg.model_copy(
        update={
            "data": base_cfg.data.model_copy(
                update={
                    "paths": base_cfg.data.paths.model_copy(update={"processed": tmp_path}),
                }
            )
        }
    )
    panel_path = tmp_path / cfg.data.snapshot.panel_filename
    _finalize_panel(stock_daily_panel, cfg).to_parquet(panel_path, index=False)
    captured: dict[str, bool] = {}

    def _fake_build_panel(
        cfg: AegisConfig,
        *,
        tickers=None,
        sleep_between_calls: float = 0.0,
        panel_filename: str | None = None,
        metadata_as_of=None,
        require_all_tickers: bool = False,
    ) -> Path:
        captured["require_all_tickers"] = require_all_tickers
        return panel_path

    monkeypatch.setattr(backtest_common_module, "build_panel", _fake_build_panel)
    monkeypatch.setattr(backtest_common_module, "current_git_sha", lambda **_: FAKE_GIT_SHA)

    run_week1_slice(cfg, tmp_path / "ledger.sqlite", sleep_between_calls=0)

    assert captured["require_all_tickers"] is True


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
