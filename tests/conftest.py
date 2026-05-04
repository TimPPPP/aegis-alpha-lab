"""Shared pytest fixtures.

Heavy fixtures (Polygon-backed panels, real risk-model outputs) are expected
to be added once the corresponding modules land. For now, we provide a tiny
deterministic synthetic panel so unit tests can exercise operators and
aggregators without touching Polygon.

At import time we also load ``.env`` (if present) so that tests marked
``@pytest.mark.polygon`` pick up ``POLYGON_API_KEY`` without requiring the
user to ``source .env`` manually. Already-set env vars are never overridden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from aegis.backtest import _common as backtest_common_module
from aegis.config import AegisConfig, load_all
from aegis.data.panel import _finalize_panel
from aegis.ledger.schema import Artifact, Candidate, Experiment
from aegis.ledger.store import open_ledger
from aegis.utils.dotenv import load_dotenv_if_present

# Load .env at conftest import so @pytest.mark.polygon tests see POLYGON_API_KEY.
load_dotenv_if_present(Path(__file__).resolve().parent.parent)


@pytest.fixture(scope="session")
def rng() -> np.random.Generator:
    return np.random.default_rng(seed=42)


@pytest.fixture(scope="session")
def synthetic_panel(rng: np.random.Generator) -> pd.DataFrame:
    """A tiny synthetic daily panel: 50 names × 252 trading days.

    Columns: date, sid, ret, mcap, adv, sector. Not calibrated to real
    market data — intended for exercising operators and shape contracts.
    """
    n_names = 50
    n_days = 252
    dates = pd.bdate_range("2020-01-02", periods=n_days)
    sids = [f"SID{n:03d}" for n in range(n_names)]

    idx = pd.MultiIndex.from_product([dates, sids], names=["date", "sid"])
    rows = len(idx)

    returns = rng.normal(loc=0.0, scale=0.015, size=rows)
    mcap = rng.lognormal(mean=22.0, sigma=1.5, size=rows)
    adv = rng.lognormal(mean=15.0, sigma=1.0, size=rows)
    sector = rng.integers(low=0, high=11, size=rows)

    return pd.DataFrame(
        {"ret": returns, "mcap": mcap, "adv": adv, "sector": sector},
        index=idx,
    ).reset_index()


# --- Day 2 fixture: loader-shape panel for universe filter tests ------------
#
# Each row has the column shape produced by aegis.data.polygon_loader.OUTPUT_COLUMNS.
# It is deliberately engineered so that EACH of the four universe rules has
# at least one stock that fails it, plus boundary-case stocks for the spec's
# price floor ($5.00) and history window (252 days).
#
# Ticker conventions (stable across test runs):
#   T_PASS_NYSE   — baseline pass: NYSE, CS, $10.00, 400 days
#   T_PASS_NASD   — baseline pass: NASDAQ, CS, $25.00, 400 days
#   T_FAIL_SHARE  — fails common_share_ok: NYSE, PFD (preferred stock)
#   T_FAIL_EXCH   — fails exchange_ok: exchange "OTC" (unmapped MIC), CS
#   T_FAIL_HIST   — fails history_ok: NYSE, CS, only 200 days of data
#   T_FAIL_PRICE  — fails price_ok: NYSE, CS, constant $4.99
#   T_BOUND_PASS  — boundary price (pass): NYSE, CS, constant $5.01, 400 days
#   T_BOUND_FAIL  — boundary price (fail): alternating $4.99/$5.00 → t-1 always below
#   T_T1_DISC     — t-1 discipline: day-0 close $4.00, day-1+ close $6.00.
#                    Must fail price_ok on day 1 (t-1 was $4), pass from day 2+.
#   T_MULTIFAIL   — multi-fail: OTC exchange AND PFD — first failing rule
#                    (common_share) should be reported, not exchange.


_FIXTURE_N_DAYS = 400
_FIXTURE_START = pd.Timestamp("2020-01-02")


def _rows_for(
    ticker: str,
    *,
    exchange: str,
    ticker_type: str,
    close_series: pd.Series,
) -> pd.DataFrame:
    """Build one stock's rows, shape-aligned with polygon_loader.OUTPUT_COLUMNS."""
    dates = close_series.index
    n = len(dates)
    shares_out = 1_000_000.0
    raw_close = close_series.astype("float64")
    is_cs = ticker_type == "CS"
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "ticker": pd.array([ticker] * n, dtype="string"),
            "exchange": pd.array([exchange] * n, dtype="string"),
            "ticker_type": pd.array([ticker_type] * n, dtype="string"),
            "is_common_share": np.full(n, is_cs, dtype=bool),
            "raw_close": raw_close.to_numpy(),
            # adj_close == raw_close in the fixture (no corporate actions engineered)
            "adj_close": raw_close.to_numpy(),
            "volume": np.full(n, 1_000_000.0, dtype="float64"),
            "shares_out": np.full(n, shares_out, dtype="float64"),
            "mcap": (raw_close.to_numpy() * shares_out),
        }
    )


