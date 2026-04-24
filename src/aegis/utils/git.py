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


class GitShaUnavailableError(RuntimeError):
    """Neither ``AEGIS_GIT_SHA`` env var nor ``git rev-parse`` worked."""


def current_git_sha(repo_path: Path | None = None) -> str:
    """Return the current git SHA (full 40-char hex).

    Args:
        repo_path: Directory to run ``git rev-parse`` in. Defaults to CWD.

    Raises:
        GitShaUnavailableError: if neither the env var nor git is reachable.
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
    return sha


__all__ = ["GitShaUnavailableError", "current_git_sha"]
