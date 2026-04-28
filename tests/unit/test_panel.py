"""Module A acceptance + Day 3 unit coverage for the panel builder.

Spec §6 Module A acceptance — reconstructed S&P 500 membership within
1 name on two ground-truth dates — flipped on Week 2 Day 10. Everything
else (ret_1d math, eligibility propagation, Parquet round-trip,
snapshot-id stability, build_panel_for_date) is exercised against the
engineered ``stock_daily_panel`` fixture from conftest (no live Polygon
calls).
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from aegis.config import AegisConfig, load_all
from aegis.data import panel as panel_module
from aegis.data.index_membership import active_on, load_sp500_membership
from aegis.data.panel import _PANEL_COLUMNS, _finalize_panel, build_panel
from aegis.data.ticker_reference import load_ticker_aliases
from aegis.utils.hashing import sha256_dataframe

# tests/unit/test_panel.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE = _REPO_ROOT / "data" / "reference"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# --- Module A §6 acceptance — Day 10 flip ------------------------------------


def _read_ticker_fixture(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def _historical_symbol(
    canonical: str,
    check_date: date,
    aliases: pd.DataFrame,
) -> str | None:
    """Map ``canonical`` back to whichever symbol the lineage was traded under on ``check_date``.

    Mirrors :func:`aegis.data.ticker_reference.canonicalize_ticker` semantics
    (walk alias intervals, return the alias active on the date) plus an
    extra "lineage didn't exist yet" filter: if every alias row for this
    canonical has ``effective_from > check_date``, the entity hadn't
    entered the chain yet and we drop it from the comparison entirely.
    """
    rows = aliases[aliases["canonical_ticker"] == canonical]
    if rows.empty:
        return canonical

    ts = pd.Timestamp(check_date)
    from_ok = rows["effective_from"].isna() | (rows["effective_from"] <= ts)
    to_ok = rows["effective_to"].isna() | (ts < rows["effective_to"])
    matches = rows[from_ok & to_ok]
    if not matches.empty:
        return str(matches["alias"].iloc[0])

    # No interval contains check_date. Two cases:
    # 1. check_date is BEFORE the earliest non-NaT effective_from → lineage hadn't started
    # 2. check_date is AFTER all effective_to → canonical itself is the active symbol
    earliest = rows["effective_from"].min()
    if pd.notna(earliest) and ts < earliest:
        return None
    return canonical


def _strip_dot(s: str) -> str:
    return s.replace(".", "")


@pytest.mark.parametrize(
    ("check_date", "fixture_file"),
    [
        (date(2018, 6, 15), "sp500_20180615.txt"),  # pre-Tesla era
        (date(2021, 1, 4), "sp500_20210104.txt"),  # post-Tesla, post-Q4-2020 rebalance
    ],
)
def test_sp500_reconstruction_within_1_name(
    check_date: date,
    fixture_file: str,
) -> None:
    """Spec §6 Module A — reconstruction matches published constituents within 1 name.

    Two ground-truth dates from different market regimes so a curated
    snapshot on one era can't mask drift on the other. Source: iShares
    Core S&P 500 ETF (IVV) historical holdings, fetched verbatim
    (historical symbols preserved — no canonicalization on the truth side).

    Comparison protocol:
    1. ``active_on(check_date, membership)`` returns canonical (current)
       symbols inherited from Wikipedia's current-table.
    2. Each canonical is mapped backward to its on-date historical symbol
       via :func:`_historical_symbol` (driven by ``ticker_aliases.csv``).
       Lineages that didn't exist on ``check_date`` are dropped.
    3. Both sides are dot-stripped (iShares strips Class B notation,
       Wikipedia preserves it) before computing symmetric_difference.
    """
    membership = load_sp500_membership(_REFERENCE / "sp500_membership.csv")
    aliases = load_ticker_aliases(_REFERENCE / "ticker_aliases.csv")

    candidates = active_on(check_date, membership)
    reconstructed: set[str] = set()
    for canonical in candidates:
        sym = _historical_symbol(canonical, check_date, aliases)
        if sym is not None:
            reconstructed.add(sym)

    truth = _read_ticker_fixture(_FIXTURES / fixture_file)

    recon_norm = {_strip_dot(t) for t in reconstructed}
    truth_norm = {_strip_dot(t) for t in truth}
    diff = recon_norm.symmetric_difference(truth_norm)

    assert len(diff) <= 1, (
        f"[{check_date}] symmetric_difference={len(diff)}: "
        f"only-in-reconstructed={sorted(recon_norm - truth_norm)}, "
        f"only-in-truth={sorted(truth_norm - recon_norm)}"
    )


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


# --- build_panel_for_date — Day 10 -------------------------------------------


def test_build_panel_for_date_uses_membership_for_tickers(
    cfg: AegisConfig,
    stock_daily_panel: pd.DataFrame,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """build_panel_for_date(cfg, sample_date, membership) restricts to active_on(sample_date).

    Constructs a tiny membership frame with two tickers — one currently a
    member, one removed before sample_date — and asserts the loader is
    called with exactly the active ticker.
    """
    sample_date = date(2020, 6, 15)
    membership = pd.DataFrame(
        {
            "ticker": ["AAA", "BBB"],
            "name": ["Active Co", "Removed Co"],
            "wiki_sector": [None, None],
            "wiki_sub_industry": [None, None],
            "date_added": [pd.Timestamp("2010-01-01"), pd.Timestamp("2010-01-01")],
            "date_removed": [pd.NaT, pd.Timestamp("2019-12-31")],
            "cik_code": [pd.NA, pd.NA],
        }
    )

    received_tickers: list[str] = []

    def _capture_loader(*, tickers: list[str], **kwargs: object) -> pd.DataFrame:
        received_tickers.extend(tickers)
        return stock_daily_panel.copy()

    monkeypatch.setattr(panel_module, "load_polygon_daily", _capture_loader)

    # Redirect output dir to avoid polluting data/processed/
    test_cfg = cfg.model_copy(
        update={
            "data": cfg.data.model_copy(
                update={"paths": cfg.data.paths.model_copy(update={"processed": tmp_path})}
            )
        }
    )

    out_path = panel_module.build_panel_for_date(
        test_cfg, sample_date, membership, sleep_between_calls=0
    )
    assert out_path.exists()
    assert received_tickers == ["AAA"], (
        f"build_panel_for_date should pass only active_on(sample_date) tickers "
        f"to the loader; got {received_tickers}"
    )


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