@pytest.fixture(scope="session")
def stock_daily_panel() -> pd.DataFrame:
    """Session-scoped engineered panel for universe-filter tests.

    Shape matches ``aegis.data.polygon_loader.OUTPUT_COLUMNS``. Each ticker
    is engineered to exercise exactly one universe rule (or, for
    T_MULTIFAIL, a multi-rule failure to assert fail_reason ordering).
    """
    full_dates = pd.bdate_range(_FIXTURE_START, periods=_FIXTURE_N_DAYS)

    parts: list[pd.DataFrame] = []

    # Baselines — pass all four rules
    parts.append(
        _rows_for(
            "T_PASS_NYSE",
            exchange="NYSE",
            ticker_type="CS",
            close_series=pd.Series(10.00, index=full_dates),
        )
    )
    parts.append(
        _rows_for(
            "T_PASS_NASD",
            exchange="NASDAQ",
            ticker_type="CS",
            close_series=pd.Series(25.00, index=full_dates),
        )
    )

    # T_FAIL_SHARE — fails common_share_ok (PFD is preferred, not common)
    parts.append(
        _rows_for(
            "T_FAIL_SHARE",
            exchange="NYSE",
            ticker_type="PFD",
            close_series=pd.Series(10.00, index=full_dates),
        )
    )

    # T_FAIL_EXCH — fails exchange_ok ("OTC" not in allowed set)
    parts.append(
        _rows_for(
            "T_FAIL_EXCH",
            exchange="OTC",
            ticker_type="CS",
            close_series=pd.Series(10.00, index=full_dates),
        )
    )

    # T_FAIL_HIST — fails history_ok (only 200 days present)
    short_dates = full_dates[:200]
    parts.append(
        _rows_for(
            "T_FAIL_HIST",
            exchange="NYSE",
            ticker_type="CS",
            close_series=pd.Series(10.00, index=short_dates),
        )
    )

    # T_FAIL_PRICE — fails price_ok ($4.99 flat; fires once history_ok passes)
    parts.append(
        _rows_for(
            "T_FAIL_PRICE",
            exchange="NYSE",
            ticker_type="CS",
            close_series=pd.Series(4.99, index=full_dates),
        )
    )

    # T_BOUND_PASS — boundary price PASS ($5.01 flat)
    parts.append(
        _rows_for(
            "T_BOUND_PASS",
            exchange="NYSE",
            ticker_type="CS",
            close_series=pd.Series(5.01, index=full_dates),
        )
    )

    # T_BOUND_FAIL — boundary FAIL. Alternating $4.99/$5.00 so that on every
    # "today == $5.00" day, t-1 close was $4.99, tripping the t-1 price rule.
    alternating = pd.Series(
        [4.99 if i % 2 == 0 else 5.00 for i in range(_FIXTURE_N_DAYS)],
        index=full_dates,
    )
    parts.append(
        _rows_for(
            "T_BOUND_FAIL",
            exchange="NYSE",
            ticker_type="CS",
            close_series=alternating,
        )
    )

    # T_T1_DISC — t-1 discipline: day 0 close $4, day 1+ close $6.
    # Day 1's t-1 is $4 → price_ok=False. Day 2+ t-1 is $6 → price_ok=True
    # (but history_ok still False until row index 252).
    tm1_series = pd.Series([4.00] + [6.00] * (_FIXTURE_N_DAYS - 1), index=full_dates)
    parts.append(
        _rows_for(
            "T_T1_DISC",
            exchange="NYSE",
            ticker_type="CS",
            close_series=tm1_series,
        )
    )

    # T_MULTIFAIL — multi-fail (OTC AND PFD). fail_reason must be
    # "share_class_not_common" (first rule in order), not "exchange_not_allowed".
    parts.append(
        _rows_for(
            "T_MULTIFAIL",
            exchange="OTC",
            ticker_type="PFD",
            close_series=pd.Series(10.00, index=full_dates),
        )
    )

    return pd.concat(parts, ignore_index=True)


