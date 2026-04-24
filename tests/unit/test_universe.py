"""Day 2 acceptance: universe filter (spec §7).

Exercises every independent rule plus the invariants and the spec boundary
cases ($5.00/$5.01 price floor, 252-day history). No Polygon calls — uses
the engineered ``stock_daily_panel`` fixture from conftest.
"""

from __future__ import annotations

import pandas as pd

from aegis.config import UniverseConfig, load_all
from aegis.data.schema import UniverseRow
from aegis.data.universe import FAIL_REASONS, build_universe_flags


def _cfg() -> UniverseConfig:
    """Real universe config from configs/universe.yaml — no hand-written dicts."""
    return load_all().universe


def test_returns_one_row_per_date_ticker(stock_daily_panel: pd.DataFrame) -> None:
    out = build_universe_flags(stock_daily_panel, _cfg())
    assert len(out) == len(stock_daily_panel)
    assert not out.duplicated(subset=["date", "ticker"]).any()


def test_price_floor_at_exact_threshold(stock_daily_panel: pd.DataFrame) -> None:
    """Spec §7: price ≥ $5. $5.01 passes; $4.99 fails."""
    out = build_universe_flags(stock_daily_panel, _cfg())

    # T_BOUND_PASS is the $5.01 flat stock. From day ≥ 252 (history_ok=True)
    # and t-1 present (day ≥ 1), it should be eligible.
    pass_stock = out[out["ticker"] == "T_BOUND_PASS"].sort_values("date").reset_index(drop=True)
    eligible_days = pass_stock[pass_stock["eligible_flag"]]
    assert len(eligible_days) > 0
    assert eligible_days["price_ok"].all()

    # T_FAIL_PRICE is the $4.99 flat stock. After day ≥ 252 (history_ok=True),
    # it must fail on price_ok specifically.
    fail_stock = out[out["ticker"] == "T_FAIL_PRICE"].sort_values("date").reset_index(drop=True)
    after_history = fail_stock.iloc[252:]
    assert not after_history["price_ok"].any()
    assert (after_history["fail_reason"] == FAIL_REASONS["price_ok"]).all()


def test_price_uses_t_minus_1_close_not_same_day(stock_daily_panel: pd.DataFrame) -> None:
    """T_T1_DISC has day-0 close $4, day-1+ close $6.

    On day 1: t-1 close is $4 → price_ok=False (even though today's close is
    $6). On day 2+: t-1 close is $6 → price_ok=True. This proves we're using
    lagged prices, i.e. the F_{t-1}-measurable decision required by spec §4.1.
    """
    out = build_universe_flags(stock_daily_panel, _cfg())
    stock = out[out["ticker"] == "T_T1_DISC"].sort_values("date").reset_index(drop=True)

    # Day 0: first day of the stock, no t-1 close. price_ok must be False
    # (NaN >= 5 → False after fillna(False)).
    assert stock.loc[0, "price_ok"] == False  # noqa: E712
    # Day 1: t-1 close was $4 < $5. price_ok must be False.
    assert stock.loc[1, "price_ok"] == False  # noqa: E712
    # Day 2: t-1 close was $6 >= $5. price_ok must be True.
    assert stock.loc[2, "price_ok"] == True  # noqa: E712


def test_history_requires_252_prior_days(stock_daily_panel: pd.DataFrame) -> None:
    """For a baseline stock, history_ok flips at day index 252."""
    out = build_universe_flags(stock_daily_panel, _cfg())
    stock = out[out["ticker"] == "T_PASS_NYSE"].sort_values("date").reset_index(drop=True)

    # Index 251 = 252nd day of data. Per the rule (cumcount >= 252), this is
    # still the 252nd row (indices 0..251) so history_ok must still be False.
    assert stock.loc[251, "history_ok"] == False  # noqa: E712
    assert stock.loc[252, "history_ok"] == True  # noqa: E712


