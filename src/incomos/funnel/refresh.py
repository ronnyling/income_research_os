"""Staleness-triggered refresh engine.

On every program execution:
  1. Read last_refresh timestamps from research-store-mcp (PostgreSQL)
  2. For each data type, check if it is stale
  3. Build a prioritised refresh queue
  4. Execute refreshes in priority order
  5. Update last_refresh ONLY on success

IMPORTANT: A failed refresh must NOT update last_refresh.
The next run will detect the data as still stale and retry.

No external scheduler is needed. The program itself is the trigger.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from incomos.core.config import get_settings
from incomos.core.types import RefreshRecord, RefreshTask

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Staleness configuration (data_type → max age in hours)
# These map directly to the values in Settings, but are expressed as
# a lookup table so the refresh engine can work without importing config
# everywhere.
# ------------------------------------------------------------------


def _build_staleness_config() -> dict[str, float]:
    cfg = get_settings()
    return {
        "macro_regime": cfg.staleness_macro_hours,           # priority 1
        "kiv_price": cfg.staleness_kiv_price_hours,          # priority 2
        "kiv_filings": cfg.staleness_kiv_filings_hours,      # priority 3
        "universe_quality": cfg.staleness_universe_hours,    # priority 4
        "full_filing": cfg.staleness_full_filing_hours,       # priority 5
    }


_PRIORITY: dict[str, int] = {
    "macro_regime": 1,
    "kiv_price": 2,
    "kiv_filings": 3,
    "universe_quality": 4,
    "full_filing": 5,
}


def is_stale(record: RefreshRecord, max_age_hours: float) -> bool:
    """Return True if the record's last_refresh is older than max_age_hours,
    or if it has never been refreshed.
    """
    if record.last_refresh is None:
        return True
    now = datetime.now(timezone.utc)
    last = record.last_refresh
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age_hours = (now - last).total_seconds() / 3600
    return age_hours > max_age_hours


def build_refresh_queue(records: list[RefreshRecord]) -> list[RefreshTask]:
    """Given a list of refresh records, return the stale ones as an ordered
    queue (lowest priority number = run first).
    """
    staleness_config = _build_staleness_config()
    tasks: list[RefreshTask] = []

    for record in records:
        max_age = staleness_config.get(record.data_type)
        if max_age is None:
            logger.debug("Unknown data_type '%s' — skipping.", record.data_type)
            continue
        if is_stale(record, max_age):
            tasks.append(
                RefreshTask(
                    data_type=record.data_type,
                    ticker=record.ticker,
                    priority=_PRIORITY.get(record.data_type, 99),
                    last_refresh=record.last_refresh,
                    max_age_hours=max_age,
                )
            )

    tasks.sort(key=lambda t: t.priority)
    return tasks


# ------------------------------------------------------------------
# KIV TTL check — always runs on startup, independent of staleness config
# ------------------------------------------------------------------


def check_kiv_ttl(
    kiv_tickers: list[tuple[str, datetime]],
) -> list[str]:
    """Identify KIV stocks whose TTL has expired.

    Args:
        kiv_tickers: list of (ticker, entered_kiv_at) tuples

    Returns:
        List of tickers to demote to DORMANT.
    """
    cfg = get_settings()
    ttl_hours = cfg.kiv_ttl_days * 24
    now = datetime.now(timezone.utc)
    to_demote: list[str] = []

    for ticker, entered_at in kiv_tickers:
        if entered_at.tzinfo is None:
            entered_at = entered_at.replace(tzinfo=timezone.utc)
        age_hours = (now - entered_at).total_seconds() / 3600
        if age_hours > ttl_hours:
            logger.info(
                "KIV TTL expired for %s (in KIV for %.0f days — limit %d days).",
                ticker,
                age_hours / 24,
                cfg.kiv_ttl_days,
            )
            to_demote.append(ticker)

    return to_demote


# ------------------------------------------------------------------
# Startup refresh orchestration
# ------------------------------------------------------------------


class RefreshEngine:
    """Orchestrates the staleness-triggered refresh on program startup.

    Usage:
        engine = RefreshEngine()
        engine.register_handler("universe_quality", refresh_universe_fn)
        engine.run(records_from_db, kiv_tickers_from_db)
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[RefreshTask], bool]] = {}

    def register_handler(
        self,
        data_type: str,
        handler: Callable[[RefreshTask], bool],
    ) -> None:
        """Register a refresh handler for a data type.

        Handler signature: (RefreshTask) -> bool
          True  = refresh succeeded → last_refresh will be updated
          False = refresh failed    → last_refresh must NOT be updated
        """
        self._handlers[data_type] = handler

    def run(
        self,
        records: list[RefreshRecord],
        kiv_tickers: list[tuple[str, datetime]],
    ) -> dict[str, list[str]]:
        """Execute the full startup refresh cycle.

        Returns a summary dict:
          "refreshed": data_types successfully refreshed
          "failed":    data_types that failed (will retry next run)
          "kiv_demoted": tickers demoted to DORMANT due to TTL expiry
        """
        summary: dict[str, list[str]] = {
            "refreshed": [],
            "failed": [],
            "kiv_demoted": [],
        }

        # Step 1: KIV TTL check (always, before anything else)
        demoted = check_kiv_ttl(kiv_tickers)
        summary["kiv_demoted"].extend(demoted)
        if demoted:
            logger.info("KIV TTL: %d stocks demoted to DORMANT: %s", len(demoted), demoted)

        # Step 2: Build refresh queue
        queue = build_refresh_queue(records)
        if not queue:
            logger.info("Startup refresh: all data is fresh. Nothing to refresh.")
            return summary

        logger.info(
            "Startup refresh: %d stale data type(s) to refresh (in priority order).",
            len(queue),
        )

        # Step 3: Execute in priority order
        for task in queue:
            handler = self._handlers.get(task.data_type)
            if handler is None:
                logger.warning(
                    "No handler registered for data_type '%s' — skipping.", task.data_type
                )
                summary["failed"].append(task.data_type)
                continue

            label = f"{task.data_type}" + (f"/{task.ticker}" if task.ticker else "")
            logger.info("Refreshing: %s (last: %s)", label, task.last_refresh or "never")
            try:
                success = handler(task)
            except Exception as exc:
                logger.error("Refresh failed for %s: %s", label, exc)
                success = False

            if success:
                summary["refreshed"].append(label)
            else:
                # Do NOT update last_refresh — the next run will retry.
                logger.warning(
                    "Refresh failed for %s — last_refresh NOT updated. Will retry next run.",
                    label,
                )
                summary["failed"].append(label)

        return summary
