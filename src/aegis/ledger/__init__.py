"""Module B — Research ledger (spec §6, principle 5).

Append-only SQLite store with (candidate_id, git_sha, config_hash, metrics,
gate_status). Backs the bit-identical replay engine: every promoted factor
must reconstruct from the ledger plus the PIT snapshot alone.

Public API:
    open_ledger            — context manager for a ledger session
    register_experiment    — insert one experiment row
    register_candidate     — insert one candidate row
    register_artifact      — insert one artifact row
    replay                 — reconstruct a candidate (Week 2 stub)
"""

from aegis.ledger.replay import replay
from aegis.ledger.store import (
    open_ledger,
    register_artifact,
    register_candidate,
    register_experiment,
)

__all__ = [
    "open_ledger",
    "register_artifact",
    "register_candidate",
    "register_experiment",
    "replay",
]
