"""SQLAlchemy 2.0 declarative tables for the research ledger (Week 1 Day 1).

Four tables:
    experiments  — one row per end-to-end run (config_hash + git_sha)
    candidates   — one row per factor/signal produced within an experiment
    artifacts    — file paths (Parquet panels, factor outputs, reports)
    metrics      — per-candidate metric values (empty until Module E lands)

Append-only semantics are enforced by the write API in ``store.py``, not by
the DDL itself. SQLite does not provide row-level immutability, so the
contract is: never UPDATE or DELETE through the ORM session. Mutating a past
row is a protocol violation (spec §6 Module B acceptance test: "every
promoted factor replays bit-identical from the ledger").

Note on identifier columns: SQLite has no native UUID type, so UUIDs are
stored as 36-char strings (canonical form). Pydantic shapes in
``models.py`` carry ``uuid.UUID``; the ``store.py`` write path converts to
str on insert.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all ledger tables."""


class Experiment(Base):
    __tablename__ = "experiments"

    experiment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    git_sha: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    candidates: Mapped[list[Candidate]] = relationship(back_populates="experiment", cascade="all")


class Candidate(Base):
    __tablename__ = "candidates"

    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("experiments.experiment_id"), nullable=False, index=True
    )
    candidate_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    candidate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    formula_string: Mapped[str] = mapped_column(Text, nullable=False)
    data_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    experiment: Mapped[Experiment] = relationship(back_populates="candidates")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="candidate", cascade="all")
    metrics: Mapped[list[Metric]] = relationship(back_populates="candidate", cascade="all")


class Artifact(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("candidates.candidate_id"), nullable=False, index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    candidate: Mapped[Candidate] = relationship(back_populates="artifacts")


class Metric(Base):
    """Declared-but-empty in Week 1. Populated during Module E (spec §4.5–§4.11)."""

    __tablename__ = "metrics"

    metric_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("candidates.candidate_id"), nullable=False, index=True
    )
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    fold: Mapped[str | None] = mapped_column(String(64), nullable=True)
    horizon: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    candidate: Mapped[Candidate] = relationship(back_populates="metrics")


__all__ = ["Artifact", "Base", "Candidate", "Experiment", "Metric"]
