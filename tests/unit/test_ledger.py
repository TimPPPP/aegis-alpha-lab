"""Module B acceptance + Day 4 unit coverage for the research ledger.

Spec §6 Module B acceptance — verify-mode replay (artifact checksums +
config_hash + git SHA all match the ledger row) — flipped on Week 2 Day 12.
Plus four mismatch / non-mutation tests pinning the failure paths and
the structural non-mutation guarantee. Day 4 write-API tests sit
alongside.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from aegis.backtest.week1 import run_week1_slice
from aegis.config import AegisConfig
from aegis.ledger import (
    FAILURE_CHECKSUM_MISMATCH,
    FAILURE_FILE_MISSING,
    FAILURE_NO_ARTIFACTS_RECORDED,
    store,
    verify,
)
from aegis.ledger.schema import Artifact, Base, Candidate, Experiment
from aegis.ledger.store import (
    open_ledger,
    register_artifact,
    register_candidate,
    register_experiment,
)
from aegis.utils.git import GitShaUnavailableError, current_git_sha
from aegis.utils.hashing import sha256_file
from tests.conftest import ledger_snapshot

HASH = "a" * 64
MEMORY = Path(":memory:")


# --- Module B §6 acceptance — Day 12 flip -----------------------------------


def test_promoted_factor_replays_bit_identical(
    pipeline_fixture: tuple[AegisConfig, Path],
    patched_git_sha: None,
) -> None:
    """Spec §6 Module B — verify-mode replay (Week 2 interpretation).

    A freshly-written candidate verifies cleanly: every artifact's
    sha256_file matches the recorded checksum, the live ``content_hash()``
    equals the stored one, and the recorded git SHA is reachable. Full
    rebuild-from-source replay is V2 scope; verify-mode is the Week 2
    acceptance interpretation.
    """
    cfg, ledger_path = pipeline_fixture
    result = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    report = verify(result.candidate_id, ledger_path, cfg)

    assert report.artifacts_verified == 2
    assert report.artifacts_failed == []
    assert report.config_hash_match is True
    assert report.config_hash_recorded == cfg.content_hash()
    assert report.git_sha_available is True
    assert report.all_ok is True


def test_replay_detects_modified_artifact(
    pipeline_fixture: tuple[AegisConfig, Path],
    patched_git_sha: None,
) -> None:
    """A single corrupted byte in an artifact surfaces as ``checksum_mismatch``."""
    cfg, ledger_path = pipeline_fixture
    result = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    # Corrupt the panel by appending bytes — leaves the factor untouched.
    result.panel_path.write_bytes(result.panel_path.read_bytes() + b"\nGARBAGE")

    report = verify(result.candidate_id, ledger_path, cfg)

    assert (str(result.panel_path), FAILURE_CHECKSUM_MISMATCH) in report.artifacts_failed
    assert report.artifacts_verified == 1  # the factor still passes
    assert report.all_ok is False
    # No exception raised; the report is still well-formed.
    assert report.config_hash_match is True
    assert report.git_sha_available is True


def test_replay_detects_missing_artifact(
    pipeline_fixture: tuple[AegisConfig, Path],
    patched_git_sha: None,
) -> None:
    """Deleting an artifact file surfaces as ``file_missing``."""
    cfg, ledger_path = pipeline_fixture
    result = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    result.factor_path.unlink()

    report = verify(result.candidate_id, ledger_path, cfg)

    assert (str(result.factor_path), FAILURE_FILE_MISSING) in report.artifacts_failed
    assert report.artifacts_verified == 1  # the panel still passes
    assert report.all_ok is False


def test_replay_detects_config_hash_drift(
    pipeline_fixture: tuple[AegisConfig, Path],
    patched_git_sha: None,
) -> None:
    """Changing a research-identity config field flips ``config_hash_match`` to False."""
    cfg, ledger_path = pipeline_fixture
    result = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    drifted_cfg = cfg.model_copy(
        update={
            "gates": cfg.gates.model_copy(
                update={
                    "promotion": cfg.gates.promotion.model_copy(update={"t_ic_min": 99.0}),
                }
            )
        }
    )
    assert drifted_cfg.content_hash() != cfg.content_hash()  # sanity: tweak registered

    report = verify(result.candidate_id, ledger_path, drifted_cfg)

    assert report.config_hash_recorded == cfg.content_hash()
    assert report.config_hash_current == drifted_cfg.content_hash()
    assert report.config_hash_match is False
    # Artifacts still pass; only the config drifted.
    assert report.artifacts_verified == 2
    assert report.artifacts_failed == []
    assert report.all_ok is False


def test_replay_detects_candidate_with_no_artifacts(
    pipeline_fixture: tuple[AegisConfig, Path],
    patched_git_sha: None,
) -> None:
    """A candidate with no artifact rows cannot pass verify-mode replay."""
    cfg, ledger_path = pipeline_fixture
    with open_ledger(ledger_path) as session:
        exp_id = register_experiment(
            session,
            name="artifactless",
            config_hash=cfg.content_hash(),
            git_sha="abc1234",
        )
        cand_id = register_candidate(
            session,
            experiment_id=exp_id,
            candidate_name="mom_12_1",
            formula_string="log(P[t-21] / P[t-252])",
            data_snapshot_id=HASH,
            status="computed",
        )

    report = verify(cand_id, ledger_path, cfg)

    assert report.artifacts_verified == 0
    assert report.artifacts_failed == [("<ledger>", FAILURE_NO_ARTIFACTS_RECORDED)]
    assert report.all_ok is False


def test_replay_can_skip_config_check_without_loading_configs(
    pipeline_fixture: tuple[AegisConfig, Path],
    patched_git_sha: None,
) -> None:
    """check_config=False skips load_all() and ignores config equality in all_ok."""
    cfg, ledger_path = pipeline_fixture
    result = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    with patch("aegis.config.load_all", side_effect=RuntimeError("should not load")):
        report = verify(result.candidate_id, ledger_path, cfg=None, check_config=False)

    assert report.config_hash_checked is False
    assert report.config_hash_current == "<skipped>"
    assert report.config_hash_match is False
    assert report.artifacts_verified == 2
    assert report.artifacts_failed == []
    assert report.all_ok is True


def test_verify_does_not_mutate_ledger_or_artifacts(
    pipeline_fixture: tuple[AegisConfig, Path],
    patched_git_sha: None,
) -> None:
    """Spec principle 5 — verify() is non-mutating in BOTH happy-path and failure-path.

    Snapshots ledger row-counts and per-artifact ``sha256_file`` before and
    after each call, asserts both unchanged. Day 11's read-only SQLite URL
    enforces this at the connection layer; this test is the regression
    guard against any future refactor that breaks the invariant.
    """
    cfg, ledger_path = pipeline_fixture
    result = run_week1_slice(cfg, ledger_path, sleep_between_calls=0)

    pre_snap = ledger_snapshot(ledger_path)
    pre_panel_sha = sha256_file(result.panel_path)
    pre_factor_sha = sha256_file(result.factor_path)

    # --- Happy path ---
    report1 = verify(result.candidate_id, ledger_path, cfg)
    assert report1.all_ok is True

    assert ledger_snapshot(ledger_path) == pre_snap
    assert sha256_file(result.panel_path) == pre_panel_sha
    assert sha256_file(result.factor_path) == pre_factor_sha

    # --- Failure path: deliberately corrupt the panel ---
    result.panel_path.write_bytes(result.panel_path.read_bytes() + b"\nGARBAGE")
    post_corrupt_panel_sha = sha256_file(result.panel_path)
    assert post_corrupt_panel_sha != pre_panel_sha  # corruption registered

    report2 = verify(result.candidate_id, ledger_path, cfg)
    assert report2.all_ok is False

    # Ledger row counts unchanged across BOTH verify calls.
    assert ledger_snapshot(ledger_path) == pre_snap
    # verify() did not "fix" the corrupted file (writes are forbidden).
    assert sha256_file(result.panel_path) == post_corrupt_panel_sha
    # The other artifact remains pristine.
    assert sha256_file(result.factor_path) == pre_factor_sha


# --- Table creation ----------------------------------------------------------
def test_open_ledger_creates_all_four_tables() -> None:
    """open_ledger's entry runs Base.metadata.create_all on the fresh SQLite."""
    with open_ledger(MEMORY) as session:
        table_names = {t.name for t in Base.metadata.tables.values()}
        assert table_names == {"experiments", "candidates", "artifacts", "metrics"}
        # Verify the tables actually exist in the DB, not just the metadata
        for cls in (Experiment, Candidate, Artifact):
            assert session.execute(select(cls)).all() == []


