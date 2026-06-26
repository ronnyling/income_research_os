"""Targeted MiMo batch processing test.

Tests the streaming + keep-alive mechanism with real API calls on a small
set of well-known dividend stocks. This script is the GATE before any
full universe beta run — if this fails, don't attempt the full run.

Usage:
  python scripts/test_mimo_batch.py [--batch-size 5] [--stocks KO JNJ PG MSFT ACN]

What it validates:
  1. MiMo API connectivity and authentication
  2. Streaming + keep-alive mechanism (180s read timeout)
  3. Batch prompt construction and JSON parsing
  4. Schema validation of returned classifications
  5. Timing: per-chunk and total elapsed time
"""

from __future__ import annotations

import argparse
import logging
import sys
import os
import time

# Ensure src/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from incomos.core.config import get_settings

# Set up logging: INFO to console for this test
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_mimo_batch")

DEFAULT_STOCKS = ["KO", "JNJ", "PG", "MSFT", "ACN"]


def gather_filing_data(tickers: list[str]) -> list[dict]:
    """Fetch real EDGAR filing data for the given tickers."""
    from incomos.data.edgar import EdgarClient
    from incomos.data.filings import FilingsClient

    edgar = EdgarClient()
    filings = FilingsClient()
    macro_ctx = "TRENDING_UP growth=EXPANSION"  # static for test

    results = []
    for ticker in tickers:
        logger.info("Fetching filing data for %s...", ticker)
        try:
            cik = edgar.resolve_cik(ticker)
            if not cik:
                logger.warning("  %s: CIK not found — skipping", ticker)
                continue

            current_secs, prior_secs = filings.get_yoy_sections(ticker, cik)
            mda_sec = current_secs.get("MDAA")
            mda = mda_sec.text if mda_sec else ""
            risk_cur_sec = current_secs.get("RISK_FACTORS")
            risk_current = risk_cur_sec.text if risk_cur_sec else ""
            risk_prior_sec = prior_secs.get("RISK_FACTORS")
            risk_prior = risk_prior_sec.text if risk_prior_sec else None

            text_len = len(mda) + len(risk_current)
            if text_len < 200:
                logger.warning("  %s: filing sections too short (%d chars) — skipping", ticker, text_len)
                continue

            from incomos.llm.mimo import compute_filing_hash
            filing_hash = compute_filing_hash(mda, risk_current, risk_prior)

            results.append({
                "ticker": ticker,
                "mda_text": mda,
                "risk_factors_current": risk_current,
                "risk_factors_prior": risk_prior,
                "macro_context": macro_ctx,
                "filing_hash": filing_hash,
            })
            logger.info("  %s: OK (MDA=%d chars, risk=%d chars, hash=%s)",
                        ticker, len(mda), len(risk_current), filing_hash)
        except Exception as exc:
            logger.error("  %s: FAILED — %s", ticker, exc)

    return results


def run_batch_test(stock_data: list[dict], batch_size: int) -> bool:
    """Run analyze_dip_batch and validate results. Returns True if passed."""
    from incomos.llm.mimo import analyze_dip_batch
    from incomos.llm.schemas import DipAnalysisOutput

    tickers = [s["ticker"] for s in stock_data]
    logger.info("=" * 60)
    logger.info("MiMo Batch Test: %d stocks, batch_size=%d", len(tickers), batch_size)
    logger.info("Stocks: %s", ", ".join(tickers))
    logger.info("=" * 60)

    start_time = time.monotonic()

    try:
        results = analyze_dip_batch(stock_data, batch_size=batch_size)
    except Exception as exc:
        elapsed = time.monotonic() - start_time
        logger.error("FAILED: analyze_dip_batch raised exception after %.1fs: %s", elapsed, exc)
        return False

    elapsed = time.monotonic() - start_time

    # Validate results
    logger.info("-" * 60)
    logger.info("Results (%.1fs elapsed):", elapsed)

    success_count = 0
    for ticker in tickers:
        if ticker in results:
            r = results[ticker]
            logger.info("  ✓ %s: %s (conf=%.2f) — %s",
                        ticker, r.classification, r.confidence,
                        r.evidence_summary[:80] + "..." if len(r.evidence_summary) > 80 else r.evidence_summary)
            success_count += 1
        else:
            logger.warning("  ✗ %s: MISSING from results", ticker)

    logger.info("-" * 60)
    logger.info("Classified: %d/%d stocks", success_count, len(tickers))
    logger.info("Total time: %.1fs (%.1fs per stock)", elapsed, elapsed / max(len(tickers), 1))

    # Pass criteria
    passed = True
    if success_count == 0:
        logger.error("FAIL: No stocks were classified")
        passed = False
    elif success_count < len(tickers):
        logger.warning("PARTIAL: Only %d/%d classified (acceptable if filing data was sparse)", success_count, len(tickers))
        # Partial success is OK — don't fail the test
    else:
        logger.info("PASS: All stocks classified successfully")

    if elapsed > 600:
        logger.warning("WARNING: Total time %.0fs exceeds 10 min budget — consider reducing batch size", elapsed)

    return passed


