"""Verify-mode replay engine (Week 2 Day 11; spec §6 Module B).

Given a ``candidate_id``, :func:`verify` returns a :class:`ReplayReport` that
answers four independent questions about the ledger row:

1. **Are the artifacts intact?** Each artifact row's recorded ``checksum`` is
   re-computed via :func:`aegis.utils.hashing.sha256_file` against the current
   bytes on disk. Missing files surface as ``"file_missing"``; checksum drift
   surfaces as ``"checksum_mismatch"``.
2. **Has the config drifted?** ``cfg.content_hash()`` is compared to the
   stored ``experiments.config_hash``.
3. **Is the code reachable?** ``git cat-file -e <git_sha>`` resolves the
   recorded SHA in the current repo.
4. **Does it all add up?** ``all_ok`` is the conjunction of "no failed
   artifacts", ``config_hash_match``, and ``git_sha_available``.

The function is **non-throwing** (always returns a report, even on missing
files / corrupted bytes / pruned SHAs) and **non-mutating** (opens the
ledger via SQLite's read-only URL — ``?mode=ro&uri=true`` — so writes are
structurally impossible at the connection layer). Day 12 lands the
regression test that asserts the non-mutation invariant.

Spec scope
----------

Spec §6 Module B's wording — "every promoted factor replays bit-identical
from the ledger" — is interpreted as **verify-mode** for Week 2: artifact
checksum + config hash + git SHA all match. Full rebuild-from-source
replay (git checkout + pipeline re-run + sha-of-output comparison) stays
on the V2 roadmap. The :func:`replay` symbol is preserved as a
``NotImplementedError`` stub pointing callers at :func:`verify`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aegis.ledger.store import read_candidate_provenance
from aegis.utils.hashing import sha256_file

if TYPE_CHECKING:
    from aegis.config import AegisConfig

# Failure-mode strings recorded inside ``ReplayReport.artifacts_failed``.
# Exported as module constants so tests can reference them by import (no
# stringly-typed comparisons sprinkled across the codebase).
FAILURE_FILE_MISSING = "file_missing"
FAILURE_CHECKSUM_MISMATCH = "checksum_mismatch"


@dataclass(frozen=True)
class ReplayReport:
    """Result of a verify-mode replay.

    ``all_ok`` is True iff every artifact verified, the recorded config hash
    matches the live one, and the recorded git SHA is reachable in the
    current repo. The other fields surface the components so a failure
    report tells you *which* invariant broke.
    """

    candidate_id: UUID
    artifacts_verified: int
    artifacts_failed: list[tuple[str, str]]
    config_hash_recorded: str
    config_hash_current: str
    config_hash_match: bool
    git_sha_recorded: str
    git_sha_available: bool
    all_ok: bool


def _check_git_sha(sha: str) -> bool:
    """True iff ``git cat-file -e <sha>`` resolves in the current repo.

    Returns False if git itself isn't on PATH, the SHA has been pruned, or
    the working directory is not a git repo. We don't surface the specific
    cause — for verify mode "the recorded SHA is no longer reachable" is the
    operationally interesting fact.
    """
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", sha],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def verify(
    candidate_id: UUID,
    ledger_path: Path,
    cfg: AegisConfig | None = None,
) -> ReplayReport:
    """Verify a candidate's artifacts, config hash, and git SHA against the ledger.

    Non-throwing: any failure (missing file, corrupted bytes, pruned SHA,
    config-load error) is recorded in the returned :class:`ReplayReport`
    rather than raised. Non-mutating: opens the ledger SQLite read-only;
    no row-count or file-byte changes anywhere in the system.

    Parameters
    ----------
    candidate_id
        UUID of the candidate to verify (one of the rows
        :func:`aegis.ledger.store.register_candidate` returned).
    ledger_path
        Path to the ledger SQLite file. Must exist; the read-only URL does
        not auto-create it.
    cfg
        The live :class:`AegisConfig` to compare ``content_hash()`` against.
        Pass ``None`` (the default) to auto-load via :func:`aegis.config.load_all`.
        Mainly an injection point for tests; real CLI usage relies on the
        default.

    Raises
    ------
    LookupError
        If ``candidate_id`` is not in the ledger. Other failures are folded
        into the returned report.
    """
    # 1. Read provenance from a read-only SQLite URL. SQLite's `?mode=ro&uri=true`
    #    syntax requires the `file:` URI prefix; SQLAlchemy passes it through
    #    when the path begins with `file:`. With this URL: writes raise
    #    SQLITE_READONLY at the wire — structural enforcement of the
    #    non-mutation guarantee.
    posix_path = Path(ledger_path).resolve().as_posix()
    url = f"sqlite:///file:{posix_path}?mode=ro&uri=true"
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            prov = read_candidate_provenance(session, candidate_id)
    finally:
        engine.dispose()

    # 2. Verify each artifact: existence + sha256_file vs stored.
    failed: list[tuple[str, str]] = []
    verified = 0
    for art in prov.artifacts:
        path = Path(art.path)
        if not path.exists():
            failed.append((art.path, FAILURE_FILE_MISSING))
            continue
        if sha256_file(path) != art.checksum:
            failed.append((art.path, FAILURE_CHECKSUM_MISMATCH))
            continue
        verified += 1

    # 3. Compare config hashes. If cfg wasn't passed, load_all() may fail on
    #    a broken on-disk YAML — fold that into a sentinel current hash so
    #    the report still returns truthfully.
    if cfg is None:
        try:
            from aegis.config import load_all

            cfg = load_all()
            config_hash_current = cfg.content_hash()
        except Exception as e:
            config_hash_current = f"<load-failed: {type(e).__name__}: {e}>"
        else:
            pass
    else:
        config_hash_current = cfg.content_hash()

    config_hash_match = config_hash_current == prov.config_hash

    # 4. Resolve git SHA.
    git_sha_available = _check_git_sha(prov.git_sha)

    all_ok = not failed and config_hash_match and git_sha_available

    return ReplayReport(
        candidate_id=prov.candidate_id,
        artifacts_verified=verified,
        artifacts_failed=failed,
        config_hash_recorded=prov.config_hash,
        config_hash_current=config_hash_current,
        config_hash_match=config_hash_match,
        git_sha_recorded=prov.git_sha,
        git_sha_available=git_sha_available,
        all_ok=all_ok,
    )


def replay(candidate_id: UUID, ledger_path: Path | None = None) -> None:
    """Reconstruct a candidate bit-identically by re-running the pipeline (V2).

    Full rebuild-from-source replay — git checkout the recorded SHA, load
    the matching config, re-run panel build + factor compute, recompute
    artifact checksums against the recorded values — is V2 scope.

    For Week 2's verify-mode acceptance (artifact + config + git SHA
    checksums match the ledger row), call :func:`verify` instead.
    """
    raise NotImplementedError(
        "Full rebuild-from-source replay is V2 scope. For verify-mode "
        "(artifact + config + git SHA checksum match), call `verify()` instead."
    )


__all__ = [
    "FAILURE_CHECKSUM_MISMATCH",
    "FAILURE_FILE_MISSING",
    "ReplayReport",
    "replay",
    "verify",
]
