# Income Compounder Research OS

A funnel-based, agentic investment research platform that surfaces quality income stocks at dip entry points — not a trading bot, a decision-support system with human-in-the-loop confirmation.

**Current status:** Core pipeline production-hardened and end-to-end validated. 114 unit tests passing. All stages run in **parallel** via `ThreadPoolExecutor` — EDGAR fetches, price snapshots, filing downloads, and MiMo batch classification all use concurrent workers. Stage 0→1 uses **domain-based TTL skip** (per-domain cache for aristocrats/kings/quality — skips re-screening when ticker lists haven't changed). Stage 1→2 redesigned from binary dip trigger to continuous Entry Attractiveness scoring (0-100) with 5 dimensions. Expanded universe: 197 US dividend stocks (110 Aristocrats + 25 Kings + 81 Quality), plus live NOBL ETF holdings via `--tier nobl`. KIV basket fills to target count (default **20**, configurable via `--kiv-target`) with configurable minimum floor. MiMo 2.5 dip classification operates in **parallel batch mode** — multiple chunks classified concurrently (default 2 concurrent batches of 5 stocks each). Classifications are **cached with filing-hash TTL** (default 90 days) — no redundant API calls when filings haven't changed. Corrective retry on schema validation failure. 5 few-shot examples in prompt.

## End Portfolio Goal

> A self-curating portfolio of dividend income stocks, each purchased at a dip entry point, yielding ≥X% (user-configured) from the purchase price, with capital appreciation potential that compounds the effective yield on cost over time.

The system does not chase yield. It finds **durable cash-generating businesses** whose current drawdown is a temporary derating, making them available at a cheaper entry that locks in a higher yield from day one. Capital gains are incidental — but they confirm business quality and increase the absolute dividend payout over time.

**Base currency: MYR.** All scoring, yield comparison, and position sizing is MYR-denominated. US stock yields are converted at the daily USD/MYR rate.

---

## Architecture — The Funnel

Stocks pass through progressively expensive gates. MiMo 2.5 (MoE 500B, thinking effort) is only invoked at Stage 2+ — never on the full universe.

````
ALL STOCKS  (US: EDGAR universe 197 stocks | MY: Bursa scraper / community data)
      │
      │  Stage 0 → 1: Quick Quantitative Screen   [rule-based MCP, near-zero cost]
      │  Checks: FCF+, dividend history, payout ratio, min market cap, positive ROIC
      │  Runs: on startup if last_refresh > 7 days
      ▼
PROSPECTS POOL  (~100–150 stocks from 197 universe)
      │
      │  Stage 1 → 2: Entry Attractiveness Ranking   [rule-based, near-zero cost]
      │  Continuous 0-100 score combining:
      │    - Price drawdown from 52W high (30%)
      │    - Yield expansion vs 5yr average (25%)
      │    - Dividend growth trajectory (20%)
      │    - Valuation / FCF quality (15%)
      │    - Price trend momentum (10%)
      │  Stocks ranked by score, tagged (DEEP_DIP, YIELD_EXPANSION, DIVIDEND_GROWTH,
      │    UNDERVALUED, TRENDING_DOWN), top N promoted to KIV (target = 20)
      │  Minimum attractiveness floor prevents promoting garbage in bull markets
      ▼
KIV BASKET  (target 20 stocks)   ← under continuous watch
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
````

---

## KIV Basket — Bidirectional Management

KIV is not a waiting room. It is an active watch list with explicit lifecycle rules.

| Transition | Direction | Trigger |
|---|---|---|
| Prospects → KIV | Promote | Entry Attractiveness score meets floor, ranked in top N (target 20) |
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

### Domain Cache TTL

The EDGAR domain cache (`.funnel_domain_cache.json`) stores XBRL metrics per domain group (aristocrats, kings, quality) to avoid re-scraping unchanged ticker lists. A **7-day TTL** ensures periodic freshness even when ticker lists haven't changed:

| Check | Condition | Action |
|---|---|---|
| Ticker list changed | Diff detected | Full re-scrape (always) |
| `cached_at` missing | Old cache format | Force re-scrape |
| TTL expired (>7 days) | Age check | Force re-scrape |
| Cache valid | Same tickers + within TTL | Use cached data |

### Pipeline Checkpoint

Every full pipeline run saves a checkpoint to `.funnel_checkpoint.json` containing all intermediate state: tickers, screen results, EA results, MiMo classifications, scores, position sizing, prices, and macro regime. MiMo results are also saved in human-readable form to `mimo_results.txt`.

**`--from-checkpoint`** regenerates the HTML report instantly from the saved checkpoint — no EDGAR scraping, no API calls, no MiMo classification. This is the recommended way to iterate on HTML styling or regenerate reports after code changes.

---

## Scoring Model

$$\text{Opportunity Score} = w_{\text{income}} \cdot \text{Income Quality} + w_{\text{business}} \cdot \text{Business Quality} + w_{\text{dip}} \cdot \text{Dip Quality} + w_{\text{oversold}} \cdot \text{Oversold}$$

All scores 0–100. Weights and all scoring thresholds are **config-driven** — no hardcoded values anywhere in the codebase. Override any threshold via environment variable (e.g. `DIP_TRIGGER__THRESHOLD_SOFT=0.10`).

Default weights: Income 0.30 · Business 0.30 · Dip 0.30 · Oversold 0.10.

All four inputs are **required**. There are no stub scores or neutral fallbacks — missing data raises a typed exception.

### Entry Attractiveness Score (Stage 1→2)

Replaces the binary dip trigger with a continuous 0-100 score that identifies the best entry opportunities across ALL quality-screened prospects:

$$\text{EA Score} = 0.30 \cdot \text{Drawdown} + 0.25 \cdot \text{Yield Expansion} + 0.20 \cdot \text{Div Growth} + 0.15 \cdot \text{Valuation} + 0.10 \cdot \text{Trend}$$

Each stock is tagged with the criteria it scored highest on:
- **DEEP_DIP** — 15%+ below 52W high
- **YIELD_EXPANSION** — current yield >10% above 5yr average
- **DIVIDEND_GROWTH** — positive DPS CAGR
- **UNDERVALUED** — high FCF margin + revenue growth
- **TRENDING_DOWN** — confirmed downtrend (below both SMAs)

Stocks are ranked by EA score, top N promoted to KIV (target count configurable, default 20). Minimum floor prevents promoting stocks below a quality threshold in bull markets.

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

# Example: shift scoring weights (must sum to 1.0 — validated at startup)
SCORING_WEIGHTS__DIP=0.40
SCORING_WEIGHTS__OVERSOLD=0.05

# Example: adjust FCF CAGR thresholds
BUSINESS_Q__FCF_CAGR_T1_MIN=0.20

# Example: adjust drawdown breakpoints
ENTRY_ATTRACTIVENESS__DRAWDOWN_T4=0.12

# Example: MiMo batch processing (5 stocks per chunk, 2 concurrent chunks, 90-day cache TTL)
MIMO_BATCH__BATCH_SIZE=5
MIMO_BATCH__MAX_CONCURRENT_BATCHES=2
MIMO_BATCH__TTL_DAYS=90
MIMO_BATCH__MAX_CHARS_PER_STOCK=2000

# Example: KIV basket target count
KIV__TARGET_SIZE=20
```

Sub-models: `DipTriggerCfg` · `EntryAttractivenessCfg` · `KivCfg` · `ScoringWeightsCfg` · `ScoreMultiplierCfg` · `SizingCfg` · `IncomeQualityCfg` · `BusinessQualityCfg` · `OversoldQualityCfg` · `DipQualityCfg` · `MacroCfg` · `MimoBatchCfg` · `FilingCfg` · `BacktestCfg`

### CLI Flags

| Flag | Default | Description |
|---|---|---|
| `--portfolio-myr` | (required) | Total portfolio value in MYR |
| `--tier` | `all` | Universe tier: `aristocrats`, `kings`, `quality`, `nobl`, `all` |
| `--tickers` | (none) | Explicit ticker list — overrides `--tier` |
| `--kiv-target` | `20` | KIV basket target count |
| `--max-concurrent` | `2` | Max concurrent MiMo batch chunks |
| `--use-db` | `false` | Enable PostgreSQL persistence |
| `--skip-mimo` | `false` | Skip MiMo classification (Stage 2→3) |
| `--output-html` | (none) | Generate HTML report at specified path |
| `--from-checkpoint` | `false` | Regenerate HTML from last checkpoint (skip full pipeline) |

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

MiMo 2.5 operates in **parallel batch mode** at Stage 2→3:
1. All filing data is gathered first in parallel (ThreadPoolExecutor, 5 workers)
2. Cached classifications are checked (filing-hash + TTL, default 90 days)
3. Uncached stocks are chunked into batches (default 5 per API call)
4. Multiple chunks are classified **concurrently** (default 2 parallel batches)
5. Results are cached for future runs

This reduces wall-clock time significantly — 5 stocks classify in ~90s (18s/stock) vs ~225s sequential (45s/stock), a 2.5× speedup. On subsequent runs, stocks whose filings haven't changed use the cache (zero MiMo calls).

MiMo API uses `httpx.post()` with 180s read timeout (thinking mode is silent for 45-72s before responding) and a keep-alive monitor that logs at 30s intervals.

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
| Tests | pytest — 114 tests, all passing |
| Parallelism | `concurrent.futures.ThreadPoolExecutor` across all stages |

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

# Run the funnel against the full US dividend universe (197 stocks)
cd income_research_os
PYTHONPATH=src python scripts/run_funnel.py --portfolio-myr 500000

# Run against a specific tier
PYTHONPATH=src python scripts/run_funnel.py --tier aristocrats --portfolio-myr 500000
PYTHONPATH=src python scripts/run_funnel.py --tier kings --portfolio-myr 500000
PYTHONPATH=src python scripts/run_funnel.py --tier quality --portfolio-myr 500000
PYTHONPATH=src python scripts/run_funnel.py --tier nobl --portfolio-myr 500000  # live NOBL ETF holdings

# Run against specific tickers (overrides --tier)
PYTHONPATH=src python scripts/run_funnel.py --tickers KO JNJ PG ACN MSFT --portfolio-myr 500000

# Customize KIV target and concurrency
PYTHONPATH=src python scripts/run_funnel.py --portfolio-myr 500000 --kiv-target 30
PYTHONPATH=src python scripts/run_funnel.py --portfolio-myr 500000 --max-concurrent 4

# With PostgreSQL persistence
PYTHONPATH=src python scripts/run_funnel.py --portfolio-myr 500000 --use-db

# Generate HTML report
PYTHONPATH=src python scripts/run_funnel.py --portfolio-myr 500000 --output-html docs/index.html

# Regenerate HTML from last checkpoint (instant, no re-run)
PYTHONPATH=src python scripts/run_funnel.py --from-checkpoint --output-html docs/index.html

# Gate test: validate MiMo batch before full universe run
PYTHONPATH=src python scripts/test_mimo_batch.py --stocks KO JNJ PG MSFT ACN --batch-size 5
PYTHONPATH=src python scripts/test_mimo_batch.py --stocks KO --batch-size 1 --skip-filing-fetch

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
| A | MiMo 2.5 few-shot grounding dataset — labeled dip classification examples not built | ✅ Resolved — 5 inline examples (TRANSIENT, STRUCTURAL, CYCLICAL_IDIOSYNCRATIC, CYCLICAL_MACRO, CYCLICAL_IDIOSYNCRATIC with segment weakness) added to `analyze_dip` prompt in `mimo.py` |
| B | Forward-return validation minimum hit rate — acceptable threshold not yet defined | ✅ Resolved — `min_return_threshold_90d` (5%) and `min_return_threshold_180d` (8%) added to `BacktestCfg`; `calibration_report_csv` defaults to these values when `threshold` is not supplied |
| C | Malaysian sector peer basket — Bursa sector indices less reliable than US ETFs | ✅ Resolved — `sector_override_eligible` now set to `True` when Bursa reports a sector, enabling peer-basket overrides where data is available |
| E | Bursa data contract — `.KL` scraper is fragile; fields/freshness not hardened | ✅ Resolved — `last_fetched_utc` ISO-8601 timestamp added to `BursaStockData` and `BursaFinancials`; quality grading upgraded to require market_cap + div_yield + sector for `FULL` |
| F | EDGAR iXBRL filing text extraction — large companies (MSFT, ACN, ABT, VZ) file inline XBRL where the `primaryDocument` is an index page, not the 10-K body. Sections are empty and MiMo is skipped. Requires index traversal to find the actual `.htm` document. | ✅ Resolved — `_clean_html` now strips `<head>` and `<ix:header>` blocks before extraction; `_find_body_document_via_index` fallback added to `get_filing_sections` for the case where primary doc still yields no sections |
| G | Regulated utility FCF screen — utilities (DUK, NEE) have structurally negative FCF due to regulated capex. The `min_fcf_positive_years` check excludes them. V1 limitation — a regulated-utility carve-out requires a separate scoring path. | ✅ Resolved — `sic_code` field added to `XBRLMetrics`; `get_sic()` populates it from EDGAR submissions; `stage01.py` detects SIC 4900-4999 and applies `min_fcf_positive_years_utility` (default 1) instead of the standard 3-year threshold |

---

## Weakness Fixes (Hardening)

Applied after comprehensive codebase audit. Each fix addresses a specific failure mode.

| # | Issue | Fix | Files |
|---|---|---|---|
| 1 | FCF CAGR used Revenue CAGR thresholds (copy-paste bug) | Added dedicated `fcf_cagr_t1..t4_min/pts` to `BusinessQualityCfg`; thresholds calibrated for FCF's higher volatility | `config.py`, `business.py` |
| 2 | Scoring/EA weights not validated to sum to 1.0 | Added `model_validator` to both `ScoringWeightsCfg` and `EntryAttractivenessCfg` — rejects env overrides that break the sum | `config.py` |
| 3 | Yield expansion gamed by dividend cut | Added DPS YoY guard: if DPS drops >20% YoY from XBRL data, yield expansion score is suppressed | `entry_attractiveness.py` |
| 4 | Valuation signal measured quality, not price | Added P/E proxy from `EarningsPerShareBasic` XBRL tag; low P/E = cheap = high score. FCF margin retained as fallback | `entry_attractiveness.py`, `edgar.py`, `types.py` |
| 5 | No retry on MiMo schema validation failure | Added corrective retry: on `ValidationError`, retries once with a prompt containing the error message and original response | `mimo.py` |
| 6 | `transience_argument` allowed empty string | Changed from `Field(default="")` to `Field(min_length=5, max_length=2000)` — forces MiMo to provide at least a brief argument | `schemas.py` |
| 7 | Drawdown scoring breakpoints hardcoded | Moved breakpoints to `EntryAttractivenessCfg` as `drawdown_t1..t7` — configurable via env vars | `config.py`, `entry_attractiveness.py` |
| 8 | `save_opportunity_score` / `save_screen_result` used plain INSERT | Changed to upsert (`_dialect_insert` + `on_conflict_do_update`) — no more duplicate rows on repeated runs | `queries.py` |
| 9 | EDGAR `total_debt` didn't aggregate long-term + short-term | Added `_extract_debt_components()` that sums all available debt tags per fiscal year; `total_debt` now correctly aggregates `LongTermDebtNoncurrent` + `DebtCurrent` + `ShortTermBorrowings` | `edgar.py` |
| 10 | Duplicate `_cagr()` function in `business.py` | Removed the duplicate at end of file | `business.py` |

---

## Beta Test Results

Tested against 15 US blue-chip dividend stocks (KO, JNJ, PG, MMM, ABT, MCD, NEE, DUK, V, ACN, MSFT, VZ, T, AMZN, MDT). Portfolio MYR 500,000. Macro: RANGING / EXPANSION / STABLE / LOOSE.

### Funnel Summary

| Stage | Input | Output | Filter |
|---|---|---|---|
| 0→1 Quality screen | 15 | 11 | 4 rejected (T, AMZN: no dividend/no FCF; NEE, DUK: utility FCF) |
| 1→2 Entry Attractiveness | 11 | 11 | All scored and ranked (top N promoted to KIV by target count) |
| 2→3 MiMo scoring | 5 | 5 | Top 5 scored with dip classification |

### Validated Finalists (MiMo-classified)

| Ticker | Classification | Conf | Score | Mult | MYR |
|---|---|---|---|---|---|
| MSFT | TRANSIENT | 0.85 | 89.0 | 1.2× | 30,000 |
| ACN | CYCLICAL_IDIOSYNCRATIC | 0.70 | 71.1 | 1.0× | 25,000 |
| MCD | CYCLICAL_IDIOSYNCRATIC | 0.65 | 66.5 | 1.0× | 25,000 |
| ABT | CYCLICAL_IDIOSYNCRATIC | 0.75 | 63.7 | 1.0× | 25,000 |
| MDT | STRUCTURAL | 0.75 | 40.9 | 0.6× | 15,000 |

### Known Limitation: MDT STRUCTURAL Classification

Medtronic (MDT) is classified as STRUCTURAL (score 40.9, DQ=10.0) despite being a likely CYCLICAL_IDIOSYNCRATIC case (China hospital spending cuts, product recall). The filing's risk factor section contains heavy trade regulation, tariff, and compliance language that reads as structural. MiMo reads risk factors literally — they are lawyer-written worst-case boilerplate, not forward-looking statements. DQ=10.0 and 0.6× sizing are appropriately defensive given the ambiguity. **Mitigation under investigation:** weight MD&A more heavily than Risk Factors in the MiMo prompt, as MD&A reflects management's actual forward view.

### Parallel Processing Benchmark

Tested with 5 stocks (KO, JNJ, PG, MSFT, ACN) using synthetic filing data, batch_size=2, max_concurrent=2.

| Chunk | Stocks | Wall time | Result |
|---|---|---|---|
| 1 | KO, JNJ | 50s | ✓ 2/2 classified |
| 2 | PG, MSFT | 64s | ✓ 2/2 classified |
| 3 | ACN | 41s | ✓ 1/1 classified |

**Total: 90.5s for 5 stocks = 18.1s/stock** (vs 44.5s sequential = **2.5× speedup**). Gate test passed — MiMo batch API works reliably with parallel chunks.

### Known Limitations

| Category | Limitation | Impact |
|---|---|---|
| MiMo risk factors | Lawyer-written boilerplate reads as structural to LLM | Stocks with heavy regulatory language (MDT, healthcare, defense) may be misclassified as STRUCTURAL |
| yfinance data | No retry, no rate limiting, silent empty returns under load | Price snapshots may be PARTIAL quality; `period="1y"` may return less for recent IPOs |
| NOBL ETF | ProShares CSV format may change; no API contract | Dynamic universe fetch may break without warning; falls back to static list |
| Test coverage | Zero tests for: macro regime, position sizing, persistence, filings, FRED, Bursa, oversold scoring, API endpoints | Integration layer between scoring components is untested |
| Malaysian stocks | Bursa scraper is fragile (V1 limitation); `sector_override_eligible` defaults false for MY | MY stocks cannot use sector peer basket overrides |
