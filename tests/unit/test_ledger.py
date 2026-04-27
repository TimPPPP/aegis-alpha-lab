"""Module B acceptance + Day 4 unit coverage for the research ledger.

The spec §6 Module B acceptance test (every promoted factor replays
bit-identical from the ledger) stays ``xfail`` until Week 2 lands the
replay engine. The Day 4 tests below exercise the write API: open_ledger,
register_experiment / _candidate / _artifact, foreign-key integrity, and
the append-only interface contract.
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

from aegis.ledger import store
from aegis.ledger.schema import Artifact, Base, Candidate, Experiment
from aegis.ledger.store import (
    open_ledger,
    register_artifact,
    register_candidate,
    register_experiment,
)
from aegis.utils.git import GitShaUnavailableError, current_git_sha

HASH = "a" * 64
MEMORY = Path(":memory:")


# --- Preserved Module B acceptance stub -------------------------------------
@pytest.mark.xfail(
    strict=True,
    reason="Module B replay engine is Week 2 (spec §6 acceptance)",
)
def test_promoted_factor_replays_bit_identical() -> None:
    """Every promoted factor replays bit-identical from the ledger + PIT snapshot."""
    raise NotImplementedError


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
