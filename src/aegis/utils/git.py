"""Git SHA capture for ledger provenance (Week 1 Day 4).

Every research-ledger row carries the git SHA of the code that produced it.
We capture the SHA in two ways, in order of preference:

  1. ``AEGIS_GIT_SHA`` environment variable. Set by the Docker build at
     image bake time (see [docker/Dockerfile](../../../docker/Dockerfile))
     so containerized runs record the SHA that was baked into the image,
     not whatever the container's `git` might or might not return.
  2. ``git rev-parse HEAD`` executed in the current working directory.
     Works for local-dev runs on Windows/macOS/Linux with a git checkout.

Hard failure if neither is available — a ledger row without a git SHA
violates spec principle 5 (auditability), so we refuse to fabricate one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_ALLOW_DIRTY_ENV = "AEGIS_ALLOW_DIRTY_GIT"


class GitShaUnavailableError(RuntimeError):
    """Neither ``AEGIS_GIT_SHA`` env var nor ``git rev-parse`` worked."""


class DirtyGitWorktreeError(RuntimeError):
    """The worktree has uncommitted or untracked changes."""


def current_git_sha(repo_path: Path | None = None, *, require_clean: bool = False) -> str:
    """Return the current git SHA (full 40-char hex).

    Args:
        repo_path: Directory to run ``git rev-parse`` in. Defaults to CWD.
        require_clean: If True, fail when the worktree has uncommitted or
            untracked changes unless ``AEGIS_ALLOW_DIRTY_GIT=1`` is set.

    Raises:
        GitShaUnavailableError: if neither the env var nor git is reachable.
        DirtyGitWorktreeError: if ``require_clean=True`` and the worktree is dirty.
    """
    if (sha := os.environ.get("AEGIS_GIT_SHA")) and len(sha) >= 7:
        return sha

    cwd = str(repo_path) if repo_path is not None else None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise GitShaUnavailableError(
            "Could not determine git SHA: AEGIS_GIT_SHA not set and "
            "`git rev-parse HEAD` failed. Ledger rows require a git SHA "
            "for provenance — refusing to record without one."
        ) from e

    sha = result.stdout.strip()
    if not sha or len(sha) < 7:
        raise GitShaUnavailableError(f"git rev-parse HEAD returned unexpected output: {sha!r}")

    if require_clean and not _dirty_git_override_enabled():
        _assert_worktree_clean(cwd)
    return sha


def _dirty_git_override_enabled() -> bool:
    return os.environ.get(_ALLOW_DIRTY_ENV, "").strip().lower() in {"1", "true", "yes"}


def _assert_worktree_clean(cwd: str | None) -> None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise GitShaUnavailableError(
            "Could not determine git worktree status. Ledger rows require clean "
            "code provenance unless AEGIS_ALLOW_DIRTY_GIT=1 is set."
        ) from e

    if result.stdout.strip():
        raise DirtyGitWorktreeError(
            "The git worktree has uncommitted or untracked changes. Commit them before writing "
            "ledger rows, or set AEGIS_ALLOW_DIRTY_GIT=1 for an explicitly dirty run."
        )


__all__ = ["DirtyGitWorktreeError", "GitShaUnavailableError", "current_git_sha"]
