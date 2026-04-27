"""Universe eligibility filter (spec §7, Week 1 Day 2).

Takes the raw daily panel from :mod:`aegis.data.polygon_loader` and applies
four independent rules to produce one :class:`aegis.data.schema.UniverseRow`
per (date, ticker). The rules follow spec §7 and ``configs/universe.yaml``:

    1. common_share_ok   — Polygon ticker_type == "CS"
    2. exchange_ok       — exchange in cfg.exchanges (typically NYSE/AMEX/NASDAQ)
    3. history_ok        — ≥ cfg.min_history_days of prior rows for this ticker
    4. price_ok          — t-1 raw_close ≥ cfg.price_floor_usd

Rule ordering matters because ``fail_reason`` reports the FIRST failing rule.
The chosen order (fundamental → daily-varying) gives crisper debug output:
"Why is GE out on 2021-06-14?" — "exchange_not_allowed" is more actionable
than "price_below_floor" when the underlying cause is a delisting.

Lookahead discipline (spec §4.1). The universe decision for day t must be
``F_{t-1}``-measurable, so ``price_ok`` is evaluated on t-1's raw_close, not
today's. First day of a ticker automatically fails ``history_ok`` (row index
0 < min_history_days) which masks the price NaN.

**CS-only policy:** Per spec §7 and Tim's 2026-04-23 confirmation, only
``ticker_type == "CS"`` counts as a common share. ADRs (ADRC), preferred
(PFD), ETFs/ETNs, units, warrants, and rights are all rejected.
"""

from __future__ import annotations

import pandas as pd

from aegis.config import UniverseConfig

_COMMON_SHARE_TYPE: str = "CS"

_REQUIRED_INPUT_COLUMNS: frozenset[str] = frozenset(
    {"date", "ticker", "exchange", "ticker_type", "raw_close"}
)

_OUTPUT_COLUMNS: tuple[str, ...] = (
    "date",
    "ticker",
    "eligible_flag",
    "price_ok",
    "history_ok",
    "exchange_ok",
    "common_share_ok",
    "fail_reason",
)


def build_universe_flags(panel: pd.DataFrame, cfg: UniverseConfig) -> pd.DataFrame:
    """Produce one eligibility row per (date, ticker).

    Args:
        panel: Long-format DataFrame from
            :func:`aegis.data.polygon_loader.load_polygon_daily`. Must
            contain at least: date, ticker, exchange, ticker_type, raw_close.
        cfg: Validated :class:`UniverseConfig` from configs/universe.yaml.

    Returns:
        Long-format DataFrame with columns:
            date, ticker, eligible_flag, price_ok, history_ok, exchange_ok,
            common_share_ok, fail_reason.
        ``fail_reason`` is None when eligible, else the name of the first failing
        rule (enum-like string; see :data:`FAIL_REASONS`).
    """
    missing = _REQUIRED_INPUT_COLUMNS - set(panel.columns)
    if missing:
        raise ValueError(f"panel missing required columns: {sorted(missing)}")

    # Sort (ticker, date) so groupby-shift and cumcount are deterministic.
    df = panel.sort_values(["ticker", "date"]).reset_index(drop=True)

    # --- Rule 1: common_share_ok --------------------------------------------
    df["common_share_ok"] = (df["ticker_type"] == _COMMON_SHARE_TYPE).astype(bool)

    # --- Rule 2: exchange_ok ------------------------------------------------
    allowed_exchanges = pd.Index(cfg.exchanges)
    df["exchange_ok"] = df["exchange"].isin(allowed_exchanges).astype(bool)

    # --- Rule 3: history_ok -------------------------------------------------
    # cumcount() gives 0 for first row per ticker, 1 for second, etc.
    # We want >= min_history_days of PRIOR rows, so the row at index N itself
    # has N prior rows. history_ok at index i = (i >= min_history_days).
    df["history_ok"] = (df.groupby("ticker").cumcount() >= cfg.min_history_days).astype(bool)

    # --- Rule 4: price_ok (t-1 close) ---------------------------------------
    prev_close = df.groupby("ticker")["raw_close"].shift(1)
    df["price_ok"] = (prev_close >= cfg.price_floor_usd).fillna(False).astype(bool)

    # --- fail_reason: first failing rule, in the declared order -------------
    df["fail_reason"] = _first_failing_reason(df)

    df["eligible_flag"] = df["fail_reason"].isna()

    return df.loc[:, list(_OUTPUT_COLUMNS)].reset_index(drop=True)


# Public enum-like catalogue so downstream code / logs / tests can reference
# stable strings without typos.
FAIL_REASONS: dict[str, str] = {
    "common_share_ok": "share_class_not_common",
    "exchange_ok": "exchange_not_allowed",
    "history_ok": "insufficient_history",
    "price_ok": "price_below_floor",
}

# Rule evaluation order. fail_reason is set to the first rule that fails.
_RULE_ORDER: tuple[str, ...] = (
    "common_share_ok",
    "exchange_ok",
    "history_ok",
    "price_ok",
)


def _first_failing_reason(df: pd.DataFrame) -> pd.Series:
    """Return a Series of fail_reason strings (or NA) per row.

    Implemented by chained masked-assign rather than per-row apply, so it stays
    vectorized for a 15M-row panel.
    """
    reason = pd.Series(pd.NA, index=df.index, dtype="string")
    for rule in _RULE_ORDER:
        mask = (~df[rule]) & reason.isna()
        reason.loc[mask] = FAIL_REASONS[rule]
    return reason


__all__ = ["FAIL_REASONS", "build_universe_flags"]
