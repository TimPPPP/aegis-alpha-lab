"""Polygon v1 fundamentals smoke tests — marked ``@pytest.mark.polygon``.

Skip-with-clear-message when:
  * ``POLYGON_API_KEY`` is not set, or
  * the key lacks Financials & Ratios Expansion / Stocks Advanced entitlement
    (the v1 financial-statement endpoints used here require that tier).

These tests primarily catch Polygon API shape drift (next time
``polygon-api-client`` releases or Polygon renames a field). Once entitlement
is granted, they exercise the merge logic against live data for AAPL.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# The scraper lives under scripts/, not the package — exercise the helpers
# by importing the script module directly.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import fetch_polygon_fundamentals as ff  # noqa: E402

_HAS_KEY = bool(os.environ.get("POLYGON_API_KEY"))


def _polygon_client():
    from polygon import RESTClient  # local import keeps unit tests deps-free

    return RESTClient(os.environ["POLYGON_API_KEY"])


@pytest.mark.polygon
@pytest.mark.skipif(not _HAS_KEY, reason="POLYGON_API_KEY not set")
def test_polygon_fundamentals_entitlement_preflight() -> None:
    """Probe each of the 3 v1 financial-statement endpoints with one ticker.

    Skip (NOT fail) if the key lacks the Financials & Ratios Expansion or
    Stocks Advanced tier — that's the expected state on Stocks Starter.
    The Day 15 scraper itself raises hard in this branch; the test stays
    gentle so it doesn't break local pytest for users on Starter only.
    """
    client = _polygon_client()
    result = ff._entitlement_preflight(client)
    assert set(result.keys()) == {
        "income_statements",
        "balance_sheets",
        "cash_flow_statements",
    }
    forbidden = [ep for ep, s in result.items() if s == "forbidden"]
    if forbidden:
        pytest.skip(
            f"Polygon key lacks v1 financials entitlement (forbidden: {forbidden}). "
            "Upgrade to Financials & Ratios Expansion or Stocks Advanced to exercise "
            "the live fundamentals path."
        )
    for ep, status in result.items():
        assert status == "ok", f"{ep}: expected 'ok', got {status!r}"


@pytest.mark.polygon
@pytest.mark.skipif(not _HAS_KEY, reason="POLYGON_API_KEY not set")
def test_polygon_income_statements_schema_round_trip() -> None:
    """Pull one AAPL income-statement row and project it through the scraper.

    Skips with a clear message if entitlement is missing.
    """
    client = _polygon_client()
    preflight = ff._entitlement_preflight(client)
    if preflight.get("income_statements") != "ok":
        pytest.skip(f"income_statements entitlement: {preflight.get('income_statements')}")

    raw_rows = list(client.list_financials_income_statements(tickers="AAPL", limit=1))
    assert raw_rows, "expected at least one income-statement row for AAPL"

    projected = ff._project_row(
        raw_rows[0],
        field_map=ff.INCOME_FIELD_MAP,
        primary_ticker="AAPL",
        endpoint_tag="income_statements",
    )
    # Shared keys must all be populated for a well-formed Polygon response.
    assert projected["ticker"] == "AAPL"
    assert projected["filing_date"] is not None
    assert projected["period_end_date"] is not None
    assert projected["period_kind"] in ff.VALID_PERIOD_KINDS
    # Income-statement keys are present (may be None for partial coverage).
    for key in ff.INCOME_FIELD_MAP.values():
        assert key in projected


@pytest.mark.polygon
@pytest.mark.skipif(not _HAS_KEY, reason="POLYGON_API_KEY not set")
def test_polygon_three_endpoints_merge_by_fiscal_period() -> None:
    """Pull all 3 endpoints for AAPL and assert at least one merged row
    has ``source_endpoints`` covering all three.
    """
    client = _polygon_client()
    preflight = ff._entitlement_preflight(client)
    forbidden = [ep for ep, s in preflight.items() if s != "ok"]
    if forbidden:
        pytest.skip(f"missing entitlement on: {forbidden}")

    income = list(client.list_financials_income_statements(tickers="AAPL", limit=4))
    balance = list(client.list_financials_balance_sheets(tickers="AAPL", limit=4))
    cashflow = list(client.list_financials_cash_flow_statements(tickers="AAPL", limit=4))

    income_proj = [
        ff._project_row(
            r,
            field_map=ff.INCOME_FIELD_MAP,
            primary_ticker="AAPL",
            endpoint_tag="income_statements",
        )
        for r in income
    ]
    balance_proj = [
        ff._project_row(
            r,
            field_map=ff.BALANCE_FIELD_MAP,
            primary_ticker="AAPL",
            endpoint_tag="balance_sheets",
        )
        for r in balance
    ]
    cashflow_proj = [
        ff._project_row(
            r,
            field_map=ff.CASHFLOW_FIELD_MAP,
            primary_ticker="AAPL",
            endpoint_tag="cash_flow_statements",
        )
        for r in cashflow
    ]
    merged = ff._merge_three_endpoints(income_proj, balance_proj, cashflow_proj)
    assert merged, "expected at least one merged AAPL row"

    full_rows = [r for r in merged if len(r["source_endpoints"]) == 3]
    assert full_rows, (
        "expected at least one row with all 3 source_endpoints; got: "
        f"{[r['source_endpoints'] for r in merged]}"
    )
