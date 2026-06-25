"""Live dip quality analysis for KIV candidates using MiMo 2.5.

Feeds XBRL financial data + EDGAR filing text (MD&A + Risk Factors) + price
action into MiMo v2.5-pro to classify the nature of each dip.

Filing text is fetched rule-based from EDGAR before calling MiMo.
MiMo receives structured financial narrative AND the actual 10-K MD&A /
Risk Factors text.  YoY Risk Factor diff (prior vs current year) is the
highest-value MiMo use case per architecture.
"""

import sys, json, time
sys.path.insert(0, "src")

import httpx
from incomos.core.config import get_settings
from incomos.data.edgar import EdgarClient
from incomos.data.filings import FilingsClient
from incomos.data.market import get_price_snapshot
from incomos.data.fred import get_usd_myr_rate
from incomos.scoring.engine import score
from incomos.scoring.dip import compute_dip_quality
from incomos.sizing import compute_position_size
from incomos.llm.schemas import validate_dip_analysis, ValidationError

COMPANY_NAMES = {"ABT": "Abbott Laboratories", "MCD": "McDonald's", "MDT": "Medtronic", "ACN": "Accenture"}
MACRO_CTX = (
    "Macro regime (2026-06-24): RANGING market structure, EXPANSION growth "
    "(NY Fed recession prob low), STABLE rates (DGS2 velocity within bounds), "
    "LOOSE financial conditions (NFCI=-0.51). USD/MYR=4.115."
)

def build_financial_narrative(ticker: str, metrics: list, snap) -> str:
    recent = sorted(metrics, key=lambda m: m.fiscal_year)[-5:]
    lines = [
        f"Company: {COMPANY_NAMES.get(ticker, ticker)} ({ticker})",
        f"Current price: ${snap.current_price:.2f}",
        f"52-week high: ${snap.week52_high:.2f}",
        f"Drawdown from 52W high: {snap.pct_below_52w_high:.1%}",
        f"RSI-14: {snap.rsi_14:.1f}",
        f"",
        "=== XBRL Financial Summary (last 5 fiscal years) ===",
    ]
    for m in recent:
        rev = f"${m.revenue/1e9:.1f}B" if m.revenue else "—"
        fcf = f"${m.free_cash_flow/1e9:.1f}B" if m.free_cash_flow is not None else "—"
        div = f"${m.dividends_paid/1e9:.2f}B" if m.dividends_paid else "—"
        payout = f"{m.fcf_payout_ratio:.1%}" if m.fcf_payout_ratio is not None else "—"
        lines.append(
            f"FY{m.fiscal_year}: Revenue={rev} FCF={fcf} Dividends={div} "
            f"FCF_Payout={payout}"
        )
    return "\n".join(lines)


def build_filing_section(current_sections: dict, prior_sections: dict) -> str:
    """Append MD&A and Risk Factor text (YoY) to the prompt narrative.

    Returns an empty string if no filing text is available.
    This is rule-based pre-processing — MiMo receives the text, not the fetch decision.
    """
    parts = []

    current_mda = current_sections.get("MDAA")
    if current_mda and current_mda.text:
        trunc = " [TRUNCATED]" if current_mda.truncated else ""
        parts.append(f"=== MD&A (current 10-K, {current_mda.filing_date}){trunc} ===")
        parts.append(current_mda.text[:8_000])  # ~2k tokens for MD&A

    current_rf = current_sections.get("RISK_FACTORS")
    prior_rf = prior_sections.get("RISK_FACTORS") if prior_sections else None

    if current_rf and current_rf.text:
        trunc = " [TRUNCATED]" if current_rf.truncated else ""
        parts.append(f"\n=== RISK FACTORS (current 10-K, {current_rf.filing_date}){trunc} ===")
        parts.append(current_rf.text[:12_000])  # ~3k tokens

    if prior_rf and prior_rf.text:
        trunc = " [TRUNCATED]" if prior_rf.truncated else ""
        parts.append(f"\n=== RISK FACTORS (PRIOR YEAR 10-K, {prior_rf.filing_date}){trunc} ===")
        parts.append("[YoY comparison: note any NEW risks or removed language vs current year]")
        parts.append(prior_rf.text[:6_000])  # ~1.5k tokens for prior year

    return "\n".join(parts) if parts else ""


def call_mimo(prompt: str) -> dict:
    s = get_settings()
    payload = {
        "model": s.mimo_model,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"Authorization": f"Bearer {s.mimo_api_key}", "Content-Type": "application/json"}
    resp = httpx.post(
        f"{s.mimo_api_base_url}/chat/completions",
        json=payload, headers=headers, timeout=90,
    )
    resp.raise_for_status()
    return resp.json()


