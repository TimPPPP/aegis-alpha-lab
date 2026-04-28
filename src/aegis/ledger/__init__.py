"""Module B — Research ledger (spec §6, principle 5).

Append-only SQLite store with (candidate_id, git_sha, config_hash, metrics,
gate_status). Backs the verify-mode replay engine: every promoted factor
must reconstruct from the ledger plus the PIT snapshot alone.

Public API:
    open_ledger                 — context manager for a ledger session
    register_experiment         — insert one experiment row
    register_candidate          — insert one candidate row
    register_artifact           — insert one artifact row
    read_candidate_provenance   — read-only join for verify-mode replay
    ArtifactRow / ProvenanceRow — frozen dataclasses returned by reads
    verify                      — verify-mode replay (Week 2 Day 11)
    ReplayReport                — result of verify()
    replay                      — full rebuild-from-source replay (V2 stub)
"""

from aegis.ledger.replay import (
    FAILURE_CHECKSUM_MISMATCH,
    FAILURE_FILE_MISSING,
    ReplayReport,
    replay,
    verify,
)
from aegis.ledger.store import (
    ArtifactRow,
    ProvenanceRow,
    open_ledger,
    read_candidate_provenance,
    register_artifact,
    register_candidate,
    register_experiment,
)

__all__ = [
    "FAILURE_CHECKSUM_MISMATCH",
    "FAILURE_FILE_MISSING",
    "ArtifactRow",
    "ProvenanceRow",
    "ReplayReport",
    "open_ledger",
    "read_candidate_provenance",
    "register_artifact",
    "register_candidate",
    "register_experiment",
    "replay",
    "verify",
]