def test_short_history_stock_never_becomes_eligible(stock_daily_panel: pd.DataFrame) -> None:
    """T_FAIL_HIST has only 200 days of data. No row ever passes history_ok."""
    out = build_universe_flags(stock_daily_panel, _cfg())
    stock = out[out["ticker"] == "T_FAIL_HIST"]
    assert not stock["history_ok"].any()
    assert not stock["eligible_flag"].any()


def test_exchange_filter_rejects_otc(stock_daily_panel: pd.DataFrame) -> None:
    out = build_universe_flags(stock_daily_panel, _cfg())
    stock = out[out["ticker"] == "T_FAIL_EXCH"]
    assert not stock["exchange_ok"].any()
    assert not stock["eligible_flag"].any()


def test_common_share_filter_rejects_preferred(stock_daily_panel: pd.DataFrame) -> None:
    """T_FAIL_SHARE has ticker_type='PFD' (preferred). Must never pass
    common_share_ok, which is the CS-only rule from the 2026-04-23 decision."""
    out = build_universe_flags(stock_daily_panel, _cfg())
    stock = out[out["ticker"] == "T_FAIL_SHARE"]
    assert not stock["common_share_ok"].any()
    assert not stock["eligible_flag"].any()


def test_fail_reason_is_first_failing_rule(stock_daily_panel: pd.DataFrame) -> None:
    """T_MULTIFAIL fails both common_share (PFD) AND exchange (OTC). The rule
    order declared in universe._RULE_ORDER puts common_share first, so the
    reported fail_reason must be 'share_class_not_common' — not 'exchange_not_allowed'.
    This pins rule ordering against silent drift."""
    out = build_universe_flags(stock_daily_panel, _cfg())
    stock = out[out["ticker"] == "T_MULTIFAIL"]

    assert not stock["common_share_ok"].any()
    assert not stock["exchange_ok"].any()
    assert (stock["fail_reason"] == FAIL_REASONS["common_share_ok"]).all()
    assert (stock["fail_reason"] != FAIL_REASONS["exchange_ok"]).all()


def test_eligible_iff_no_fail_reason(stock_daily_panel: pd.DataFrame) -> None:
    """The core UniverseRow invariant applied at frame level."""
    out = build_universe_flags(stock_daily_panel, _cfg())

    # eligible_flag == (fail_reason is null)
    assert (out["eligible_flag"] == out["fail_reason"].isna()).all()

    # every ineligible row has a fail_reason in the known catalogue
    ineligible_reasons = set(out.loc[~out["eligible_flag"], "fail_reason"].unique())
    assert ineligible_reasons.issubset(set(FAIL_REASONS.values()))


def test_output_rows_validate_as_universe_row(stock_daily_panel: pd.DataFrame) -> None:
    """Sample rows and round-trip through the Pydantic UniverseRow. Catches
    type/value drift between the filter's output and the schema contract."""
    out = build_universe_flags(stock_daily_panel, _cfg())

    # Sample a mix of eligible and ineligible rows deterministically.
    sample = pd.concat(
        [
            out[out["eligible_flag"]].head(5),
            out[~out["eligible_flag"]].head(5),
        ]
    )
    for _, row in sample.iterrows():
        UniverseRow(
            date=row["date"].date() if hasattr(row["date"], "date") else row["date"],
            ticker=str(row["ticker"]),
            eligible_flag=bool(row["eligible_flag"]),
            price_ok=bool(row["price_ok"]),
            history_ok=bool(row["history_ok"]),
            exchange_ok=bool(row["exchange_ok"]),
            common_share_ok=bool(row["common_share_ok"]),
            fail_reason=(None if pd.isna(row["fail_reason"]) else str(row["fail_reason"])),
        )


def test_panel_missing_required_column_raises(stock_daily_panel: pd.DataFrame) -> None:
    bad = stock_daily_panel.drop(columns=["raw_close"])
    try:
        build_universe_flags(bad, _cfg())
    except ValueError as e:
        assert "raw_close" in str(e)
    else:
        raise AssertionError("expected ValueError for missing column")