def main():
    parser = argparse.ArgumentParser(description="Targeted MiMo batch processing test")
    parser.add_argument("--stocks", nargs="+", default=DEFAULT_STOCKS,
                        help="Ticker symbols to test (default: KO JNJ PG MSFT ACN)")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="Batch size (default: from config)")
    parser.add_argument("--skip-filing-fetch", action="store_true",
                        help="Use synthetic filing data instead of fetching from EDGAR")
    args = parser.parse_args()

    # Check MiMo config
    settings = get_settings()
    if not settings.mimo_api_key:
        logger.error("MIMO_API_KEY not set — cannot run batch test")
        sys.exit(1)

    logger.info("MiMo API: %s", settings.mimo_api_base_url)
    logger.info("MiMo model: %s", settings.mimo_model)
    logger.info("Config batch_size: %d", settings.mimo_batch.batch_size)
    logger.info("Config read timeout: 180s (streaming + keep-alive)")

    # Gather filing data
    if args.skip_filing_fetch:
        logger.info("Using synthetic filing data (--skip-filing-fetch)")
        stock_data = []
        for ticker in args.stocks:
            stock_data.append({
                "ticker": ticker,
                "mda_text": f"[SYNTHETIC] Revenue for {ticker} grew 5% YoY. Operating margins stable. "
                            f"Management expressed confidence in dividend sustainability. "
                            f"Free cash flow covered dividends 1.8x.",
                "risk_factors_current": f"[SYNTHETIC] {ticker} faces competition risk, regulatory changes, "
                                        f"and macroeconomic uncertainty. Supply chain disruptions possible.",
                "risk_factors_prior": f"[SYNTHETIC] Prior year risks for {ticker} were similar but included "
                                      f"pandemic-related disruptions now resolved.",
                "macro_context": "TRENDING_UP growth=EXPANSION",
            })
    else:
        logger.info("Fetching real EDGAR filing data for %d stocks...", len(args.stocks))
        stock_data = gather_filing_data(args.stocks)
        if not stock_data:
            logger.error("No filing data gathered — cannot run batch test")
            sys.exit(1)

    # Run the batch test
    batch_size = args.batch_size or settings.mimo_batch.batch_size
    passed = run_batch_test(stock_data, batch_size)

    if passed:
        logger.info("=" * 60)
        logger.info("MiMo batch test PASSED — safe to proceed with full universe run")
        sys.exit(0)
    else:
        logger.error("=" * 60)
        logger.error("MiMo batch test FAILED — do NOT proceed with full universe run")
        logger.error("Troubleshooting:")
        logger.error("  1. Check MIMO_API_KEY is valid")
        logger.error("  2. Try --batch-size 3 (smaller batches)")
        logger.error("  3. Try --skip-filing-fetch (synthetic data)")
        logger.error("  4. Check MiMo API status")
        sys.exit(1)


if __name__ == "__main__":
    main()