# --- Week 3 Day 16 fundamentals fixture --------------------------------------
#
# An engineered ~45-row fundamentals frame, shape-aligned with the scraper's
# EXPECTED_COLUMNS. Encodes 7 distinct cases used by tests/unit/test_fundamentals.py
# AND tests/unit/test_earnings_yield.py (Day 17). Polygon-free by construction.
#
# Cases:
#   AAPL_X    — 8 normal quarterlies + 1 annual + 1 TTM (booby trap so
#               ttm_at correctly filters period_kind=='quarterly' only).
#               Sep FY-end (Q1 ends Dec, Q2 Mar, Q3 Jun, Q4 Sep).
#               revenues   = 100, 200, 300, 400, 500, 600, 700, 800
#               net_income = 10, 20, 30, 40, 50, 60, 70, 80
#   MSFT_X    — 8 quarterlies + 1 RESTATEMENT of FY24-Q3 (revenues 300 -> 350,
#               filed 30 days later). De-dupe by fiscal period must keep
#               the later filing.
#               Jun FY-end (Q1 Sep, Q2 Dec, Q3 Mar, Q4 Jun).
#   MAR_FY_X  — 8 quarterlies, March FY-end. Designed so that at as_of=2024-12-01
#               the latest 4 PIT-eligible quarters span FY24 and FY25
#               (FY24-Q3, FY24-Q4, FY25-Q1, FY25-Q2).
#   SHORT_X   — 2 quarterlies only -> ttm_at returns None (insufficient_quarters).
#   SPARSE_X  — 4 quarterlies all PIT-eligible, but Q2 has revenues=None
#               -> ttm_with_status returns missing_field_value.
#   GAP_X     — Q1, Q2, Q4, next-FY Q1. Has four PIT-eligible rows, but not
#               four consecutive fiscal quarters -> insufficient_quarters.
#   REUSE_X   — same ticker, two CIKs, each with four valid-looking quarterlies.
#               Ticker-only lookup must not blend the two histories.
#   MISSING_X — appears nowhere in the fixture -> missing_fundamentals.

_FUND_COLUMNS: tuple[str, ...] = (
    "ticker",
    "cik",
    "filing_date",
    "period_end_date",
    "fiscal_year",
    "fiscal_quarter",
    "period_kind",
    "revenues",
    "net_income",
    "eps_basic",
    "eps_diluted",
    "weighted_avg_shares_basic",
    "weighted_avg_shares_diluted",
    "common_equity",
    "total_assets",
    "operating_cash_flow",
    "source_endpoints",
)


def _fund_row(
    ticker: str,
    cik: int,
    *,
    filing_date: date,
    period_end_date: date,
    fiscal_year: int | None,
    fiscal_quarter: int | None,
    period_kind: str,
    revenues: float | None = None,
    net_income: float | None = None,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "cik": cik,
        "filing_date": filing_date,
        "period_end_date": period_end_date,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "period_kind": period_kind,
        "revenues": revenues,
        "net_income": net_income,
        "eps_basic": None,
        "eps_diluted": None,
        "weighted_avg_shares_basic": None,
        "weighted_avg_shares_diluted": None,
        "common_equity": None,
        "total_assets": None,
        "operating_cash_flow": None,
        "source_endpoints": ("income_statements",),
    }


