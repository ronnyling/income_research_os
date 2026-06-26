"""Full funnel orchestration script.

Usage:
  python scripts/run_funnel.py [--tickers KO JNJ PG ...] [--portfolio-myr 500000] [--use-db]

Runs:
  1. Startup staleness-triggered refresh (macro, price, filings)
  2. Stage 0->1: EDGAR quality screen for all tickers
  3. Stage 1->2: Dip trigger scan for PROSPECTS
  4. Stage 2->3: KIV context check (macro regime gate)
  5. Scoring: Income + Business + Dip Quality (MiMo 2.5) + Oversold
  6. Position sizing: MYR-base, score-adjusted
  7. Rich output table with all funnel stages and recommendations

Requires MIMO_API_KEY env var for full scoring (Dip Quality dimension).
Add --use-db to persist funnel state and scores to PostgreSQL.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Force UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError for box-drawing chars)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass  # Python < 3.7 fallback

# Ensure src/ is on the path when running from project root
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from incomos.data.edgar import EdgarClient
from incomos.data.market import get_price_snapshot
from incomos.data.fred import get_usd_myr_rate
from incomos.data.filings import FilingsClient
from incomos.data.universe import get_universe, get_universe_count
from incomos.macro.regime import detect_macro_regime
from incomos.core.config import get_settings
from incomos.screening.stage01 import run_quality_screen, ScreenResult
from incomos.funnel.dip_trigger import check_dip_trigger, DipTriggerResult
from incomos.funnel.entry_attractiveness import compute_entry_attractiveness, EntryAttractivenessResult
from incomos.funnel.kiv import evaluate_kiv_entry, evaluate_kiv_demotion
from incomos.scoring.engine import score as compute_score, ScoringResult
from incomos.sizing import compute_position_size
from incomos.core.types import Exchange, StockRecord, XBRLMetrics
from incomos.core.exceptions import (
    FXRateUnavailableError,
    MimoNotConfiguredError,
    MimoAnalysisRequiredError,
    PriceDataUnavailableError,
)

_root = logging.getLogger()
_root.setLevel(logging.DEBUG)
# Console handler: only WARNING+ (keeps Rich output clean)
_ch = logging.StreamHandler()
_ch.setLevel(logging.WARNING)
_ch.setFormatter(logging.Formatter("%(levelname)s  %(name)s  %(message)s"))
_root.addHandler(_ch)
# File handler: DEBUG+ (captures everything for debugging)
_fh = logging.FileHandler("funnel_debug.log", mode="w", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s  %(name)s  %(message)s"))
_root.addHandler(_fh)
logger = logging.getLogger("run_funnel")
console = Console(width=200)

# Default universe: use the expanded dividend stock universe module
# Falls back to a small curated list if --tickers is specified
DEFAULT_UNIVERSE = get_universe("all")


# ---------------------------------------------------------------------------
# Domain-based Stage 0→1 TTL cache
# ---------------------------------------------------------------------------
# Tracks which tickers were last processed per domain (aristocrats, kings,
# quality). On subsequent runs, only domains whose ticker set has changed
# need re-scraping. Stored in a lightweight JSON file alongside the project.

_DOMAIN_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".funnel_domain_cache.json")
_PIPELINE_CHECKPOINT_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".funnel_checkpoint.json")

# Domain cache TTL: re-scrape XBRL data after this many days even if ticker list unchanged.
# EDGAR XBRL facts update on 10-K/10-Q filings (quarterly). 7 days is conservative.
_DOMAIN_CACHE_TTL_DAYS = 7


def _load_domain_cache() -> dict[str, dict]:
    """Load the domain cache from disk. Returns {domain: {tickers: [...], screen_results: {...}, metrics: {}, cached_at: ...}}."""
    if os.path.exists(_DOMAIN_CACHE_FILE):
        try:
            with open(_DOMAIN_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _is_domain_cache_stale(cached: dict) -> bool:
    """Check if domain cache entry is older than TTL."""
    cached_at = cached.get("cached_at")
    if not cached_at:
        return True
    try:
        ts = datetime.fromisoformat(cached_at)
        age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
        return age_days > _DOMAIN_CACHE_TTL_DAYS
    except Exception:
        return True


def _save_pipeline_checkpoint(data: dict) -> None:
    """Save pipeline results checkpoint for HTML generation decoupling."""
    try:
        with open(_PIPELINE_CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
        logger.info("Pipeline checkpoint saved to %s", _PIPELINE_CHECKPOINT_FILE)
    except Exception as exc:
        logger.warning("Failed to save pipeline checkpoint: %s", exc)


def _load_pipeline_checkpoint() -> dict | None:
    """Load pipeline results checkpoint."""
    if not os.path.exists(_PIPELINE_CHECKPOINT_FILE):
        return None
    try:
        with open(_PIPELINE_CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to load pipeline checkpoint: %s", exc)
        return None


def _save_domain_cache(cache: dict[str, dict]) -> None:
    """Save the domain cache to disk."""
    try:
        with open(_DOMAIN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as exc:
        logger.warning("Failed to save domain cache: %s", exc)


def _get_domain_tickers(tickers: list[str]) -> dict[str, list[str]]:
    """Split tickers into domains based on the universe module's tier definitions.

    Returns {domain_name: [tickers]}.
    """
    from incomos.data.universe import get_universe as _gu
    domains: dict[str, list[str]] = {}
    for tier in ("aristocrats", "kings", "quality"):
        tier_tickers = set(_gu(tier))
        domain_set = [t for t in tickers if t in tier_tickers]
        if domain_set:
            domains[tier] = sorted(domain_set)
    # Any tickers not in a named tier go into "other"
    known = set()
    for ts in domains.values():
        known.update(ts)
    other = sorted(set(tickers) - known)
    if other:
        domains["other"] = other
    return domains


# ---------------------------------------------------------------------------
# Parallel fetch helpers
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Thread-safe rate limiter (token bucket)."""
    def __init__(self, requests_per_second: float):
        self._interval = 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


def _fetch_edgar_ticker(ticker: str, edgar: EdgarClient, rate_limiter: _RateLimiter) -> tuple[str, ScreenResult, list]:
    """Fetch EDGAR data and run quality screen for a single ticker. Thread-safe."""
    try:
        rate_limiter.acquire()
        cik = edgar.resolve_cik(ticker)
        if not cik:
            return ticker, ScreenResult(
                ticker=ticker, passed=False,
                checks={}, notes=["CIK not found in EDGAR index"],
            ), []
        rate_limiter.acquire()
        metrics = edgar.get_annual_metrics(ticker, cik, years=5)
        result = run_quality_screen(ticker, metrics)
        return ticker, result, metrics
    except Exception as exc:
        return ticker, ScreenResult(
            ticker=ticker, passed=False,
            checks={}, notes=[f"EDGAR fetch error: {exc}"],
        ), []


