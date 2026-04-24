"""Day 5 unit coverage for :mod:`aegis.features.operators`.

Winsorize + zscore, cross-sectional (per-date), lookahead-safe.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aegis.features.operators import winsorize_cross_section, zscore_cross_section


def _daily_panel(dates: list[str], values_by_date: dict[str, list[float]]) -> pd.DataFrame:
    """Build a tiny long-format (date, ticker, value) frame for tests."""
    rows = []
    for d in dates:
        for i, v in enumerate(values_by_date[d]):
            rows.append({"date": pd.Timestamp(d), "ticker": f"T{i}", "value": v})
    return pd.DataFrame(rows)


# --- winsorize_cross_section -------------------------------------------------
def test_winsorize_clips_top_and_bottom_percentiles() -> None:
    # 11 tickers on one date, linear 0..10
    df = _daily_panel(["2025-01-02"], {"2025-01-02": list(range(11))})
    out = winsorize_cross_section(df, "value", pct=(0.1, 0.9))
    # 10th percentile = 1.0, 90th = 9.0 → values 0 and 10 clip in.
    assert out.iloc[0] == 1.0
    assert out.iloc[-1] == 9.0
    # Middle values untouched
    assert (out.iloc[1:-1].to_numpy() == list(range(1, 10))).all()


def test_winsorize_preserves_values_inside_bounds() -> None:
    df = _daily_panel(["2025-01-02"], {"2025-01-02": [1.0, 2.0, 3.0, 4.0, 5.0]})
    out = winsorize_cross_section(df, "value", pct=(0.0, 1.0))
    # (0,1) bounds = min, max → no clipping
    np.testing.assert_array_equal(out.to_numpy(), [1.0, 2.0, 3.0, 4.0, 5.0])


def test_winsorize_per_date_independently() -> None:
    df = _daily_panel(
        ["2025-01-02", "2025-01-03"],
        {
            "2025-01-02": [1.0, 2.0, 3.0, 4.0, 5.0],
            "2025-01-03": [100.0, 200.0, 300.0, 400.0, 500.0],
        },
    )
    out = winsorize_cross_section(df, "value", pct=(0.0, 1.0))
    # Each date clipped to its own min/max — dates don't bleed into each other
    d1 = out[df["date"] == pd.Timestamp("2025-01-02")]
    d2 = out[df["date"] == pd.Timestamp("2025-01-03")]
    assert d1.min() == 1.0 and d1.max() == 5.0
    assert d2.min() == 100.0 and d2.max() == 500.0


def test_winsorize_passes_nan_through() -> None:
    df = _daily_panel(["2025-01-02"], {"2025-01-02": [1.0, 2.0, float("nan"), 4.0, 5.0]})
    out = winsorize_cross_section(df, "value", pct=(0.0, 1.0))
    assert np.isnan(out.iloc[2])
    # Non-NaN values untouched (full-range bounds)
    assert out.iloc[0] == 1.0 and out.iloc[-1] == 5.0


def test_winsorize_empty_df_raises() -> None:
    with pytest.raises(ValueError, match="empty DataFrame"):
        winsorize_cross_section(pd.DataFrame({"date": [], "value": []}), "value")


def test_winsorize_invalid_percentiles_raises() -> None:
    df = _daily_panel(["2025-01-02"], {"2025-01-02": [1.0, 2.0]})
    with pytest.raises(ValueError, match="invalid percentile bounds"):
        winsorize_cross_section(df, "value", pct=(0.9, 0.1))
    with pytest.raises(ValueError, match="invalid percentile bounds"):
        winsorize_cross_section(df, "value", pct=(-0.1, 0.5))
    with pytest.raises(ValueError, match="invalid percentile bounds"):
        winsorize_cross_section(df, "value", pct=(0.5, 1.5))


def test_winsorize_missing_value_col_raises() -> None:
    df = pd.DataFrame({"date": [pd.Timestamp("2025-01-02")], "other_col": [1.0]})
    with pytest.raises(ValueError, match=r"not in df\.columns"):
        winsorize_cross_section(df, "value")


# --- zscore_cross_section ----------------------------------------------------
def test_zscore_has_zero_mean_and_unit_std_per_date() -> None:
    df = _daily_panel(["2025-01-02"], {"2025-01-02": [1.0, 2.0, 3.0, 4.0, 5.0]})
    out = zscore_cross_section(df, "value", ddof=0)
    assert abs(out.mean()) < 1e-12
    assert abs(out.std(ddof=0) - 1.0) < 1e-12


def test_zscore_preserves_ordering_within_date() -> None:
    df = _daily_panel(["2025-01-02"], {"2025-01-02": [3.0, 1.0, 2.0, 5.0, 4.0]})
    out = zscore_cross_section(df, "value")
    # Argsort of z-scores equals argsort of raws
    np.testing.assert_array_equal(out.argsort().to_numpy(), df["value"].argsort().to_numpy())


def test_zscore_per_date_independently() -> None:
    df = _daily_panel(
        ["2025-01-02", "2025-01-03"],
        {"2025-01-02": [10.0, 20.0, 30.0], "2025-01-03": [100.0, 200.0, 300.0]},
    )
    out = zscore_cross_section(df, "value")
    # Both days standardize to mean~0, std~1 on their OWN distribution
    d1_out = out[df["date"] == pd.Timestamp("2025-01-02")]
    d2_out = out[df["date"] == pd.Timestamp("2025-01-03")]
    assert abs(d1_out.mean()) < 1e-12
    assert abs(d2_out.mean()) < 1e-12
    # Both days' z-score distributions equal up to rounding (same shape different scale)
    np.testing.assert_allclose(d1_out.to_numpy(), d2_out.to_numpy(), atol=1e-12)


def test_zscore_all_identical_values_returns_nan() -> None:
    """Spec-edge: zero variance → NaN, not inf."""
    df = _daily_panel(["2025-01-02"], {"2025-01-02": [7.0, 7.0, 7.0, 7.0]})
    out = zscore_cross_section(df, "value")
    assert out.isna().all()


def test_zscore_empty_df_raises() -> None:
    with pytest.raises(ValueError, match="empty DataFrame"):
        zscore_cross_section(pd.DataFrame({"date": [], "value": []}), "value")


def test_zscore_missing_value_col_raises() -> None:
    df = pd.DataFrame({"date": [pd.Timestamp("2025-01-02")], "other_col": [1.0]})
    with pytest.raises(ValueError, match=r"not in df\.columns"):
        zscore_cross_section(df, "value")
