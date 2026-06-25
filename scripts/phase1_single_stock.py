"""Phase 1 — Single-stock research script.

Usage:
    python scripts/phase1_single_stock.py ACN
    python scripts/phase1_single_stock.py ACN --save        # persist to DB
    python scripts/phase1_single_stock.py ACN --years 7     # extend lookback
    python scripts/phase1_single_stock.py ACN --no-db       # skip DB (output only)

What it does:
    1. Resolve ticker → CIK via EDGAR
    2. Extract XBRL annual metrics (default: 5 years)
    3. Run Stage 0→1 quality screen
    4. Print a rich summary table
    5. Optionally persist metrics + screen result to research store (PostgreSQL)

Phase 1 gate: run this for ACN, MSFT, JNJ, KO, and one MY stock.
If the metrics look right and the screen logic is sound, proceed to Phase 2.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

# Allow running from project root without pip install
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from incomos.core.types import Exchange, FunnelStage, StockRecord
from incomos.data.edgar import EdgarClient
from incomos.screening.stage01 import run_quality_screen

console = Console()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("phase1")


def _fmt_usd(value: float | None, scale: float = 1e9) -> str:
    """Format a USD value in billions (default) or as-is."""
    if value is None:
        return "[dim]—[/dim]"
    scaled = value / scale
    return f"${scaled:,.2f}B"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "[dim]—[/dim]"
    return f"{value:.1%}"


def _fmt_growth(series: list[float | None]) -> str:
    """Show direction of last 3 values as arrows."""
    valid = [v for v in series[-3:] if v is not None]
    if len(valid) < 2:
        return "[dim]—[/dim]"
    arrows = []
    for i in range(1, len(valid)):
        if valid[i] > valid[i - 1] * 1.005:
            arrows.append("[green]↑[/green]")
        elif valid[i] < valid[i - 1] * 0.995:
            arrows.append("[red]↓[/red]")
        else:
            arrows.append("[yellow]→[/yellow]")
    return " ".join(arrows)


@click.command()
@click.argument("ticker")
@click.option("--years", default=5, show_default=True, help="Number of annual periods to fetch.")
@click.option("--save", is_flag=True, default=False, help="Persist results to PostgreSQL.")
@click.option("--no-db", is_flag=True, default=False, help="Skip DB entirely (print only).")
def main(ticker: str, years: int, save: bool, no_db: bool) -> None:
    ticker = ticker.upper()
    console.rule(f"[bold]Income Compounder Research OS — Phase 1[/bold]")
    console.print(f"[bold]Ticker:[/bold] {ticker}  |  [bold]Exchange:[/bold] US (EDGAR)  |  [bold]Base currency:[/bold] MYR\n")

    # ------------------------------------------------------------------
    # Step 1: Resolve CIK
    # ------------------------------------------------------------------
    with EdgarClient() as edgar:
        try:
            cik = edgar.resolve_cik(ticker)
            console.print(f"[green]✓[/green] CIK resolved: {cik}")
        except ValueError as e:
            console.print(f"[red]✗ {e}[/red]")
            raise SystemExit(1)

        # ------------------------------------------------------------------
        # Step 2: Fetch company info
        # ------------------------------------------------------------------
        info = edgar.get_company_info(cik)
        company_name = info.get("name", ticker)
        sic_desc = info.get("sicDescription", "")
        console.print(f"[green]✓[/green] Company: [bold]{company_name}[/bold]  ({sic_desc})\n")

        # ------------------------------------------------------------------
        # Step 3: Extract XBRL metrics
        # ------------------------------------------------------------------
        console.print(f"Fetching {years} years of XBRL annual metrics...")
        metrics = edgar.get_annual_metrics(ticker, cik, years=years)

    if not metrics:
        console.print("[red]✗ No annual XBRL data returned. Cannot proceed.[/red]")
        raise SystemExit(1)

    console.print(f"[green]✓[/green] {len(metrics)} annual periods extracted.\n")

    # ------------------------------------------------------------------
    # Step 4: Print metrics table
    # ------------------------------------------------------------------
    tbl = Table(
        title=f"{ticker} — Annual XBRL Metrics (USD)",
        box=box.SIMPLE_HEAD,
        show_lines=True,
        title_style="bold",
    )
    tbl.add_column("Metric", style="bold", no_wrap=True)
    for m in metrics:
        tbl.add_column(f"FY{m.fiscal_year}", justify="right")

    rows = [
        ("Revenue", [_fmt_usd(m.revenue) for m in metrics]),
        ("Net Income", [_fmt_usd(m.net_income) for m in metrics]),
        ("Operating CF", [_fmt_usd(m.operating_cash_flow) for m in metrics]),
        ("CapEx", [_fmt_usd(m.capex) for m in metrics]),
        ("Free Cash Flow", [_fmt_usd(m.free_cash_flow) for m in metrics]),
        ("Dividends Paid", [_fmt_usd(m.dividends_paid) for m in metrics]),
        ("DPS Declared", [
            f"${m.dps_declared:.2f}" if m.dps_declared is not None else "[dim]—[/dim]"
            for m in metrics
        ]),
        ("FCF Payout Ratio", [_fmt_pct(m.fcf_payout_ratio) for m in metrics]),
        ("Total Debt", [_fmt_usd(m.total_debt) for m in metrics]),
        ("Cash", [_fmt_usd(m.cash) for m in metrics]),
        ("Net Debt", [_fmt_usd(m.net_debt) for m in metrics]),
    ]
    for label, values in rows:
        tbl.add_row(label, *values)

    console.print(tbl)

    # Trend summary
    rev_trend = _fmt_growth([m.revenue for m in metrics])
    fcf_trend = _fmt_growth([m.free_cash_flow for m in metrics])
    dps_trend = _fmt_growth([m.dps_declared for m in metrics])
    console.print(
        f"Trends (last 3 years):  Revenue {rev_trend}  |  FCF {fcf_trend}  |  DPS {dps_trend}\n"
    )

    # ------------------------------------------------------------------
    # Step 5: Quality screen (Stage 0→1)
    # ------------------------------------------------------------------
    screen = run_quality_screen(ticker, metrics)
    status_color = "green" if screen.passed else "red"
    status_icon = "✓ PASS" if screen.passed else "✗ FAIL"

    scr_tbl = Table(title="Stage 0→1 Quality Screen", box=box.SIMPLE_HEAD, show_lines=True)
    scr_tbl.add_column("Check", style="bold")
    scr_tbl.add_column("Result")
    for check, result in screen.checks.items():
        icon = "[green]✓[/green]" if result else "[red]✗[/red]"
        scr_tbl.add_row(check.replace("_", " ").title(), icon)

    console.print(scr_tbl)

    for note in screen.notes:
        console.print(f"  [dim]{note}[/dim]")

    console.print()
    console.print(
        f"[bold]Screen result: [{status_color}]{status_icon}[/{status_color}][/bold]  →  "
        + (
            f"[{status_color}]Promote to PROSPECTS[/{status_color}]"
            if screen.passed
            else f"[{status_color}]REJECTED (reason recorded)[/{status_color}]"
        )
    )
    console.print()

    # ------------------------------------------------------------------
    # Step 6: Persist (optional)
    # ------------------------------------------------------------------
    if save and not no_db:
        _persist(ticker, cik, company_name, metrics, screen)
    elif not no_db and not save:
        console.print("[dim]Tip: use --save to persist results to PostgreSQL.[/dim]")


def _persist(
    ticker: str,
    cik: str,
    company_name: str,
    metrics: list,
    screen,
) -> None:
    from incomos.persistence.db import create_schema, get_engine
    from incomos.persistence.queries import (
        save_screen_result,
        transition_stage,
        upsert_stock,
        upsert_xbrl_metrics,
    )
    from datetime import datetime, timezone

    engine = get_engine()
    create_schema()

    with engine.begin() as conn:
        # Upsert stock record
        record = StockRecord(
            ticker=ticker,
            exchange=Exchange.US,
            company_name=company_name,
            cik=cik,
            funnel_stage=FunnelStage.UNIVERSE,
        )
        upsert_stock(conn, record)

        # Save XBRL metrics
        for m in metrics:
            upsert_xbrl_metrics(conn, m)

        # Save screen result + transition stage
        save_screen_result(conn, screen)
        new_stage = FunnelStage.PROSPECTS if screen.passed else FunnelStage.REJECTED
        transition_stage(conn, ticker, new_stage, screen.notes[-1])

    console.print(f"[green]✓[/green] Saved to DB. Stage → [bold]{new_stage.value}[/bold]")


if __name__ == "__main__":
    main()
