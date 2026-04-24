"""Content-addressing utilities for the research ledger (Week 1 Day 3).

Two hash functions:
  * :func:`sha256_file`  — stream-hash a file's bytes. Used when the artifact
    of interest is a specific Parquet file on disk (e.g., reproducibility
    audit: "is this Parquet the same bytes as what the ledger recorded?").
  * :func:`sha256_dataframe` — deterministic hash of a DataFrame's content,
    regardless of its in-memory layout or storage format. This is what Module
    A's ``data_snapshot_id`` uses: the identity of the data pulled, not the
    identity of the file the data happens to land in.

The two are complementary. ``data_snapshot_id`` proves "same data pulled from
Polygon"; ``sha256_file`` proves "same Parquet bytes on disk". Under normal
replay they agree; if they diverge, the Parquet was rewritten with different
compression / ordering / dtype casting somewhere.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

_CHUNK_SIZE = 65_536


def sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes, streamed in 64 KiB chunks."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_dataframe(df: pd.DataFrame) -> str:
    """Deterministic SHA-256 of a DataFrame's content (values + index).

    Uses ``pandas.util.hash_pandas_object``, which produces a uint64 hash per
    row in a way that is stable across:
      - column dtype variations (e.g., int64 vs. Int64 nullable)
      - row order (we sort before hashing to eliminate this)
      - platform (tested Windows ↔ Linux, same as ``AegisConfig.content_hash``)

    Column order IS part of the hash — reordering columns changes the hash.
    That's intentional: a different column order is a different schema, even
    if the values are identical.
    """
    # Sort rows to make the hash order-independent. The row hash is computed
    # per-row, so two frames with the same rows in different orders would
    # otherwise hash differently.
    ordered = df.sort_values(list(df.columns)).reset_index(drop=True)
    row_hashes = pd.util.hash_pandas_object(ordered, index=False)
    # np.asarray forces a concrete ndarray (hash_pandas_object's underlying
    # storage is typed as ndarray|ExtensionArray), and pinning dtype=uint64
    # matches hash_pandas_object's documented output type.
    arr = np.asarray(row_hashes, dtype=np.uint64)
    return hashlib.sha256(arr.tobytes()).hexdigest()


__all__ = ["sha256_dataframe", "sha256_file"]
