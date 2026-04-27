"""Day 8 unit coverage for :mod:`aegis.data.index_membership`.

Six tests, against the checked-in ``data/reference/sp500_membership.csv``:

- 5 tests from the locked Week 2 plan (schema, 2020 size, post-delisting
  exclusion, post-addition inclusion, window shape).
- 1 test added by the proposal-compliance audit (§4.1 σ-algebra
  measurability regression guard).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from aegis.data.index_membership import (
    EXPECTED_COLUMNS,
    active_on,
    load_sp500_membership,
    membership_window,
)

# tests/unit/test_index_membership.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MEMBERSHIP_CSV = _REPO_ROOT / "data" / "reference" / "sp500_membership.csv"


@pytest.fixture(scope="module")
def membership() -> pd.DataFrame:
    return load_sp500_membership(_MEMBERSHIP_CSV)


def test_load_sp500_membership_schema(membership: pd.DataFrame) -> None:
    """CSV header matches EXPECTED_COLUMNS; no null tickers or date_added."""
    assert tuple(membership.columns) == EXPECTED_COLUMNS
    assert len(membership) >= 640
    assert membership["ticker"].notna().all()
    assert membership["date_added"].notna().all()
    bad = membership["date_removed"].notna() & (
        membership["date_removed"] < membership["date_added"]
    )
    assert not bad.any(), "found rows with date_removed < date_added"


def test_active_on_2020_returns_500_names(membership: pd.DataFrame) -> None:
    """Mid-2020 reconstruction lands within ±10 of 500 (Day 10 tightens to ±1)."""
    tickers = active_on(date(2020, 6, 15), membership)
    assert 485 <= len(tickers) <= 510, f"got {len(tickers)} names on 2020-06-15"


def test_active_on_excludes_post_delisting(membership: pd.DataFrame) -> None:
    """Monsanto (MON) was acquired by Bayer 2018-06-07 → not in 2020-01-02 active set."""
    tickers = active_on(date(2020, 1, 2), membership)
    assert "MON" not in tickers, "Monsanto should not be active two years after acquisition"


def test_active_on_includes_post_addition(membership: pd.DataFrame) -> None:
    """Tesla (TSLA) added 2020-12-21 — absent before, present after."""
    pre = active_on(date(2020, 6, 15), membership)
    post = active_on(date(2021, 1, 4), membership)
    assert "TSLA" not in pre, "TSLA should not be active before 2020-12-21"
    assert "TSLA" in post, "TSLA should be active by 2021-01-04"


def test_membership_window_shape(membership: pd.DataFrame) -> None:
    """≈21 trading days × ~500 names → low-thousands long-format rows."""
    mw = membership_window(date(2020, 1, 2), date(2020, 1, 31), membership)
    assert set(mw.columns) == {"date", "ticker"}
    # 22 business days in the window × ~500 names ≈ 10,500–11,500 rows
    assert 9000 < len(mw) < 12000, f"got {len(mw)} rows"
    # Trading-day-only: no Saturdays/Sundays
    weekdays = pd.to_datetime(mw["date"]).dt.dayofweek
    assert (weekdays < 5).all(), "membership_window emitted weekend rows"


def test_active_on_is_filtration_measurable(membership: pd.DataFrame) -> None:
    """Spec §4.1 regression guard.

    For any ``t``, ``active_on(t, membership)`` must be measurable with
    respect to ``F_t``. Operationally: restricting the input frame to the
    F_t-measurable subset (rows whose ``date_added <= t``) must produce
    the same answer. If a refactor lets future-dated rows leak into the
    membership set at ``t``, this test fails.
    """
    t = date(2018, 6, 15)
    ts = pd.Timestamp(t)

    full_actives = active_on(t, membership)
    restricted = membership[membership["date_added"] <= ts]
    restricted_actives = active_on(t, restricted)

    assert full_actives == restricted_actives, (
        "active_on is not filtration-measurable: dropping rows with "
        f"date_added > {t} changed the active set"
    )

    # Stronger property: every ticker in the active set has at least one
    # row in `membership` with date_added <= t. Guards against an off-by-one
    # or stray strict-vs-non-strict inequality.
    for ticker in full_actives:
        rows = membership[membership["ticker"] == ticker]
        assert (rows["date_added"] <= ts).any(), (
            f"{ticker} active on {t} but no row has date_added <= {t}"
        )
