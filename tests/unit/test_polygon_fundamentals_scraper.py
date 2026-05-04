"""Polygon-free unit tests for the Day 15 fundamentals scraper helpers."""

from __future__ import annotations

import sys
from datetime import date
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from aegis.data.schema import FundamentalsRow

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import fetch_polygon_fundamentals as ff  # noqa: E402


def _row(**overrides: object) -> dict[str, object]:
    base = dict.fromkeys(ff.EXPECTED_COLUMNS)
    base.update(
        {
            "ticker": "AAPL",
            "cik": 320193,
            "filing_date": date(2024, 11, 1),
            "period_end_date": date(2024, 9, 28),
            "fiscal_year": 2024,
            "fiscal_quarter": 4,
            "period_kind": "quarterly",
            "source_endpoints": ("income_statements",),
        }
    )
    base.update(overrides)
    return base


def test_format_cik_zero_pads_and_handles_missing_values() -> None:
    assert ff._format_cik(320193) == "0000320193"
    assert ff._format_cik("0000320193") == "0000320193"
    assert ff._format_cik(float("nan")) is None
    assert ff._format_cik(None) is None


def test_project_row_prefers_primary_ticker_and_maps_values() -> None:
    raw = SimpleNamespace(
        tickers=["GOOG", "GOOGL"],
        cik="1652044",
        filing_date="2024-10-30",
        period_end="2024-09-30",
        fiscal_year=2024,
        fiscal_quarter=3,
        timeframe="quarterly",
        revenue=88.3,
        net_income_loss_attributable_common_shareholders=26.3,
    )

    out = ff._project_row(
        raw,
        field_map=ff.INCOME_FIELD_MAP,
        primary_ticker="GOOGL",
        endpoint_tag="income_statements",
    )

    assert out["ticker"] == "GOOGL"
    assert out["cik"] == 1652044
    assert out["revenues"] == 88.3
    assert out["net_income"] == 26.3
    assert out["_endpoint_tag"] == "income_statements"


def test_merge_three_endpoints_unions_values_and_uses_latest_filing_date() -> None:
    income = [
        _row(
            filing_date=date(2024, 10, 30),
            revenues=94.9,
            net_income=14.7,
            source_endpoints=None,
            _endpoint_tag="income_statements",
        )
    ]
    balance = [
        _row(
            filing_date=date(2024, 11, 1),
            common_equity=56.9,
            total_assets=365.0,
            source_endpoints=None,
            _endpoint_tag="balance_sheets",
        )
    ]
    cashflow = [
        _row(
            filing_date=date(2024, 10, 31),
            operating_cash_flow=26.8,
            source_endpoints=None,
            _endpoint_tag="cash_flow_statements",
        )
    ]

    merged = ff._merge_three_endpoints(income, balance, cashflow)

    assert len(merged) == 1
    out = merged[0]
    assert out["filing_date"] == date(2024, 11, 1)
    assert out["revenues"] == 94.9
    assert out["common_equity"] == 56.9
    assert out["operating_cash_flow"] == 26.8
    assert out["source_endpoints"] == (
        "income_statements",
        "balance_sheets",
        "cash_flow_statements",
    )


def test_validate_allows_limited_smoke_snapshot_without_sanity_floors() -> None:
    df = pd.DataFrame([_row()], columns=list(ff.EXPECTED_COLUMNS))

    ff._validate(df, enforce_sanity_floors=False)


def test_validate_full_snapshot_enforces_sanity_floors() -> None:
    df = pd.DataFrame([_row()], columns=list(ff.EXPECTED_COLUMNS))

    with pytest.raises(RuntimeError, match="expected >=10,000 rows"):
        ff._validate(df)


def test_generated_parquet_round_trip_rows_validate_as_fundamentals_row() -> None:
    df = pd.DataFrame(
        [
            _row(),
            _row(period_kind="annual", fiscal_quarter=None),
        ],
        columns=list(ff.EXPECTED_COLUMNS),
    )
    buffer = BytesIO()
    df.to_parquet(buffer, index=False)

    buffer.seek(0)
    read_back = pd.read_parquet(buffer)
    rows = [FundamentalsRow(**record) for record in read_back.to_dict("records")]

    assert rows[0].source_endpoints == ("income_statements",)
    assert rows[0].fiscal_quarter == 4
    assert rows[1].fiscal_quarter is None
