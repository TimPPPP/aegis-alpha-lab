"""Feature-library base types (Week 1 Day 1, Week 3 Day 17 expansion).

Defines:
  * ``FactorObservation`` — the per-(date, ticker) row contract every factor
    emits. Carries raw, winsorized, and z-scored values plus ``valid_flag``,
    ``tradable_flag``, ``invalid_reason`` (Day 17), and a snapshot identifier.
  * ``FactorContext`` (Day 17) — sidecar data passed into ``compute`` beyond
    the price panel. Currently carries ``fundamentals``; future weeks may
    add risk-model exposures, sector_proxy maps, etc.
  * ``Factor`` — the ABC every concrete factor subclasses. ``compute`` takes
    ``(panel, *, context)`` and returns a long-format frame; ``diagnostics``
    returns a per-factor metric dict that gets embedded in parquet metadata.
  * ``write_factor_parquet`` / ``read_factor_diagnostics`` — round-trip the
    diagnostics dict via pyarrow's schema metadata so a single parquet file
    carries both the factor values and their per-factor stats.

The σ-algebra measurability constraint of spec §4.1 is the responsibility of
each concrete factor: the operators used inside ``compute`` must only touch
data with index ≤ t.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
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
    means the factor math did not produce a usable value at that row;
    ``invalid_reason`` (Day 17) names *why*, with a closed enum so
    diagnostics aggregate cleanly. The bidirectional invariant
    ``valid_flag=True ⟺ invalid_reason is None`` is enforced.

    ``tradable_flag`` is a stricter pipeline-level signal availability flag:
    it is True only when the factor math is valid AND the panel row is
    universe-eligible/tradable. Universe-ineligibility never appears in
    ``invalid_reason`` — that lives in ``tradable_flag`` alone.
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

    invalid_reason: str | None = None  # Day 17: 10th column

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
        # Day 17 bidirectional invariant: valid_flag <-> invalid_reason is None.
        if self.valid_flag and self.invalid_reason is not None:
            raise ValueError(f"valid_flag=True but invalid_reason={self.invalid_reason!r} is set")
        if (not self.valid_flag) and self.invalid_reason is None:
            raise ValueError("valid_flag=False but invalid_reason is null")
        return self


@dataclass(frozen=True)
class FactorContext:
    """Sidecar data passed into ``Factor.compute`` beyond the price panel.

    Day 17 carries fundamentals; later waves may add risk-model exposures,
    sector_proxy maps, calendar metadata, etc. Every field is Optional so a
    factor that doesn't need it can ignore it (``Momentum12m1m``) and a
    factor that does need it can fail loud at the top of ``compute``
    (``EarningsYield``).
    """

    fundamentals: pd.DataFrame | None = None


class Factor(ABC):
    """Abstract base class for every V1 deterministic factor.

    Subclasses set ``name``, ``formula``, and ``lookback_days`` as class
    attributes and implement ``compute(panel, *, context=None)``. The return
    of ``compute`` is a long-format DataFrame whose rows conform to
    :class:`FactorObservation`. No ``__init__`` is required in the common
    case — factors hold no state.

    ``diagnostics`` returns a per-factor dict that gets embedded in the
    factor parquet's pyarrow schema metadata via :func:`write_factor_parquet`.
    The default implementation returns an empty dict; concrete factors
    override to surface invalid_reason counts, lag-day distributions, etc.
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
    def compute(self, panel: pd.DataFrame, *, context: FactorContext | None = None) -> pd.DataFrame:
        """Compute factor values over the provided panel.

        Args:
            panel: Long-format DataFrame with at least (date, ticker,
                adj_close) columns, sorted by (ticker, date).
            context: Optional sidecar data (fundamentals, risk model, etc.).
                Factors that don't need it can ignore it; factors that do
                need it must raise ``ValueError`` if the relevant attribute
                is missing.

        Returns:
            Long-format DataFrame conforming to :class:`FactorObservation`.
        """

    def diagnostics(
        self,
        factor_out: pd.DataFrame,
        *,
        context: FactorContext | None = None,
    ) -> dict[str, Any]:
        """Per-factor metric dict embedded in the factor parquet's metadata.

        Default implementation returns ``{}``. Override to surface invalid
        reason counts, freshness statistics, etc. Output must be JSON-serializable.
        """
        return {}

    def __repr__(self) -> str:  # pragma: no cover — trivial
        return f"<Factor {self.name}: {self.formula}>"


_FACTOR_DIAGNOSTICS_KEY: bytes = b"factor_diagnostics"


def write_factor_parquet(
    factor_out: pd.DataFrame,
    path: str | Path,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    """Write factor output to parquet with optional diagnostics in metadata.

    Diagnostics are JSON-encoded under the schema-metadata key
    ``b"factor_diagnostics"``. Always writes via pyarrow with snappy
    compression to keep file shape stable across pandas/pyarrow upgrades.
    """
    table = pa.Table.from_pandas(factor_out, preserve_index=False)
    if diagnostics is not None:
        existing = dict(table.schema.metadata or {})
        existing[_FACTOR_DIAGNOSTICS_KEY] = json.dumps(diagnostics).encode("utf-8")
        table = table.replace_schema_metadata(existing)
    pq.write_table(table, str(path), compression="snappy")


def read_factor_diagnostics(path: str | Path) -> dict[str, Any]:
    """Extract the diagnostics dict from a factor parquet's pyarrow metadata.

    Returns ``{}`` if the parquet has no ``factor_diagnostics`` metadata key
    (e.g. a Week 1 / Week 2 parquet written before Day 17).
    """
    table_meta = pq.read_metadata(str(path))
    schema_meta = table_meta.schema.to_arrow_schema().metadata or {}
    raw = schema_meta.get(_FACTOR_DIAGNOSTICS_KEY)
    if raw is None:
        return {}
    decoded: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return decoded


__all__ = [
    "Factor",
    "FactorContext",
    "FactorObservation",
    "read_factor_diagnostics",
    "write_factor_parquet",
]
