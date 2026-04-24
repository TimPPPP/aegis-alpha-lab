"""Integration test — requires live Polygon.io API key. Skipped in CI."""

from __future__ import annotations

import pytest


@pytest.mark.polygon
@pytest.mark.xfail(strict=True, reason="Integration suite not implemented yet")
def test_polygon_panel_build_end_to_end() -> None:
    """End-to-end Polygon pull produces expected panel shape."""
    raise NotImplementedError