def _aapl_x_rows() -> list[dict[str, Any]]:
    """8 quarterlies + 1 annual (FY24) + 1 TTM (booby trap)."""
    quarters = [
        # (fy, fq, period_end,           filing_date,           rev,  ni)
        (2024, 1, date(2023, 12, 30), date(2024, 1, 25), 100, 10),
        (2024, 2, date(2024, 3, 30), date(2024, 4, 25), 200, 20),
        (2024, 3, date(2024, 6, 29), date(2024, 7, 25), 300, 30),
        (2024, 4, date(2024, 9, 28), date(2024, 10, 25), 400, 40),
        (2025, 1, date(2024, 12, 28), date(2025, 1, 25), 500, 50),
        (2025, 2, date(2025, 3, 29), date(2025, 4, 25), 600, 60),
        (2025, 3, date(2025, 6, 28), date(2025, 7, 25), 700, 70),
        (2025, 4, date(2025, 9, 27), date(2025, 10, 25), 800, 80),
    ]
    rows = [
        _fund_row(
            "AAPL_X",
            320193,
            fiscal_year=fy,
            fiscal_quarter=fq,
            filing_date=fd,
            period_end_date=pe,
            period_kind="quarterly",
            revenues=float(rev),
            net_income=float(ni),
        )
        for (fy, fq, pe, fd, rev, ni) in quarters
    ]
    # FY24 annual = sum of FY24 Q1..Q4 quarterlies = 1000 / 100
    rows.append(
        _fund_row(
            "AAPL_X",
            320193,
            fiscal_year=2024,
            fiscal_quarter=None,
            filing_date=date(2024, 11, 1),
            period_end_date=date(2024, 9, 28),
            period_kind="annual",
            revenues=1000.0,
            net_income=100.0,
        )
    )
    # TTM booby trap — must be ignored by ttm_at.
    rows.append(
        _fund_row(
            "AAPL_X",
            320193,
            fiscal_year=2025,
            fiscal_quarter=None,
            filing_date=date(2025, 5, 1),
            period_end_date=date(2025, 3, 29),
            period_kind="trailing_twelve_months",
            revenues=99999.0,  # if ever summed in, the test assertion will scream
            net_income=99999.0,
        )
    )
    return rows


def _msft_x_rows() -> list[dict[str, Any]]:
    """8 quarterlies (Jun FY-end) + 1 restatement of FY24-Q3."""
    quarters = [
        (2024, 1, date(2023, 9, 30), date(2023, 10, 25), 100, 10),
        (2024, 2, date(2023, 12, 31), date(2024, 1, 25), 200, 20),
        (2024, 3, date(2024, 3, 31), date(2024, 4, 25), 300, 30),  # original Q3
        (2024, 4, date(2024, 6, 30), date(2024, 7, 25), 400, 40),
        (2025, 1, date(2024, 9, 30), date(2024, 10, 25), 500, 50),
        (2025, 2, date(2024, 12, 31), date(2025, 1, 25), 600, 60),
        (2025, 3, date(2025, 3, 31), date(2025, 4, 25), 700, 70),
        (2025, 4, date(2025, 6, 30), date(2025, 7, 25), 800, 80),
    ]
    rows = [
        _fund_row(
            "MSFT_X",
            789019,
            fiscal_year=fy,
            fiscal_quarter=fq,
            filing_date=fd,
            period_end_date=pe,
            period_kind="quarterly",
            revenues=float(rev),
            net_income=float(ni),
        )
        for (fy, fq, pe, fd, rev, ni) in quarters
    ]
    # Restated FY24-Q3: same fiscal period, later filing_date, revenue 300 -> 350.
    rows.append(
        _fund_row(
            "MSFT_X",
            789019,
            fiscal_year=2024,
            fiscal_quarter=3,
            filing_date=date(2024, 5, 25),
            period_end_date=date(2024, 3, 31),
            period_kind="quarterly",
            revenues=350.0,
            net_income=35.0,
        )
    )
    return rows


def _mar_fy_x_rows() -> list[dict[str, Any]]:
    """8 quarterlies, March FY-end. Latest 4 at as_of=2024-12-01 cross fiscal years."""
    quarters = [
        (2024, 1, date(2023, 6, 30), date(2023, 8, 15), 100, 10),
        (2024, 2, date(2023, 9, 30), date(2023, 11, 15), 200, 20),
        (2024, 3, date(2023, 12, 31), date(2024, 2, 15), 300, 30),
        (2024, 4, date(2024, 3, 31), date(2024, 5, 15), 400, 40),
        (2025, 1, date(2024, 6, 30), date(2024, 8, 15), 500, 50),
        (2025, 2, date(2024, 9, 30), date(2024, 11, 15), 600, 60),
        (2025, 3, date(2024, 12, 31), date(2025, 2, 15), 700, 70),
        (2025, 4, date(2025, 3, 31), date(2025, 5, 15), 800, 80),
    ]
    return [
        _fund_row(
            "MAR_FY_X",
            12345,
            fiscal_year=fy,
            fiscal_quarter=fq,
            filing_date=fd,
            period_end_date=pe,
            period_kind="quarterly",
            revenues=float(rev),
            net_income=float(ni),
        )
        for (fy, fq, pe, fd, rev, ni) in quarters
    ]


