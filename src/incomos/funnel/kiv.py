"""KIV basket — bidirectional promotion and demotion logic.

The KIV basket is NOT a waiting room. Every stock in KIV has an explicit
lifecycle, and demotion is as important as promotion.

Promotion paths (→ KIV):
  Prospects → KIV    when dip trigger fires

Demotion paths (KIV →):
  KIV → Dormant      TTL expired (90 days, no further trigger)
  KIV → Rejected     Material negative event, dividend cut, FCF negative,
                     dividend suspended

Promotion paths out of KIV (KIV →):
  KIV → Candidate    Secondary signal confirmed + context check passes

Demotion from higher stages:
  Candidate → KIV    Macro unfavorable, fundamentals still intact
  Finalist  → KIV    DD passed but entry price not attractive

All transitions record a reason in the audit trail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from incomos.core.types import DipClassification, FunnelStage, MacroRegimeContract, XBRLMetrics
from incomos.funnel.dip_trigger import DipTriggerResult

logger = logging.getLogger(__name__)


@dataclass
class KIVTransition:
    ticker: str
    from_stage: FunnelStage
    to_stage: FunnelStage
    direction: str          # PROMOTE | DEMOTE
    reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class KIVContextCheck:
    """Result of the Stage 2→3 context check.

    A context check determines whether a KIV stock is ready for full DD
    (promote to CANDIDATE) or should stay in KIV (or be demoted).
    """
    ticker: str
    promote_to_candidate: bool
    macro_allows: bool
    no_recent_red_flags: bool
    notes: list[str] = field(default_factory=list)


def evaluate_kiv_entry(
    ticker: str,
    dip: DipTriggerResult,
    current_stage: FunnelStage,
) -> KIVTransition | None:
    """Decide whether a PROSPECTS stock should be promoted to KIV.

    Returns a KIVTransition if the stock should move, None otherwise.
    Only runs when the dip trigger fires.
    """
    if current_stage != FunnelStage.PROSPECTS:
        return None
    if not dip.triggered:
        return None

    reason = (
        f"Dip trigger fired: {dip.trigger_strength} | "
        f"{dip.pct_below_52w_high:.1%} below 52W high | "
        f"RSI={dip.rsi:.1f}" if dip.rsi else f"{dip.pct_below_52w_high:.1%} below 52W high"
    )
    return KIVTransition(
        ticker=ticker,
        from_stage=FunnelStage.PROSPECTS,
        to_stage=FunnelStage.KIV,
        direction="PROMOTE",
        reason=reason,
    )


def evaluate_kiv_demotion(
    ticker: str,
    entered_kiv_at: datetime,
    latest_metrics: list[XBRLMetrics] | None,
    recent_8k_flags: list[str] | None = None,
) -> KIVTransition | None:
    """Check if a KIV stock should be demoted.

    Demotion triggers:
      1. Material negative events (8-K flags)
      2. FCF turning negative in latest fiscal year
      3. Dividend suspended (dividends_paid = 0 in latest year after being positive)

    TTL expiry is handled separately by refresh.check_kiv_ttl().
    Returns a KIVTransition to REJECTED if any hard demotion trigger fires.
    """
    flags: list[str] = []

    # Check 1: Material negative events from 8-K
    if recent_8k_flags:
        for flag in recent_8k_flags:
            if any(kw in flag.lower() for kw in
                   ["fraud", "restatement", "going concern", "bankruptcy",
                    "dividend suspended", "dividend eliminated"]):
                flags.append(f"8-K red flag: {flag}")

    # Check 2: FCF turned negative
    if latest_metrics:
        sorted_m = sorted(latest_metrics, key=lambda m: m.fiscal_year)
        latest = sorted_m[-1]
        if latest.free_cash_flow is not None and latest.free_cash_flow < 0:
            flags.append(f"FCF negative in FY{latest.fiscal_year}: ${latest.free_cash_flow/1e9:.1f}B")

        # Check 3: Dividend cut / suspension (if we have 2+ years of data)
        if len(sorted_m) >= 2:
            prev = sorted_m[-2]
            if (prev.dividends_paid is not None and prev.dividends_paid > 0
                    and latest.dividends_paid is not None and latest.dividends_paid == 0):
                flags.append(f"Dividend suspended in FY{latest.fiscal_year}")

    if not flags:
        return None

    return KIVTransition(
        ticker=ticker,
        from_stage=FunnelStage.KIV,
        to_stage=FunnelStage.REJECTED,
        direction="DEMOTE",
        reason=" | ".join(flags),
    )


def evaluate_kiv_to_candidate(
    ticker: str,
    dip: DipTriggerResult,
    macro: MacroRegimeContract,
    latest_metrics: list[XBRLMetrics] | None = None,
) -> KIVContextCheck:
    """Stage 2→3 context check: should this KIV stock become a CANDIDATE?

    Rules:
    - Macro must allow promotion (confidence ≥ 0.55 and not blocking)
    - No recent filing red flags
    - Dip must still be present (not recovered fully)
    """
    notes: list[str] = []
    macro_allows = True
    no_red_flags = True

    # Macro check: if we're in RECESSION_RISK with high confidence, tighten threshold
    if (macro.growth.state == "RECESSION_RISK"
            and macro.growth.confidence >= 0.85
            and macro.rates.state == "RATE_SHOCK_UP"):
        macro_allows = False
        notes.append(
            "Macro blocks promotion: RECESSION_RISK + RATE_SHOCK_UP at high confidence. "
            "Keep in KIV until macro regime improves."
        )

    # Dip still present check
    if dip.pct_below_52w_high < 0.05:
        macro_allows = False
        notes.append("Stock has recovered to near 52W high — dip thesis may be exhausted.")

    # Latest metrics check (quick — full DD is for Stage 3)
    if latest_metrics:
        sorted_m = sorted(latest_metrics, key=lambda m: m.fiscal_year)
        latest = sorted_m[-1]
        if latest.free_cash_flow is not None and latest.free_cash_flow < 0:
            no_red_flags = False
            notes.append(f"FCF negative in most recent year (FY{latest.fiscal_year}).")

    if macro_allows and no_red_flags:
        notes.append("Context check passed → promoting to CANDIDATE for full DD.")

    return KIVContextCheck(
        ticker=ticker,
        promote_to_candidate=macro_allows and no_red_flags,
        macro_allows=macro_allows,
        no_recent_red_flags=no_red_flags,
        notes=notes,
    )


def evaluate_candidate_demotion(
    ticker: str,
    macro: MacroRegimeContract,
    fundamentals_intact: bool,
) -> KIVTransition | None:
    """Stage 3 → demote back to KIV if macro unfavorable but fundamentals still good."""
    if (macro.growth.state == "RECESSION_RISK"
            and macro.growth.confidence >= 0.80
            and fundamentals_intact):
        return KIVTransition(
            ticker=ticker,
            from_stage=FunnelStage.CANDIDATE,
            to_stage=FunnelStage.KIV,
            direction="DEMOTE",
            reason="Macro unfavorable (RECESSION_RISK high confidence) but fundamentals intact. "
                   "Returning to KIV — watch for macro improvement.",
        )
    return None


def evaluate_finalist_demotion(
    ticker: str,
    current_price: float,
    target_entry_price: float,
) -> KIVTransition | None:
    """Demote a FINALIST back to KIV if entry price is not yet attractive."""
    if current_price > target_entry_price * 1.05:  # 5% tolerance
        return KIVTransition(
            ticker=ticker,
            from_stage=FunnelStage.FINALIST,
            to_stage=FunnelStage.KIV,
            direction="DEMOTE",
            reason=f"Entry price not yet attractive. Current: ${current_price:.2f}, "
                   f"target: ${target_entry_price:.2f}. Returning to KIV.",
        )
    return None
