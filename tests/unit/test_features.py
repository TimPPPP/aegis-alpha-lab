"""Module C acceptance (spec §6)."""

from __future__ import annotations

import pytest


@pytest.mark.xfail(strict=True, reason="Module C not implemented yet (spec §12 weeks 9-12)")
def test_12m1m_momentum_reference_ic_within_0_005() -> None:
    """12-1 momentum and B/P reproduce published reference IC within 0.005."""
    raise NotImplementedError
