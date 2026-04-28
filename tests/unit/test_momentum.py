"""Day 5 unit coverage for :class:`aegis.features.momentum.Momentum12m1m`.

The §6 Module C acceptance test (reference IC within 0.005) stays ``xfail``
in ``test_features.py`` — it needs forward-returns machinery that lands in
Module E (Week 13-15). Here we exercise the math and the schema contract.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from aegis.features.base import Factor, FactorObservation
from aegis.features.momentum import Momentum12m1m


def _single_ticker_panel(
    ticker: str, closes: list[float], start: str = "2023-01-02"
) -> pd.DataFrame:
    """Build a single-ticker (date, ticker, adj_close) DataFrame."""
    dates = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"date": dates, "ticker": ticker, "adj_close": closes})


def _multi_ticker_panel(n_tickers: int, n_days: int, rng: np.random.Generator) -> pd.DataFrame:
    """Build a multi-ticker panel with geometric random walks."""
    dates = pd.bdate_range("2023-01-02", periods=n_days)
    rows = []
    for t in range(n_tickers):
        shocks = rng.normal(0.0005, 0.015, size=n_days)
        price = 100.0 * np.cumprod(1.0 + shocks)
        for d, p in zip(dates, price, strict=True):
            rows.append({"date": d, "ticker": f"T{t:03d}", "adj_close": p})
    return pd.DataFrame(rows)


# --- Metadata ----------------------------------------------------------------
def test_momentum_is_a_factor_subclass() -> None:
    assert issubclass(Momentum12m1m, Factor)


def test_momentum_metadata_matches_spec() -> None:
    assert Momentum12m1m.name == "mom_12_1"
    assert Momentum12m1m.formula == "log(P[t-21] / P[t-252])"
    assert Momentum12m1m.lookback_days == 252


# --- Raw value math ----------------------------------------------------------
def test_raw_value_matches_hand_computed_log_ratio() -> None:
    """Spec §8.1: raw[t] = log(adj_close[t-21] / adj_close[t-252]).

    Build a deterministic 300-day series with known values, verify the
    raw factor at day 252 (index 252, the first date with full history)
    equals the hand-computed log ratio.
    """
    closes = [100.0 + i * 0.5 for i in range(300)]  # 100, 100.5, 101, ...
    panel = _single_ticker_panel("AAA", closes)
    out = Momentum12m1m().compute(panel)

    # Day 252 (0-indexed) — first row with both [t-21] and [t-252] defined.
    # [t-21] = closes[231]; [t-252] = closes[0]
    expected = math.log(closes[231] / closes[0])
    got = out.iloc[252]["raw_value"]
    assert abs(got - expected) < 1e-12


def test_raw_value_nan_before_day_252() -> None:
    """No 252-day history → raw is NaN."""
    closes = [100.0 + i for i in range(300)]
    panel = _single_ticker_panel("AAA", closes)
    out = Momentum12m1m().compute(panel)

    # First 252 rows have no t-252 predecessor
    assert out.iloc[:252]["raw_value"].isna().all()
    # From day 252 onward, raw is defined
    assert out.iloc[252:]["raw_value"].notna().all()


def test_raw_uses_lag_21_for_short_skip() -> None:
    """The numerator is P[t-21], not P[t]. A last-21-day spike should NOT show up."""
    closes = [100.0] * 280 + [200.0] * 20  # flat then doubles on last 20 days
    panel = _single_ticker_panel("AAA", closes)
    out = Momentum12m1m().compute(panel)

    # At the very last day (index 299), t-21 is index 278 (still flat at 100),
    # t-252 is index 47 (flat at 100) → raw should be log(100/100) = 0.
    assert abs(out.iloc[-1]["raw_value"]) < 1e-12


# --- valid_flag invariant ----------------------------------------------------
def test_valid_flag_false_when_insufficient_history() -> None:
    closes = [100.0 + i for i in range(300)]
    panel = _single_ticker_panel("AAA", closes)
    out = Momentum12m1m().compute(panel)

    # Rows before day 252 have raw=NaN → valid_flag must be False
    assert not out.iloc[:252]["valid_flag"].any()


def test_valid_flag_matches_finite_triple() -> None:
    """valid_flag == (raw, winsorized, zscore all finite)."""
    rng = np.random.default_rng(42)
    panel = _multi_ticker_panel(n_tickers=5, n_days=300, rng=rng)
    out = Momentum12m1m().compute(panel)

    triple_finite = (
        np.isfinite(out["raw_value"])
        & np.isfinite(out["winsorized_value"])
        & np.isfinite(out["zscore_value"])
    )
    assert (out["valid_flag"] == triple_finite).all()


def test_tradable_flag_combines_valid_factor_and_panel_eligibility() -> None:
    """Finite factor math is not enough; ineligible panel rows are not tradable."""
    rng = np.random.default_rng(42)
    panel = _multi_ticker_panel(n_tickers=3, n_days=300, rng=rng)
    panel["eligible_flag"] = panel["ticker"] != "T001"

    out = Momentum12m1m().compute(panel)
    ineligible_valid = out[(out["ticker"] == "T001") & out["valid_flag"]]

    assert not ineligible_valid.empty
    assert not ineligible_valid["tradable_flag"].any()
    assert out.loc[(out["ticker"] != "T001") & out["valid_flag"], "tradable_flag"].all()


# --- Cross-sectional properties ---------------------------------------------
def test_zscore_has_zero_mean_per_date_when_enough_tickers() -> None:
    """After day 252, per-date zscore mean should be ~0 (population std)."""
    rng = np.random.default_rng(7)
    panel = _multi_ticker_panel(n_tickers=20, n_days=300, rng=rng)
    out = Momentum12m1m().compute(panel)

    # Sample a date that has all 20 tickers eligible
    valid_out = out[out["valid_flag"]]
    per_date_mean = valid_out.groupby("date")["zscore_value"].mean()
    # All per-date zscore means should be ~0
    assert per_date_mean.abs().max() < 1e-10


def test_per_date_winsorize_trims_extremes() -> None:
    """A single 10-sigma outlier on one date is clipped to the 99th percentile."""
    rng = np.random.default_rng(13)
    panel = _multi_ticker_panel(n_tickers=50, n_days=275, rng=rng)

    # Inject a huge outlier on one specific (ticker, date) to blow up raw_value
    outlier_ticker = "T000"
    outlier_date_idx = 270  # after day 252 eligibility starts

    # Massive down-spike: force adj_close on day outlier_date_idx - 21 (the lag-21
    # point) to be tiny, so log(P[t-21]/P[t-252]) is hugely negative.
    mask = (panel["ticker"] == outlier_ticker) & (
        panel["date"] == panel["date"].unique()[outlier_date_idx - 21]
    )
    panel.loc[mask, "adj_close"] = 0.01

    out = Momentum12m1m().compute(panel)
    outlier_row = out[
        (out["ticker"] == outlier_ticker)
        & (out["date"] == panel["date"].unique()[outlier_date_idx])
    ].iloc[0]

    # Winsorized value should be strictly less extreme than raw
    assert abs(outlier_row["winsorized_value"]) < abs(outlier_row["raw_value"])


# --- σ-algebra measurability -------------------------------------------------
def test_measurability_truncation_stability() -> None:
    """Spec §4.1: factor at date t depends only on inputs at dates ≤ t.

    Compute on full panel, then on panel truncated at some date t_cut, then
    assert values at dates ≤ t_cut are bit-identical in both outputs.
    """
    rng = np.random.default_rng(99)
    full_panel = _multi_ticker_panel(n_tickers=5, n_days=320, rng=rng)

    # Cut off the last 40 days — forget about the future
    all_dates = full_panel["date"].unique()
    t_cut = all_dates[-41]
    truncated = full_panel[full_panel["date"] <= t_cut].copy()

    full_out = Momentum12m1m().compute(full_panel)
    trunc_out = Momentum12m1m().compute(truncated)

    # Align: restrict full_out to dates ≤ t_cut
    full_masked = (
        full_out[full_out["date"] <= t_cut].sort_values(["ticker", "date"]).reset_index(drop=True)
    )
    trunc_sorted = trunc_out.sort_values(["ticker", "date"]).reset_index(drop=True)

    pd.testing.assert_frame_equal(
        full_masked[["date", "ticker", "raw_value"]],
        trunc_sorted[["date", "ticker", "raw_value"]],
        check_dtype=False,
    )


# --- Output contract ---------------------------------------------------------
def test_missing_required_columns_raises() -> None:
    df = pd.DataFrame({"date": [pd.Timestamp("2023-01-02")], "ticker": ["AAA"]})  # no adj_close
    with pytest.raises(ValueError, match="missing required columns"):
        Momentum12m1m().compute(df)


def test_output_row_validates_as_factor_observation() -> None:
    """Sample a valid row, round-trip through the Pydantic FactorObservation."""
    rng = np.random.default_rng(42)
    panel = _multi_ticker_panel(n_tickers=10, n_days=260, rng=rng)
    out = Momentum12m1m().compute(panel)

    valid_rows = out[out["valid_flag"]].head(1)
    row = valid_rows.iloc[0]

    FactorObservation(
        date=row["date"].date() if hasattr(row["date"], "date") else row["date"],
        ticker=str(row["ticker"]),
        factor_name=str(row["factor_name"]),
        raw_value=float(row["raw_value"]),
        winsorized_value=float(row["winsorized_value"]),
        zscore_value=float(row["zscore_value"]),
        valid_flag=bool(row["valid_flag"]),
        tradable_flag=bool(row["tradable_flag"]),
        feature_snapshot_id=str(row["feature_snapshot_id"]),
    )


def test_feature_snapshot_id_stable_on_same_input() -> None:
    rng = np.random.default_rng(42)
    panel = _multi_ticker_panel(n_tickers=5, n_days=260, rng=rng)

    a = Momentum12m1m().compute(panel)
    b = Momentum12m1m().compute(panel)
    assert a["feature_snapshot_id"].iloc[0] == b["feature_snapshot_id"].iloc[0]
    assert len(a["feature_snapshot_id"].iloc[0]) == 64  # sha256 hex


def test_feature_snapshot_id_changes_on_perturbed_input() -> None:
    rng = np.random.default_rng(42)
    panel_a = _multi_ticker_panel(n_tickers=5, n_days=260, rng=rng)
    panel_b = panel_a.copy()
    panel_b.loc[0, "adj_close"] = panel_b.loc[0, "adj_close"] * 1.0001  # tiny nudge

    a = Momentum12m1m().compute(panel_a)
    b = Momentum12m1m().compute(panel_b)
    assert a["feature_snapshot_id"].iloc[0] != b["feature_snapshot_id"].iloc[0]