def _fetch_price_ticker(ticker: str, metrics_cache: dict) -> tuple[str, EntryAttractivenessResult | None, DipTriggerResult | None, object | None]:
    """Fetch price snapshot and compute EA for a single ticker. Thread-safe."""
    try:
        snap = get_price_snapshot(ticker)
        mets = metrics_cache.get(ticker, [])
        ea = compute_entry_attractiveness(ticker, snap, mets)
        dip = check_dip_trigger(snap)
        return ticker, ea, dip, snap
    except Exception as exc:
        logger.warning("Price/EA fetch failed for %s: %s", ticker, exc)
        return ticker, None, None, None


def _fetch_filing_ticker(ticker: str, edgar: EdgarClient, filings: FilingsClient,
                         macro_ctx: str, rate_limiter: _RateLimiter) -> tuple[str, dict | None]:
    """Fetch filing data for a single ticker. Thread-safe."""
    try:
        rate_limiter.acquire()
        cik = edgar.resolve_cik(ticker)
        if not cik:
            return ticker, None
        rate_limiter.acquire()
        current_secs, prior_secs = filings.get_yoy_sections(ticker, cik)
        mda_sec = current_secs.get("MDAA")
        mda = mda_sec.text if mda_sec else ""
        risk_cur_sec = current_secs.get("RISK_FACTORS")
        risk_current = risk_cur_sec.text if risk_cur_sec else ""
        risk_prior_sec = prior_secs.get("RISK_FACTORS")
        risk_prior = risk_prior_sec.text if risk_prior_sec else None
        if len(mda) + len(risk_current) < 200:
            return ticker, None
        from incomos.llm.mimo import compute_filing_hash
        filing_hash = compute_filing_hash(mda, risk_current, risk_prior)
        return ticker, {
            "ticker": ticker,
            "mda_text": mda,
            "risk_factors_current": risk_current,
            "risk_factors_prior": risk_prior,
            "macro_context": macro_ctx,
            "filing_hash": filing_hash,
        }
    except Exception as exc:
        logger.warning("Filing fetch failed for %s: %s", ticker, exc)
        return ticker, None


def format_pct(val: float | None) -> str:
    return f"{val:.1%}" if val is not None else "-"


def format_float(val: float | None, decimals: int = 1) -> str:
    return f"{val:.{decimals}f}" if val is not None else "-"


def _macro_allows_promotion(macro) -> tuple[bool, str]:
    """Stage 2->3 gate: check if macro regime allows KIV -> Candidate promotion.

    Returns (allowed, reason_if_blocked).
    Block states and confidence threshold are read from config.
    Macro can block promotion but NEVER upgrade a STRUCTURAL dip classification.
    """
    if macro is None:
        return True, ""  # if macro detection failed, do not block (fail-open)

    from incomos.core.config import get_settings
    cfg = get_settings().macro
    block_conf = cfg.block_confidence

    rates_state = str(macro.rates.state).split(".")[-1]
    fin_state = str(macro.financial_conditions.state).split(".")[-1]

    if rates_state in cfg.block_rates_set() and macro.rates.confidence >= block_conf:
        return False, f"Rates={rates_state} (conf={macro.rates.confidence:.2f}) blocks promotion"

    if fin_state in cfg.block_fin_set() and macro.financial_conditions.confidence >= block_conf:
        return False, f"FinCond={fin_state} (conf={macro.financial_conditions.confidence:.2f}) blocks promotion"

    return True, ""


