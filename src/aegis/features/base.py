"""Feature-library base types (Week 1 Day 1).

Defines:
  * ``FactorObservation`` — the per-(date, permno) row contract every factor
    emits. Carries raw, winsorized, and z-scored values plus a validity flag
    and a snapshot identifier for ledger provenance.
  * ``Factor`` — the ABC every concrete factor subclasses. A factor is a pure
    function ``panel -> factor frame``; it carries only metadata (name,
    formula, lookback) plus the ``compute`` method.

The σ-algebra measurability constraint of spec §4.1 is the responsibility of
each concrete factor: the operators used inside ``compute`` must only touch
data with index ≤ t. ``src/aegis/features/operators.py`` will provide a
catalogue of such measurability-preserving ops (lag, rolling, cross-sectional
rank, winsorize, zscore) in Day 5.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import date
from typing import ClassVar

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _FrozenRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class FactorObservation(_FrozenRow):
    """One (date, ticker, factor) row of factor output.

    Three values are carried: ``raw_value`` from the factor formula itself,
    ``winsorized_value`` after per-date ±3σ (or 1%/99%) winsorization, and
    ``zscore_value`` after per-date cross-sectional z-scoring. Downstream
    IC calculations read ``zscore_value``.

    ``valid_flag`` is True iff all three values are finite. A False flag
    typically means insufficient lookback history at that date.
    ``tradable_flag`` is a stricter pipeline-level signal availability flag:
    it is True only when the factor math is valid and the panel row is
    universe-eligible/tradable.
    """

    date: date
    ticker: str = Field(min_length=1, max_length=16)
    factor_name: str = Field(min_length=1)

    raw_value: float | None = None
    winsorized_value: float | None = None
    zscore_value: float | None = None

    valid_flag: bool
    tradable_flag: bool | None = None
    feature_snapshot_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_flag_matches_values(self) -> FactorObservation:
        values = (self.raw_value, self.winsorized_value, self.zscore_value)
        all_present_and_finite = all(v is not None and math.isfinite(v) for v in values)
        if self.valid_flag and not all_present_and_finite:
            raise ValueError(
                "valid_flag=True but at least one of raw/winsorized/zscore is null or non-finite"
            )
        if (not self.valid_flag) and all_present_and_finite:
            raise ValueError("valid_flag=False but all three values are present and finite")
        if self.tradable_flag and not self.valid_flag:
            raise ValueError("tradable_flag=True but valid_flag=False")
        return self


class Factor(ABC):
    """Abstract base class for every V1 deterministic factor.

    Subclasses set ``name``, ``formula``, and ``lookback_days`` as class
    attributes and implement ``compute``. The return of ``compute`` is a
    long-format DataFrame whose rows conform to :class:`FactorObservation`.
    No __init__ is required in the common case — factors hold no state.
    """

    name: ClassVar[str]
    formula: ClassVar[str]
    lookback_days: ClassVar[int]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Enforce that concrete subclasses declare the three class attributes.
        # ABC's @abstractmethod covers `compute`; this covers the metadata.
        if ABC not in cls.__bases__:
            return
        for attr in ("name", "formula", "lookback_days"):
            if not hasattr(cls, attr):
                raise TypeError(f"{cls.__name__} must declare class attribute {attr!r}")

    @abstractmethod
    def compute(self, panel: pd.DataFrame) -> pd.DataFrame:
        """Compute factor values over the provided panel.

        Args:
            panel: Long-format DataFrame with at least (date, ticker,
                adj_close) columns, sorted by (ticker, date).

        Returns:
            Long-format DataFrame conforming to :class:`FactorObservation`.
        """

    def __repr__(self) -> str:  # pragma: no cover — trivial
        return f"<Factor {self.name}: {self.formula}>"


__all__ = ["Factor", "FactorObservation"]
