"""Pydantic row models for the research ledger (Week 1 Day 1).

These shapes are what the write-side of the ledger (``src/aegis/ledger/store.py``,
to be implemented Day 4) constructs and serializes into the SQLAlchemy tables
declared in ``src/aegis/ledger/schema.py``.

Separation from ``schema.py``:
    * ``schema.py``: SQLAlchemy ORM — DDL, indexes, FK constraints.
    * ``models.py``: Pydantic row contracts — validation, API types.

All models are frozen. The ledger is append-only (spec principle 5); mutating
a past row is a protocol violation, not a feature.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

CandidateType = Literal["deterministic_factor"]  # V2 adds "llm_proposed"
CandidateStatus = Literal["registered", "computed", "promoted", "held", "retired"]
ArtifactType = Literal["panel", "factor", "risk", "metrics", "report"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _FrozenRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# --- Per-table row shapes ---------------------------------------------------
class ExperimentRecord(_FrozenRow):
    experiment_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64, description="SHA-256 hex")
    git_sha: str = Field(min_length=7, max_length=40)
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("config_hash")
    @classmethod
    def _hex_hash(cls, v: str) -> str:
        int(v, 16)  # raises ValueError if not hex
        return v


class CandidateRecord(_FrozenRow):
    candidate_id: UUID = Field(default_factory=uuid4)
    experiment_id: UUID
    candidate_name: str = Field(min_length=1)
    candidate_type: CandidateType = "deterministic_factor"
    formula_string: str = Field(min_length=1)
    data_snapshot_id: str = Field(min_length=1)
    status: CandidateStatus = "registered"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ArtifactRecord(_FrozenRow):
    artifact_id: UUID = Field(default_factory=uuid4)
    candidate_id: UUID
    artifact_type: ArtifactType
    path: str = Field(min_length=1)
    checksum: str = Field(min_length=64, max_length=64, description="SHA-256 hex")
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("checksum")
    @classmethod
    def _hex_checksum(cls, v: str) -> str:
        int(v, 16)
        return v


class MetricRecord(_FrozenRow):
    """Single metric value attached to a candidate. Empty in Week 1; landing
    pad for HAC IC / BH-FDR / DSR / FF6 α / decay in weeks 13-15."""

    metric_id: UUID = Field(default_factory=uuid4)
    candidate_id: UUID
    metric_name: str = Field(min_length=1)
    value: float
    fold: str | None = None  # e.g. "oos_2023"
    horizon: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=_utcnow)


# --- Umbrella row-shape exposed by the Week 1 plan --------------------------
class ResearchRecord(_FrozenRow):
    """Minimum-viable per-candidate record as specified in the Week 1 plan.

    This is a denormalized convenience view over (experiment, candidate,
    artifact). The canonical persisted form is the four per-table records
    above; this class is the shape most CLI outputs consume.
    """

    experiment_id: UUID
    candidate_id: UUID
    candidate_name: str = Field(min_length=1)
    candidate_type: CandidateType = "deterministic_factor"
    formula_string: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    git_sha: str = Field(min_length=7, max_length=40)
    data_snapshot_id: str = Field(min_length=1)
    status: CandidateStatus = "registered"
    artifact_path: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("config_hash")
    @classmethod
    def _hex_hash(cls, v: str) -> str:
        int(v, 16)
        return v


__all__ = [
    "ArtifactRecord",
    "ArtifactType",
    "CandidateRecord",
    "CandidateStatus",
    "CandidateType",
    "ExperimentRecord",
    "MetricRecord",
    "ResearchRecord",
]