def run_funnel(tickers: list[str], portfolio_myr: float, use_db: bool = False, output_html: str | None = None) -> None:
    start = datetime.now(timezone.utc)

    # DB wiring: create schema on startup if --use-db is set
    if use_db:
        try:
            from incomos.persistence.db import create_schema
            from incomos.persistence.session import db_session
            import incomos.persistence.queries as queries
            create_schema()
            console.print("[dim]PostgreSQL schema ready.[/dim]")
        except Exception as exc:
            console.print(f"[yellow]DB unavailable -- running without persistence: {exc}[/yellow]")
            use_db = False

    console.print()
    db_label = "[green]DB ON[/green]" if use_db else "[dim]no-db[/dim]"
    kiv_target = get_settings().kiv.target_size
    console.print(Panel(
        f"[bold cyan]Income Compounder Research OS[/bold cyan]\n"
        f"Full Funnel Run - {start.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Universe: {len(tickers)} tickers | KIV target: {kiv_target} | "
        f"Portfolio: MYR {portfolio_myr:,.0f} | {db_label}",
        expand=False,
    ))

    # ---- Step 0: Get USD/MYR rate ---------------------------------------
    console.print("\n[bold]Fetching USD/MYR rate (FRED DEXMAUS)...[/bold]")
    usd_myr: float | None = None
    try:
        usd_myr = get_usd_myr_rate()
        console.print(f"  USD/MYR: [green]{usd_myr:.4f}[/green]")
    except FXRateUnavailableError:
        console.print("  [yellow]USD/MYR unavailable (FRED DEXMAUS) -- US stock sizing will be skipped.[/yellow]")

    # ---- Step 1: Macro regime -------------------------------------------
    console.print("\n[bold]Computing macro regime (FRED data)...[/bold]")
    try:
        macro = detect_macro_regime()
        mkt = str(macro.market_structure.state).split(".")[-1]
        gwth = str(macro.growth.state).split(".")[-1]
        rts = str(macro.rates.state).split(".")[-1]
        fin = str(macro.financial_conditions.state).split(".")[-1]
        console.print(f"  Market:   [cyan]{mkt}[/cyan] (confidence {macro.market_structure.confidence:.2f})")
        console.print(f"  Growth:   [cyan]{gwth}[/cyan] (confidence {macro.growth.confidence:.2f})")
        console.print(f"  Rates:    [cyan]{rts}[/cyan] (confidence {macro.rates.confidence:.2f})")
        console.print(f"  FinCond:  [cyan]{fin}[/cyan] (confidence {macro.financial_conditions.confidence:.2f})")
    except Exception as exc:
        console.print(f"  [red]Macro regime detection failed: {exc}[/red]")
        macro = None

    # ---- Step 2: EDGAR quality screen (parallel + domain TTL) -----------
    console.print(f"\n[bold]Stage 0->1: Quality screen ({len(tickers)} tickers)...[/bold]")
    logger.info("Stage 0->1: Quality screen for %d tickers", len(tickers))
    edgar = EdgarClient()

    screen_results: dict[str, ScreenResult] = {}
    metrics_cache: dict[str, list] = {}

    # Domain-based TTL: check which domains need re-scraping
    domain_cache = _load_domain_cache()
    domains = _get_domain_tickers(tickers)
    domains_to_scrape: list[str] = []
    domains_cached: list[str] = []

    for domain, domain_tickers in domains.items():
        cached = domain_cache.get(domain, {})
        cached_tickers = cached.get("tickers", [])
        is_stale = _is_domain_cache_stale(cached)
        if cached_tickers == domain_tickers and not is_stale:
            # Domain unchanged AND fresh — reuse cached screen results and metrics
            domains_cached.append(domain)
            cached_screens = cached.get("screen_results", {})
            cached_metrics = cached.get("metrics", {})
            for t in domain_tickers:
                if t in cached_screens:
                    sr = cached_screens[t]
                    screen_results[t] = ScreenResult(
                        ticker=sr["ticker"], passed=sr["passed"],
                        checks=sr.get("checks", {}), notes=sr.get("notes", []),
                    )
                if t in cached_metrics:
                    metrics_cache[t] = [
                        XBRLMetrics(
                            ticker=t,
                            cik=d.get("cik", ""),
                            fiscal_year=d.get("fiscal_year", d.get("year", 0)),
                            fiscal_period=d.get("fiscal_period", "FY"),
                            revenue=d.get("revenue"),
                            net_income=d.get("net_income"),
                            operating_cash_flow=d.get("operating_cash_flow"),
                            capex=d.get("capex"),
                            dividends_paid=d.get("dividends_paid"),
                            dps_declared=d.get("dps_declared"),
                            dps_paid=d.get("dps_paid"),
                            total_debt=d.get("total_debt"),
                            cash=d.get("cash"),
                            equity=d.get("equity"),
                            earnings_per_share=d.get("earnings_per_share"),
                        )
                        for d in cached_metrics[t]
                    ]
            logger.info("Domain '%s': %d tickers unchanged — reusing cache", domain, len(domain_tickers))
        else:
            domains_to_scrape.append(domain)
            reason = "stale" if is_stale else "ticker list changed"
            added = set(domain_tickers) - set(cached_tickers)
            removed = set(cached_tickers) - set(domain_tickers)
            logger.info("Domain '%s': %s (added=%d, removed=%d) — will scrape %d tickers",
                        domain, reason, len(added), len(removed), len(domain_tickers))

    if domains_cached:
        console.print(f"  [green]Cache hit: {len(domains_cached)} domains ({sum(len(domains[d]) for d in domains_cached)} tickers)[/green]")
    if domains_to_scrape:
        scrape_tickers = [t for d in domains_to_scrape for t in domains[d]]
        console.print(f"  [dim]Scraping: {len(scrape_tickers)} tickers from {len(domains_to_scrape)} domains[/dim]")

        # Parallel EDGAR fetches
        edgar_limiter = _RateLimiter(get_settings().edgar_requests_per_second)
        max_workers = min(get_settings().edgar_requests_per_second * 2, len(scrape_tickers), 10)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_fetch_edgar_ticker, t, edgar, edgar_limiter): t
                for t in scrape_tickers
            }
            done_count = 0
            for future in as_completed(futures):
                done_count += 1
                ticker, result, metrics = future.result()
                screen_results[ticker] = result
                if metrics:
                    metrics_cache[ticker] = metrics
                if done_count % 20 == 0 or done_count == len(scrape_tickers):
                    console.print(f"  [dim]EDGAR progress: {done_count}/{len(scrape_tickers)}[/dim]")

        # Update domain cache with new results
        for domain in domains_to_scrape:
            domain_tickers = domains[domain]
            domain_screens = {}
            for t in domain_tickers:
                sr = screen_results.get(t)
                if sr:
                    domain_screens[t] = {"ticker": sr.ticker, "passed": sr.passed, "checks": sr.checks, "notes": sr.notes}
            domain_metrics = {t: metrics_cache.get(t, []) for t in domain_tickers if t in metrics_cache}
            # Convert metrics to serializable form (list of XBRLMetrics -> list of dicts)
            domain_metrics_ser = {}
            for t, mlist in domain_metrics.items():
                domain_metrics_ser[t] = [
                    {"ticker": m.ticker, "cik": m.cik, "fiscal_year": m.fiscal_year,
                     "fiscal_period": m.fiscal_period, "revenue": m.revenue, "net_income": m.net_income,
                     "operating_cash_flow": m.operating_cash_flow, "capex": m.capex,
                     "dividends_paid": m.dividends_paid, "dps_declared": m.dps_declared,
                     "dps_paid": m.dps_paid, "total_debt": m.total_debt, "cash": m.cash,
                     "equity": m.equity,
                     "earnings_per_share": m.earnings_per_share}
                    for m in mlist
                ] if mlist else []
            domain_cache[domain] = {
                "tickers": domain_tickers,
                "screen_results": domain_screens,
                "metrics": domain_metrics_ser,
                "cached_at": datetime.now(timezone.utc).isoformat(),
            }
        _save_domain_cache(domain_cache)

    # DB persistence (only for newly scraped tickers)
    if use_db:
        for ticker in (t for d in domains_to_scrape for t in domains[d]):
            try:
                cik = edgar.resolve_cik(ticker)
                company_name = ticker
                if cik:
                    try:
                        company_name = edgar.get_company_facts(cik).get("entityName", ticker)
                    except Exception:
                        pass
                with db_session() as conn:
                    if conn is not None:
                        queries.upsert_stock(conn, StockRecord(
                            ticker=ticker, exchange=Exchange.US,
                            company_name=company_name, cik=cik or "",
                        ))
                        sr = screen_results.get(ticker)
                        if sr:
                            queries.save_screen_result(conn, sr)
                            if sr.passed:
                                queries.transition_stage(conn, ticker, "PROSPECTS", "Passed quality screen")
                            else:
                                reason = "; ".join(sr.notes[:2]) if sr.notes else "Failed quality screen"
                                queries.transition_stage(conn, ticker, "REJECTED", reason)
            except Exception:
                pass

    passed_screen = [t for t, r in screen_results.items() if r.passed]
    failed_screen = [t for t, r in screen_results.items() if not r.passed]
    console.print(f"  PASS: [green]{', '.join(passed_screen) or 'none'}[/green]")
    console.print(f"  FAIL: [red]{', '.join(failed_screen) or 'none'}[/red]")
    logger.info("Stage 0->1 complete: %d PASS, %d FAIL", len(passed_screen), len(failed_screen))

    # ---- Step 3: Price data + entry attractiveness scoring ----------------
    cfg = get_settings()
    kiv_target = cfg.kiv.target_size
    ea_cfg = cfg.entry_attractiveness

    console.print(f"\n[bold]Stage 1->2: Entry attractiveness ranking ({len(passed_screen)} PROSPECTS)...[/bold]")
    console.print(f"  [dim]Target KIV size: {kiv_target} | Min floor: {ea_cfg.min_attractiveness_score}[/dim]")
    logger.info("Stage 1->2: EA ranking for %d prospects", len(passed_screen))

    ea_results: dict[str, EntryAttractivenessResult] = {}
    price_cache: dict[str, object] = {}
    dip_results: dict[str, DipTriggerResult] = {}  # Keep for legacy output

    # Parallel price + EA fetches
    max_price_workers = min(10, len(passed_screen))
    with ThreadPoolExecutor(max_workers=max_price_workers) as pool:
        futures = {
            pool.submit(_fetch_price_ticker, t, metrics_cache): t
            for t in passed_screen
        }
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            ticker, ea, dip, snap = future.result()
            if ea is not None:
                ea_results[ticker] = ea
            if dip is not None:
                dip_results[ticker] = dip
            if snap is not None:
                price_cache[ticker] = snap
            if done_count % 20 == 0 or done_count == len(passed_screen):
                console.print(f"  [dim]Price/EA progress: {done_count}/{len(passed_screen)}[/dim]")

    # Rank by attractiveness score, filter by minimum floor
    ranked = sorted(
        [(t, ea) for t, ea in ea_results.items() if ea.meets_floor],
        key=lambda x: x[1].score,
        reverse=True,
    )
    kiv_selected = [t for t, _ in ranked[:kiv_target]]
    below_floor = [t for t, ea in ea_results.items() if not ea.meets_floor]

    console.print(f"  Promoted to KIV: [green]{len(kiv_selected)}[/green] of {len(ea_results)} scored")
    console.print(f"  Below floor ({ea_cfg.min_attractiveness_score}): [dim]{len(below_floor)}[/dim]")
    if len(kiv_selected) < kiv_target:
        console.print(f"  [yellow]Warning: only {len(kiv_selected)} stocks meet floor (target was {kiv_target})[/yellow]")

    # Print top KIV details with tags
    for ticker in kiv_selected[:20]:  # Show top 20
        ea = ea_results[ticker]
        snap = price_cache.get(ticker)
        tags_str = ", ".join(ea.tags) if ea.tags else "none"
        console.print(
            f"    [green]{ticker}[/green]  "
            f"EA={ea.score:.1f}  "
            f"drawdown={ea.component_scores['drawdown']:.0f}  "
            f"yield={ea.component_scores['yield_expansion']:.0f}  "
            f"divgrowth={ea.component_scores['dividend_growth']:.0f}  "
            f"val={ea.component_scores['valuation']:.0f}  "
            f"trend={ea.component_scores['trend']:.0f}  "
            f"tags=[{tags_str}]"
        )
    if len(kiv_selected) > 20:
        console.print(f"    [dim]... and {len(kiv_selected) - 20} more[/dim]")

    if use_db:
        for ticker in kiv_selected:
            ea = ea_results[ticker]
            with db_session() as conn:
                if conn is not None:
                    queries.transition_stage(
                        conn, ticker, "KIV",
                        f"EA={ea.score:.1f} tags={','.join(ea.tags)}"
                    )

    # ---- Step 4: Stage 2->3 gate + scoring -------------------------------
    score_results: dict[str, ScoringResult] = {}
    sizing_results: dict[str, object] = {}
    macro_blocked: list[str] = []
    macro_allowed, macro_block_reason = _macro_allows_promotion(macro)

    if kiv_selected:
        if not macro_allowed:
            console.print(f"\n[bold yellow]Stage 2->3 gate BLOCKED:[/bold yellow] {macro_block_reason}")
            console.print("  [dim]Stocks remain in KIV -- no promotion to Candidate this run.[/dim]")
            macro_blocked = kiv_selected[:]
            # Persist KIV state in DB if enabled
            if use_db:
                for ticker in kiv_selected:
                    with db_session() as conn:
                        if conn is not None:
                            queries.transition_stage(conn, ticker, "KIV", f"Macro gate blocked: {macro_block_reason}")
        else:
            console.print(f"\n[bold]Stage 2->3: Context check + scoring ({len(kiv_selected)} KIV stocks)...[/bold]")
            if macro:
                mkt = str(macro.market_structure.state).split('.')[-1]
                console.print(f"  [dim]Macro gate: {mkt} -- promotion allowed[/dim]")

            # MiMo integration: batch mode with caching
            mimo_enabled = bool(get_settings().mimo_api_key)
            if mimo_enabled:
                console.print("  [dim]MiMo 2.5 configured -- batch classification with caching...[/dim]")
                from incomos.llm.mimo import analyze_dip_batch, compute_filing_hash
                filings_client = FilingsClient()
            else:
                console.print("  [yellow]MIMO_API_KEY not set -- Dip Quality scoring unavailable.[/yellow]")
                console.print("  [yellow]Set MIMO_API_KEY to enable full scoring. KIV stocks will not be promoted.[/yellow]")

            # Phase 1: Gather all filing data in parallel (no MiMo calls yet)
            filing_data: dict[str, dict] = {}  # ticker -> {mda, risk_current, risk_prior, hash}
            if mimo_enabled:
                macro_ctx = f"{str(macro.primary_regime)} growth={str(macro.growth.state).split('.')[-1]}" if macro else ""
                filing_limiter = _RateLimiter(get_settings().edgar_requests_per_second)
                max_filing_workers = min(5, len(kiv_selected))

                with ThreadPoolExecutor(max_workers=max_filing_workers) as pool:
                    futures = {
                        pool.submit(_fetch_filing_ticker, t, edgar, filings_client, macro_ctx, filing_limiter): t
                        for t in kiv_selected
                    }
                    done_count = 0
                    for future in as_completed(futures):
                        done_count += 1
                        ticker, data = future.result()
                        if data is not None:
                            filing_data[ticker] = data
                        else:
                            console.print(f"  [yellow]{ticker}: filing sections empty or fetch failed -- skipping MiMo[/yellow]")
                        if done_count % 10 == 0 or done_count == len(kiv_selected):
                            console.print(f"  [dim]Filing fetch progress: {done_count}/{len(kiv_selected)}[/dim]")

                console.print(f"  [dim]Filing data gathered for {len(filing_data)}/{len(kiv_selected)} stocks[/dim]")
                logger.info("Filing data gathered for %d/%d stocks", len(filing_data), len(kiv_selected))

            # Phase 2: Check cache + batch MiMo (only if DB enabled)
            mimo_results: dict[str, dict] = {}  # ticker -> validated result dict

            if mimo_enabled and filing_data:
                # Check cache first
                uncached_tickers: list[str] = list(filing_data.keys())

                if use_db:
                    ticker_hashes = {t: d["filing_hash"] for t, d in filing_data.items()}
                    with db_session() as conn:
                        if conn is not None:
                            cached, uncached_tickers = queries.get_cached_mimo_batch(conn, ticker_hashes)
                            mimo_results.update(cached)
                            if cached:
                                console.print(f"  [green]Cache hit: {len(cached)} stocks[/green]")
                            if uncached_tickers:
                                console.print(f"  [dim]Cache miss: {len(uncached_tickers)} stocks need classification[/dim]")

                # Batch classify uncached stocks
                if uncached_tickers:
                    batch_data = [filing_data[t] for t in uncached_tickers if t in filing_data]
                    console.print(f"  [dim]Batch MiMo: {len(batch_data)} stocks in {get_settings().mimo_batch.batch_size}-stock chunks...[/dim]")
                    logger.info("Starting MiMo batch classification: %d stocks, batch_size=%d", len(batch_data), get_settings().mimo_batch.batch_size)
                    try:
                        batch_results = analyze_dip_batch(batch_data)
                        logger.info("MiMo batch complete: %d stocks classified", len(batch_results))
                        for ticker, result in batch_results.items():
                            mimo_results[ticker] = result.model_dump()
                            console.print(f"  [dim]{ticker}: {result.classification} (conf={result.confidence:.2f})[/dim]")
                    except Exception as exc:
                        console.print(f"  [red]Batch MiMo failed: {exc}[/red]")
                        logger.error("Batch MiMo failed: %s", exc, exc_info=True)

                    # Cache the new results
                    if use_db and batch_results:
                        ttl = get_settings().mimo_batch.ttl_days
                        with db_session() as conn:
                            if conn is not None:
                                for ticker, result in batch_results.items():
                                    if ticker in filing_data:
                                        queries.save_mimo_classification(
                                            conn, ticker, result.model_dump(),
                                            filing_data[ticker]["filing_hash"], ttl,
                                        )
                                conn.commit()
                        console.print(f"  [dim]Cached {len(batch_results)} classifications (TTL={ttl}d)[/dim]")

            # Phase 3: Score all stocks with MiMo results
            for ticker in kiv_selected:
                try:
                    snap = price_cache.get(ticker)
                    mets = metrics_cache.get(ticker, [])
                    mimo_result = mimo_results.get(ticker)

                    if mimo_result is None:
                        console.print(f"  [dim]{ticker}: skipping score (no MiMo result)[/dim]")
                        continue

                    result = compute_score(ticker, mets, snap, mimo_result)
                    score_results[ticker] = result

                    sizing = compute_position_size(
                        result=result,
                        portfolio_size_myr=portfolio_myr,
                        exchange="US",
                        usd_myr_rate=usd_myr,
                    )
                    sizing_results[ticker] = sizing

                    # Persist to DB if enabled
                    if use_db:
                        with db_session() as conn:
                            if conn is not None:
                                queries.save_opportunity_score(conn, ticker, {
                                    "composite": result.composite,
                                    "income_quality": result.income_quality,
                                    "business_quality": result.business_quality,
                                    "dip_quality": result.dip_quality,
                                    "oversold_confidence": result.oversold_confidence,
                                    "base_size_multiplier": result.base_size_multiplier,
                                })
                                stage = "FINALIST" if result.composite >= 70 else "CANDIDATE"
                                queries.transition_stage(conn, ticker, stage,
                                    f"Score={result.composite:.1f} composite")

                except Exception as exc:
                    console.print(f"  [red]{ticker} scoring failed: {exc}[/red]")

    # ---- Step 5: Output summary table -----------------------------------
    logger.info("Scoring complete: %d stocks scored, %d sized", len(score_results), len(sizing_results))
    console.print()
    _print_summary_table(
        tickers, screen_results, ea_results, score_results, sizing_results, usd_myr
    )

    # ---- Step 6: Below-floor stocks in prospects ------------------------
    if below_floor:
        console.print(f"\n[dim]Stocks passing quality screen but below attractiveness floor ({ea_cfg.min_attractiveness_score}):[/dim]")
        for ticker in below_floor[:20]:
            ea = ea_results.get(ticker)
            if ea:
                snap = price_cache.get(ticker)
                tags_str = ", ".join(ea.tags) if ea.tags else "none"
                console.print(
                    f"  [dim]{ticker}: EA={ea.score:.1f} tags=[{tags_str}][/dim]"
                )
        if len(below_floor) > 20:
            console.print(f"  [dim]... and {len(below_floor) - 20} more[/dim]")

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    console.print(f"\n[dim]Run completed in {elapsed:.1f}s[/dim]")
    console.print()

    # ---- Save pipeline checkpoint (always) --------------------------------
    # This allows HTML regeneration without re-running the full pipeline.
    # MiMo results are the expensive part — checkpoint them immediately.
    checkpoint_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tickers": tickers,
        "portfolio_myr": portfolio_myr,
        "kiv_target": kiv_target,
        "usd_myr": usd_myr,
        "elapsed": elapsed,
        "macro": {
            "market": str(macro.primary_regime) if macro else None,
            "growth": str(macro.growth.state).split(".")[-1] if macro else None,
            "rates": str(macro.rates.state).split(".")[-1] if macro else None,
            "fincond": str(macro.financial_conditions.state).split(".")[-1] if macro else None,
        } if macro else None,
        "screen_results": {t: dataclasses.asdict(sr) for t, sr in screen_results.items()},
        "ea_results": {t: dataclasses.asdict(ea) for t, ea in ea_results.items()},
        "score_results": {t: dataclasses.asdict(sc) for t, sc in score_results.items()},
        "sizing_results": {t: dataclasses.asdict(sz) for t, sz in sizing_results.items()},
        "mimo_results": mimo_results,
        "price_cache": {t: dataclasses.asdict(p) for t, p in price_cache.items()},
        "below_floor": below_floor,
    }
    _save_pipeline_checkpoint(checkpoint_data)

    # Also save MiMo results to a human-readable text file
    mimo_txt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mimo_results.txt")
    try:
        with open(mimo_txt_path, "w", encoding="utf-8") as f:
            f.write(f"MiMo 2.5 Batch Classification Results\n")
            f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"{'=' * 60}\n\n")
            for ticker, mr in sorted(mimo_results.items()):
                cls = mr.get('classification', '?')
                conf = mr.get('confidence', 0)
                reason = mr.get('reasoning', '')[:120]
                f.write(f"{ticker:8s}  {cls:25s}  conf={conf:.2f}  {reason}\n")
        console.print(f"[dim]MiMo results saved to {mimo_txt_path}[/dim]")
    except Exception as exc:
        logger.warning("Failed to save MiMo results text: %s", exc)

    # ---- HTML report output -------------------------------------------
    if output_html:
        _generate_html_report(
            output_html, tickers, screen_results, ea_results,
            score_results, sizing_results, price_cache, mimo_results,
            usd_myr, macro, portfolio_myr, kiv_target, start, elapsed,
            below_floor,
        )
        console.print(f"[green]HTML report written to {output_html}[/green]")

    # ---- Note on status -----------------------------------------
    gap_lines = []
    from incomos.core.config import get_settings as _gs
    if not _gs().mimo_api_key:
        gap_lines.append(
            "[bold yellow]Action needed:[/bold yellow] Set MIMO_API_KEY to enable Dip Quality scoring.\n"
            "  Without it, triggered KIV stocks cannot be scored or promoted to Candidate."
        )
    if not use_db:
        gap_lines.append(
            "[bold yellow]No-DB mode:[/bold yellow] Add [cyan]--use-db[/cyan] to persist "
            "funnel state to PostgreSQL."
        )
    if gap_lines:
        console.print(Panel("\n".join(gap_lines), title="Status", expand=False))