DIP_SCHEMA_PROMPT = (
    "\n\nClassify the nature of this stock's current price decline. "
    "Return ONLY a JSON object (no markdown, no explanation outside the JSON) matching exactly:\n"
    '{"ticker":"...","classification":"TRANSIENT|TRANSIENT_MACRO_AMPLIFIED|CYCLICAL_IDIOSYNCRATIC|CYCLICAL_MACRO|STRUCTURAL|STRUCTURAL_MACRO_EXPOSED|UNKNOWN",'
    '"confidence":0.0_to_1.0,"evidence_summary":"50-200 word summary","key_risks":["..."],'
    '"transience_argument":"why this dip is or is not temporary",'
    '"structural_flags":["list ONLY genuine permanent business deterioration risks here — e.g. sustained FCF decline, dividend coverage breakdown, secular revenue loss. Use an EMPTY ARRAY [] if no structural concerns exist. Do NOT populate this field with positive statements or absence-of-risk notes."]}'
)


def main():
    settings = get_settings()
    if not settings.mimo_api_key:
        print("ERROR: MIMO_API_KEY not configured.")
        sys.exit(1)

    edgar = EdgarClient()
    filings = FilingsClient()
    usd_myr = get_usd_myr_rate()
    print(f"USD/MYR: {usd_myr}\n")
    print("=" * 70)
    print("KIV DIP QUALITY ANALYSIS — MiMo v2.5-pro")
    print("=" * 70)

    for ticker in ["ABT", "MCD", "MDT", "ACN"]:
        print(f"\n>>> Analysing {ticker} ({COMPANY_NAMES[ticker]})...")
        cik = edgar.resolve_cik(ticker)
        metrics = edgar.get_annual_metrics(ticker, cik, years=5)
        snap = get_price_snapshot(ticker)

        # Rule-based: fetch filing text BEFORE calling MiMo (architecture rule)
        print(f"    Fetching EDGAR filing sections (10-K MD&A + Risk Factors)...")
        try:
            current_sections, prior_sections = filings.get_yoy_sections(ticker, cik)
            mda_words = current_sections.get("MDAA", None)
            rf_words = current_sections.get("RISK_FACTORS", None)
            print(f"    MD&A: {mda_words.word_count if mda_words else 0} words | "
                  f"Risk Factors: {rf_words.word_count if rf_words else 0} words")
        except Exception as exc:
            print(f"    [warn] Filing fetch failed: {exc} — continuing without filing text")
            current_sections, prior_sections = {}, {}

        narrative = build_financial_narrative(ticker, metrics, snap)
        filing_text = build_filing_section(current_sections, prior_sections)

        if filing_text:
            prompt = f"{MACRO_CTX}\n\n{narrative}\n\n{filing_text}{DIP_SCHEMA_PROMPT}"
        else:
            prompt = f"{MACRO_CTX}\n\n{narrative}{DIP_SCHEMA_PROMPT}"

        try:
            raw_resp = call_mimo(prompt)
            content = raw_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content:
                # Log full response for debug
                print(f"  Empty content. finish_reason={raw_resp.get('choices',[{}])[0].get('finish_reason')}")
                print(f"  Full resp keys: {list(raw_resp.keys())}")
                continue

            # Strip markdown code fences if present
            content = content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()

            result_dict = json.loads(content)
        except json.JSONDecodeError as e:
            print(f"  JSON parse error: {e}\n  Raw: {content[:300]}")
            continue
        except Exception as e:
            print(f"  API error: {e}")
            continue

        # Mandatory schema validation
        try:
            validated = validate_dip_analysis(result_dict, ticker)
        except ValidationError as e:
            print(f"  SCHEMA VALIDATION FAILED — flagged for human review: {e}")
            continue

        print(f"  Classification : {validated.classification}")
        print(f"  Confidence     : {validated.confidence:.2f}")
        print(f"  Evidence       : {validated.evidence_summary[:200]}")
        print(f"  Transience arg : {validated.transience_argument[:150]}")
        if validated.structural_flags:
            print(f"  Structural flags: {validated.structural_flags}")

        # Score with real dip quality
        dq_score, dq_breakdown = compute_dip_quality(ticker, validated.model_dump())
        flag_penalty = dq_breakdown.get("flag_penalty", 0)
        penalty_str = f"  (flag penalty: -{flag_penalty})" if flag_penalty else ""
        print(f"\n  Dip Quality Score: {dq_score:.1f}/100{penalty_str}")

        # Full composite
        result = score(ticker, metrics, price_snap=snap, mimo_dip_result=validated.model_dump())
        sz = compute_position_size(result, 500_000, exchange="US", usd_myr_rate=usd_myr)

        print(f"  COMPOSITE SCORE: {result.composite:.1f}/100  "
              f"({'PARTIAL' if result.is_partial else 'FULL'})  "
              f"multiplier={result.base_size_multiplier}x")
        print(f"  Income={result.income_quality:.1f}  Business={result.business_quality:.1f}  "
              f"Dip={result.dip_quality:.1f}  Oversold={result.oversold_confidence:.1f}")
        print(f"  Position: MYR {sz.adjusted_position_myr:,.0f} (USD {sz.position_usd:,.0f})")
        print()
        time.sleep(3)  # brief cooldown between API calls


if __name__ == "__main__":
    main()
