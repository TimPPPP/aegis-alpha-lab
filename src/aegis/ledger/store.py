"""Append-only write API for the research ledger (Week 1 Day 4).

The public surface is exactly four functions:
  * :func:`open_ledger`         — context manager yielding a SQLAlchemy Session
  * :func:`register_experiment` — insert one row into ``experiments``
  * :func:`register_candidate`  — insert one row into ``candidates``
  * :func:`register_artifact`   — insert one row into ``artifacts``

There is **no** ``update_*`` or ``delete_*`` function. The ledger is
append-only; mutating a past row is a protocol violation (spec principle 5,
§6 Module B acceptance). SQLite can't enforce immutability at the DDL level,
so we enforce it at the interface level — callers that need to "correct" a
ledger row must write a new experiment instead.

All register functions return the row's UUID (``uuid.UUID``), not the ORM
instance. UUIDs are portable across machines (see Option A discussion in
the Day 4 plan); ORM instances are not valid outside the session that
created them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aegis.ledger.models import (
    ArtifactType,
    CandidateStatus,
    CandidateType,
)
from aegis.ledger.schema import Artifact, Base, Candidate, Experiment


@contextmanager
def open_ledger(path: Path) -> Iterator[Session]:
    """Open (or create) a SQLite ledger at ``path`` and yield a Session.

    Runs ``Base.metadata.create_all`` on entry — idempotent, a no-op if all
    four tables already exist. On clean exit, commits the session; on
    exception, rolls back. Always closes the session.

    Pass ``Path(":memory:")`` to get an in-memory ledger (used by tests).
    """
    if str(path) == ":memory:":
        url = "sqlite:///:memory:"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"

    engine = create_engine(url)
    Base.metadata.create_all(engine)

    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()


def register_experiment(
    session: Session,
    *,
    name: str,
    config_hash: str,
    git_sha: str,
) -> UUID:
    """Insert one row into ``experiments`` and return its UUID.

    Args:
        session: Active SQLAlchemy session (from :func:`open_ledger`).
        name: Human-readable experiment label (e.g. ``"week1_vertical_slice"``).
        config_hash: SHA-256 hex of the research-identity config subset —
            call ``cfg.content_hash()``.
        git_sha: Git SHA of the code — call
            :func:`aegis.utils.git.current_git_sha`.
    """
    experiment_id = uuid4()
    session.add(
        Experiment(
            experiment_id=str(experiment_id),
            name=name,
            config_hash=config_hash,
            git_sha=git_sha,
        )
    )
    session.flush()
    return experiment_id


def register_candidate(
    session: Session,
    *,
    experiment_id: UUID,
    candidate_name: str,
    formula_string: str,
    data_snapshot_id: str,
    status: CandidateStatus = "registered",
    candidate_type: CandidateType = "deterministic_factor",
) -> UUID:
    """Insert one row into ``candidates`` and return its UUID.

    Args:
        session: Active SQLAlchemy session.
        experiment_id: UUID of the parent experiment (returned by
            :func:`register_experiment`).
        candidate_name: Factor name (e.g. ``"mom_12_1"``).
        formula_string: Exact formula in string form; stored for replay.
        data_snapshot_id: SHA-256 of the input panel — see
            :func:`aegis.utils.hashing.sha256_dataframe`.
        status: One of the statuses in :data:`CandidateStatus`. Defaults to
            ``"registered"`` — promotion/hold/retire is Week 13+.
        candidate_type: Kept for V2's ``"llm_proposed"``; default is
            ``"deterministic_factor"``.
    """
    candidate_id = uuid4()
    session.add(
        Candidate(
            candidate_id=str(candidate_id),
            experiment_id=str(experiment_id),
            candidate_name=candidate_name,
            candidate_type=candidate_type,
            formula_string=formula_string,
            data_snapshot_id=data_snapshot_id,
            status=status,
        )
    )
    session.flush()
    return candidate_id


def register_artifact(
    session: Session,
    *,
    candidate_id: UUID,
    artifact_type: ArtifactType,
    path: Path,
    checksum: str,
) -> UUID:
    """Insert one row into ``artifacts`` and return its UUID.

    Args:
        session: Active SQLAlchemy session.
        candidate_id: UUID of the parent candidate.
        artifact_type: ``"panel"``, ``"factor"``, ``"risk"``, ``"metrics"``,
            or ``"report"``.
        path: Filesystem path to the artifact (Parquet, SQLite, HTML report).
        checksum: SHA-256 hex of the artifact's bytes — see
            :func:`aegis.utils.hashing.sha256_file`.
    """
    artifact_id = uuid4()
    session.add(
        Artifact(
            artifact_id=str(artifact_id),
            candidate_id=str(candidate_id),
            artifact_type=artifact_type,
            path=str(path),
            checksum=checksum,
        )
    )
    session.flush()
    return artifact_id


# --- Read side (Week 2 Day 11) ----------------------------------------------
# Read-only access for the verify-mode replay engine. These are pure reads —
# no inserts, updates, or deletes — so adding them to __all__ does not violate
# the append-only contract. The two row dataclasses are frozen so callers can
# pass them around without worrying about accidental mutation.


@dataclass(frozen=True)
class ArtifactRow:
    """Immutable snapshot of one ``artifacts`` row, detached from the session."""

    artifact_id: UUID
    artifact_type: str
    path: str
    checksum: str


@dataclass(frozen=True)
class ProvenanceRow:
    """Joined view of (candidate, parent experiment, artifacts) for ``verify()``.

    Returned by :func:`read_candidate_provenance`. Carries every hash the
    replay engine needs (``config_hash`` from the experiment, ``git_sha`` from
    the experiment, ``data_snapshot_id`` from the candidate, per-artifact
    ``checksum`` from artifacts).
    """

    candidate_id: UUID
    candidate_name: str
    formula_string: str
    data_snapshot_id: str
    config_hash: str
    git_sha: str
    artifacts: tuple[ArtifactRow, ...]


def read_candidate_provenance(
    session: Session,
    candidate_id: UUID,
) -> ProvenanceRow:
    """Read a candidate's full provenance bundle for verify-mode replay.

    Joins ``candidates`` to its parent ``experiments`` and child
    ``artifacts``. Returns a :class:`ProvenanceRow` detached from the
    session (frozen dataclass), so the caller can close the session and
    still use the data.

    Raises
    ------
    LookupError
        If ``candidate_id`` is not in the ``candidates`` table.
    """
    candidate = session.get(Candidate, str(candidate_id))
    if candidate is None:
        raise LookupError(f"candidate_id {candidate_id} not in ledger")
    experiment = session.get(Experiment, candidate.experiment_id)
    if experiment is None:  # pragma: no cover — FK invariant violation
        raise LookupError(
            f"candidate {candidate_id} references missing experiment {candidate.experiment_id}"
        )
    artifacts = tuple(
        ArtifactRow(
            artifact_id=UUID(a.artifact_id),
            artifact_type=a.artifact_type,
            path=a.path,
            checksum=a.checksum,
        )
        for a in candidate.artifacts
    )
    return ProvenanceRow(
        candidate_id=UUID(candidate.candidate_id),
        candidate_name=candidate.candidate_name,
        formula_string=candidate.formula_string,
        data_snapshot_id=candidate.data_snapshot_id,
        config_hash=experiment.config_hash,
        git_sha=experiment.git_sha,
        artifacts=artifacts,
    )


# Explicitly list the public surface so tests can assert no UPDATE/DELETE
# functions have been silently added. If you add ``update_candidate_status``
# or similar, the append-only contract breaks — instead, write a new
# experiment or a new candidate.
__all__ = [
    "ArtifactRow",
    "ProvenanceRow",
    "open_ledger",
    "read_candidate_provenance",
    "register_artifact",
    "register_candidate",
    "register_experiment",
]
