"""Day 13 unit coverage for ``aegis.backtest.full.run_full_slice``.

Three tests, all Polygon-free (monkey-patched):

1. ``test_full_slice_uses_date_aware_universe`` — different ``sample_date``
   values produce different ticker sets passed to ``load_polygon_daily``.
2. ``test_full_slice_ledger_records_universe_date`` — the experiment row's
   ``name`` encodes the ``sample_date`` ISO string; two dates yield two
   distinct experiment rows.
3. ``test_full_slice_synthetic_500_ticker_fixture_is_sensible`` — the
   acceptance gate from the locked Week 2 plan: ~500 tickers × ~500 trading
   days, panel shape (rows, 15), factor shape (rows, 8), wall time < 30s,
   peak RSS < 1.5 GB, ledger (1, 1, 2), determinism (re-run with same seed
   produces identical panel sha256_file).
"""

from __future__ import annotations

import hashlib
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import psutil
import pytest

from aegis.backtest import _common as backtest_common_module
from aegis.backtest import full as full_module
from aegis.backtest.full import EXPERIMENT_NAME_PREFIX, run_full_slice
from aegis.config import AegisConfig, load_all
from aegis.data import panel as panel_module
from aegis.utils.hashing import sha256_file
from tests.conftest import FAKE_GIT_SHA, ledger_snapshot

# --- Synthetic 500-ticker loader -------------------------------------------
#
# Each ticker gets a stable per-symbol log-walk close-price series via a
# deterministic seed (``hash(ticker)``). Two consequences:
#  - re-running with the same ticker list produces identical panel bytes;
#  - different ticker lists produce different panel bytes.
# This is what the determinism gate in test #3 asserts.

_SYNTH_PRICE_FLOOR: float = 50.0  # all tickers pass the §7 $5 filter
_SYNTH_DAILY_VOL: float = 0.005


def _synthetic_loader(
    *,
    tickers: list[str],
    start: date,
    end: date,
    sleep_between_calls: float = 0.0,
) -> pd.DataFrame:
    """Drop-in replacement for ``aegis.data.polygon_loader.load_polygon_daily``.

    Returns a DataFrame shape-aligned with ``polygon_loader.OUTPUT_COLUMNS``.
    All synthetic tickers are NYSE/CS with average close ~$50, so they
    pass the §7 universe filters (price >= $5, exchange OK, common share).
    """
    dates = pd.bdate_range(start, end)
    n_days = len(dates)
    parts: list[pd.DataFrame] = []
    for ticker in tickers:
        seed = int(hashlib.sha256(ticker.encode("utf-8")).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        log_returns = rng.normal(0.0, _SYNTH_DAILY_VOL, n_days)
        close = _SYNTH_PRICE_FLOOR * np.exp(np.cumsum(log_returns))
        shares_out = 1_000_000.0
        parts.append(
            pd.DataFrame(
                {
                    "date": pd.to_datetime(dates),
                    "ticker": pd.array([ticker] * n_days, dtype="string"),
                    "exchange": pd.array(["NYSE"] * n_days, dtype="string"),
                    "ticker_type": pd.array(["CS"] * n_days, dtype="string"),
                    "is_common_share": np.full(n_days, True, dtype=bool),
                    "raw_close": close,
                    "adj_close": close,
                    "volume": np.full(n_days, 1_000_000.0),
                    "shares_out": np.full(n_days, shares_out),
                    "mcap": close * shares_out,
                }
            )
        )
    return pd.concat(parts, ignore_index=True)


def _synthetic_membership(tickers: list[str]) -> pd.DataFrame:
    """Build a fake S&P 500 membership frame with 500 always-active tickers."""
    return pd.DataFrame(
        {
            "ticker": tickers,
            "name": [f"{t} Holdings Inc" for t in tickers],
            "wiki_sector": ["Industrials"] * len(tickers),
            "wiki_sub_industry": ["Diversified"] * len(tickers),
            "date_added": pd.to_datetime([date(2010, 1, 1)] * len(tickers)),
            "date_removed": pd.array([pd.NaT] * len(tickers), dtype="datetime64[ns]"),
            "cik_code": pd.array(range(len(tickers)), dtype="Int64"),
        }
    )


def _synthetic_metadata(
    tickers: list[str],
    *,
    list_date: date = date(2010, 1, 1),
    delisted_date: date | None = None,
) -> pd.DataFrame:
    """Build fake ticker metadata with all tickers tradable unless specified."""
    return pd.DataFrame(
        {
            "ticker": tickers,
            "name": [f"{t} Holdings Inc" for t in tickers],
            "primary_exchange": ["XNYS"] * len(tickers),
            "ticker_type": ["CS"] * len(tickers),
            "list_date": pd.to_datetime([list_date] * len(tickers)),
            "delisted_date": pd.to_datetime([delisted_date] * len(tickers)),
            "sic_code": [None] * len(tickers),
            "sic_description": [None] * len(tickers),
            "cik": pd.array(range(len(tickers)), dtype="Int64"),
        }
    )


def _empty_aliases() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "canonical_ticker": pd.Series(dtype="object"),
            "alias": pd.Series(dtype="object"),
            "effective_from": pd.Series(dtype="datetime64[ns]"),
            "effective_to": pd.Series(dtype="datetime64[ns]"),
            "note": pd.Series(dtype="object"),
        }
    )


