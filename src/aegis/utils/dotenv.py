"""Minimal .env loader — no dependency on python-dotenv.

Used at CLI startup (:mod:`aegis.cli`) and at pytest-conftest time to make
``POLYGON_API_KEY`` visible to the running process. Already-set env vars
are never overridden.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv_if_present(start: Path | None = None) -> Path | None:
    """Find a ``.env`` in the provided dir (or CWD), load it, return the path.

    Search order:
      1. ``start`` (if given), or current working directory.
      2. Walk up parent directories looking for ``.env`` — stops at filesystem root.

    Returns the loaded path on success, None if no .env was found. Silent
    on all parse errors; this is a convenience helper, not a config gate.
    """
    start = (start or Path.cwd()).resolve()
    for candidate_dir in (start, *start.parents):
        env_path = candidate_dir / ".env"
        if env_path.is_file():
            _apply_env_file(env_path)
            return env_path
    return None


def _apply_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


__all__ = ["load_dotenv_if_present"]