def _print_summary_table(
    tickers, screen_results, ea_results, score_results, sizing_results, usd_myr
):
    t = Table(
        title="Funnel Summary",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold",
    )
    t.add_column("Ticker", style="bold", width=8)
    t.add_column("Stage", width=12)
    t.add_column("EA", justify="right", width=6)
    t.add_column("Dip%", justify="right", width=7)
    t.add_column("RSI", justify="right", width=6)
    t.add_column("Tags", width=24)
    t.add_column("IQ", justify="right", width=6)
    t.add_column("BQ", justify="right", width=6)
    t.add_column("DQ*", justify="right", width=6)
    t.add_column("OC", justify="right", width=6)
    t.add_column("Score", justify="right", width=7)
    t.add_column("Mult", justify="right", width=5)
    t.add_column("Size (MYR)", justify="right", width=12)

    for ticker in tickers:
        sr = screen_results.get(ticker)
        ea = ea_results.get(ticker)
        sc = score_results.get(ticker)
        sz = sizing_results.get(ticker)

        if not sr:
            t.add_row(ticker, "ERROR", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "No data")
            continue

        if not sr.passed:
            note = "; ".join(sr.notes[:2]) if sr.notes else ""
            t.add_row(
                ticker, "[red]REJECTED[/red]",
                "-", "-", "-", "-", "-", "-", "-", "-", "-", "-",
                f"[dim]{note[:30]}[/dim]"
            )
            continue

        # Passed screen — determine stage
        if ea is None:
            stage = "[blue]PROSPECTS[/blue]"
            ea_str = "-"
            dip_pct = "-"
            rsi_str = "-"
            tags_str = "-"
        elif ea.meets_floor:
            stage = "[yellow]KIV[/yellow]"
            ea_str = f"[bold]{ea.score:.0f}[/bold]"
            # Get dip% and RSI from price cache if available
            dip_pct = "-"
            rsi_str = "-"
            tags_str = ", ".join(ea.tags[:3]) if ea.tags else "none"
        else:
            stage = "[dim]PROSPECTS[/dim]"
            ea_str = f"[dim]{ea.score:.0f}[/dim]"
            dip_pct = "-"
            rsi_str = "-"
            tags_str = ", ".join(ea.tags[:3]) if ea.tags else "none"

        if sc:
            iq = f"{sc.income_quality:.1f}"
            bq = f"{sc.business_quality:.1f}"
            dq = f"{sc.dip_quality:.1f}"
            oc = f"{sc.oversold_confidence:.1f}"
            composite = f"[bold]{sc.composite:.1f}[/bold]"
            mult = f"{sc.base_size_multiplier:.1f}x"
            if sz and sz.adjusted_position_myr > 0:
                size_str = f"MYR {sz.adjusted_position_myr:,.0f}"
            else:
                size_str = "-"
        else:
            iq = bq = dq = oc = composite = mult = size_str = "-"

        # Color code score
        if sc and sc.composite >= 70:
            composite = f"[green]{sc.composite:.1f}[/green]"
        elif sc and sc.composite >= 60:
            composite = f"[yellow]{sc.composite:.1f}[/yellow]"
        elif sc:
            composite = f"[dim]{sc.composite:.1f}[/dim]"

        t.add_row(ticker, stage, ea_str, dip_pct, rsi_str, tags_str,
                  iq, bq, dq, oc, composite, mult, size_str)

    console.print(t)
    console.print("[dim]EA = Entry Attractiveness (0-100) | DQ = Dip Quality (MiMo 2.5)[/dim]")


