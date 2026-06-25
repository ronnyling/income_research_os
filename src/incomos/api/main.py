"""FastAPI application — Income Compounder Research OS.

Endpoints:
  GET  /health                   — liveness check
  POST /screen/{ticker}          — run Stage 0→1 quality screen
  POST /price/{ticker}           — fetch current price snapshot
  GET  /kiv                      — list KIV basket stocks (from DB)
  GET  /prospects                — list PROSPECTS stocks (from DB)
  POST /score/{ticker}           — compute opportunity score (XBRL + optional price)
  POST /refresh                  — trigger manual staleness refresh
  GET  /macro                    — current macro regime contract
  GET  /memo/{ticker}            — latest filing memo for a ticker

This is a research decision-support API. It does not submit orders.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---- Lifespan -----------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("IncomOS API starting up...")
    yield
    logger.info("IncomOS API shutting down.")


# ---- App ----------------------------------------------------------------

app = FastAPI(
    title="Income Compounder Research OS",
    description=(
        "Funnel-based income stock research system. "
        "Surfaces quality dividend stocks at dip entry points. "
        "MYR base currency. Decision-support only — not automated trading."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


# ---- Response models ----------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str = "0.1.0"


class ScreenRequest(BaseModel):
    years: int = 5


class ScoreRequest(BaseModel):
    include_price: bool = True
    portfolio_size_myr: float = 500_000


# ---- Endpoints ----------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/screen/{ticker}")
async def screen_ticker(ticker: str, req: ScreenRequest = ScreenRequest()):
    """Run Stage 0→1 quality screen for a ticker.

    Fetches EDGAR XBRL, runs rule-based quality checks.
    Near-zero cost: no LLM.
    """
    ticker = ticker.upper()
    try:
        from incomos.data.edgar import EdgarClient
        from incomos.screening.stage01 import run_quality_screen

        client = EdgarClient()
        cik = client.resolve_cik(ticker)
        if not cik:
            raise HTTPException(status_code=404, detail=f"CIK not found for ticker {ticker}")

        metrics = client.get_annual_metrics(ticker, cik, years=req.years)
        result = run_quality_screen(ticker, metrics)

        return {
            "ticker": ticker,
            "passed": result.passed,
            "checks": result.checks,
            "notes": result.notes,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Screen failed for %s: %s", ticker, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/price/{ticker}")
async def get_price(ticker: str):
    """Fetch current price snapshot (yfinance)."""
    ticker = ticker.upper()
    try:
        from incomos.data.market import get_price_snapshot
        snap = get_price_snapshot(ticker)
        return {
            "ticker": snap.ticker,
            "current_price": snap.current_price,
            "week52_high": snap.week52_high,
            "pct_below_52w_high": round(snap.pct_below_52w_high, 4),
            "rsi_14": snap.rsi_14,
            "volume_ratio": snap.volume_ratio,
            "price_trend": snap.price_trend,
            "data_quality": snap.data_quality,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/score/{ticker}")
async def score_ticker(ticker: str, req: ScoreRequest = ScoreRequest()):
    """Compute opportunity score for a ticker.

    Fetches EDGAR XBRL + optionally price data.
    Dip Quality will be a stub (50) until MIMO_API_KEY is configured.
    """
    ticker = ticker.upper()
    try:
        from incomos.data.edgar import EdgarClient
        from incomos.data.market import get_price_snapshot
        from incomos.data.fred import get_usd_myr_rate
        from incomos.scoring.engine import score as compute_score
        from incomos.sizing import compute_position_size

        client = EdgarClient()
        cik = client.resolve_cik(ticker)
        if not cik:
            raise HTTPException(status_code=404, detail=f"CIK not found for ticker {ticker}")

        metrics = client.get_annual_metrics(ticker, cik, years=5)
        snap = get_price_snapshot(ticker) if req.include_price else None
        result = compute_score(ticker, metrics, price_snap=snap)

        usd_myr = get_usd_myr_rate()
        sizing = compute_position_size(
            result=result,
            portfolio_size_myr=req.portfolio_size_myr,
            exchange="US",
            usd_myr_rate=usd_myr,
        )

        return {
            "ticker": ticker,
            "income_quality": result.income_quality,
            "business_quality": result.business_quality,
            "dip_quality": result.dip_quality,
            "oversold_confidence": result.oversold_confidence,
            "composite": result.composite,
            "is_partial": result.is_partial,
            "partial_reasons": result.partial_reasons,
            "base_size_multiplier": result.base_size_multiplier,
            "position_myr": sizing.adjusted_position_myr,
            "position_usd": sizing.position_usd,
            "usd_myr_rate": usd_myr,
            "sizing_notes": sizing.notes,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Score failed for %s: %s", ticker, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/macro")
async def get_macro():
    """Return current macro regime contract."""
    try:
        from incomos.macro.regime import detect_macro_regime
        contract = detect_macro_regime()
        return {
            "as_of": contract.as_of.isoformat(),
            "primary_regime": contract.primary_regime,
            "primary_confidence": contract.primary_confidence,
            "market_structure": str(contract.market_structure.state),
            "growth": str(contract.growth.state),
            "rates": str(contract.rates.state),
            "financial_conditions": str(contract.financial_conditions.state),
            "evidence": contract.evidence,
        }
    except Exception as exc:
        logger.error("Macro regime detection failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/kiv")
async def list_kiv(db: bool = Query(default=False)):
    """List KIV basket stocks.

    db=false (default): returns a note that DB is not connected.
    db=true: queries PostgreSQL (requires DATABASE_URL in .env).
    """
    if not db:
        return {
            "note": "Set ?db=true to query PostgreSQL. "
                    "Run the full funnel (scripts/run_funnel.py) to populate.",
            "kiv_stocks": [],
        }
    try:
        from incomos.persistence.db import get_engine
        from incomos.persistence.queries import get_kiv_stocks
        with get_engine().connect() as conn:
            stocks = get_kiv_stocks(conn)
        return {"kiv_stocks": [dict(s) for s in stocks]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/prospects")
async def list_prospects():
    return {
        "note": "Prospect list is populated by scripts/run_funnel.py. "
                "This endpoint will query PostgreSQL when DATABASE_URL is configured.",
        "prospects": [],
    }