def test_open_ledger_on_disk_creates_parent_dir(tmp_path: Path) -> None:
    """If the path's parent directory doesn't exist, open_ledger creates it."""
    nested = tmp_path / "nested" / "deeper" / "ledger.sqlite"
    with open_ledger(nested):
        pass
    assert nested.exists()


# --- Register: experiments ---------------------------------------------------
def test_register_experiment_round_trip() -> None:
    """Insert one experiment row, read it back, field-by-field match."""
    with open_ledger(MEMORY) as session:
        exp_id = register_experiment(
            session,
            name="week1_vertical_slice",
            config_hash=HASH,
            git_sha="a1b2c3d4e5f6",
        )
        session.flush()

        row = session.execute(
            select(Experiment).where(Experiment.experiment_id == str(exp_id))
        ).scalar_one()

        assert row.name == "week1_vertical_slice"
        assert row.config_hash == HASH
        assert row.git_sha == "a1b2c3d4e5f6"
        assert row.created_at is not None


def test_register_experiment_returns_uuid() -> None:
    """Return value is a real UUID, not a string."""
    with open_ledger(MEMORY) as session:
        exp_id = register_experiment(session, name="x", config_hash=HASH, git_sha="abc1234")
        assert isinstance(exp_id, UUID)


# --- Register: candidates ----------------------------------------------------
def test_register_candidate_foreign_key_to_experiment() -> None:
    """A candidate with a bogus experiment_id must fail the FK.

    Nested contexts: the IntegrityError needs to escape ``open_ledger`` so
    pytest.raises catches it. If we catch it inside the ``with``, the
    subsequent commit on clean exit would fail with PendingRollbackError.
    """
    with pytest.raises(IntegrityError), open_ledger(MEMORY) as session:
        # SQLite's default mode doesn't enforce FKs; enable them here.
        session.execute(text("PRAGMA foreign_keys = ON"))

        register_candidate(
            session,
            experiment_id=uuid4(),  # unlinked to any experiment row
            candidate_name="mom_12_1",
            formula_string="log(P[t-21] / P[t-252])",
            data_snapshot_id=HASH,
        )


