"""PostgreSQL schema definition and connection management.

research-store-mcp owns:
  - stocks:            funnel state per ticker
  - refresh_log:       last_refresh timestamps (staleness tracking)
  - xbrl_metrics:      extracted annual financial data
  - screen_results:    Stage 0-1 quality screen outcomes
  - filing_memos:      filing analysis summaries (Phase 2+)
  - opportunity_scores: scored candidates (Phase 5+)
  - annotations:        human conviction notes and size overrides

All schema changes go through Alembic migrations.
This module handles engine creation and the initial schema bootstrap only.
"""

from __future__ import annotations

import logging

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.engine import Engine

from incomos.core.config import get_settings

logger = logging.getLogger(__name__)

metadata = MetaData()

# ------------------------------------------------------------------
# Table definitions
# ------------------------------------------------------------------

stocks = Table(
    "stocks",
    metadata,
    Column("ticker", String(20), primary_key=True),
    Column("exchange", String(5), nullable=False),          # US | MY
    Column("company_name", String(255), nullable=False),
    Column("cik", String(15), nullable=True),               # EDGAR CIK (US only)
    Column("funnel_stage", String(30), nullable=False, server_default="UNIVERSE"),
    Column("last_stage_change", DateTime(timezone=True), nullable=True),
    Column("stage_change_reason", Text, nullable=True),
    Column("dip_classification", String(40), nullable=True),
    Column("dip_severity_pct", Float, nullable=True),
    Column("conviction_note", Text, nullable=True),
    Column("annotation_size_multiplier", Float, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

refresh_log = Table(
    "refresh_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("data_type", String(50), nullable=False),
    Column("ticker", String(20), nullable=True),            # NULL = global data type
    Column("last_refresh", DateTime(timezone=True), nullable=True),
    UniqueConstraint("data_type", "ticker", name="uq_refresh_log_type_ticker"),
)

xbrl_metrics = Table(
    "xbrl_metrics",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticker", String(20), nullable=False),
    Column("cik", String(15), nullable=False),
    Column("fiscal_year", Integer, nullable=False),
    Column("fiscal_period", String(5), nullable=False, server_default="FY"),
    Column("revenue", Float, nullable=True),
    Column("net_income", Float, nullable=True),
    Column("operating_cash_flow", Float, nullable=True),
    Column("capex", Float, nullable=True),
    Column("dividends_paid", Float, nullable=True),
    Column("dps_declared", Float, nullable=True),
    Column("dps_paid", Float, nullable=True),
    Column("total_debt", Float, nullable=True),
    Column("cash", Float, nullable=True),
    Column("equity", Float, nullable=True),
    Column("free_cash_flow", Float, nullable=True),         # computed, stored for convenience
    Column("fcf_payout_ratio", Float, nullable=True),
    Column("net_debt", Float, nullable=True),
    Column("extracted_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("ticker", "fiscal_year", "fiscal_period", name="uq_xbrl_metrics"),
)

screen_results = Table(
    "screen_results",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticker", String(20), nullable=False),
    Column("passed", Boolean, nullable=False),
    Column("checks", JSON, nullable=False),                 # {check_name: bool}
    Column("notes", JSON, nullable=False),                  # [string]
    Column("screened_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

filing_memos = Table(
    "filing_memos",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticker", String(20), nullable=False),
    Column("memo_type", String(50), nullable=False),        # XBRL_METRICS | DIP_ANALYSIS | ANNOTATION | etc.
    Column("content", JSON, nullable=False),
    Column("validated", Boolean, nullable=False, server_default=text("FALSE")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("valid_until", DateTime(timezone=True), nullable=True),
    UniqueConstraint("ticker", "memo_type", name="uq_filing_memos_ticker_type"),
)

opportunity_scores = Table(
    "opportunity_scores",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticker", String(20), nullable=False),
    Column("income_quality", Float, nullable=False),
    Column("business_quality", Float, nullable=False),
    Column("dip_quality", Float, nullable=False),
    Column("oversold_confidence", Float, nullable=False),
    Column("composite", Float, nullable=False),
    Column("base_size_multiplier", Float, nullable=False),
    Column("scored_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


# ------------------------------------------------------------------
# Engine factory
# ------------------------------------------------------------------

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        cfg = get_settings()
        _engine = create_engine(cfg.database_url, pool_pre_ping=True)
    return _engine


def create_schema() -> None:
    """Create all tables if they don't already exist.

    For production use, prefer Alembic migrations over this function.
    This is a convenience bootstrap for development and testing.
    """
    engine = get_engine()
    metadata.create_all(engine, checkfirst=True)
    logger.info("Schema bootstrapped successfully.")
