"""Pydantic row contracts for the Module A panel (spec §7, Week 1 Day 1).

These models document the column schema of the Parquet panel. They are not
instantiated per-row at runtime (that would be far too slow for a 15M-row
panel); they are used for:
  - documentation of the canonical PIT row;
  - validation of small samples and fixtures in tests;
  - downstream type-checking of record-level APIs (ledger registration, etc).

All models are frozen: a row, once constructed, is immutable. This matches
spec principle 5 — auditability — at the per-row level.

**Polygon.io taxonomy (2026-04-23 pivot from CRSP):** The primary security
key is ``ticker`` (string). ``ticker_type`` carries Polygon's security-class
code — we filter to ``"CS"`` (common stock) only per spec §7. CRSP's
``permno`` / integer ``share_code`` are gone.
"""

from __future__ import annotations

from datetime import date
from math import isnan
from numbers import Integral, Real
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExchangeCode = Literal["NYSE", "AMEX", "NASDAQ"]

# Polygon's ticker_type values we recognize. "OTHER" is a catch-all that
# fails the common-share filter (anything that isn't CS/PFD/ETF/etc. falls
# into OTHER so we can still carry the row for diagnostics).
TickerType = Literal[
    "CS",  # common stock — the only type that passes common_share_ok
    "PFD",  # preferred
    "ETF",
    "ETN",
    "ADRC",  # American depositary receipt (common)
    "ADRP",  # ADR (preferred)
    "UNIT",
    "WARRANT",
    "RIGHT",
    "FUND",
    "SP",  # structured product
    "OTHER",
]


class _FrozenRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class StockDailyRow(_FrozenRow):
    """One row of the canonical daily PIT panel.

    Every field here must be measurable with respect to the information
    filtration at ``date`` (spec §4.1). In particular, ``ret_1d`` uses only
    adjusted prices at or before ``date``; no forward returns live in the
    panel.

    ``ticker`` is the primary key; ``ticker_type`` replaces CRSP's
    ``share_code`` integer. ``adj_close`` is populated directly by Polygon's
    ``adjusted=true`` aggregate endpoint — we do not apply our own
    corporate-action adjustment.
    """

    date: date
    ticker: str = Field(min_length=1, max_length=16, description="Polygon ticker symbol")
    exchange: ExchangeCode
    ticker_type: TickerType
    is_common_share: bool

    raw_close: float = Field(gt=0.0)
    adj_close: float = Field(gt=0.0, description="Split/dividend-adjusted close")
    ret_1d: float | None = Field(
        default=None,
        description="Log return on adj_close; None only on the first eligible day",
    )

    volume: float = Field(ge=0.0)
    shares_out: float = Field(gt=0.0)
    mcap: float = Field(gt=0.0)

    # GICS filled in during Week 2 (requires Compustat or Polygon ticker details). Optional for now.
    gics_sector: str | None = None
    gics_industry: str | None = None

    eligible_flag: bool
    data_snapshot_id: str = Field(min_length=1, description="Hash of the source raw pull")

    @model_validator(mode="after")
    def _common_share_matches_ticker_type(self) -> StockDailyRow:
        """Invariant: is_common_share ↔ ticker_type == 'CS'.

        Spec §7 restricts V1 to common shares only. Tim confirmed ADRC
        exclusion on 2026-04-23 — ADRs are explicitly NOT common shares here.
        """
        expected = self.ticker_type == "CS"
        if self.is_common_share != expected:
            raise ValueError(
                f"is_common_share={self.is_common_share} inconsistent with "
                f"ticker_type={self.ticker_type!r} (only 'CS' is common-share)"
            )
        return self


class UniverseRow(_FrozenRow):
    """One row of the per-date eligibility decision for a given ticker.

    ``fail_reason`` names the FIRST failing rule encountered. Keeping it
    deterministic (not a list) makes universe-churn audits crisp: "Why did
    GE drop out on 2021-06-14?" has one answer.
    """

    date: date
    ticker: str = Field(min_length=1, max_length=16)

    eligible_flag: bool
    price_ok: bool
    history_ok: bool
    exchange_ok: bool
    common_share_ok: bool

    fail_reason: str | None = None

    @model_validator(mode="after")
    def _eligible_iff_no_fail_reason(self) -> UniverseRow:
        # The core invariant: a row is eligible if and only if every rule passed
        # AND fail_reason is None. Violating this is a correctness bug upstream.
        all_passed = self.price_ok and self.history_ok and self.exchange_ok and self.common_share_ok
        if self.eligible_flag and not all_passed:
            raise ValueError(
                "eligible_flag=True but at least one rule is False; universe filter is inconsistent"
            )
        if self.eligible_flag and self.fail_reason is not None:
            raise ValueError("eligible_flag=True but fail_reason is non-null")
        if (not self.eligible_flag) and self.fail_reason is None:
            raise ValueError("eligible_flag=False but fail_reason is null")
        return self