def test_register_candidate_defaults_status_registered() -> None:
    """Default status is 'registered' — promotion is Week 13+ work."""
    with open_ledger(MEMORY) as session:
        exp_id = register_experiment(session, name="x", config_hash=HASH, git_sha="abc1234")
        cand_id = register_candidate(
            session,
            experiment_id=exp_id,
            candidate_name="mom_12_1",
            formula_string="log(P[t-21] / P[t-252])",
            data_snapshot_id=HASH,
        )
        session.flush()

        row = session.execute(
            select(Candidate).where(Candidate.candidate_id == str(cand_id))
        ).scalar_one()
        assert row.status == "registered"
        assert row.candidate_type == "deterministic_factor"


# --- Register: artifacts -----------------------------------------------------
def test_full_write_chain_experiment_candidate_artifact(tmp_path: Path) -> None:
    """End-to-end: one experiment, one candidate, one artifact."""
    parquet = tmp_path / "fake_panel.parquet"
    parquet.write_bytes(b"\x00" * 128)  # dummy content; test only needs a path

    with open_ledger(MEMORY) as session:
        exp_id = register_experiment(
            session,
            name="week1_vertical_slice",
            config_hash=HASH,
            git_sha="a1b2c3d",
        )
        cand_id = register_candidate(
            session,
            experiment_id=exp_id,
            candidate_name="mom_12_1",
            formula_string="log(P[t-21] / P[t-252])",
            data_snapshot_id=HASH,
        )
        art_id = register_artifact(
            session,
            candidate_id=cand_id,
            artifact_type="panel",
            path=parquet,
            checksum=HASH,
        )
        session.flush()

        # Read them back with the relationship traversal
        exp_row = session.execute(
            select(Experiment).where(Experiment.experiment_id == str(exp_id))
        ).scalar_one()
        assert len(exp_row.candidates) == 1
        cand_row = exp_row.candidates[0]
        assert cand_row.candidate_name == "mom_12_1"
        assert len(cand_row.artifacts) == 1
        art_row = cand_row.artifacts[0]
        assert art_row.artifact_type == "panel"
        assert art_row.checksum == HASH
        assert UUID(art_row.artifact_id) == art_id


