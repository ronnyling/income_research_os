# Income Compounder Research OS

A funnel-based, agentic investment research platform that surfaces quality income stocks at dip entry points — not a trading bot, a decision-support system with human-in-the-loop confirmation.

**Current status:** Core pipeline production-hardened and end-to-end validated. 64 unit tests passing. MiMo 2.5 dip classification live — scores and MYR position sizes produced in beta run (MCD: 72/100 MYR 25k, MDT: 48.6/100 MYR 15k). EDGAR XBRL extraction fixed for companies that switched revenue/dividend reporting tags (V, ACN, MSFT, DUK dividend path).

## End Portfolio Goal

> A self-curating portfolio of dividend income stocks, each purchased at a dip entry point, yielding ≥X% (user-configured) from the purchase price, with capital appreciation potential that compounds the effective yield on cost over time.

The system does not chase yield. It finds **durable cash-generating businesses** whose current drawdown is a temporary derating, making them available at a cheaper entry that locks in a higher yield from day one. Capital gains are incidental — but they confirm business quality and increase the absolute dividend payout over time.

**Base currency: MYR.** All scoring, yield comparison, and position sizing is MYR-denominated. US stock yields are converted at the daily USD/MYR rate.

---

## Architecture — The Funnel

Stocks pass through progressively expensive gates. MiMo 2.5 (MoE 500B, thinking effort) is only invoked at Stage 2+ — never on the full universe.

```
ALL STOCKS  (US: EDGAR universe | MY: Bursa scraper / community data)
      │
      │  Stage 0 → 1: Quick Quantitative Screen   [rule-based MCP, near-zero cost]
      │  Checks: FCF+, dividend history, payout ratio, min market cap, positive ROIC
      │  Runs: on startup if last_refresh > 7 days
      ▼
PROSPECTS POOL  (~200–600 stocks)
      │
      │  Stage 1 → 2: Dip Trigger Screen   [rule-based price + volume, near-zero cost]
      │  Checks: % drawdown from 52W high, RSI below threshold, abnormal sell volume,
      │          any recent 8-K / Bursa announcement filed?
      │  Runs: on startup if last_refresh > 1 day
      ▼
KIV BASKET  (~30–80 stocks)   ← under continuous watch
      │     daily: price + technicals
      │     weekly: new filing scan
      │     TTL: 90 days max → Dormant if no trigger
      │
      │  Bidirectional — stocks are PROMOTED and DEMOTED:
      │  Promote → Candidate:  dip trigger fires + secondary signal confirmed
      │  Demote  → Dormant:    TTL expired (no trigger in 90 days)
      │  Demote  → Rejected:   material negative event, dividend cut,
      │                        fundamental deterioration, dividend suspended
      │
      │  Stage 2 → 3: Context Check   [light MiMo 2.5 if filing needs interpretation]
      │  Checks: macro regime alignment, sector peer breadth,
      │          quick filing scan for recent bad news
      │  Trigger: KIV stock hits secondary signal
      ▼
CANDIDATES  (~10–25 stocks)
      │
      │  Bidirectional:
      │  Demote → KIV: macro unfavorable but fundamentals still good (wait)
      │  Demote → Rejected: Stage 3 reveals structural deterioration
      │
      │  Stage 3 → 4: Full Due Diligence   [MiMo 2.5 with thinking effort, expensive]
      │  5-year filing analysis, dip classification reconciliation,
      │  full 4-dimension scoring, calculated risk annotation check
      │  Trigger: candidate threshold OR manual
      ▼
FINALISTS  (~3–10 stocks)
      │
      │  Bidirectional:
      │  Demote → KIV: DD passed but entry price not attractive yet (wait for better dip)
      │  Demote → Rejected: human review rejects with reason
      │
      │  Stage 4 → Decision: human review + annotation + position sizing
      ▼
DECISION QUEUE  →  Buy / Hold / Reject with conviction memo (MYR-sized)
```

---

## KIV Basket — Bidirectional Management

KIV is not a waiting room. It is an active watch list with explicit lifecycle rules.