@pytest.fixture
def widened_cfg(tmp_path: Path) -> AegisConfig:
    """Cfg with ``processed`` / ``reference`` redirected to tmp_path and a
    wide-enough ``date_range`` to give the synthetic 500-ticker panel
    >= 500 trading days (~250k rows). Real cfg's 2024-06 to 2026-03 window
    is ~458 trading days, just shy of the locked-plan 500-day floor.
    """
    cfg = load_all()
    return cfg.model_copy(
        update={
            "data": cfg.data.model_copy(
                update={
                    "paths": cfg.data.paths.model_copy(
                        update={
                            "processed": tmp_path,
                            "reference": tmp_path,  # not actually read in these tests
                        }
                    ),
                    "date_range": cfg.data.date_range.model_copy(
                        update={
                            "start": date(2023, 1, 2),  # ~2.5 years
                            "end": date(2025, 6, 30),  # >= 500 trading days
                        }
                    ),
                }
            )
        }
    )


# --- Test 1: date-aware universe selection ----------------------------------


def test_full_slice_uses_date_aware_universe(
    widened_cfg: AegisConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two different ``sample_date`` values produce two different ticker lists.

    Asserts the loader was called with different ``tickers`` arguments.
    AAA is always active; BBB is removed 2019-12-31.
    """
    membership = pd.DataFrame(
        {
            "ticker": ["AAA", "BBB"],
            "name": ["Active Co", "Removed Co"],
            "wiki_sector": [None, None],
            "wiki_sub_industry": [None, None],
            "date_added": [pd.Timestamp("2010-01-01"), pd.Timestamp("2010-01-01")],
            "date_removed": [pd.NaT, pd.Timestamp("2019-12-31")],
            "cik_code": pd.array([1, 2], dtype="Int64"),
        }
    )
    monkeypatch.setattr(full_module, "load_sp500_membership", lambda _: membership)
    monkeypatch.setattr(
        full_module, "load_ticker_metadata", lambda _: _synthetic_metadata(["AAA", "BBB"])
    )
    monkeypatch.setattr(full_module, "load_ticker_aliases", lambda _: _empty_aliases())

    captured: list[tuple[str, ...]] = []

    def _capture(**kwargs: object) -> pd.DataFrame:
        tickers = kwargs.get("tickers", [])
        captured.append(tuple(tickers))  # type: ignore[arg-type]
        return _synthetic_loader(
            tickers=list(tickers),  # type: ignore[arg-type]
            start=widened_cfg.data.date_range.start,
            end=widened_cfg.data.date_range.end,
        )

    monkeypatch.setattr(panel_module, "load_polygon_daily", _capture)
    monkeypatch.setattr(backtest_common_module, "current_git_sha", lambda: FAKE_GIT_SHA)

    ledger_path = tmp_path / "ledger.sqlite"

    run_full_slice(widened_cfg, ledger_path, date(2018, 6, 15), sleep_between_calls=0)
    run_full_slice(widened_cfg, ledger_path, date(2020, 6, 15), sleep_between_calls=0)

    # 2018-06-15: BBB still active. 2020-06-15: BBB removed.
    assert captured[0] == ("AAA", "BBB")
    assert captured[1] == ("AAA",)


# --- Test 2: experiment name encodes the sample_date ------------------------


def test_full_slice_ledger_records_universe_date(
    widened_cfg: AegisConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runs with different sample_dates create two distinct experiment
    rows whose ``name`` field includes the ISO date string."""
    membership = _synthetic_membership(["AAA", "BBB"])
    monkeypatch.setattr(full_module, "load_sp500_membership", lambda _: membership)
    monkeypatch.setattr(
        full_module, "load_ticker_metadata", lambda _: _synthetic_metadata(["AAA", "BBB"])
    )
    monkeypatch.setattr(full_module, "load_ticker_aliases", lambda _: _empty_aliases())
    monkeypatch.setattr(
        panel_module,
        "load_polygon_daily",
        lambda **kwargs: _synthetic_loader(
            tickers=list(kwargs["tickers"]),
            start=widened_cfg.data.date_range.start,
            end=widened_cfg.data.date_range.end,
        ),
    )
    monkeypatch.setattr(backtest_common_module, "current_git_sha", lambda: FAKE_GIT_SHA)

    ledger_path = tmp_path / "ledger.sqlite"
    run_full_slice(widened_cfg, ledger_path, date(2024, 1, 5), sleep_between_calls=0)
    run_full_slice(widened_cfg, ledger_path, date(2024, 6, 14), sleep_between_calls=0)

    snap = ledger_snapshot(ledger_path)
    assert len(snap.experiments) == 2

    names = {e["name"] for e in snap.experiments}
    assert f"{EXPERIMENT_NAME_PREFIX}_2024-01-05" in names
    assert f"{EXPERIMENT_NAME_PREFIX}_2024-06-14" in names


# --- Test 3: 500-ticker synthetic acceptance gate ---------------------------


_N_SYNTH_TICKERS = 500


def _peak_rss_bytes() -> int:
    """Cross-platform peak resident-set-size in bytes since process start.

    On Windows, ``psutil.memory_info().peak_wset`` is exact. On Linux,
    ``resource.getrusage().ru_maxrss`` is in KB. On macOS it's in bytes.
    Falls back to current RSS if neither is available.
    """
    proc = psutil.Process()
    mem = proc.memory_info()
    if hasattr(mem, "peak_wset"):
        return int(mem.peak_wset)  # type: ignore[attr-defined]
    try:
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(maxrss if sys.platform == "darwin" else maxrss * 1024)
    except ImportError:
        return int(mem.rss)


def test_full_slice_synthetic_500_ticker_fixture_is_sensible(
    widened_cfg: AegisConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locked-plan acceptance: 500 tickers x ~500 days, all gates pass.

    See ``docs/plans/week2.md`` lines 234-247 for the criteria. This single
    test pins each one explicitly so a regression on any gate names itself.
    """
    base_tickers = [f"S{i:04d}" for i in range(_N_SYNTH_TICKERS - 1)]
    # REN is a continuing lineage that traded as OLD on the sample date.
    # FUT is a ticker-reuse guard: it is in the synthetic membership table
    # but does not list until after sample_date, so the resolver must drop it.
    membership = _synthetic_membership([*base_tickers, "REN", "FUT"])
    metadata = pd.concat(
        [
            _synthetic_metadata(base_tickers),
            _synthetic_metadata(["OLD"]),
            _synthetic_metadata(["FUT"], list_date=date(2026, 1, 1)),
        ],
        ignore_index=True,
    )
    aliases = pd.DataFrame(
        {
            "canonical_ticker": ["REN"],
            "alias": ["OLD"],
            "effective_from": [pd.NaT],
            "effective_to": [pd.Timestamp("2026-01-01")],
            "note": ["synthetic rename"],
        }
    )
    monkeypatch.setattr(full_module, "load_sp500_membership", lambda _: membership)
    monkeypatch.setattr(full_module, "load_ticker_metadata", lambda _: metadata)
    monkeypatch.setattr(full_module, "load_ticker_aliases", lambda _: aliases)
    monkeypatch.setattr(
        panel_module,
        "load_polygon_daily",
        lambda **kwargs: _synthetic_loader(
            tickers=list(kwargs["tickers"]),
            start=widened_cfg.data.date_range.start,
            end=widened_cfg.data.date_range.end,
        ),
    )
    monkeypatch.setattr(backtest_common_module, "current_git_sha", lambda: FAKE_GIT_SHA)

    ledger_path = tmp_path / "ledger.sqlite"
    sample_date = date(2025, 6, 15)

    t0 = time.perf_counter()
    result = run_full_slice(widened_cfg, ledger_path, sample_date, sleep_between_calls=0)
    wall_time = time.perf_counter() - t0

    # Gate 1: panel size (>= 250k rows = 500 tickers x >= 500 trading days)
    assert result.panel_rows >= 250_000, (
        f"panel_rows={result.panel_rows:,} below the 250k locked-plan floor"
    )

    # Gate 2: panel shape (rows, 15) — Day 3's _PANEL_COLUMNS
    panel = pd.read_parquet(result.panel_path)
    assert panel.shape == (result.panel_rows, 15), (
        f"panel shape {panel.shape} does not match (rows, 15)"
    )
    assert "OLD" in set(panel["ticker"])
    assert "REN" not in set(panel["ticker"])
    assert "FUT" not in set(panel["ticker"])

    # Gate 3: factor shape (rows, 8) — Day 5's FactorObservation columns
    factor = pd.read_parquet(result.factor_path)
    assert factor.shape == (result.panel_rows, 8), (
        f"factor shape {factor.shape} does not match (rows, 8)"
    )

    # Gate 4: wall time < 30s on a dev laptop
    assert wall_time < 30.0, f"wall time {wall_time:.1f}s exceeds the 30s ceiling"

    # Gate 5: ledger row stamping (1 experiment, 1 candidate, 2 artifacts)
    snap = ledger_snapshot(ledger_path)
    assert (len(snap.experiments), len(snap.candidates), len(snap.artifacts)) == (1, 1, 2)
    assert sample_date.isoformat() in snap.experiments[0]["name"]

    # Gate 6: peak RSS < 1.5 GB
    peak = _peak_rss_bytes()
    assert peak < int(1.5 * 1024**3), f"peak RSS {peak / 1024**3:.2f} GB exceeds the 1.5 GB ceiling"

    # Gate 7: determinism — the panel sha256 is a function of the inputs.
    panel_sha_1 = sha256_file(result.panel_path)

    # Wipe and re-run; the loader is deterministic per ticker, so bytes match.
    result.panel_path.unlink()
    result.factor_path.unlink()
    ledger_path.unlink()
    rerun_result = run_full_slice(widened_cfg, ledger_path, sample_date, sleep_between_calls=0)
    panel_sha_2 = sha256_file(rerun_result.panel_path)
    assert panel_sha_1 == panel_sha_2, (
        f"panel sha256 differs across runs: {panel_sha_1[:16]} vs {panel_sha_2[:16]}"
    )