class SP500MembershipRow(_FrozenRow):
    """One historical S&P 500 membership interval (Week 2 Day 8).

    Multi-interval tickers (added → removed → re-added) are encoded as
    multiple rows: one per closed ``(date_added, date_removed)`` interval,
    plus one row with ``date_removed=None`` for current membership.

    The ``wiki_*`` prefix on the sector/sub-industry columns is deliberate
    (terminology discipline): Wikipedia's strings are derivative, not
    licensed GICS. Real GICS lands with Barra-lite in Week 6+.
    """

    ticker: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1)
    wiki_sector: str | None = None
    wiki_sub_industry: str | None = None
    date_added: date
    date_removed: date | None = None
    cik_code: int | None = None

    @model_validator(mode="after")
    def _removed_after_added(self) -> SP500MembershipRow:
        if self.date_removed is not None and self.date_removed < self.date_added:
            raise ValueError(
                f"date_removed={self.date_removed} precedes date_added={self.date_added}"
            )
        return self


class TickerMetadataRow(_FrozenRow):
    """One row of Polygon ticker reference metadata (Week 2 Day 9).

    Used by :mod:`aegis.data.ticker_reference` to answer "was this specific
    ticker actually listed on date t?" — the per-ticker tradability check
    that complements Day 8's index-membership gate.

    ``delisted_date`` is None for currently-active tickers. Boundary
    semantics match :func:`aegis.data.index_membership.active_on`:
    ``list_date <= t < delisted_date`` (inclusive at list, exclusive at
    delist), per spec §4.1 t-1 discipline.

    The ``sic_*`` columns ship the raw Polygon SIC payload — we deliberately
    do NOT call them ``gics_*`` (terminology discipline; SIC is not GICS).
    Sector-proxy enrichment (SIC → coarse sector mapping) lands in Week 3.
    """

    ticker: str = Field(min_length=1, max_length=16)
    name: str | None = None
    primary_exchange: str | None = None
    ticker_type: str | None = None  # Polygon's "CS"/"PFD"/"ETF"/... raw string
    list_date: date | None = None
    delisted_date: date | None = None
    sic_code: str | None = None  # Polygon returns SIC as 4-digit string
    sic_description: str | None = None
    cik: int | None = None

    @model_validator(mode="after")
    def _delisted_after_listed(self) -> TickerMetadataRow:
        if (
            self.delisted_date is not None
            and self.list_date is not None
            and self.delisted_date < self.list_date
        ):
            raise ValueError(
                f"delisted_date={self.delisted_date} precedes list_date={self.list_date}"
            )
        return self


class TickerAliasRow(_FrozenRow):
    """One historical ticker-rename interval (Week 2 Day 9).

    Encodes "during ``[effective_from, effective_to)``, the entity now
    known as ``canonical_ticker`` was traded under symbol ``alias``".
    Both endpoints can be None (NaT in pandas): ``effective_from=None``
    means "from the beginning of trading", ``effective_to=None`` means
    "still the active symbol" (rare — usually the canonical itself
    fills that period).

    Scope: small by design (~10–20 entries), high-impact reconciliation
    cases only. NOT a full historical security master.
    """

    canonical_ticker: str = Field(min_length=1, max_length=16)
    alias: str = Field(min_length=1, max_length=16)
    effective_from: date | None = None
    effective_to: date | None = None
    note: str = Field(default="")

    @model_validator(mode="after")
    def _to_after_from(self) -> TickerAliasRow:
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError(
                f"effective_to={self.effective_to} precedes effective_from={self.effective_from}"
            )
        return self


PeriodKind = Literal["quarterly", "annual", "trailing_twelve_months"]

# The three Polygon v1 financial statement endpoints. A merged FundamentalsRow
# may carry data from any subset of these (Polygon files them separately and
# they're not always all present for the same fiscal period yet).
FinancialEndpoint = Literal["income_statements", "balance_sheets", "cash_flow_statements"]


