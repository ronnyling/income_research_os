"""Database query helpers for the research store.

All queries are thin wrappers over raw SQLAlchemy core — no ORM overhead.
Each function is responsible for exactly one operation and has no side effects
beyond the intended write.

Refresh timestamps follow the atomic rule:
  update_refresh_timestamp() is ONLY called by the refresh engine on success.
  Never call it on a partial or failed refresh.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, insert, update, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from incomos.core.types import (
    FunnelStage,
    RefreshRecord,
    StockRecord,
    XBRLMetrics,
)
from incomos.screening.stage01 import ScreenResult
from incomos.persistence.db import (
    filing_memos,
    opportunity_scores,
    refresh_log,
    screen_results,
    stocks,
    xbrl_metrics,
)

logger = logging.getLogger(__name__)


def _dialect_insert(conn: Connection, table):
    """Return an INSERT builder with ON CONFLICT support for the active dialect."""
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table)
    return pg_insert(table)


# ------------------------------------------------------------------
# Stocks (funnel state)
# ------------------------------------------------------------------


def upsert_stock(conn: Connection, record: StockRecord) -> None:
    """Insert or update a stock's funnel state."""
    stmt = (
        _dialect_insert(conn, stocks)
        .values(
            ticker=record.ticker,
            exchange=record.exchange.value,
            company_name=record.company_name,
            cik=record.cik,
            funnel_stage=record.funnel_stage.value,
            last_stage_change=record.last_stage_change,
            stage_change_reason=record.stage_change_reason,
            dip_classification=record.dip_classification.value if record.dip_classification else None,
            dip_severity_pct=record.dip_severity_pct,
            conviction_note=record.conviction_note,
            annotation_size_multiplier=record.annotation_size_multiplier,
            updated_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_update(
            index_elements=["ticker"],
            set_={
                "company_name": record.company_name,
                "cik": record.cik,
                "funnel_stage": record.funnel_stage.value,
                "last_stage_change": record.last_stage_change,
                "stage_change_reason": record.stage_change_reason,
                "dip_classification": record.dip_classification.value if record.dip_classification else None,
                "dip_severity_pct": record.dip_severity_pct,
                "conviction_note": record.conviction_note,
                "annotation_size_multiplier": record.annotation_size_multiplier,
                "updated_at": datetime.now(timezone.utc),
            },
        )
    )
    conn.execute(stmt)


def get_stock(conn: Connection, ticker: str) -> dict | None:
    """Return a stock's current record as a dict, or None if not found."""
    stmt = select(stocks).where(stocks.c.ticker == ticker)
    row = conn.execute(stmt).fetchone()
    if row is None:
        return None
    return dict(row._mapping)


def transition_stage(
    conn: Connection,
    ticker: str,
    new_stage: "FunnelStage | str",
    reason: str,
) -> None:
    """Move a stock to a new funnel stage with an audit trail reason.

    Accepts either a FunnelStage enum or a plain string (e.g. from the MCP layer).
    This is the only correct way to change funnel_stage — never update
    the column directly without also recording the reason.
    """
    stage_value = new_stage.value if isinstance(new_stage, FunnelStage) else str(new_stage)
    stmt = (
        update(stocks)
        .where(stocks.c.ticker == ticker)
        .values(
            funnel_stage=stage_value,
            last_stage_change=datetime.now(timezone.utc),
            stage_change_reason=reason,
            updated_at=datetime.now(timezone.utc),
        )
    )
    result = conn.execute(stmt)
    if result.rowcount == 0:
        logger.warning("transition_stage: ticker '%s' not found in stocks table.", ticker)


def get_kiv_stocks(conn: Connection) -> list[tuple[str, datetime]]:
    """Return (ticker, last_stage_change) for all stocks currently in KIV."""
    stmt = select(stocks.c.ticker, stocks.c.last_stage_change).where(
        stocks.c.funnel_stage == FunnelStage.KIV.value
    )
    rows = conn.execute(stmt).fetchall()
    return [(row.ticker, row.last_stage_change) for row in rows]


# ------------------------------------------------------------------
# XBRL metrics
# ------------------------------------------------------------------


def upsert_xbrl_metrics(conn: Connection, m: XBRLMetrics) -> None:
    """Insert or update annual XBRL metrics for a ticker/year combination."""
    stmt = (
        _dialect_insert(conn, xbrl_metrics)
        .values(
            ticker=m.ticker,
            cik=m.cik,
            fiscal_year=m.fiscal_year,
            fiscal_period=m.fiscal_period,
            revenue=m.revenue,
            net_income=m.net_income,
            operating_cash_flow=m.operating_cash_flow,
            capex=m.capex,
            dividends_paid=m.dividends_paid,
            dps_declared=m.dps_declared,
            dps_paid=m.dps_paid,
            total_debt=m.total_debt,
            cash=m.cash,
            equity=m.equity,
            free_cash_flow=m.free_cash_flow,
            fcf_payout_ratio=m.fcf_payout_ratio,
            net_debt=m.net_debt,
            extracted_at=m.extracted_at,
        )
        .on_conflict_do_update(
            index_elements=["ticker", "fiscal_year", "fiscal_period"],
            set_={
                "revenue": m.revenue,
                "net_income": m.net_income,
                "operating_cash_flow": m.operating_cash_flow,
                "capex": m.capex,
                "dividends_paid": m.dividends_paid,
                "dps_declared": m.dps_declared,
                "dps_paid": m.dps_paid,
                "total_debt": m.total_debt,
                "cash": m.cash,
                "equity": m.equity,
                "free_cash_flow": m.free_cash_flow,
                "fcf_payout_ratio": m.fcf_payout_ratio,
                "net_debt": m.net_debt,
                "extracted_at": m.extracted_at,
            },
        )
    )
    conn.execute(stmt)


def get_xbrl_metrics(
    conn: Connection,
    ticker: str,
    years: int = 5,
) -> list[XBRLMetrics]:
    """Retrieve the most recent N years of XBRL metrics for a ticker."""
    stmt = (
        select(xbrl_metrics)
        .where(
            (xbrl_metrics.c.ticker == ticker)
            & (xbrl_metrics.c.fiscal_period == "FY")
        )
        .order_by(xbrl_metrics.c.fiscal_year.desc())
        .limit(years)
    )
    rows = conn.execute(stmt).fetchall()
    result = []
    for row in reversed(rows):
        m = XBRLMetrics(
            ticker=row.ticker,
            cik=row.cik,
            fiscal_year=row.fiscal_year,
            fiscal_period=row.fiscal_period,
            revenue=row.revenue,
            net_income=row.net_income,
            operating_cash_flow=row.operating_cash_flow,
            capex=row.capex,
            dividends_paid=row.dividends_paid,
            dps_declared=row.dps_declared,
            dps_paid=row.dps_paid,
            total_debt=row.total_debt,
            cash=row.cash,
            equity=row.equity,
            extracted_at=row.extracted_at,
        )
        result.append(m)
    return result


# ------------------------------------------------------------------
# Refresh log
# ------------------------------------------------------------------


def get_refresh_records(conn: Connection) -> list[RefreshRecord]:
    """Return all refresh log records."""
    rows = conn.execute(select(refresh_log)).fetchall()
    return [
        RefreshRecord(
            data_type=row.data_type,
            ticker=row.ticker,
            last_refresh=row.last_refresh,
        )
        for row in rows
    ]


def update_refresh_timestamp(
    conn: Connection,
    data_type: str,
    ticker: str | None = None,
) -> None:
    """Update last_refresh for a data_type (and optionally a specific ticker).

    ONLY call this on successful refresh completion. Never on failure.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        _dialect_insert(conn, refresh_log)
        .values(data_type=data_type, ticker=ticker, last_refresh=now)
        .on_conflict_do_update(
            index_elements=["data_type", "ticker"],
            set_={"last_refresh": now},
        )
    )
    conn.execute(stmt)
    logger.debug("Refresh timestamp updated: %s/%s → %s", data_type, ticker or "global", now)


# ------------------------------------------------------------------
# Screen results
# ------------------------------------------------------------------


def save_screen_result(conn: Connection, result: ScreenResult) -> None:
    stmt = _dialect_insert(conn, screen_results).values(
        ticker=result.ticker,
        passed=result.passed,
        checks=result.checks,
        notes=result.notes,
    ).on_conflict_do_update(
        index_elements=["ticker"],
        set_={
            "passed": result.passed,
            "checks": result.checks,
            "notes": result.notes,
        },
    )
    conn.execute(stmt)


# ------------------------------------------------------------------
# Opportunity scores
# ------------------------------------------------------------------

def save_opportunity_score(conn: Connection, ticker: str, score_dict: dict) -> None:
    """Persist an opportunity score record.

    score_dict must include: composite, income_quality, business_quality,
    dip_quality, oversold_confidence, base_size_multiplier.
    Uses upsert to avoid duplicate rows on repeated scoring runs.
    """
    stmt = _dialect_insert(conn, opportunity_scores).values(
        ticker=ticker,
        income_quality=float(score_dict["income_quality"]),
        business_quality=float(score_dict["business_quality"]),
        dip_quality=float(score_dict["dip_quality"]),
        oversold_confidence=float(score_dict["oversold_confidence"]),
        composite=float(score_dict["composite"]),
        base_size_multiplier=float(score_dict.get("base_size_multiplier", 1.0)),
    ).on_conflict_do_update(
        index_elements=["ticker"],
        set_={
            "income_quality": float(score_dict["income_quality"]),
            "business_quality": float(score_dict["business_quality"]),
            "dip_quality": float(score_dict["dip_quality"]),
            "oversold_confidence": float(score_dict["oversold_confidence"]),
            "composite": float(score_dict["composite"]),
            "base_size_multiplier": float(score_dict.get("base_size_multiplier", 1.0)),
        },
    )
    conn.execute(stmt)


# ------------------------------------------------------------------
# KIV basket query
# ------------------------------------------------------------------

def get_kiv_basket(conn: Connection) -> list[dict]:
    """Return all stocks currently in KIV stage with days_in_kiv."""
    stmt = select(
        stocks.c.ticker,
        stocks.c.company_name,
        stocks.c.last_stage_change,
        stocks.c.stage_change_reason,
        stocks.c.dip_classification,
        stocks.c.dip_severity_pct,
    ).where(stocks.c.funnel_stage == FunnelStage.KIV.value)

    rows = conn.execute(stmt).fetchall()
    now = datetime.now(timezone.utc)
    results = []
    for row in rows:
        days_in_kiv: int | None = None
        if row.last_stage_change:
            lsc = row.last_stage_change
            if lsc.tzinfo is None:
                from datetime import timezone as tz
                lsc = lsc.replace(tzinfo=tz.utc)
            days_in_kiv = (now - lsc).days
        results.append({
            "ticker": row.ticker,
            "company_name": row.company_name,
            "last_stage_change": str(row.last_stage_change),
            "stage_change_reason": row.stage_change_reason,
            "dip_classification": row.dip_classification,
            "dip_severity_pct": row.dip_severity_pct,
            "days_in_kiv": days_in_kiv,
            "ttl_warning": days_in_kiv is not None and days_in_kiv >= 80,
        })
    return results


# ------------------------------------------------------------------
# Refresh timestamp lookup
# ------------------------------------------------------------------

def get_refresh_timestamp(
    conn: Connection,
    data_type: str,
    ticker: str | None = None,
) -> "datetime | None":
    """Return the last_refresh timestamp for a data_type/ticker pair, or None."""
    stmt = select(refresh_log.c.last_refresh).where(
        (refresh_log.c.data_type == data_type)
        & (refresh_log.c.ticker == ticker)
    )
    row = conn.execute(stmt).fetchone()
    return row.last_refresh if row else None


# ------------------------------------------------------------------
# MiMo classification cache (stored in filing_memos)
# ------------------------------------------------------------------


def save_mimo_classification(
    conn: Connection,
    ticker: str,
    result: dict,
    filing_hash: str,
    ttl_days: int = 90,
) -> None:
    """Cache a validated MiMo dip classification.

    Stores the result in filing_memos with memo_type="DIP_ANALYSIS".
    The filing_hash is embedded in the content JSON so the caller can
    detect when the filing text has changed (and re-classify).

    ttl_days controls how long the cached result is considered fresh.
    Architecture rule: failed refresh must NOT update the cache.
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    content = {
        "result": result,
        "filing_hash": filing_hash,
        "cached_at": now.isoformat(),
    }
    stmt = (
        _dialect_insert(conn, filing_memos)
        .values(
            ticker=ticker,
            memo_type="DIP_ANALYSIS",
            content=content,
            validated=True,
            created_at=now,
            valid_until=now + timedelta(days=ttl_days),
        )
        .on_conflict_do_update(
            index_elements=["ticker", "memo_type"],
            set_={
                "content": content,
                "validated": True,
                "created_at": now,
                "valid_until": now + timedelta(days=ttl_days),
            },
        )
    )
    conn.execute(stmt)
    logger.debug("Cached MiMo classification for %s (hash=%s, ttl=%dd)", ticker, filing_hash, ttl_days)


def get_cached_mimo_classification(
    conn: Connection,
    ticker: str,
    filing_hash: str,
) -> dict | None:
    """Retrieve a cached MiMo classification if it exists AND the filing hasn't changed.

    Returns the cached result dict if:
      1. A DIP_ANALYSIS memo exists for this ticker
      2. The memo hasn't expired (valid_until > now)
      3. The filing_hash matches (filing text hasn't changed)

    Returns None if any condition fails — caller should re-classify.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(filing_memos.c.content, filing_memos.c.valid_until)
        .where(
            (filing_memos.c.ticker == ticker)
            & (filing_memos.c.memo_type == "DIP_ANALYSIS")
        )
        .limit(1)
    )
    row = conn.execute(stmt).fetchone()
    if row is None:
        return None

    content = row.content
    valid_until = row.valid_until

    # Check TTL
    if valid_until and valid_until < now:
        logger.debug("Cached MiMo classification for %s expired (%s)", ticker, valid_until)
        return None

    # Check filing hash — if filing text changed, cache is stale
    cached_hash = content.get("filing_hash") if isinstance(content, dict) else None
    if cached_hash != filing_hash:
        logger.debug("Cached MiMo classification for %s: filing hash mismatch (cached=%s, current=%s)",
                      ticker, cached_hash, filing_hash)
        return None

    result = content.get("result") if isinstance(content, dict) else None
    if result:
        logger.debug("Cache hit: MiMo classification for %s", ticker)
    return result


def get_cached_mimo_batch(
    conn: Connection,
    ticker_hashes: dict[str, str],
) -> tuple[dict[str, dict], list[str]]:
    """Retrieve cached MiMo classifications for multiple tickers at once.

    Args:
        ticker_hashes: Dict mapping ticker -> filing_hash

    Returns:
        Tuple of (cached_results, uncached_tickers):
        - cached_results: Dict mapping ticker -> cached result dict
        - uncached_tickers: List of tickers that need re-classification
    """
    now = datetime.now(timezone.utc)
    tickers = list(ticker_hashes.keys())
    if not tickers:
        return {}, []

    stmt = (
        select(
            filing_memos.c.ticker,
            filing_memos.c.content,
            filing_memos.c.valid_until,
        )
        .where(
            (filing_memos.c.ticker.in_(tickers))
            & (filing_memos.c.memo_type == "DIP_ANALYSIS")
        )
    )
    rows = conn.execute(stmt).fetchall()

    cached: dict[str, dict] = {}
    seen_tickers: set[str] = set()

    for row in rows:
        t = row.ticker
        seen_tickers.add(t)
        content = row.content
        valid_until = row.valid_until

        # Check TTL
        if valid_until and valid_until < now:
            continue

        # Check filing hash
        cached_hash = content.get("filing_hash") if isinstance(content, dict) else None
        if cached_hash != ticker_hashes.get(t):
            continue

        result = content.get("result") if isinstance(content, dict) else None
        if result:
            cached[t] = result

    uncached = [t for t in tickers if t not in cached]
    logger.info("MiMo cache: %d hits, %d misses (of %d requested)", len(cached), len(uncached), len(tickers))
    return cached, uncached

def save_annotation(
    conn: Connection,
    ticker: str,
    annotation: str,
    analyst: str = "human",
) -> None:
    """Persist a human annotation for a ticker.

    Stored in filing_memos with memo_type="ANNOTATION".
    Annotations can increase position size up to 1.5× base (architecture rule).
    """
    stmt = (
        _dialect_insert(conn, filing_memos)
        .values(
            ticker=ticker,
            memo_type="ANNOTATION",
            content={"annotation": annotation, "analyst": analyst},
            validated=True,
        )
        .on_conflict_do_update(
            index_elements=["ticker", "memo_type"],
            set_={
                "content": {"annotation": annotation, "analyst": analyst},
                "validated": True,
            },
        )
    )
    conn.execute(stmt)