def _generate_html_report(
    output_path: str,
    tickers: list[str],
    screen_results: dict,
    ea_results: dict,
    score_results: dict,
    sizing_results: dict,
    price_cache: dict,
    mimo_results: dict,
    usd_myr: float | None,
    macro,
    portfolio_myr: float,
    kiv_target: int,
    start: datetime,
    elapsed: float,
    below_floor: list[str],
) -> None:
    """Generate a self-contained HTML report for GitHub Pages."""
    import os

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Build rows
    kiv_rows = []
    prospect_rows = []
    rejected_rows = []

    for ticker in tickers:
        sr = screen_results.get(ticker)
        ea = ea_results.get(ticker)
        sc = score_results.get(ticker)
        sz = sizing_results.get(ticker)
        snap = price_cache.get(ticker)
        mimo = mimo_results.get(ticker)

        if not sr:
            rejected_rows.append((ticker, "ERROR", "No data"))
            continue

        if not sr.passed:
            note = "; ".join(sr.notes[:2]) if sr.notes else "Quality screen failed"
            rejected_rows.append((ticker, "REJECTED", note))
            continue

        # Passed screen
        ea_score = f"{ea.score:.0f}" if ea else "-"
        tags = ", ".join(ea.tags[:3]) if ea and ea.tags else "-"
        dip_pct = "-"
        rsi_val = "-"
        if snap:
            try:
                dip_pct = f"{snap.drawdown_pct:.1f}%" if hasattr(snap, 'drawdown_pct') and snap.drawdown_pct else "-"
                rsi_val = f"{snap.rsi:.1f}" if hasattr(snap, 'rsi') and snap.rsi else "-"
            except Exception:
                pass

        if sc:
            iq = f"{sc.income_quality:.1f}"
            bq = f"{sc.business_quality:.1f}"
            dq = f"{sc.dip_quality:.1f}"
            oc = f"{sc.oversold_confidence:.1f}"
            comp = f"{sc.composite:.1f}"
            mult = f"{sc.base_size_multiplier:.1f}x"
            size = f"MYR {sz.adjusted_position_myr:,.0f}" if sz and hasattr(sz, 'adjusted_position_myr') and sz.adjusted_position_myr > 0 else "-"
            mimo_class = mimo.get("classification", "-") if mimo else "-"
            mimo_conf = f"{mimo.get('confidence', 0):.2f}" if mimo else "-"
        else:
            iq = bq = dq = oc = comp = mult = size = mimo_class = mimo_conf = "-"

        if ea and ea.meets_floor and sc:
            kiv_rows.append((ticker, ea_score, dip_pct, rsi_val, tags, mimo_class, mimo_conf, iq, bq, dq, oc, comp, mult, size))
        elif ea:
            prospect_rows.append((ticker, ea_score, dip_pct, rsi_val, tags))
        else:
            prospect_rows.append((ticker, "-", "-", "-", "-"))

    # Sort KIV by composite score descending (index 11 = comp)
    kiv_rows.sort(key=lambda r: float(r[11]) if r[11] != "-" else 0, reverse=True)

    # Macro regime
    macro_market = str(macro.market_structure.state).split(".")[-1] if macro else "-"
    macro_growth = str(macro.growth.state).split(".")[-1] if macro else "-"
    macro_rates = str(macro.rates.state).split(".")[-1] if macro else "-"
    macro_fin = str(macro.financial_conditions.state).split(".")[-1] if macro else "-"

    date_str = start.strftime("%Y-%m-%d %H:%M UTC")
    total_kiv = len(kiv_rows)
    total_prospects = len(prospect_rows)
    total_rejected = len(rejected_rows)

    # Score distribution (comp is index 11)
    scored = [r for r in kiv_rows if r[11] != "-"]
    avg_score = sum(float(r[11]) for r in scored) / len(scored) if scored else 0
    usd_myr_str = f"{usd_myr:.4f}" if usd_myr else "-"

    # Build HTML
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Income Compounder Research OS — Beta Results</title>
<style>
  :root {{
    --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #e6edf3;
    --muted: #8b949e; --accent: #58a6ff; --green: #3fb950; --yellow: #d29922;
    --red: #f85149; --purple: #bc8cff;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
         background: var(--bg); color: var(--text); padding: 2rem; line-height: 1.5; }}
  .container {{ max-width: 1400px; margin: 0 auto; }}
  h1 {{ font-size: 1.8rem; margin-bottom: 0.3rem; }}
  h2 {{ font-size: 1.3rem; margin: 2rem 0 0.8rem; color: var(--accent); border-bottom: 1px solid var(--border); padding-bottom: 0.4rem; }}
  .subtitle {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1.5rem; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1rem; }}
  .card .label {{ color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }}
  .card .value {{ font-size: 1.6rem; font-weight: 700; margin-top: 0.3rem; }}
  .card .value.green {{ color: var(--green); }}
  .card .value.yellow {{ color: var(--yellow); }}
  .card .value.blue {{ color: var(--accent); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-bottom: 1rem; }}
  th {{ background: var(--card); color: var(--muted); text-transform: uppercase; font-size: 0.7rem;
       letter-spacing: 0.05em; padding: 0.6rem 0.8rem; text-align: left; border-bottom: 2px solid var(--border);
       position: sticky; top: 0; }}
  td {{ padding: 0.5rem 0.8rem; border-bottom: 1px solid var(--border); }}
  tr:hover td {{ background: rgba(88,166,255,0.04); }}
  .score-high {{ color: var(--green); font-weight: 700; }}
  .score-mid {{ color: var(--yellow); font-weight: 600; }}
  .score-low {{ color: var(--muted); }}
  .tag {{ display: inline-block; background: rgba(88,166,255,0.12); color: var(--accent);
          border-radius: 4px; padding: 0.1rem 0.4rem; font-size: 0.7rem; margin: 0.1rem; }}
  .rejected-note {{ color: var(--muted); font-size: 0.8rem; }}
  .footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--border);
             color: var(--muted); font-size: 0.75rem; text-align: center; }}
  .macro-pills {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .pill {{ background: var(--card); border: 1px solid var(--border); border-radius: 20px;
           padding: 0.3rem 0.8rem; font-size: 0.8rem; }}
  .pill .axis {{ color: var(--muted); font-size: 0.7rem; }}
</style>
</head>
<body>
<div class="container">

<h1>📊 Income Compounder Research OS</h1>
<p class="subtitle">Full Universe Beta Results — {date_str} &middot; Portfolio MYR {portfolio_myr:,.0f} &middot; {elapsed:.0f}s</p>

<div class="cards">
  <div class="card"><div class="label">Universe</div><div class="value blue">{len(tickers)}</div></div>
  <div class="card"><div class="label">Prospects</div><div class="value">{total_prospects}</div></div>
  <div class="card"><div class="label">KIV Basket</div><div class="value yellow">{total_kiv} / {kiv_target}</div></div>
  <div class="card"><div class="label">Rejected</div><div class="value" style="color:var(--red)">{total_rejected}</div></div>
  <div class="card"><div class="label">Avg Score</div><div class="value green">{avg_score:.1f}</div></div>
  <div class="card"><div class="label">USD/MYR</div><div class="value">{usd_myr_str}</div></div>
</div>

<h2>🌍 Macro Regime</h2>
<div class="macro-pills">
  <div class="pill"><span class="axis">Market</span> {macro_market}</div>
  <div class="pill"><span class="axis">Growth</span> {macro_growth}</div>
  <div class="pill"><span class="axis">Rates</span> {macro_rates}</div>
  <div class="pill"><span class="axis">FinCond</span> {macro_fin}</div>
</div>

<h2>🎯 KIV Basket ({total_kiv} stocks)</h2>
<table>
<thead><tr>
  <th>#</th><th>Ticker</th><th>EA</th><th>Dip%</th><th>RSI</th><th>Tags</th>
  <th>MiMo Class</th><th>Conf</th><th>IQ</th><th>BQ</th><th>DQ</th><th>OC</th>
  <th>Score</th><th>Mult</th><th>Size (MYR)</th>
</tr></thead>
<tbody>
"""

    for i, row in enumerate(kiv_rows, 1):
        ticker, ea_s, dip, rsi, tags, mm_class, mm_conf, iq, bq, dq, oc, comp, mult, size = row
        score_val = float(comp) if comp != "-" else 0
        score_cls = "score-high" if score_val >= 70 else ("score-mid" if score_val >= 60 else "score-low")
        tags_html = "".join(f'<span class="tag">{t.strip()}</span>' for t in tags.split(",") if t.strip() != "-")
        html += f"""<tr>
  <td>{i}</td><td><strong>{ticker}</strong></td><td>{ea_s}</td><td>{dip}</td><td>{rsi}</td>
  <td>{tags_html if tags_html else '-'}</td>
  <td>{mm_class}</td><td>{mm_conf}</td><td>{iq}</td><td>{bq}</td><td>{dq}</td><td>{oc}</td>
  <td class="{score_cls}">{comp}</td><td>{mult}</td><td><strong>{size}</strong></td>
</tr>
"""

    html += """</tbody></table>

<h2>📋 Prospects (below KIV threshold)</h2>
<table>
<thead><tr><th>Ticker</th><th>EA Score</th><th>Dip%</th><th>RSI</th><th>Tags</th></tr></thead>
<tbody>
"""

    for row in prospect_rows:
        ticker, ea_s, dip, rsi, tags = row
        tags_html = "".join(f'<span class="tag">{t.strip()}</span>' for t in tags.split(",") if t.strip() != "-")
        html += f"<tr><td>{ticker}</td><td>{ea_s}</td><td>{dip}</td><td>{rsi}</td><td>{tags_html if tags_html else '-'}</td></tr>\n"

    html += """</tbody></table>

<h2>❌ Rejected</h2>
<table>
<thead><tr><th>Ticker</th><th>Status</th><th>Reason</th></tr></thead>
<tbody>
"""

    for ticker, status, note in rejected_rows:
        html += f'<tr><td>{ticker}</td><td style="color:var(--red)">{status}</td><td class="rejected-note">{note}</td></tr>\n'

    html += f"""</tbody></table>

<div class="footer">
  Generated by Income Compounder Research OS &middot; {date_str}<br>
  Architecture: 5-stage funnel &middot; Rule-based first, MiMo 2.5 fallback &middot; MYR base currency
</div>

</div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("HTML report written to %s (%d bytes)", output_path, len(html))


def main():
    parser = argparse.ArgumentParser(description="Income Compounder Research OS — Full Funnel")
    parser.add_argument(
        "--tickers", nargs="+", default=None,
        help="List of ticker symbols to screen (overrides --tier)",
    )
    parser.add_argument(
        "--tier", default="all", choices=["aristocrats", "kings", "quality", "nobl", "all"],
        help="Universe tier to screen (default: all). 'nobl' fetches live NOBL ETF holdings.",
    )
    parser.add_argument(
        "--portfolio-myr", type=float, default=500_000,
        help="Portfolio size in MYR for position sizing",
    )
    parser.add_argument(
        "--kiv-target", type=int, default=None,
        help="Override KIV target size (default: from config, currently 20)",
    )
    parser.add_argument(
        "--max-concurrent", type=int, default=None,
        help="Max parallel MiMo API calls (default: from config, currently 2)",
    )
    parser.add_argument(
        "--use-db", action="store_true", default=False,
        help="Persist funnel state and scores to PostgreSQL (requires DB running)",
    )
    parser.add_argument(
        "--output-html", type=str, default=None,
        help="Generate HTML report at this path (e.g. docs/index.html for GitHub Pages)",
    )
    parser.add_argument(
        "--from-checkpoint", action="store_true", default=False,
        help="Generate HTML from last pipeline checkpoint (skip full pipeline run)",
    )
    args = parser.parse_args()

    # ---- Fast path: generate HTML from checkpoint ----------------------
    if args.from_checkpoint:
        if not args.output_html:
            print("ERROR: --from-checkpoint requires --output-html")
            sys.exit(1)
        ckpt = _load_pipeline_checkpoint()
        if not ckpt:
            print(f"ERROR: No checkpoint found at {_PIPELINE_CHECKPOINT_FILE}")
            sys.exit(1)
        print(f"Loaded checkpoint from {ckpt.get('timestamp', '?')}")
        # Reconstruct dataclass objects from checkpoint dicts
        from incomos.core.types import ScreenResult as _SR
        from incomos.funnel.entry_attractiveness import EntryAttractivenessResult as _EAR
        from incomos.scoring.engine import ScoringResult as _ScR
        from incomos.sizing import PositionSizing as _PS
        from incomos.data.market import PriceSnapshot as _PSnap

        def _reconstruct(cls, d):
            """Reconstruct a dataclass from a dict, ignoring unknown keys."""
            import dataclasses as _dc
            field_names = {f.name for f in _dc.fields(cls)}
            filtered = {k: v for k, v in d.items() if k in field_names}
            return cls(**filtered)

        _screen = {t: _reconstruct(_SR, d) for t, d in ckpt.get("screen_results", {}).items()}
        _ea = {t: _reconstruct(_EAR, d) for t, d in ckpt.get("ea_results", {}).items()}
        _scores = {t: _reconstruct(_ScR, d) for t, d in ckpt.get("score_results", {}).items()}
        _sizing = {t: _reconstruct(_PS, d) for t, d in ckpt.get("sizing_results", {}).items()}
        _prices = {}
        for t, d in ckpt.get("price_cache", {}).items():
            try:
                _prices[t] = _reconstruct(_PSnap, d)
            except Exception:
                pass
        _start = datetime.fromisoformat(ckpt["timestamp"]) if "timestamp" in ckpt else datetime.now(timezone.utc)
        _elapsed = ckpt.get("elapsed", 0)

        class _Macro:
            class _Axis:
                def __init__(self, s): self.state = s
                def __str__(self): return str(self.state)
            def __init__(self, d):
                self.primary_regime = self._Axis(d.get("market", ""))
                self.growth = self._Axis(d.get("growth", ""))
                self.rates = self._Axis(d.get("rates", ""))
                self.financial_conditions = self._Axis(d.get("fincond", ""))

        _macro = _Macro(ckpt["macro"]) if ckpt.get("macro") else None

        _generate_html_report(
            args.output_html, ckpt["tickers"], _screen, _ea,
            _scores, _sizing, _prices, ckpt.get("mimo_results", {}),
            ckpt.get("usd_myr"), _macro, ckpt["portfolio_myr"],
            ckpt["kiv_target"], _start, _elapsed, ckpt.get("below_floor", []),
        )
        print(f"HTML report written to {args.output_html}")
        return

    # ---- Normal path: full pipeline -----------------------------------
    # Apply CLI overrides to config
    if args.kiv_target is not None:
        get_settings().kiv.target_size = args.kiv_target
    if args.max_concurrent is not None:
        get_settings().mimo_batch.max_concurrent_batches = args.max_concurrent

    tickers = [t.upper() for t in args.tickers] if args.tickers else get_universe(args.tier)
    run_funnel(
        tickers=tickers,
        portfolio_myr=args.portfolio_myr,
        use_db=args.use_db,
        output_html=args.output_html,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Fatal error in run_funnel")
        raise