class FundamentalsRow(_FrozenRow):
    """One Polygon v1 financial statement row (Week 3 Day 15).

    PIT discipline (spec §4.1): ``filing_date`` is the t-1 visibility boundary,
    NOT ``period_end_date``. ``period_end_date`` is "what economic period this
    report describes"; ``filing_date`` is "when this report became publicly
    available." Only the latter is F_t-measurable at t.

    Logical key: ``(ticker_or_cik, fiscal_year, fiscal_quarter, period_kind)``.
    Restatements break uniqueness on this key — multiple rows can share a
    fiscal period with different ``filing_date`` values. PIT lookup helpers
    (Day 16) de-dupe by keeping the latest filing per period.

    Field naming matches our internal vocabulary, NOT Polygon's. Mapping
    (Polygon → ours) lives in :mod:`scripts.fetch_polygon_fundamentals`:
      revenue → revenues
      net_income_loss_attributable_common_shareholders → net_income
      basic_earnings_per_share → eps_basic
      diluted_earnings_per_share → eps_diluted
      basic_shares_outstanding → weighted_avg_shares_basic
      diluted_shares_outstanding → weighted_avg_shares_diluted
      total_equity_attributable_to_parent → common_equity
      net_cash_from_operating_activities → operating_cash_flow
      timeframe → period_kind
      period_end → period_end_date
      tickers[0] → ticker (multi-class structures keep tickers[1:] dropped)

    ``source_endpoints`` records which of the three Polygon endpoints
    contributed to this merged row. A row with only an income-statement
    fingerprint will have ``("income_statements",)``; a fully merged row
    will have all three.
    """

    ticker: str = Field(min_length=1, max_length=16)
    cik: int | None = None
    filing_date: date  # PIT visibility boundary; non-null required
    period_end_date: date  # the as-of date the report describes
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None  # 1-4 for quarterly, None for annual/TTM
    period_kind: PeriodKind

    revenues: float | None = None
    net_income: float | None = None
    eps_basic: float | None = None
    eps_diluted: float | None = None
    weighted_avg_shares_basic: float | None = None
    weighted_avg_shares_diluted: float | None = None
    common_equity: float | None = None
    total_assets: float | None = None
    operating_cash_flow: float | None = None

    source_endpoints: tuple[FinancialEndpoint, ...] = Field(default=())

    @field_validator("cik", "fiscal_year", "fiscal_quarter", mode="before")
    @classmethod
    def _coerce_optional_int_from_parquet(cls, value: object) -> object:
        """Accept pandas/pyarrow nullable integer shapes after parquet round-trip."""
        if value is None or value.__class__.__name__ in {"NAType", "NaTType"}:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, Integral):
            return int(value)
        if isinstance(value, Real):
            as_float = float(value)
            if isnan(as_float):
                return None
            if as_float.is_integer():
                return int(as_float)
        return value

    @field_validator("source_endpoints", mode="before")
    @classmethod
    def _coerce_source_endpoints_from_parquet(cls, value: object) -> object:
        """PyArrow returns list-like parquet cells as ndarray/list, not tuple."""
        if value is None or value.__class__.__name__ in {"NAType", "NaTType"}:
            return ()
        if isinstance(value, str):
            return (value,)
        if hasattr(value, "tolist"):
            value = value.tolist()
        if isinstance(value, list | tuple | set):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def _filing_not_before_period_end(self) -> FundamentalsRow:
        """Filings always land on or after the period close.

        SEC Form 10-Q is due ~40-45 days after quarter-end; 10-K is ~60-90.
        We allow same-day equality (it never happens in real data but
        engineered fixtures should not have to invent +1 days).
        """
        if self.filing_date < self.period_end_date:
            raise ValueError(
                f"filing_date={self.filing_date} precedes "
                f"period_end_date={self.period_end_date} "
                "(filings always land on or after period close)"
            )
        return self

    @model_validator(mode="after")
    def _quarter_consistent_with_kind(self) -> FundamentalsRow:
        """quarterly rows must have fiscal_quarter ∈ {1,2,3,4}; annual/TTM
        rows must have fiscal_quarter=None."""
        if self.period_kind == "quarterly":
            if self.fiscal_quarter not in (1, 2, 3, 4):
                raise ValueError(
                    f"period_kind='quarterly' but fiscal_quarter={self.fiscal_quarter!r} "
                    "(must be 1, 2, 3, or 4)"
                )
        elif self.fiscal_quarter is not None:
            raise ValueError(
                f"period_kind={self.period_kind!r} but fiscal_quarter is set; "
                "annual/TTM rows must have fiscal_quarter=None"
            )
        return self


__all__ = [
    "ExchangeCode",
    "FinancialEndpoint",
    "FundamentalsRow",
    "PeriodKind",
    "SP500MembershipRow",
    "StockDailyRow",
    "TickerAliasRow",
    "TickerMetadataRow",
    "TickerType",
    "UniverseRow",
]
