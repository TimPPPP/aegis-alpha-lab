"""Module A panel builder (spec §6, §7; Week 1 Day 3).

Composes the loader + universe filter + Parquet write into a single
``build_panel`` function. Produces ``data/processed/daily_panel_week1.parquet``
— the artifact every downstream module (features, risk, optimizer) consumes.

For Week 1 we use a hardcoded ticker list (:data:`WEEK1_TICKERS`) rather than
historical S&P 500 membership. Real index-membership tracking is Week 2. The
Week 1 panel is **not survivorship-bias-free** — any output built on it must
carry a "NOT PRODUCTION GRADE" caveat until the tracker lands.

Data flow:
    load_polygon_daily
        → sort by (ticker, date)
        → compute ret_1d = log(adj_close / adj_close.shift(1)) per ticker
        → build_universe_flags → merge eligible_flag in
        → compute data_snapshot_id over the raw loader output
        → stamp data_snapshot_id into every row
        → write Parquet
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from aegis.config import AegisConfig
from aegis.data.polygon_loader import OUTPUT_COLUMNS as LOADER_COLUMNS
from aegis.data.polygon_loader import load_polygon_daily
from aegis.data.universe import build_universe_flags
from aegis.utils.hashing import sha256_dataframe

# Week 1 universe — hardcoded blue-chips, all CS on NYSE/NASDAQ.
# Historical index constituency is Week 2. Size tuned to fit Polygon
# free-tier rate-limit budget (~5 min for a full pull).
WEEK1_TICKERS: tuple[str, ...] = (
    "AAPL",  # Apple
    "MSFT",  # Microsoft
    "GOOGL",  # Alphabet Class A
    "AMZN",  # Amazon
    "NVDA",  # Nvidia
    "META",  # Meta Platforms
    "JPM",  # JPMorgan Chase
    "JNJ",  # Johnson & Johnson
)

# Final panel column order (StockDailyRow-shape + loader-derived columns).
_PANEL_COLUMNS: tuple[str, ...] = (
    "date",
    "ticker",
    "exchange",
    "ticker_type",
    "is_common_share",
    "raw_close",
    "adj_close",
    "ret_1d",
    "volume",
    "shares_out",
    "mcap",
    "gics_sector",
    "gics_industry",
    "eligible_flag",
    "data_snapshot_id",
)


def build_panel(
    cfg: AegisConfig,
    tickers: Sequence[str] | None = None,
    *,
    sleep_between_calls: float = 12.5,
    panel_filename: str | None = None,
) -> Path:
    """Build the Week 1 PIT panel end-to-end and write to Parquet.

    Args:
        cfg: Loaded :class:`AegisConfig`. Uses ``cfg.data`` for paths,
            ``cfg.universe`` for eligibility rules.
        tickers: Override the default Week 1 universe. Passing None uses
            :data:`WEEK1_TICKERS`.
        sleep_between_calls: Seconds to wait between Polygon API calls.
            Defaults to 12.5 (free-tier safe). Paid-tier users pass 0.
        panel_filename: Override the basename of the output Parquet
            (joined onto ``cfg.data.paths.processed``). Defaults to
            ``cfg.data.snapshot.panel_filename`` (Week 1's filename).
            Day 13's full-universe slice passes a date-tagged filename
            so its artifacts don't collide with Week 1's.

    Returns:
        Path to the written Parquet file.
    """
    tickers_used = tuple(tickers) if tickers is not None else WEEK1_TICKERS

    raw = load_polygon_daily(
        tickers=list(tickers_used),
        start=cfg.data.date_range.start,
        end=cfg.data.date_range.end,
        sleep_between_calls=sleep_between_calls,
    )
    if raw.empty:
        raise RuntimeError(
            f"load_polygon_daily returned 0 rows for "
            f"{len(tickers_used)} tickers over {cfg.data.date_range.start} "
            f"to {cfg.data.date_range.end}. Check POLYGON_API_KEY and date window."
        )

    panel = _finalize_panel(raw, cfg)

    out_basename = panel_filename or cfg.data.snapshot.panel_filename
    out_path = cfg.data.paths.processed / out_basename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(out_path, index=False)
    return out_path


def build_panel_for_date(
    cfg: AegisConfig,
    sample_date: date,
    membership_df: pd.DataFrame,
    *,
    sleep_between_calls: float = 12.5,
) -> Path:
    """Build a daily panel restricted to S&P 500 members on ``sample_date``.

    Composes Day 8's index-membership gate (:func:`aegis.data.index_membership.active_on`)
    into the panel pipeline. The §7 sample filters (common-share /
    exchange / price ≥ $5 / ≥252-day history) compose downstream inside
    ``build_universe_flags``.

    The full :data:`AegisConfig.data.date_range` is pulled from Polygon
    (the eventual panel covers many trading days even though the
    membership gate is anchored at one ``sample_date``). Day 13's full-
    universe scale test consumes this primitive directly.
    """
    from aegis.data.index_membership import active_on

    tickers = sorted(active_on(sample_date, membership_df))
    if not tickers:
        raise RuntimeError(
            f"active_on({sample_date}) returned 0 tickers — check the membership "
            f"CSV; the index reconstruction window may not cover this date"
        )
    return build_panel(cfg, tickers=tickers, sleep_between_calls=sleep_between_calls)


def _finalize_panel(raw: pd.DataFrame, cfg: AegisConfig) -> pd.DataFrame:
    """Internal: apply ret_1d + universe filter + snapshot id + column order.

    Split out from ``build_panel`` so unit tests can exercise the
    post-loader pipeline without mocking the Polygon client itself.
    """
    # 1. Sort for deterministic groupby operations
    sorted_raw = raw.sort_values(["ticker", "date"]).reset_index(drop=True)

    # 2. Log return on adjusted close, per ticker. First row per ticker → NaN.
    sorted_raw["ret_1d"] = (
        np.log(sorted_raw["adj_close"]).groupby(sorted_raw["ticker"], sort=False).diff()
    )

    # 3. Universe eligibility — right-join on (date, ticker)
    universe = build_universe_flags(sorted_raw, cfg.universe)
    merged = sorted_raw.merge(
        universe[["date", "ticker", "eligible_flag"]],
        on=["date", "ticker"],
        how="left",
    )

    # 4. GICS columns — Week 2 populates; None for now.
    merged["gics_sector"] = None
    merged["gics_industry"] = None

    # 5. data_snapshot_id: hash the RAW loader output (before our own derived
    #    columns). Stamping the ID into every row preserves per-row replay.
    snapshot_id = sha256_dataframe(raw[list(LOADER_COLUMNS)])
    merged["data_snapshot_id"] = snapshot_id

    # 6. Enforce column order
    return merged.loc[:, list(_PANEL_COLUMNS)].reset_index(drop=True)


__all__ = ["WEEK1_TICKERS", "build_panel"]
