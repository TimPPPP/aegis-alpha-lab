"""Module F acceptance (spec §6)."""

from __future__ import annotations

import pytest


@pytest.mark.xfail(strict=True, reason="Module F not implemented yet (spec §12 weeks 16-18)")
def test_attribution_residual_under_1bp_per_day() -> None:
    """Daily P&L attribution residual ≤ 1 bp/day; turnover matches QP dual."""
    raise NotImplementedError