| Transition | Direction | Trigger |
|---|---|---|
| Prospects → KIV | Promote | Dip trigger fires |
| KIV → Candidate | Promote | Secondary signal confirmed + context check passes |
| KIV → Dormant | Demote | TTL 90 days, no trigger — revisit monthly |
| KIV → Rejected | Demote | Material negative event, dividend cut, FCF turns negative, dividend suspended |
| Candidate → KIV | Demote | Macro unfavorable but fundamentals still good |
| Finalist → KIV | Demote | DD passed but entry price not attractive yet |
| Portfolio → Watch | Flag | Held stock: dividend safety score drops below threshold |

---

## Staleness-Triggered Refresh

Refreshes run on program startup, not on an external cron/scheduler. Each data type has its own staleness threshold.

| Data type | Stale after | Priority |
|---|---|---|
| Macro regime contract | 1 day | 1 (affects all decisions) |
| KIV price / dip trigger | 1 day | 2 |
| KIV filing watch (8-Ks / Bursa announcements) | 1 day | 3 |
| Universe quality filters | 7 days | 4 |
| Full filing re-analysis | 90 days OR new filing detected | 5 |
| KIV TTL check | Always on startup | — |

**Failure rule:** If a refresh fails, the `last_refresh` timestamp is NOT updated. The next run detects it as still stale and retries.

---

## Scoring Model

$$\text{Opportunity Score} = w_{\text{income}} \cdot \text{Income Quality} + w_{\text{business}} \cdot \text{Business Quality} + w_{\text{dip}} \cdot \text{Dip Quality} + w_{\text{oversold}} \cdot \text{Oversold}$$

All scores 0–100. Weights and all scoring thresholds are **config-driven** — no hardcoded values anywhere in the codebase. Override any threshold via environment variable (e.g. `DIP_TRIGGER__THRESHOLD_SOFT=0.10`).

Default weights: Income 0.30 · Business 0.30 · Dip 0.30 · Oversold 0.10.

All four inputs are **required**. There are no stub scores or neutral fallbacks — missing data raises a typed exception.

### Position sizing (yield-aware)

- Max income contribution per stock: `SIZING__MAX_INCOME_CONTRIBUTION` (default 15%)
- Score < `SCORE_MULTIPLIER__MID_THRESHOLD` (default 60) → `SCORE_MULTIPLIER__LOW` (default 0.6×)
- Score 60–79 → `SCORE_MULTIPLIER__MID` (default 1.0×)
- Score ≥ `SCORE_MULTIPLIER__HIGH_THRESHOLD` (default 80) → `SCORE_MULTIPLIER__HIGH` (default 1.2×)
- Analyst annotation override → up to `SCORE_MULTIPLIER__ANNOTATION_MAX` (default 1.5×)

---

## Production Design Principles

These rules are enforced in code, not by convention:

1. **No hardcoded thresholds** — every dip trigger level, scoring tier, weight, and multiplier lives in `core/config.py` as a Pydantic sub-model. Override via env var.
2. **No silent fallbacks** — if data is unavailable, a typed exception is raised. There are no neutral/zero/empty results that hide failure.
3. **No stub scores** — Dip Quality requires a validated MiMo 2.5 result (`MimoAnalysisRequiredError` otherwise). Price data requires a live snapshot (`PriceDataUnavailableError` otherwise).
4. **No partial credit** — missing XBRL fields score 0 pts. No benefit of the doubt in production scoring.
5. **LLM last** — rule-based MCP tools are always attempted first. MiMo 2.5 is invoked only at Stage 2+ for narrative interpretation and dip classification.
6. **Schema validation mandatory** — all MiMo 2.5 outputs are validated against Pydantic schemas before entering the scoring engine. Validation failure flags for human review.

---

## Configuration

All thresholds are in `src/incomos/core/config.py` as nested Pydantic sub-models. Override any value via environment variable using `__` as delimiter:

