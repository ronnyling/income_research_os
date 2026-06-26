"""Core domain types for the Income Compounder Research OS.

All Pydantic models. No logic here — types only.
MYR is the base currency for all yield and sizing calculations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Exchange(str, Enum):
    US = "US"   # SEC-registered, EDGAR data
    MY = "MY"   # Bursa Malaysia, scraper / community data (V1 limitation)


class FunnelStage(str, Enum):
    """All stages a stock can occupy in the funnel.

    Transitions are bidirectional — demotion is as important as promotion.
    See README § KIV Basket — Bidirectional Management for transition rules.
    """
    UNIVERSE = "UNIVERSE"               # Known but not yet quality-screened
    PROSPECTS = "PROSPECTS"             # Passed Stage 0-1 quality screen
    KIV = "KIV"                         # Keep In View — dip trigger fired, watching
    DORMANT = "DORMANT"                 # KIV TTL expired with no trigger (revisit monthly)
    REJECTED = "REJECTED"               # Failed quality or material negative event
    CANDIDATE = "CANDIDATE"             # Passed Stage 2 context check
    FINALIST = "FINALIST"               # Passed Stage 3 full DD
    DECISION_QUEUE = "DECISION_QUEUE"   # Ready for human buy/hold/reject decision
    PORTFOLIO = "PORTFOLIO"             # Currently held
    PORTFOLIO_WATCH = "PORTFOLIO_WATCH" # Held but flagged — dividend safety score dropped


class DipClassification(str, Enum):
    """Dip reason taxonomy. Output of the Dip Reasoning Agent (Stage 3).

    Macro can adjust classification (confidence ≥ 0.70) or override it
    (confidence ≥ 0.85 + sector_override_eligible). Macro CANNOT upgrade
    STRUCTURAL → TRANSIENT. That is a hard rule.
    """
    TRANSIENT = "TRANSIENT"
    TRANSIENT_MACRO_AMPLIFIED = "TRANSIENT_MACRO_AMPLIFIED"
    CYCLICAL_IDIOSYNCRATIC = "CYCLICAL_IDIOSYNCRATIC"
    CYCLICAL_MACRO = "CYCLICAL_MACRO"
    STRUCTURAL = "STRUCTURAL"
    STRUCTURAL_MACRO_EXPOSED = "STRUCTURAL_MACRO_EXPOSED"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Macro regime contract (v1 states — do not add new states without review)
# ---------------------------------------------------------------------------


class MarketStructure(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    UNKNOWN = "UNKNOWN"


class GrowthState(str, Enum):
    EXPANSION = "EXPANSION"
    RECESSION_RISK = "RECESSION_RISK"
    UNKNOWN = "UNKNOWN"


class RatesState(str, Enum):
    STABLE = "STABLE"
    RATE_SHOCK_UP = "RATE_SHOCK_UP"
    RATE_SHOCK_DOWN = "RATE_SHOCK_DOWN"
    UNKNOWN = "UNKNOWN"


class FinancialConditions(str, Enum):
    LOOSE = "LOOSE"
    TIGHTENING = "TIGHTENING"
    UNKNOWN = "UNKNOWN"


class MacroAxisReading(BaseModel):
    state: str
    confidence: float   # 0.0 – 1.0
    severity: float     # 0.0 – 1.0


class SectorImpact(BaseModel):
    sector: str
    macro_alignment: float          # 0.0 – 1.0 (how correlated sector is to macro move)
    peer_drawdown_breadth: float    # 0.0 – 1.0 (% of sector peers also down)
    # V1 NOTE: sector_override_eligible defaults False for MY stocks — Bursa sector
    # indices are less liquid and less reliable than US ETFs. Do not silently apply.
    sector_override_eligible: bool = False


class MacroUsagePolicy(BaseModel):
    min_confidence_to_reference: float = 0.55
    min_confidence_to_adjust_classification: float = 0.70
    min_confidence_to_override_classification: float = 0.85
    ttl_hours: int = 24


class MacroRegimeContract(BaseModel):
    """Versioned macro regime payload. Dip Reasoning Agent consumes this as a
    typed input — not free-form context.

    Confidence-gated permission levels:
      ≥ 0.55 → reference only, no classification change
      ≥ 0.70 → adjust classification weights
      ≥ 0.85 + sector_override_eligible → override eligible

    Hard rule: macro CANNOT upgrade STRUCTURAL → TRANSIENT.
    """
    schema_version: str = "1.0.0"
    as_of: datetime
    primary_regime: str
    primary_confidence: float

    market_structure: MacroAxisReading
    growth: MacroAxisReading
    rates: MacroAxisReading
    financial_conditions: MacroAxisReading

    evidence: list[dict[str, Any]] = Field(default_factory=list)
    sector_impact: SectorImpact | None = None
    usage_policy: MacroUsagePolicy = Field(default_factory=MacroUsagePolicy)


# ---------------------------------------------------------------------------
# Financial data
# ---------------------------------------------------------------------------


class XBRLMetrics(BaseModel):
    """Annual XBRL metrics extracted from EDGAR company facts.

    All monetary values are in USD (EDGAR native). MYR conversion is applied
    at scoring time using the daily USD/MYR rate from FRED (series DEXMAUS).
    """
    ticker: str
    cik: str
    fiscal_year: int
    fiscal_period: str          # "FY" for annual
    sic_code: str | None = None  # EDGAR SIC code, e.g. "4911" for electric utilities

    revenue: float | None = None
    net_income: float | None = None
    operating_cash_flow: float | None = None
    capex: float | None = None                   # Payments for PP&E (positive = outflow)
    dividends_paid: float | None = None          # Cash dividends paid (positive = outflow)
    dps_declared: float | None = None            # Dividends per share declared
    dps_paid: float | None = None                # Dividends per share paid
    total_debt: float | None = None
    cash: float | None = None
    equity: float | None = None
    earnings_per_share: float | None = None  # Basic EPS from XBRL

    @computed_field
    @property
    def free_cash_flow(self) -> float | None:
        if self.operating_cash_flow is None or self.capex is None:
            return None
        return self.operating_cash_flow - self.capex

    @computed_field
    @property
    def fcf_payout_ratio(self) -> float | None:
        if self.free_cash_flow is None or self.dividends_paid is None:
            return None
        if self.free_cash_flow <= 0:
            return None  # FCF negative — payout ratio meaningless (and dangerous)
        return self.dividends_paid / self.free_cash_flow

    @computed_field
    @property
    def net_debt(self) -> float | None:
        if self.total_debt is None or self.cash is None:
            return None
        return self.total_debt - self.cash

    source: str = "EDGAR_XBRL"
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Funnel state
# ---------------------------------------------------------------------------


class StockRecord(BaseModel):
    """A stock's current position in the funnel.

    Stored in research-store-mcp (PostgreSQL). Transitions are always recorded
    with a reason so the audit trail is complete.
    """
    ticker: str
    exchange: Exchange
    company_name: str
    cik: str | None = None      # EDGAR CIK — US stocks only

    funnel_stage: FunnelStage = FunnelStage.UNIVERSE
    last_stage_change: datetime | None = None
    stage_change_reason: str | None = None

    # Set when stock enters KIV or beyond
    dip_classification: DipClassification | None = None
    dip_severity_pct: float | None = None   # % below 52W high at time of KIV entry

    # Human annotation override — set via decision queue review
    conviction_note: str | None = None
    annotation_size_multiplier: float | None = None  # 0.6–1.5 range

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class OpportunityScore(BaseModel):
    """4-dimension opportunity score. All values 0–100. MYR-base.

    Weights are implementation defaults — calibrate at Phase 2→3 gate via
    forward-return analysis before trusting these numbers (Gap B).
    """
    ticker: str
    income_quality: float       # 0–100
    business_quality: float     # 0–100
    dip_quality: float          # 0–100
    oversold_confidence: float  # 0–100

    @computed_field
    @property
    def composite(self) -> float:
        return (
            0.30 * self.income_quality
            + 0.30 * self.business_quality
            + 0.30 * self.dip_quality
            + 0.10 * self.oversold_confidence
        )

    @computed_field
    @property
    def base_size_multiplier(self) -> float:
        """Position sizing multiplier before annotation override.
        Max 15% of total portfolio income contribution per stock.
        """
        s = self.composite
        if s < 60:
            return 0.6
        elif s < 80:
            return 1.0
        return 1.2

    scored_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Refresh tracking
# ---------------------------------------------------------------------------


class RefreshRecord(BaseModel):
    """Tracks last_refresh per data type (and optionally per ticker).

    IMPORTANT: last_refresh is only updated on successful refresh completion.
    A failed refresh must not update this timestamp — the next run will
    detect the data as still stale and retry.
    """
    data_type: str
    ticker: str | None = None   # None = global data type (e.g., macro regime)
    last_refresh: datetime | None = None


class RefreshTask(BaseModel):
    """A queued refresh task, ordered by priority (lower = higher priority)."""
    data_type: str
    ticker: str | None = None
    priority: int
    last_refresh: datetime | None = None
    max_age_hours: float
