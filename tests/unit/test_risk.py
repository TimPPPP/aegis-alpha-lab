"""Module D acceptance (spec §6)."""

from __future__ import annotations

import pytest


@pytest.mark.xfail(strict=True, reason="Module D not implemented yet (spec §12 weeks 6-8)")
def test_mean_style_r2_above_0_25() -> None:
    """Cross-sectional WLS achieves mean style R² ≥ 0.25 with residual lag-1 |ρ| ≤ 0.05."""
    raise NotImplementedError