```bash
# Example: tighten dip trigger to 10% soft / 20% hard
DIP_TRIGGER__THRESHOLD_SOFT=0.10
DIP_TRIGGER__THRESHOLD_HARD=0.20

# Example: shift scoring weights
SCORING_WEIGHTS__DIP=0.40
SCORING_WEIGHTS__OVERSOLD=0.05
```

Sub-models: `DipTriggerCfg` · `ScoringWeightsCfg` · `ScoreMultiplierCfg` · `SizingCfg` · `IncomeQualityCfg` · `BusinessQualityCfg` · `OversoldQualityCfg` · `DipQualityCfg` · `MacroCfg` · `FilingCfg` · `BacktestCfg`

---



Multi-axis, versioned, confidence-gated. The Dip Reasoning Agent consumes this as a typed input — not free-form context.

### V1 axes and states

| Axis | V1 States | Data source |
|---|---|---|
| Market structure | `TRENDING_UP` `TRENDING_DOWN` `RANGING` | Price data (market-data-mcp) |
| Growth | `EXPANSION` `RECESSION_RISK` | NY Fed yield-curve recession probability |
| Rates | `STABLE` `RATE_SHOCK_UP` `RATE_SHOCK_DOWN` | FRED H.15 — 2Y/10Y velocity |
| Financial conditions | `LOOSE` `TIGHTENING` | NFCI / ANFCI (Chicago Fed, weekly) |

### Confidence-gated permission levels

| Confidence | Permission |
|---|---|
| ≥ 0.55 | Reference only — memo may mention macro, no classification change |
| ≥ 0.70 | Adjust weights — reduce oversold trust in TRENDING_DOWN; raise cyclical probability in RECESSION_RISK |
| ≥ 0.85 + sector_override_eligible | Override eligible — TRANSIENT → CYCLICAL_MACRO if peer breadth ≥ 0.65 and no new filing red flag |

### Classification outcomes

`TRANSIENT` · `TRANSIENT_MACRO_AMPLIFIED` · `CYCLICAL_IDIOSYNCRATIC` · `CYCLICAL_MACRO` · `STRUCTURAL` · `STRUCTURAL_MACRO_EXPOSED`

### Precedence rules (never override these)

1. Fraud / accounting / governance red flag → always `STRUCTURAL`, macro ignored
2. New filing deterioration (guidance cut + margin + balance sheet) → `STRUCTURAL`
3. Macro cannot upgrade `STRUCTURAL` → `TRANSIENT` (never)
4. NBER recession labels are backtest truth labels only — not live triggers
5. `RANGING` → increase oversold signal weight; `TRENDING_DOWN` → reduce it

---

## LLM Strategy

Rule-based MCP tools are always attempted first. MiMo 2.5 is invoked only when:
- XBRL / structured data is unavailable or returns null
- Filing section requires narrative understanding (MD&A, Risk Factors, YoY language diff)
- Dip classification requires reconciling conflicting signals

MiMo 2.5 always outputs into a **schema-validated JSON envelope**. If output fails schema validation, the stock is flagged for human review — never silently mis-scored.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (Python 3.14) |
| Database | PostgreSQL + SQLAlchemy |
| Document parsing | EDGAR XBRL API (staleness-aware tag selection) + 10-K/10-Q full-text extraction |
| LLM | MiMo 2.5 API (`mimo-v2.5-pro`, MoE 500B, thinking effort) |
| MCP servers | 6 domain servers (Python, FastMCP) |
| Price data | yfinance (US + `.KL` MY tickers) |
| Macro data | FRED API (free) — DGS2, DGS10, NFCI, DEXMAUS, RECPROUSM156N |
| Config | pydantic-settings v2 with `env_nested_delimiter="__"` |
| Tests | pytest — 64 tests, all passing |

---

## MCP Server Layout

