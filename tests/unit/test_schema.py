"""Day 1 acceptance: every Pydantic row schema accepts a good row and rejects
a malformed one.

Covers:
  * StockDailyRow, UniverseRow (src/aegis/data/schema.py)
  * FactorObservation (src/aegis/features/base.py)
  * ResearchRecord + per-table records (src/aegis/ledger/models.py)

Polygon.io taxonomy: identifiers are ticker (string) + ticker_type (Literal).
CRSP's permno / integer share_code are gone as of the 2026-04-23 pivot.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegis.data.schema import StockDailyRow, UniverseRow
from aegis.features.base import Factor, FactorObservation
from aegis.ledger.models import (
    ArtifactRecord,
    CandidateRecord,
    ExperimentRecord,
    MetricRecord,
    ResearchRecord,
)

# 64-char hex string for hash-like fields
HASH = "a" * 64


# --- StockDailyRow -----------------------------------------------------------
def _good_stock_row(**overrides: object) -> StockDailyRow:
    base: dict[str, object] = {
        "date": date(2022, 6, 15),
        "ticker": "AAPL",
        "exchange": "NASDAQ",
        "ticker_type": "CS",
        "is_common_share": True,
        "raw_close": 135.43,
        "adj_close": 135.43,
        "ret_1d": 0.0102,
        "volume": 7.2e7,
        "shares_out": 1.6e10,
        "mcap": 2.2e12,
        "gics_sector": "Information Technology",
        "gics_industry": "Technology Hardware, Storage & Peripherals",
        "eligible_flag": True,
        "data_snapshot_id": HASH,
    }
    base.update(overrides)
    return StockDailyRow(**base)


def test_stock_daily_row_accepts_valid() -> None:
    row = _good_stock_row()
    assert row.ticker == "AAPL"
    assert row.exchange == "NASDAQ"
    assert row.ticker_type == "CS"


def test_stock_daily_row_rejects_bad_exchange() -> None:
    with pytest.raises(ValidationError):
        _good_stock_row(exchange="LSE")


def test_stock_daily_row_rejects_nonpositive_price() -> None:
    with pytest.raises(ValidationError):
        _good_stock_row(raw_close=0.0)


def test_stock_daily_row_rejects_common_share_inconsistency() -> None:
    # PFD (preferred) with is_common_share=True is a contradiction.
    # Per spec §7 + Tim 2026-04-23: only ticker_type=="CS" is a common share.
    with pytest.raises(ValidationError):
        _good_stock_row(ticker_type="PFD", is_common_share=True)


def test_stock_daily_row_rejects_adrc_as_common_share() -> None:
    # ADRs are explicitly NOT common shares per the 2026-04-23 decision.
    with pytest.raises(ValidationError):
        _good_stock_row(ticker_type="ADRC", is_common_share=True)


def test_stock_daily_row_accepts_pfd_with_is_common_false() -> None:
    # A preferred stock row can still exist in the panel; it just has
    # is_common_share=False and will be filtered out by the universe rule.
    row = _good_stock_row(ticker_type="PFD", is_common_share=False)
    assert row.ticker_type == "PFD"
    assert row.is_common_share is False


def test_stock_daily_row_allows_null_ret_on_first_day() -> None:
    row = _good_stock_row(ret_1d=None)
    assert row.ret_1d is None


# --- UniverseRow -------------------------------------------------------------
def _good_universe_row(**overrides: object) -> UniverseRow:
    base: dict[str, object] = {
        "date": date(2022, 6, 15),
        "ticker": "AAPL",
        "eligible_flag": True,
        "price_ok": True,
        "history_ok": True,
        "exchange_ok": True,
        "common_share_ok": True,
        "fail_reason": None,
    }
    base.update(overrides)
    return UniverseRow(**base)


def test_universe_row_accepts_valid_eligible() -> None:
    row = _good_universe_row()
    assert row.eligible_flag is True
    assert row.fail_reason is None
    assert row.ticker == "AAPL"


def test_universe_row_accepts_valid_ineligible() -> None:
    row = _good_universe_row(eligible_flag=False, price_ok=False, fail_reason="price_below_floor")
    assert row.eligible_flag is False
    assert row.fail_reason == "price_below_floor"


def test_universe_row_rejects_eligible_with_fail_reason() -> None:
    with pytest.raises(ValidationError):
        _good_universe_row(fail_reason="something")


def test_universe_row_rejects_ineligible_without_fail_reason() -> None:
    with pytest.raises(ValidationError):
        _good_universe_row(eligible_flag=False, price_ok=False, fail_reason=None)


def test_universe_row_rejects_eligible_with_failing_rule() -> None:
    with pytest.raises(ValidationError):
        _good_universe_row(eligible_flag=True, price_ok=False, fail_reason=None)


# --- FactorObservation -------------------------------------------------------
def _good_obs(**overrides: object) -> FactorObservation:
    base: dict[str, object] = {
        "date": date(2022, 6, 15),
        "ticker": "AAPL",
        "factor_name": "mom_12_1",
        "raw_value": 0.123,
        "winsorized_value": 0.120,
        "zscore_value": 0.45,
        "valid_flag": True,
        "tradable_flag": True,
        "feature_snapshot_id": HASH,
    }
    base.update(overrides)
    return FactorObservation(**base)


def test_factor_observation_accepts_valid() -> None:
    obs = _good_obs()
    assert obs.factor_name == "mom_12_1"
    assert obs.valid_flag is True
    assert obs.ticker == "AAPL"


def test_factor_observation_accepts_invalid_with_nulls() -> None:
    obs = _good_obs(
        raw_value=None,
        winsorized_value=None,
        zscore_value=None,
        valid_flag=False,
        tradable_flag=False,
    )
    assert obs.valid_flag is False


def test_factor_observation_rejects_valid_flag_with_null_value() -> None:
    with pytest.raises(ValidationError):
        _good_obs(raw_value=None)


def test_factor_observation_rejects_invalid_flag_with_all_values_present() -> None:
    with pytest.raises(ValidationError):
        _good_obs(valid_flag=False)


def test_factor_observation_rejects_tradable_when_invalid() -> None:
    with pytest.raises(ValidationError):
        _good_obs(raw_value=None, valid_flag=False, tradable_flag=True)


# --- Factor ABC --------------------------------------------------------------
def test_factor_abc_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        Factor()  # type: ignore[abstract]


# --- ResearchRecord + per-table records --------------------------------------
def test_experiment_record_accepts_valid() -> None:
    exp = ExperimentRecord(
        name="week1_vertical_slice",
        config_hash=HASH,
        git_sha="a1b2c3d",
    )
    assert exp.name == "week1_vertical_slice"
    assert isinstance(exp.created_at, datetime)
    assert exp.created_at.tzinfo is not None


def test_experiment_record_rejects_non_hex_hash() -> None:
    with pytest.raises(ValidationError):
        ExperimentRecord(name="x", config_hash="Z" * 64, git_sha="a1b2c3d")


def test_experiment_record_rejects_wrong_length_hash() -> None:
    with pytest.raises(ValidationError):
        ExperimentRecord(name="x", config_hash="a" * 32, git_sha="a1b2c3d")


def test_candidate_record_default_status_registered() -> None:
    eid = uuid4()
    cand = CandidateRecord(
        experiment_id=eid,
        candidate_name="mom_12_1",
        formula_string="log(P[t-21] / P[t-252])",
        data_snapshot_id=HASH,
    )
    assert cand.status == "registered"
    assert cand.candidate_type == "deterministic_factor"


def test_candidate_record_rejects_bad_status() -> None:
    with pytest.raises(ValidationError):
        CandidateRecord(
            experiment_id=uuid4(),
            candidate_name="x",
            formula_string="x",
            data_snapshot_id=HASH,
            status="in_progress",  # not a valid CandidateStatus literal
        )


def test_artifact_record_accepts_valid() -> None:
    art = ArtifactRecord(
        candidate_id=uuid4(),
        artifact_type="factor",
        path="data/processed/factor_mom_12_1_week1.parquet",
        checksum=HASH,
    )
    assert art.artifact_type == "factor"


def test_artifact_record_rejects_bad_type() -> None:
    with pytest.raises(ValidationError):
        ArtifactRecord(
            candidate_id=uuid4(),
            artifact_type="unknown",
            path="x",
            checksum=HASH,
        )


def test_metric_record_accepts_valid() -> None:
    m = MetricRecord(
        candidate_id=uuid4(),
        metric_name="ic_mean",
        value=0.032,
        fold="oos_2023",
        horizon=21,
    )
    assert m.horizon == 21


def test_metric_record_rejects_nonpositive_horizon() -> None:
    with pytest.raises(ValidationError):
        MetricRecord(candidate_id=uuid4(), metric_name="x", value=0.0, horizon=0)


def test_research_record_accepts_valid() -> None:
    rec = ResearchRecord(
        experiment_id=uuid4(),
        candidate_id=uuid4(),
        candidate_name="mom_12_1",
        formula_string="log(P[t-21] / P[t-252])",
        config_hash=HASH,
        git_sha="a1b2c3d",
        data_snapshot_id=HASH,
        artifact_path="data/processed/factor_mom_12_1_week1.parquet",
    )
    assert rec.status == "registered"
    assert rec.candidate_type == "deterministic_factor"
    assert rec.created_at.tzinfo is not None


def test_research_record_is_frozen() -> None:
    rec = ResearchRecord(
        experiment_id=uuid4(),
        candidate_id=uuid4(),
        candidate_name="x",
        formula_string="x",
        config_hash=HASH,
        git_sha="a1b2c3d",
        data_snapshot_id=HASH,
        artifact_path="x",
    )
    with pytest.raises(ValidationError):
        rec.status = "promoted"  # type: ignore[misc]
