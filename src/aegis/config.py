"""Pydantic v2 configuration loader.

All numeric thresholds from spec §4–§11 live in configs/*.yaml. This module
validates them into typed, frozen containers. A content hash of the loaded
config is written into every ledger row (spec principle 5 — auditability).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Gates -------------------------------------------------------------------
class PromotionThresholds(_Frozen):
    t_ic_min: float
    fdr_q: float
    dsr_min: float
    ff6_t_min: float


class RetirementThresholds(_Frozen):
    t_ic_max: float
    dsr_max: float


class DecayGate(_Frozen):
    horizons_days: list[int]
    retire_if_h1_above_and_h3_below: dict[str, float]
    retire_if_sign_flip: dict[str, int]


class CVConfig(_Frozen):
    forward_label_days: int
    embargo_days: int
    scheme: str
    fold_length_days: int


class HoldoutWindow(_Frozen):
    start: date
    end: date


class HoldoutConfig(_Frozen):
    in_sample: HoldoutWindow
    out_of_sample: HoldoutWindow
    locked_sub_holdout: HoldoutWindow


class GateConfig(_Frozen):
    promotion: PromotionThresholds
    retirement: RetirementThresholds
    decay_gate: DecayGate
    cv: CVConfig
    holdout: HoldoutConfig


# --- Risk --------------------------------------------------------------------
class RiskEstimation(_Frozen):
    method: Literal["wls", "ols"]
    weights: Literal["sqrt_mcap", "equal"]
    winsorize_sigma: float
    standardize: str


class CovarianceBlock(_Frozen):
    kind: Literal["ewma"]
    halflife_days: int
    v2_shrinkage: str | None = None


class RiskCovariance(_Frozen):
    factor: CovarianceBlock
    specific: CovarianceBlock


class RiskAcceptance(_Frozen):
    mean_style_r2_min: float
    residual_lag1_acf_max: float


class IndustryConfig(_Frozen):
    source: str
    count: int


class CountryConfig(_Frozen):
    include: bool


class RiskConfig(_Frozen):
    estimation: RiskEstimation
    styles: list[str]
    industries: IndustryConfig
    country: CountryConfig
    covariance: RiskCovariance
    acceptance: RiskAcceptance


# --- Universe ----------------------------------------------------------------
class ForwardReturnConfig(_Frozen):
    winsorize_percentiles: list[float]
    kind: Literal["arithmetic", "log"]


class DateRange(_Frozen):
    start: date
    end: date


class UniverseConfig(_Frozen):
    exchanges: list[str]
    share_classes: list[str]
    price_floor_usd: float
    min_history_days: int
    target_cross_section: int
    forward_return: ForwardReturnConfig
    date_range: DateRange


# --- Data layout & current working slice -------------------------------------
class DataPaths(_Frozen):
    raw: Path
    interim: Path
    processed: Path
    # Reference data (small, curated, version-controlled). Holds the S&P 500
    # membership CSV used by the Day 13 widened-universe slice and any
    # future reference tables.
    reference: Path = Path("data/reference")


class DataSnapshot(_Frozen):
    panel_filename: str
    factor_filename: str


class DataConfig(_Frozen):
    """Data layout + current working slice (configs/data.yaml).

    ``date_range`` here is the dev-iteration window. It narrows the full
    ``UniverseConfig.date_range`` during Weeks 1-4; by Week 5 the two should
    agree. Narrowing here never re-opens the locked sub-holdout (§5.2).
    """

    paths: DataPaths
    date_range: DateRange
    id_column: Literal["ticker"]
    calendar: Literal["NYSE"]
    snapshot: DataSnapshot


# --- Portfolio ---------------------------------------------------------------
class ObjectiveConfig(_Frozen):
    ex_ante_vol_target_annualized: float
    turnover_penalty_kappa: float | Literal["auto"]


class PortfolioConstraints(_Frozen):
    dollar_neutral: bool
    beta_neutral_to_crsp_vw: bool
    gross_leverage: float
    position_cap_abs: float
    position_cap_adv_pct: float
    style_exposure_budget_sigma: float
    industry_exposure_budget_sigma: float


class SolverConfig(_Frozen):
    backend: Literal["osqp", "ecos", "scs"]
    max_iter: int
    eps_abs: float
    eps_rel: float
    polish: bool


class AttributionConfig(_Frozen):
    residual_bp_per_day_max: float


class PortfolioConfig(_Frozen):
    objective: ObjectiveConfig
    constraints: PortfolioConstraints
    solver: SolverConfig
    attribution: AttributionConfig


# --- Costs -------------------------------------------------------------------
class SpreadConfig(_Frozen):
    method: Literal["corwin_schultz"]
    half_spread_floor_bp: dict[str, float]


class ImpactConfig(_Frozen):
    model: Literal["almgren"]
    eta: float
    exponent: float


class BorrowConfig(_Frozen):
    tiers_bp_per_year: dict[str, float]


class ComparisonConvention(_Frozen):
    flat_bp_one_way: float


class CapacityConfig(_Frozen):
    gmv_pct_of_adv_max: float
    position_pct_max: float


class CostConfig(_Frozen):
    spread: SpreadConfig
    impact: ImpactConfig
    borrow: BorrowConfig
    comparison_convention: ComparisonConvention
    capacity: CapacityConfig


# --- Factors -----------------------------------------------------------------
class FactorSpec(_Frozen):
    name: str
    family: str
    description: str
    formula: str
    inputs: list[str]
    operators: list[str]
    lookback_days: int
    neutralize: list[Literal["style", "industry", "country"]] = Field(default_factory=list)
    winsorize_sigma: float = 3.0
    horizon_days: int = 21
    reference_ic: float | None = None


class FactorCatalog(_Frozen):
    factors: list[FactorSpec] = Field(default_factory=list)


# --- Top-level container -----------------------------------------------------
# Fields that constitute research identity. ``content_hash`` covers exactly
# these and nothing else, so the hash is stable across Windows/macOS/Linux.
# If a new field is added to AegisConfig that represents a research
# commitment (not deployment layout or workflow state), add it here.
_RESEARCH_IDENTITY_FIELDS: frozenset[str] = frozenset(
    {"gates", "risk", "universe", "portfolio", "costs", "factors"}
)
_DATA_RESEARCH_IDENTITY_FIELDS: frozenset[str] = frozenset({"date_range", "id_column", "calendar"})


class AegisConfig(_Frozen):
    gates: GateConfig
    risk: RiskConfig
    universe: UniverseConfig
    portfolio: PortfolioConfig
    costs: CostConfig
    factors: FactorCatalog
    data: DataConfig

    def content_hash(self) -> str:
        """Platform-invariant SHA-256 of the research-identity subset.

        Includes the data sample definition (date range, id column, calendar)
        because changing the sampled research window must create config drift.
        Excludes data paths and artifact filenames because they are deployment
        layout, not research identity.

        This is what makes spec-§6 Module B ("every promoted factor replays
        bit-identical from the ledger") achievable when ledger rows written
        on Windows are replayed by CI on Linux or by a collaborator on macOS.
        """
        payload_obj = self.model_dump(mode="json", include=set(_RESEARCH_IDENTITY_FIELDS))
        payload_obj["data"] = self.data.model_dump(
            mode="json",
            include=set(_DATA_RESEARCH_IDENTITY_FIELDS),
        )
        payload = json.dumps(
            payload_obj,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    return loaded if isinstance(loaded, dict) else {}


def load_all(configs_dir: Path | None = None) -> AegisConfig:
    """Load and validate every config under ``configs/``."""
    root = configs_dir or CONFIGS_DIR
    return AegisConfig(
        gates=GateConfig(**_load_yaml(root / "gates.yaml")),
        risk=RiskConfig(**_load_yaml(root / "risk.yaml")),
        universe=UniverseConfig(**_load_yaml(root / "universe.yaml")),
        portfolio=PortfolioConfig(**_load_yaml(root / "portfolio.yaml")),
        costs=CostConfig(**_load_yaml(root / "costs.yaml")),
        factors=FactorCatalog(**_load_yaml(root / "factors.yaml")),
        data=DataConfig(**_load_yaml(root / "data.yaml")),
    )