def _short_x_rows() -> list[dict[str, Any]]:
    """Only 2 quarterlies — ttm_at must return None."""
    quarters = [
        (2024, 1, date(2024, 6, 30), date(2024, 8, 15), 100, 10),
        (2024, 2, date(2024, 9, 30), date(2024, 11, 15), 200, 20),
    ]
    return [
        _fund_row(
            "SHORT_X",
            55555,
            fiscal_year=fy,
            fiscal_quarter=fq,
            filing_date=fd,
            period_end_date=pe,
            period_kind="quarterly",
            revenues=float(rev),
            net_income=float(ni),
        )
        for (fy, fq, pe, fd, rev, ni) in quarters
    ]


def _sparse_x_rows() -> list[dict[str, Any]]:
    """4 quarterlies, but Q2 has revenues=None — ttm_with_status returns
    (None, 'missing_field_value')."""
    quarters = [
        (2024, 1, date(2023, 12, 31), date(2024, 1, 31), 100, 10),
        (2024, 2, date(2024, 3, 31), date(2024, 4, 30), None, 20),  # missing revenues
        (2024, 3, date(2024, 6, 30), date(2024, 7, 31), 300, 30),
        (2024, 4, date(2024, 9, 30), date(2024, 10, 31), 400, 40),
    ]
    return [
        _fund_row(
            "SPARSE_X",
            66666,
            fiscal_year=fy,
            fiscal_quarter=fq,
            filing_date=fd,
            period_end_date=pe,
            period_kind="quarterly",
            revenues=(float(rev) if rev is not None else None),
            net_income=float(ni),
        )
        for (fy, fq, pe, fd, rev, ni) in quarters
    ]


def _gap_x_rows() -> list[dict[str, Any]]:
    """Four PIT-eligible quarterlies with a missing middle fiscal quarter."""
    quarters = [
        (2024, 1, date(2023, 12, 31), date(2024, 1, 31), 100, 10),
        (2024, 2, date(2024, 3, 31), date(2024, 4, 30), 200, 20),
        # FY24-Q3 is intentionally absent.
        (2024, 4, date(2024, 9, 30), date(2024, 10, 31), 400, 40),
        (2025, 1, date(2024, 12, 31), date(2025, 1, 31), 500, 50),
    ]
    return [
        _fund_row(
            "GAP_X",
            77777,
            fiscal_year=fy,
            fiscal_quarter=fq,
            filing_date=fd,
            period_end_date=pe,
            period_kind="quarterly",
            revenues=float(rev),
            net_income=float(ni),
        )
        for (fy, fq, pe, fd, rev, ni) in quarters
    ]


def _reuse_x_rows() -> list[dict[str, Any]]:
    """Same ticker reused across two CIKs; callers must resolve by CIK."""
    rows: list[dict[str, Any]] = []
    for cik, base in ((11111, 10), (22222, 100)):
        quarters = [
            (2024, 1, date(2023, 12, 31), date(2024, 1, 31), base * 1, base * 0.1),
            (2024, 2, date(2024, 3, 31), date(2024, 4, 30), base * 2, base * 0.2),
            (2024, 3, date(2024, 6, 30), date(2024, 7, 31), base * 3, base * 0.3),
            (2024, 4, date(2024, 9, 30), date(2024, 10, 31), base * 4, base * 0.4),
        ]
        rows.extend(
            _fund_row(
                "REUSE_X",
                cik,
                fiscal_year=fy,
                fiscal_quarter=fq,
                filing_date=fd,
                period_end_date=pe,
                period_kind="quarterly",
                revenues=float(rev),
                net_income=float(ni),
            )
            for (fy, fq, pe, fd, rev, ni) in quarters
        )
    return rows


@pytest.fixture(scope="session")
def fundamentals_fixture() -> pd.DataFrame:
    """Engineered fundamentals frame for Day 16 / Day 17 tests.

    See the comment block above for the encoded cases. The frame is
    session-scoped — tests must not mutate it.
    """
    rows = (
        _aapl_x_rows()
        + _msft_x_rows()
        + _mar_fy_x_rows()
        + _short_x_rows()
        + _sparse_x_rows()
        + _gap_x_rows()
        + _reuse_x_rows()
    )
    df = pd.DataFrame(rows, columns=list(_FUND_COLUMNS))
    return df