| Server | Domain | Status |
|---|---|---|
| `sec-filings-mcp` | EDGAR submissions, XBRL facts, 10-K/10-Q/8-K, YoY text diff | ✅ Built |
| `market-data-mcp` | OHLCV, RSI, volume ratio, 52W high, technicals (yfinance) | ✅ Built |
| `macro-regime-mcp` | 4-axis regime detection — rates, growth, fin. conditions, market structure | ✅ Built |
| `research-store-mcp` | Funnel state, opportunity scores, refresh timestamps, annotations (PostgreSQL) | ✅ Built |
| `backtest-mcp` | Forward-return validator, calibration reports, score bucket hit rates | ✅ Built |
| `bursa-malaysia-mcp` | Bursa announcements, annual reports, `.KL` price data | ✅ Built (V1, fragile) |

---

## Quickstart

```bash
# Install
pip install -e ".[dev]"

# Required env vars
export MIMO_API_KEY=<your-key>          # required for Stage 3+ (dip classification)
export FRED_API_KEY=<optional>          # FRED public endpoints work without a key
export DATABASE_URL=postgresql://...    # required for --use-db mode

# Run the funnel against the default US universe
cd income_research_os
PYTHONPATH=src python scripts/run_funnel.py --portfolio-myr 500000

# With PostgreSQL persistence
PYTHONPATH=src python scripts/run_funnel.py --portfolio-myr 500000 --use-db

# Tests
PYTHONPATH=src python -m pytest tests/ -v
```

---

## Phase Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 1 | Core pipeline: EDGAR ingest → XBRL metrics → dip trigger → KIV lifecycle | ✅ Complete |
| 2 | Production hardening: config-driven thresholds, typed exceptions, no stubs, 64 tests | ✅ Complete |
| 3 | End-to-end live run: MiMo 2.5 dip classification → full scoring → MYR position sizing | ✅ Complete |
| 4 | Forward-return validation: score bucket hit rates against 30/90/180/365d returns | 🔜 Next |
| 5 | Malaysian stocks: Bursa scraper hardening + MYR-native scoring | 🔜 Parallel track |
| 6 | Broker MCP — pre-filled order generation for manual execution | 📋 Backlog |

---

## Open Items

| ID | Gap | Status |
|---|---|---|
| A | MiMo 2.5 few-shot grounding dataset — labeled dip classification examples not built | ✅ Resolved — 3 inline examples (TRANSIENT, STRUCTURAL, CYCLICAL_IDIOSYNCRATIC) added to `analyze_dip` prompt in `mimo.py` |
| B | Forward-return validation minimum hit rate — acceptable threshold not yet defined | ✅ Resolved — `min_return_threshold_90d` (5%) and `min_return_threshold_180d` (8%) added to `BacktestCfg`; `calibration_report_csv` defaults to these values when `threshold` is not supplied |
| C | Malaysian sector peer basket — Bursa sector indices less reliable than US ETFs | ✅ Resolved — `sector_override_eligible` now set to `True` when Bursa reports a sector, enabling peer-basket overrides where data is available |
| E | Bursa data contract — `.KL` scraper is fragile; fields/freshness not hardened | ✅ Resolved — `last_fetched_utc` ISO-8601 timestamp added to `BursaStockData` and `BursaFinancials`; quality grading upgraded to require market_cap + div_yield + sector for `FULL` |
| F | EDGAR iXBRL filing text extraction — large companies (MSFT, ACN, ABT, VZ) file inline XBRL where the `primaryDocument` is an index page, not the 10-K body. Sections are empty and MiMo is skipped. Requires index traversal to find the actual `.htm` document. | ✅ Resolved — `_clean_html` now strips `<head>` and `<ix:header>` blocks before extraction; `_find_body_document_via_index` fallback added to `get_filing_sections` for the case where primary doc still yields no sections |
| G | Regulated utility FCF screen — utilities (DUK, NEE) have structurally negative FCF due to regulated capex. The `min_fcf_positive_years` check excludes them. V1 limitation — a regulated-utility carve-out requires a separate scoring path. | ✅ Resolved — `sic_code` field added to `XBRLMetrics`; `get_sic()` populates it from EDGAR submissions; `stage01.py` detects SIC 4900-4999 and applies `min_fcf_positive_years_utility` (default 1) instead of the standard 3-year threshold |
