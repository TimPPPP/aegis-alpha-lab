"""Cross-sectional transform operators (spec §4.1 measurability, §8 winsorize/zscore).

Two operators, both stateless, both vectorized, both σ-algebra-safe:

  * :func:`winsorize_cross_section` — per-date percentile clipping (1%/99% by
    default per spec §8 intro). Used on raw factor values.
  * :func:`zscore_cross_section`    — per-date mean-0 / std-1 standardization
    (population std, ddof=0).

Both apply per ``date`` via ``groupby`` and use only same-day information,
so they preserve spec §4.1 measurability: output at row (t, i) depends
only on inputs at dates ≤ t.

NaN behavior: missing inputs pass through as NaN. Empty input frames raise;
all-NaN groups silently produce NaN output rows. The caller decides what to
do with NaNs via the ``valid_flag`` on :class:`aegis.features.base.FactorObservation`.
"""

from __future__ import annotations

import pandas as pd


def winsorize_cross_section(
    df: pd.DataFrame,
    value_col: str,
    pct: tuple[float, float] = (0.01, 0.99),
    date_col: str = "date",
) -> pd.Series:
    """Clip ``value_col`` to per-date [lower, upper] percentiles.

    Args:
        df: Long-format DataFrame. Must contain ``date_col`` and ``value_col``.
        value_col: Name of the numeric column to winsorize.
        pct: (lower, upper) percentile bounds. Default (0.01, 0.99) per spec §8.
        date_col: Name of the date column used for the per-date groupby.

    Returns:
        A Series aligned to ``df.index`` — winsorized values.
    """
    if df.empty:
        raise ValueError(f"cannot winsorize empty DataFrame (value_col={value_col!r})")
    if value_col not in df.columns:
        raise ValueError(f"value_col={value_col!r} not in df.columns={list(df.columns)}")

    lower, upper = pct
    if not (0.0 <= lower < upper <= 1.0):
        raise ValueError(f"invalid percentile bounds pct={pct!r}; require 0 <= lower < upper <= 1")

    grouped = df.groupby(date_col, sort=False)[value_col]
    # pandas .quantile skips NaN by default — desired behavior: the bounds come
    # from the non-NaN population, then clip() preserves the original NaN rows.
    lo = grouped.transform(lambda s: s.quantile(lower))
    hi = grouped.transform(lambda s: s.quantile(upper))
    return df[value_col].clip(lower=lo, upper=hi)


def zscore_cross_section(
    df: pd.DataFrame,
    value_col: str,
    date_col: str = "date",
    ddof: int = 0,
) -> pd.Series:
    """Standardize ``value_col`` per date to mean 0, std 1.

    Args:
        df: Long-format DataFrame. Must contain ``date_col`` and ``value_col``.
        value_col: Name of the numeric column to standardize.
        date_col: Name of the date column for the per-date groupby.
        ddof: Delta degrees of freedom for the std denominator. Default 0
            (population std) — matches numpy's default and academic z-score
            convention. Use ddof=1 for sample std.

    Returns:
        A Series aligned to ``df.index`` — z-scored values. Dates with a
        degenerate distribution (all-NaN or zero variance) yield NaN.
    """
    if df.empty:
        raise ValueError(f"cannot z-score empty DataFrame (value_col={value_col!r})")
    if value_col not in df.columns:
        raise ValueError(f"value_col={value_col!r} not in df.columns={list(df.columns)}")

    grouped = df.groupby(date_col, sort=False)[value_col]
    mean = grouped.transform("mean")
    std = grouped.transform("std", ddof=ddof)
    # Where std == 0 (all values identical on that date), the z-score is
    # undefined; propagate NaN rather than infinity.
    std = std.where(std > 0)
    return (df[value_col] - mean) / std


__all__ = ["winsorize_cross_section", "zscore_cross_section"]
