"""Full funnel orchestration script.

Usage:
  python scripts/run_funnel.py [--tickers KO JNJ PG ...] [--portfolio-myr 500000] [--use-db]

Runs:
  1. Startup staleness-triggered refresh (macro, price, filings)
  2. Stage 0→1: EDGAR quality screen for all tickers
  3. Stage 1→2: Dip trigger scan for PROSPECTS
  4. Stage 2→3: KIV context check (macro regime gate)
  5. Scoring: Income + Business + Oversold (Dip Quality stub until MiMo configured)
  6. Position sizing: MYR-base, score-adjusted
  7. Rich output table with all funnel stages and recommendations

Add --use-db to persist funnel state and scores to PostgreSQL.
"""

from __future__ import annotations

import argparse
import logging
import sys
import os

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
from incomos.macro.regime import detect_macro_regime
from incomos.screening.stage01 import run_quality_screen, ScreenResult
from incomos.funnel.dip_trigger import check_dip_trigger, DipTriggerResult
from incomos.funnel.kiv import evaluate_kiv_entry, evaluate_kiv_demotion
from incomos.scoring.engine import score as compute_score, ScoringResult
from incomos.sizing import compute_position_size

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger("run_funnel")
console = Console()

# Default income stock universe (US-listed dividend payers / candidates)
DEFAULT_UNIVERSE = [
    "KO",    # Coca-Cola — Dividend King
    "JNJ",   # Johnson & Johnson
    "PG",    # Procter & Gamble
    "MMM",   # 3M — been in prolonged dip
    "ABT",   # Abbott Laboratories
    "MCD",   # McDonald's
    "NEE",   # NextEra Energy — utility under rate pressure
    "DUK",   # Duke Energy
    "V",     # Visa — strong FCF, growing dividend
    "ACN",   # Accenture — reference archetype
    "MSFT",  # Microsoft
    "VZ",    # Verizon
    "T",     # AT&T — expected FAIL
    "AMZN",  # Amazon — expected FAIL (no dividend)
    "MDT",   # Medtronic
]


def format_pct(val: float | None) -> str:
    return f"{val:.1%}" if val is not None else "—"


def format_float(val: float | None, decimals: int = 1) -> str:
    return f"{val:.{decimals}f}" if val is not None else "—"


