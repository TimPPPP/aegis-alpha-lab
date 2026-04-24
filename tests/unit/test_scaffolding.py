"""Smoke tests that verify the scaffolding itself is wired correctly.

These should pass on day one — if they fail, the repo is broken, not the
research code. They intentionally do not touch modules A-F.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import aegis
from aegis import config as cfg


def test_package_importable() -> None:
    assert aegis.__version__ != ""


def test_all_configs_load_and_validate() -> None:
    loaded = cfg.load_all()
    # Thresholds from spec §4 must be present and match.
    assert loaded.gates.promotion.t_ic_min == 3.0
    assert loaded.gates.promotion.fdr_q == 0.10
    assert loaded.gates.promotion.dsr_min == 0.95
    assert loaded.gates.promotion.ff6_t_min == 2.5

    # Barra-lite dimensionality from spec §4.2 / §6 / §10.
    assert len(loaded.risk.styles) == 9
    assert loaded.risk.industries.count == 24

    # Universe filters from spec §7.
    assert loaded.universe.price_floor_usd == 5.0
    assert loaded.universe.min_history_days == 252


def test_config_hash_is_stable() -> None:
    a = cfg.load_all().content_hash()
    b = cfg.load_all().content_hash()
    assert a == b
    assert len(a) == 64  # sha256 hex


# --- Cross-platform replayability (Windows / macOS / Linux) ------------------
# The config_hash is stamped into every ledger row. For spec-§6 Module B
# ("every promoted factor replays bit-identical from the ledger") to hold
# across platforms, the hash must depend only on research identity, never on
# deployment layout (path separators, absolute paths, working directories).


def _swap_data_paths(
    base: cfg.AegisConfig, raw: Path, interim: Path, processed: Path
) -> cfg.AegisConfig:
    new_data = base.data.model_copy(
        update={"paths": cfg.DataPaths(raw=raw, interim=interim, processed=processed)}
    )
    return base.model_copy(update={"data": new_data})


def test_content_hash_invariant_to_data_paths() -> None:
    """Changing data.paths must NOT change the hash.

    Covers the Windows-vs-POSIX Path-separator bug: on Windows a Path
    serializes as ``data\\raw``; on POSIX as ``data/raw``. Before this
    guarantee, a ledger row written on Tim's Windows laptop could not be
    verified by a CI replay on Linux or a collaborator on macOS.
    """
    base = cfg.load_all()
    win_style = _swap_data_paths(
        base,
        raw=Path(PureWindowsPath(r"C:\data\raw").as_posix()),
        interim=Path(PureWindowsPath(r"C:\data\interim").as_posix()),
        processed=Path(PureWindowsPath(r"C:\data\processed").as_posix()),
    )
    posix_style = _swap_data_paths(
        base,
        raw=Path(PurePosixPath("/mnt/shared/data/raw")),
        interim=Path(PurePosixPath("/mnt/shared/data/interim")),
        processed=Path(PurePosixPath("/mnt/shared/data/processed")),
    )
    assert base.content_hash() == win_style.content_hash() == posix_style.content_hash()


def test_content_hash_invariant_to_data_snapshot_filenames() -> None:
    """Everything under ``data`` is excluded — including snapshot filenames.

    Data integrity comes from ``data_snapshot_id`` (SHA-256 of the actual
    Parquet bytes) attached to each artifact, not from the filename the
    researcher happened to pick.
    """
    base = cfg.load_all()
    alt_snapshot = cfg.DataSnapshot(
        panel_filename="something_else.parquet",
        factor_filename="also_different.parquet",
    )
    alt = base.model_copy(update={"data": base.data.model_copy(update={"snapshot": alt_snapshot})})
    assert base.content_hash() == alt.content_hash()


def test_content_hash_changes_when_research_identity_changes() -> None:
    """The other direction: touching gates/risk/universe/portfolio/costs/factors
    MUST change the hash. Excluding data cannot weaken the gate against silent
    threshold drift — that's the whole point of pre-commitment (spec §5.2).
    """
    base = cfg.load_all()

    # Tweak the Harvey-Liu threshold by 0.01. In practice this would be a
    # spec violation; here we just assert the hash notices.
    tweaked_promotion = base.gates.promotion.model_copy(update={"t_ic_min": 3.01})
    tweaked_gates = base.gates.model_copy(update={"promotion": tweaked_promotion})
    tweaked = base.model_copy(update={"gates": tweaked_gates})

    assert base.content_hash() != tweaked.content_hash()


def test_content_hash_length_and_hex() -> None:
    h = cfg.load_all().content_hash()
    assert len(h) == 64
    int(h, 16)  # valid hex; raises ValueError otherwise