def test_two_candidates_under_one_experiment() -> None:
    """One experiment can parent multiple candidates (1-to-many)."""
    with open_ledger(MEMORY) as session:
        exp_id = register_experiment(session, name="x", config_hash=HASH, git_sha="abc1234")
        for name, formula in [
            ("mom_12_1", "log(P[t-21] / P[t-252])"),
            ("mom_1m", "log(P[t-1] / P[t-21])"),
        ]:
            register_candidate(
                session,
                experiment_id=exp_id,
                candidate_name=name,
                formula_string=formula,
                data_snapshot_id=HASH,
            )
        session.flush()

        exp_row = session.execute(
            select(Experiment).where(Experiment.experiment_id == str(exp_id))
        ).scalar_one()
        assert {c.candidate_name for c in exp_row.candidates} == {"mom_12_1", "mom_1m"}


# --- Append-only interface contract ------------------------------------------
def test_store_module_has_no_update_or_delete_exports() -> None:
    """Interface-level append-only guarantee: no update_* / delete_* in __all__."""
    for fn_name in store.__all__:
        assert not fn_name.startswith("update_"), (
            f"Found update_* in store.__all__: {fn_name}. The ledger is "
            "append-only — write a new experiment instead of updating a past row."
        )
        assert not fn_name.startswith("delete_"), (
            f"Found delete_* in store.__all__: {fn_name}. The ledger is append-only."
        )
    # Also check: no common "mutate" names sneaked in as bare attributes
    for name in ("update_candidate_status", "delete_experiment", "purge_ledger"):
        assert not hasattr(store, name), f"store exposes {name} — violates append-only"


# --- Git SHA helper ----------------------------------------------------------
def test_git_sha_env_var_takes_precedence() -> None:
    """AEGIS_GIT_SHA beats git rev-parse — Docker-baked SHA wins."""
    with patch.dict(os.environ, {"AEGIS_GIT_SHA": "d0ckerbak3dsha"}):
        assert current_git_sha() == "d0ckerbak3dsha"


def test_git_sha_falls_back_to_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without env var, shells out to `git rev-parse HEAD` in the repo."""
    monkeypatch.delenv("AEGIS_GIT_SHA", raising=False)
    repo_root = Path(__file__).resolve().parents[2]  # the aegis repo itself

    sha = current_git_sha(repo_root)
    assert len(sha) >= 7
    # Hex-only
    int(sha, 16)


def test_git_sha_raises_when_neither_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """If env var is unset AND git is unreachable, hard-fail — refuse to
    fabricate provenance."""
    monkeypatch.delenv("AEGIS_GIT_SHA", raising=False)

    def _fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(subprocess, "run", _fail)

    with pytest.raises(GitShaUnavailableError):
        current_git_sha()