def _macro_allows_promotion(macro) -> tuple[bool, str]:
    """Stage 2→3 gate: check if macro regime allows KIV → Candidate promotion.

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


def run_funnel(tickers: list[str], portfolio_myr: float, use_db: bool = False) -> None:
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
            console.print(f"[yellow]DB unavailable — running without persistence: {exc}[/yellow]")
            use_db = False

    console.print()
    db_label = "[green]DB ON[/green]" if use_db else "[dim]no-db[/dim]"
    console.print(Panel(
        f"[bold cyan]Income Compounder Research OS[/bold cyan]\n"
        f"Full Funnel Run — {start.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Universe: {len(tickers)} tickers | Portfolio: MYR {portfolio_myr:,.0f} | {db_label}",
        expand=False,
    ))

    # ---- Step 0: Get USD/MYR rate ---------------------------------------
    console.print("\n[bold]Fetching USD/MYR rate (FRED DEXMAUS)...[/bold]")
    usd_myr = get_usd_myr_rate()
    if usd_myr:
        console.print(f"  USD/MYR: [green]{usd_myr:.4f}[/green]")
    else:
        console.print("  [yellow]USD/MYR unavailable — US stock sizing will be skipped.[/yellow]")

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

    # ---- Step 2: EDGAR quality screen -----------------------------------
    console.print(f"\n[bold]Stage 0→1: Quality screen ({len(tickers)} tickers)...[/bold]")
    edgar = EdgarClient()

    screen_results: dict[str, ScreenResult] = {}
    metrics_cache: dict[str, list] = {}

    for ticker in tickers:
        try:
            cik = edgar.resolve_cik(ticker)
            if not cik:
                screen_results[ticker] = ScreenResult(
                    ticker=ticker, passed=False,
                    checks={}, notes=[f"CIK not found in EDGAR index"],
                )
                continue
            metrics = edgar.get_annual_metrics(ticker, cik, years=5)
            metrics_cache[ticker] = metrics
            result = run_quality_screen(ticker, metrics)
            screen_results[ticker] = result
        except Exception as exc:
            screen_results[ticker] = ScreenResult(
                ticker=ticker, passed=False,
                checks={}, notes=[f"EDGAR fetch error: {exc}"],
            )

    passed_screen = [t for t, r in screen_results.items() if r.passed]
    failed_screen = [t for t, r in screen_results.items() if not r.passed]
    console.print(f"  PASS: [green]{', '.join(passed_screen) or 'none'}[/green]")
    console.print(f"  FAIL: [red]{', '.join(failed_screen) or 'none'}[/red]")

    # ---- Step 3: Price data + dip trigger -------------------------------
    console.print(f"\n[bold]Stage 1→2: Dip trigger screen ({len(passed_screen)} PROSPECTS)...[/bold]")
    dip_results: dict[str, DipTriggerResult] = {}
    price_cache: dict[str, object] = {}

    for ticker in passed_screen:
        try:
            snap = get_price_snapshot(ticker)
            price_cache[ticker] = snap
            dip = check_dip_trigger(snap)
            dip_results[ticker] = dip
        except Exception as exc:
            console.print(f"  [yellow]{ticker}: price fetch failed — {exc}[/yellow]")

    triggered = [t for t, d in dip_results.items() if d.triggered]
    not_triggered = [t for t in passed_screen if t not in triggered]
    console.print(f"  Triggered (→ KIV): [yellow]{', '.join(triggered) or 'none'}[/yellow]")

    # Print dip details for triggered stocks
    for ticker in triggered:
        d = dip_results[ticker]
        snap = price_cache[ticker]
        console.print(
            f"    [yellow]{ticker}[/yellow]  "
            f"pct_below={d.pct_below_52w_high:.1%}  "
            f"RSI={format_float(d.rsi)}  "
            f"vol_ratio={format_float(d.volume_ratio, 2)}  "
            f"[{d.trigger_strength}]"
        )

    console.print(f"  Not triggered: {', '.join(not_triggered) or 'none'}")

    # ---- Step 4: Stage 2→3 gate + scoring -------------------------------
    score_results: dict[str, ScoringResult] = {}
    sizing_results: dict[str, object] = {}
    macro_blocked: list[str] = []
    macro_allowed, macro_block_reason = _macro_allows_promotion(macro)

    if triggered:
        if not macro_allowed:
            console.print(f"\n[bold yellow]Stage 2→3 gate BLOCKED:[/bold yellow] {macro_block_reason}")
            console.print("  [dim]Stocks remain in KIV — no promotion to Candidate this run.[/dim]")
            macro_blocked = triggered[:]
            # Persist KIV state in DB if enabled
            if use_db:
                for ticker in triggered:
                    with db_session() as conn:
                        if conn is not None:
                            queries.transition_stage(conn, ticker, "KIV", f"Macro gate blocked: {macro_block_reason}")
        else:
            console.print(f"\n[bold]Stage 2→3: Context check + scoring ({len(triggered)} KIV stocks)...[/bold]")
            if macro:
                mkt = str(macro.market_structure.state).split('.')[-1]
                console.print(f"  [dim]Macro gate: {mkt} — promotion allowed[/dim]")

            for ticker in triggered:
                try:
                    snap = price_cache.get(ticker)
                    mets = metrics_cache.get(ticker, [])
                    result = compute_score(ticker, mets, price_snap=snap)
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
    console.print()
    _print_summary_table(
        tickers, screen_results, dip_results, score_results, sizing_results, usd_myr
    )

    # ---- Step 6: Not-triggered stocks in prospects ----------------------
    if not_triggered:
        console.print("\n[dim]Stocks passing quality screen but NOT in dip:[/dim]")
        for ticker in not_triggered:
            if ticker in dip_results:
                d = dip_results[ticker]
                snap = price_cache.get(ticker)
                rsi_str = f"RSI={snap.rsi_14:.1f}" if snap and snap.rsi_14 else "RSI=—"
                console.print(
                    f"  [dim]{ticker}: {d.pct_below_52w_high:.1%} below 52W high, {rsi_str}[/dim]"
                )

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    console.print(f"\n[dim]Run completed in {elapsed:.1f}s[/dim]")
    console.print()

    # ---- Note on partial scores -----------------------------------------
    gap_lines = [
        "[bold yellow]DQ stubs:[/bold yellow] Dip Quality = neutral 50 until MiMo 2.5 configured.",
        "  Run [cyan]scripts/_dip_analysis.py[/cyan] for live MiMo dip classification.",
        "",
        "[bold yellow]Gap A:[/bold yellow] MiMo few-shot grounding dataset not built — "
        "schema-constrained output mandatory.",
        "[bold yellow]Gap B:[/bold yellow] Forward-return threshold undefined — use "
        "backtest/validation.py once entries accumulate.",
    ]
    if not use_db:
        gap_lines.append(
            "[bold yellow]No-DB mode:[/bold yellow] Add [cyan]--use-db[/cyan] to persist "
            "funnel state to PostgreSQL."
        )
    console.print(Panel("\n".join(gap_lines), title="Architecture Status", expand=False))


def _print_summary_table(
    tickers, screen_results, dip_results, score_results, sizing_results, usd_myr
):
    t = Table(
        title="Funnel Summary",
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold",
    )
    t.add_column("Ticker", style="bold", width=8)
    t.add_column("Stage", width=12)
    t.add_column("Dip%", justify="right", width=7)
    t.add_column("RSI", justify="right", width=6)
    t.add_column("IQ", justify="right", width=6)
    t.add_column("BQ", justify="right", width=6)
    t.add_column("DQ*", justify="right", width=6)
    t.add_column("OC", justify="right", width=6)
    t.add_column("Score", justify="right", width=7)
    t.add_column("Mult", justify="right", width=5)
    t.add_column("Size (MYR)", justify="right", width=12)
    t.add_column("Notes", width=30)

    for ticker in tickers:
        sr = screen_results.get(ticker)
        dr = dip_results.get(ticker)
        sc = score_results.get(ticker)
        sz = sizing_results.get(ticker)

        if not sr:
            t.add_row(ticker, "ERROR", *["—"] * 9, "No data")
            continue

        if not sr.passed:
            note = "; ".join(sr.notes[:2]) if sr.notes else ""
            t.add_row(
                ticker, "[red]REJECTED[/red]",
                "—", "—", "—", "—", "—", "—", "—", "—", "—",
                f"[dim]{note[:30]}[/dim]"
            )
            continue

        # Passed screen
        if dr is None:
            stage = "[blue]PROSPECTS[/blue]"
        elif not dr.triggered:
            stage = "[blue]PROSPECTS[/blue]"
        else:
            stage = "[yellow]KIV[/yellow]"

        dip_pct = f"{dr.pct_below_52w_high:.1%}" if dr else "—"
        rsi_str = f"{dr.rsi:.1f}" if dr and dr.rsi else "—"

        if sc:
            iq = f"{sc.income_quality:.1f}"
            bq = f"{sc.business_quality:.1f}"
            dq = f"{sc.dip_quality:.1f}"
            oc = f"{sc.oversold_confidence:.1f}"
            composite = f"[bold]{sc.composite:.1f}[/bold]"
            mult = f"{sc.base_size_multiplier:.1f}×"
            partial = "*" if sc.is_partial else ""
            if sz and sz.adjusted_position_myr > 0:
                size_str = f"MYR {sz.adjusted_position_myr:,.0f}"
            else:
                size_str = "—"
            note_str = "; ".join(sc.partial_reasons[:2])
        else:
            iq = bq = dq = oc = composite = "—"
            mult = "—"
            size_str = "—"
            note_str = "Not scored (no dip trigger)"

        # Color code score
        if sc and sc.composite >= 70:
            composite = f"[green]{sc.composite:.1f}[/green]"
        elif sc and sc.composite >= 60:
            composite = f"[yellow]{sc.composite:.1f}[/yellow]"
        elif sc:
            composite = f"[dim]{sc.composite:.1f}[/dim]"

        t.add_row(ticker, stage, dip_pct, rsi_str, iq, bq, dq, oc, composite, mult, size_str, note_str)

    console.print(t)
    console.print("[dim]* DQ = Dip Quality — stub (50) pending MiMo 2.5 configuration[/dim]")


def main():
    parser = argparse.ArgumentParser(description="Income Compounder Research OS — Full Funnel")
    parser.add_argument(
        "--tickers", nargs="+", default=DEFAULT_UNIVERSE,
        help="List of ticker symbols to screen",
    )
    parser.add_argument(
        "--portfolio-myr", type=float, default=500_000,
        help="Portfolio size in MYR for position sizing",
    )
    parser.add_argument(
        "--use-db", action="store_true", default=False,
        help="Persist funnel state and scores to PostgreSQL (requires DB running)",
    )
    args = parser.parse_args()
    run_funnel(
        tickers=[t.upper() for t in args.tickers],
        portfolio_myr=args.portfolio_myr,
        use_db=args.use_db,
    )


if __name__ == "__main__":
    main()
