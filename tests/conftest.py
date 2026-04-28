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
from pathlib import Path

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
        cfg: AegisConfig, *, tickers=None, sleep_between_calls: float = 0.0
    ) -> Path:
        return panel_path

    # Patch the names as imported into _common (Day 13 refactor moved the
    # pipeline body out of week1.py). The Week 1 wrapper resolves these
    # through _common at call time.
    monkeypatch.setattr(backtest_common_module, "build_panel", _fake_build_panel)
    monkeypatch.setattr(backtest_common_module, "current_git_sha", lambda: FAKE_GIT_SHA)

    ledger_path = tmp_path / "ledger.sqlite"
    return test_cfg, ledger_path