# --- Week 1 / Week 2 ledger pipeline scaffolding ----------------------------
# Lifted from tests/unit/test_week1_pipeline.py on Day 12 so test_ledger.py
# can consume the same fixture. Polygon-free by construction: monkey-patches
# ``build_panel`` and ``current_git_sha`` inside :mod:`aegis.backtest.week1`.

# Deterministic git SHA stamped by the pipeline in tests (no subprocess).
FAKE_GIT_SHA: str = "a1b2c3d4e5f6"


@dataclass(frozen=True)
class LedgerSnapshot:
    """Plain-value snapshot of the ledger at one moment, safe to use post-session."""

    experiments: list[dict[str, str]]
    candidates: list[dict[str, str]]
    artifacts: list[dict[str, str]]


def ledger_snapshot(ledger_path: Path) -> LedgerSnapshot:
    """Read every row into a dict, avoiding DetachedInstanceError."""
    with open_ledger(ledger_path) as session:
        experiments = [
            {
                "experiment_id": e.experiment_id,
                "name": e.name,
                "config_hash": e.config_hash,
                "git_sha": e.git_sha,
            }
            for e in session.execute(select(Experiment)).scalars().all()
        ]
        candidates = [
            {
                "candidate_id": c.candidate_id,
                "experiment_id": c.experiment_id,
                "candidate_name": c.candidate_name,
                "candidate_type": c.candidate_type,
                "formula_string": c.formula_string,
                "data_snapshot_id": c.data_snapshot_id,
                "status": c.status,
            }
            for c in session.execute(select(Candidate)).scalars().all()
        ]
        artifacts = [
            {
                "artifact_id": a.artifact_id,
                "candidate_id": a.candidate_id,
                "artifact_type": a.artifact_type,
                "path": a.path,
                "checksum": a.checksum,
            }
            for a in session.execute(select(Artifact)).scalars().all()
        ]
    return LedgerSnapshot(experiments=experiments, candidates=candidates, artifacts=artifacts)


@pytest.fixture
def patched_git_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``aegis.ledger.replay._check_git_sha`` to return True.

    The :data:`FAKE_GIT_SHA` stamped by ``pipeline_fixture`` is not a real
    git object, so the real ``_check_git_sha`` would return False — useful
    in production but unhelpful when a test wants to assert
    ``git_sha_available is True``. Verify-mode tests that don't care about
    this branch consume this fixture to short-circuit the check.
    """
    from aegis.ledger import replay as replay_module

    monkeypatch.setattr(replay_module, "_check_git_sha", lambda sha: True)


@pytest.fixture
def pipeline_fixture(
    stock_daily_panel: pd.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AegisConfig, Path]:
    """Isolated pipeline invocation: redirected paths, mocked loader + git.

    Returns ``(test_cfg, ledger_path)``. Tests then call ``run_week1_slice``
    themselves and reason about the resulting ledger / artifacts.
    """
    cfg = load_all()

    test_cfg = cfg.model_copy(
        update={
            "data": cfg.data.model_copy(
                update={
                    "paths": cfg.data.paths.model_copy(update={"processed": tmp_path}),
                }
            )
        }
    )

    # Pre-finalize the fixture through Day 3's pipeline → writes the panel
    # Parquet exactly where build_panel would write it.
    finalized = _finalize_panel(stock_daily_panel, test_cfg)
    panel_path = tmp_path / test_cfg.data.snapshot.panel_filename
    finalized.to_parquet(panel_path, index=False)

    def _fake_build_panel(
        cfg: AegisConfig,
        *,
        tickers=None,
        sleep_between_calls: float = 0.0,
        panel_filename: str | None = None,
        metadata_as_of=None,
        require_all_tickers: bool = False,
    ) -> Path:
        return panel_path

    # Patch the names as imported into _common (Day 13 refactor moved the
    # pipeline body out of week1.py). The Week 1 wrapper resolves these
    # through _common at call time.
    monkeypatch.setattr(backtest_common_module, "build_panel", _fake_build_panel)
    monkeypatch.setattr(backtest_common_module, "current_git_sha", lambda **_: FAKE_GIT_SHA)

    ledger_path = tmp_path / "ledger.sqlite"
    return test_cfg, ledger_path
