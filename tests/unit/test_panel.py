"""Module A acceptance + Day 3 unit coverage for the panel builder.

The spec §6 Module A acceptance test (reconstructed S&P 500 membership
within 1 name) stays ``xfail`` until Week 2 lands the historical-index
tracker. Everything else — ret_1d math, eligibility propagation, Parquet
round-trip, snapshot-id stability — is exercised here against the
engineered ``stock_daily_panel`` fixture from conftest (no live Polygon
calls).
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from aegis.config import AegisConfig, load_all
from aegis.data import panel as panel_module
from aegis.data.panel import _PANEL_COLUMNS, _finalize_panel, build_panel
from aegis.utils.hashing import sha256_dataframe


# --- Preserved Module A acceptance stub -------------------------------------
@pytest.mark.xfail(strict=True, reason="Module A S&P 500 index-history deferred to Week 2")
def test_sp500_reconstruction_within_1_name() -> None:
    """Reconstructed S&P 500 membership on any past date matches published within 1 name."""
    raise NotImplementedError


# --- Fixtures local to this test module --------------------------------------
@pytest.fixture(scope="module")
def cfg() -> AegisConfig:
    return load_all()


@pytest.fixture
def finalized_panel(cfg: AegisConfig, stock_daily_panel: pd.DataFrame) -> pd.DataFrame:
    """Apply _finalize_panel to the engineered fixture. Reusable across tests."""
    return _finalize_panel(stock_daily_panel, cfg)


# --- ret_1d correctness ------------------------------------------------------
def test_ret_1d_matches_log_ratio_for_hand_checked_ticker(
    finalized_panel: pd.DataFrame, stock_daily_panel: pd.DataFrame
) -> None:
    """Spec §4.1 measurability: ret_1d at row t uses adj_close[t]/adj_close[t-1]."""
    ticker = "T_PASS_NYSE"
    src = (
        stock_daily_panel[stock_daily_panel["ticker"] == ticker]
        .sort_values("date")
        .reset_index(drop=True)
    )
    out = (
        finalized_panel[finalized_panel["ticker"] == ticker]
        .sort_values("date")
        .reset_index(drop=True)
    )

    # Row 5: expect log(adj_close[5] / adj_close[4])
    expected = math.log(src.loc[5, "adj_close"] / src.loc[4, "adj_close"])
    assert abs(out.loc[5, "ret_1d"] - expected) < 1e-12


def test_ret_1d_is_nan_on_first_row_per_ticker(finalized_panel: pd.DataFrame) -> None:
    """Schema: ret_1d must be None/NaN on each ticker's first day."""
    firsts = finalized_panel.sort_values(["ticker", "date"]).groupby("ticker").head(1)
    assert firsts["ret_1d"].isna().all()


# --- Universe flag propagation ----------------------------------------------
def test_eligible_flag_propagated_from_universe_filter(finalized_panel: pd.DataFrame) -> None:
    """After Day 252 a baseline-pass ticker is eligible; a PFD ticker never is."""
    pass_stock = finalized_panel[finalized_panel["ticker"] == "T_PASS_NYSE"].sort_values("date")
    # At index ≥ 252 the baseline stock should be eligible
    assert pass_stock.iloc[253]["eligible_flag"] == True  # noqa: E712

    # PFD stock is never eligible
    pfd = finalized_panel[finalized_panel["ticker"] == "T_FAIL_SHARE"]
    assert not pfd["eligible_flag"].any()


# --- Column contract ---------------------------------------------------------
def test_panel_has_canonical_columns_in_order(finalized_panel: pd.DataFrame) -> None:
    """Panel output column order is stable (hash-stable across rebuilds)."""
    assert tuple(finalized_panel.columns) == _PANEL_COLUMNS


def test_gics_columns_are_null_in_week1(finalized_panel: pd.DataFrame) -> None:
    """GICS sector/industry are Week 2 — stay None in Week 1."""
    assert finalized_panel["gics_sector"].isna().all()
    assert finalized_panel["gics_industry"].isna().all()


