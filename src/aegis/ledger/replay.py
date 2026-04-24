"""Research-ledger replay engine — stub for Week 2.

The real replay engine reconstructs a promoted candidate bit-identically
from the ledger row alone (spec §6 Module B acceptance). That requires:

  1. Looking up the candidate row → get (experiment_id, data_snapshot_id).
  2. Looking up the experiment → get (git_sha, config_hash).
  3. Checking out the git SHA in a scratch workspace.
  4. Loading the config matching ``config_hash``.
  5. Re-running the pipeline (panel build + factor compute).
  6. Recomputing the artifact checksum and comparing against the stored
     ``artifacts.checksum``.

Steps 3-6 are Week 2 work. Day 4 only lands the data model the replay
engine consumes.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID


def replay(candidate_id: UUID, ledger_path: Path | None = None) -> None:
    """Reconstruct a candidate bit-identically from its ledger row.

    Not implemented in Week 1 — the replay engine is a Week 2 deliverable.
    The ledger itself (written by :mod:`aegis.ledger.store`) is ready to be
    read and diffed; only the executor that replays the pipeline is
    missing.
    """
    raise NotImplementedError(
        "Ledger replay engine is Week 2 (spec §6 Module B acceptance). "
        "Day 4 lands the write-side; the replay executor follows."
    )


__all__ = ["replay"]
