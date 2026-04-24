"""Module E acceptance (spec §6)."""

from __future__ import annotations

import pytest


@pytest.mark.xfail(strict=True, reason="Module E not implemented yet (spec §12 weeks 13-15)")
def test_fdr_on_1000_null_signals_at_or_below_q() -> None:
    """BH-FDR on 1,000 synthetic null signals keeps false-positive rate ≤ q=0.10."""
    raise NotImplementedError