# --- data_snapshot_id integrity ---------------------------------------------
def test_data_snapshot_id_stamped_on_every_row(finalized_panel: pd.DataFrame) -> None:
    """Every row carries the same snapshot_id (one panel = one snapshot)."""
    unique_ids = finalized_panel["data_snapshot_id"].unique()
    assert len(unique_ids) == 1
    assert len(unique_ids[0]) == 64  # sha256 hex


def test_data_snapshot_id_stable_on_same_input(
    cfg: AegisConfig, stock_daily_panel: pd.DataFrame
) -> None:
    """Re-finalizing the identical raw panel produces the same snapshot_id."""
    a = _finalize_panel(stock_daily_panel, cfg)
    b = _finalize_panel(stock_daily_panel, cfg)
    assert a["data_snapshot_id"].iloc[0] == b["data_snapshot_id"].iloc[0]


def test_data_snapshot_id_changes_on_modified_input(
    cfg: AegisConfig, stock_daily_panel: pd.DataFrame
) -> None:
    """Perturbing one close flips the snapshot_id — integrity is live."""
    perturbed = stock_daily_panel.copy()
    perturbed.loc[0, "adj_close"] = perturbed.loc[0, "adj_close"] + 0.01

    baseline = _finalize_panel(stock_daily_panel, cfg)
    modified = _finalize_panel(perturbed, cfg)

    assert baseline["data_snapshot_id"].iloc[0] != modified["data_snapshot_id"].iloc[0]


# --- Parquet round-trip + end-to-end build_panel -----------------------------
def test_build_panel_writes_parquet_and_round_trips(
    cfg: AegisConfig,
    stock_daily_panel: pd.DataFrame,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: build_panel → Parquet → read back → content matches.

    Monkeypatches ``load_polygon_daily`` to return the fixture, so no live
    Polygon calls. Also redirects the output path into tmp_path to avoid
    polluting the real ``data/processed/`` directory during tests.
    """

    # Stub out the live loader
    def _fake_loader(*args: object, **kwargs: object) -> pd.DataFrame:
        return stock_daily_panel.copy()

    monkeypatch.setattr(panel_module, "load_polygon_daily", _fake_loader)

    # Redirect output dir
    test_cfg = cfg.model_copy(
        update={
            "data": cfg.data.model_copy(
                update={
                    "paths": cfg.data.paths.model_copy(update={"processed": tmp_path}),
                }
            )
        }
    )

    out_path = build_panel(test_cfg, tickers=["AAPL"], sleep_between_calls=0)
    assert out_path.exists()
    assert out_path.parent == tmp_path

    read_back = pd.read_parquet(out_path)
    assert tuple(read_back.columns) == _PANEL_COLUMNS
    assert len(read_back) == len(stock_daily_panel)


def test_build_panel_raises_on_empty_loader_output(
    cfg: AegisConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Polygon returns 0 rows (e.g., bad date window), fail loudly."""

    def _empty_loader(*args: object, **kwargs: object) -> pd.DataFrame:
        return pd.DataFrame(columns=list(_PANEL_COLUMNS[:5]))  # empty

    monkeypatch.setattr(panel_module, "load_polygon_daily", _empty_loader)

    with pytest.raises(RuntimeError, match="0 rows"):
        build_panel(cfg, tickers=["AAPL"], sleep_between_calls=0)


# --- hashing utility sanity checks ------------------------------------------
def test_sha256_dataframe_is_column_order_sensitive() -> None:
    """Reordering columns changes the hash — schema drift is detectable."""
    df1 = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    df2 = pd.DataFrame({"b": [3, 4], "a": [1, 2]})
    assert sha256_dataframe(df1) != sha256_dataframe(df2)


def test_sha256_dataframe_is_row_order_insensitive() -> None:
    """Row shuffling does NOT change the hash — order isn't content."""
    df1 = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    df2 = pd.DataFrame({"a": [3, 1, 2], "b": [6, 4, 5]})
    assert sha256_dataframe(df1) == sha256_dataframe(df2)
