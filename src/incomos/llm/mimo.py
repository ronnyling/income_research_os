"""MiMo 2.5 API client.

Raises MimoNotConfiguredError if MIMO_API_KEY is not set.
All responses are validated against Pydantic schemas before return.
Validation failure raises ValidationError — caller flags for human review.

LLM Strategy Rule (architecture locked):
    Rule-based MCP tools FIRST. MiMo 2.5 ONLY when rule-based fails or returns null.
    This module is never called from Stage 0-1 or Stage 1-2.
    It is only called from Stage 2-3 (context check) and Stage 3-4 (full DD).

Few-shot grounding examples are embedded directly in the dip-classification prompt.
Schema-constrained output remains mandatory before any score uses MiMo output.
"""

from __future__ import annotations

import hashlib
import json
import re
import logging
import threading
import time
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from incomos.core.config import get_settings
from incomos.llm.schemas import (
    DipAnalysisOutput,
    FilingExtractionOutput,
    RiskFactorDiffOutput,
    ValidationError,
    validate_dip_analysis,
    validate_filing_extraction,
    validate_risk_factor_diff,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Streaming + keep-alive MiMo API client
# ---------------------------------------------------------------------------
#
# MiMo 2.5 thinking mode on batch prompts (10 stocks with full filing context)
# can take 2-5 minutes. A monolithic httpx timeout kills the connection before
# the API finishes thinking. Instead we use:
#
#   1. EXTENDED TIMEOUT: httpx.post() with 180s read timeout. MiMo thinking
#      mode can be silent for 1-2 minutes before producing output. The 180s
#      timeout gives enough headroom while the 600s total is a hard cap.
#
#   2. KEEP-ALIVE MONITOR: A background thread logs a heartbeat every 30s.
#      If MiMo hasn't sent any data for 120s, the monitor logs a warning.
#      This gives operators visibility into whether the API is stuck or just slow.
#
#   3. GRANULAR TIMEOUTS:
#      - connect: 30s (server must accept connection)
#      - read: 180s (server must send *some* data at least every 180s —
#        MiMo thinking mode can be silent for 1-2 minutes before producing output)
#      - write: 30s (we must be able to send the request)
#      - pool: 30s (connection pool acquisition)
#      - total: 600s (10 min hard cap per API call)
#
#   4. RETRY: 3 attempts with exponential backoff. On each retry, the keep-alive
#      thread is restarted.
#
# If this still fails (API genuinely takes >10 min), the caller should reduce
# batch_size in config (MIMO_BATCH__BATCH_SIZE).


def _assert_configured() -> None:
    """Raise MimoNotConfiguredError if MIMO_API_KEY is absent."""
    from incomos.core.exceptions import MimoNotConfiguredError
    if not get_settings().mimo_api_key:
        raise MimoNotConfiguredError()


class _KeepAliveMonitor:
    """Background thread that logs heartbeat while an API call is in progress.

    Usage:
        monitor = _KeepAliveMonitor(label="KO/MSFT/...")
        monitor.start()
        try:
            result = do_api_call()
        finally:
            monitor.stop()
    """

    def __init__(self, label: str, interval: float = 30.0, stale_threshold: float = 60.0):
        self.label = label
        self.interval = interval
        self.stale_threshold = stale_threshold
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_data_time = time.monotonic()
        self._start_time = time.monotonic()

    def mark_data_received(self) -> None:
        """Call this whenever a chunk of data arrives from the API."""
        self._last_data_time = time.monotonic()

    def start(self) -> None:
        self._start_time = time.monotonic()
        self._last_data_time = self._start_time
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"mimo-keepalive-{self.label}")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval):
            elapsed = time.monotonic() - self._start_time
            since_data = time.monotonic() - self._last_data_time
            if since_data >= self.stale_threshold:
                logger.warning(
                    "MiMo keep-alive [%s]: %.0fs elapsed, NO DATA for %.0fs — API may be stuck",
                    self.label, elapsed, since_data,
                )
            else:
                logger.info(
                    "MiMo keep-alive [%s]: %.0fs elapsed, last data %.0fs ago — still processing",
                    self.label, elapsed, since_data,
                )


def _call_api_streaming(endpoint: str, payload: dict[str, Any], label: str = "") -> dict:
    """Make an authenticated API call to MiMo 2.5 with keep-alive monitoring.

    Uses a regular httpx.post() with extended read timeout (180s) to handle
    MiMo's thinking mode which can be silent for 1-2 minutes. A background
    keep-alive thread logs every 30s so operators can see progress.

    Returns the parsed JSON response.

    Raises httpx.HTTPStatusError on 4xx/5xx (triggering tenacity retry).
    Raises httpx.TimeoutException if no data received for 180s or total >600s.
    """
    settings = get_settings()
    base_url = settings.mimo_api_base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {settings.mimo_api_key}",
        "Content-Type": "application/json",
    }

    # Granular timeouts:
    #   connect: 30s — server must accept TCP connection
    #   read: 180s — MiMo thinking mode can be silent for 1-2 minutes
    #   write: 30s — we must be able to send the request body
    #   pool: 30s — connection pool acquisition
    timeout = httpx.Timeout(600.0, connect=30.0, read=180.0, write=30.0, pool=30.0)

    monitor = _KeepAliveMonitor(label=label or endpoint, interval=30.0, stale_threshold=120.0)
    monitor.start()

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{base_url}/{endpoint}", json=payload, headers=headers)
            resp.raise_for_status()
            monitor.mark_data_received()

            body = resp.text
            logger.debug("MiMo response: %d chars, content-type=%s", len(body), resp.headers.get("content-type", ""))

            # Try to parse as JSON directly
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                pass

            # Try SSE format (data: {...} lines)
            sse_data_parts: list[str] = []
            for line in body.split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    sse_data_parts.append(data)

            if sse_data_parts:
                logger.debug("MiMo: SSE format (%d data lines)", len(sse_data_parts))
                # Try last chunk first (usually complete response)
                try:
                    return json.loads(sse_data_parts[-1])
                except json.JSONDecodeError:
                    pass
                # Try concatenating all chunks
                return json.loads("".join(sse_data_parts))

            # If nothing works, raise with body preview
            raise ValueError(f"MiMo response is not valid JSON or SSE. Preview: {body[:200]}")

    finally:
        monitor.stop()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=30),
       after=lambda retry_state: logger.warning(
           "MiMo API retry #%d for %s: %s",
           retry_state.attempt_number,
           retry_state.fn.__name__,
           retry_state.outcome.exception() if retry_state.outcome else "unknown",
       ) if retry_state.outcome and retry_state.outcome.failed else None)
def _call_api(endpoint: str, payload: dict[str, Any]) -> dict:
    """Make an authenticated API call to MiMo 2.5.

    Delegates to _call_api_streaming which provides:
    - Chunked response reading (each chunk resets read timeout)
    - Background keep-alive logging every 30s
    - Granular timeouts (read=180s, total=600s)
    - Visibility into whether the API is stuck or just slow

    Retries up to 3 times with exponential backoff (5s, 15s, 30s).
    """
    # Derive a label from the payload for keep-alive logging
    model = payload.get("model", "unknown")
    # Try to extract ticker(s) from the prompt for the label
    messages = payload.get("messages", [])
    prompt_text = messages[0].get("content", "") if messages else ""
    # Look for ticker patterns — batch prompts have "### Stock: XXX"
    tickers_found = re.findall(r'### Stock:\s*([A-Z]{1,5})', prompt_text[:2000])
    if tickers_found:
        label = "/".join(tickers_found[:5]) + ("..." if len(tickers_found) > 5 else "")
    else:
        # Single stock: "Stock: XXX"
        ticker_match = re.search(r'(?:Stock|ticker):\s*([A-Z]{1,5})', prompt_text[:500])
        label = ticker_match.group(1) if ticker_match else model

    logger.info("MiMo API call [%s]: streaming with keep-alive (read=180s, total=600s)", label)
    return _call_api_streaming(endpoint, payload, label=label)


def _parse_json_response(content: str, schema_name: str, ticker: str) -> dict:
    """Parse a MiMo response that should contain one JSON object."""
    import json

    stripped = content.strip()
    if not stripped:
        raise ValidationError(schema_name, ticker, ["Response content was empty"])

    candidates: list[str] = [stripped]

    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())

    first_brace = stripped.find("{")
    if first_brace != -1:
        candidates.append(stripped[first_brace:])

    decoder = json.JSONDecoder()
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            result_dict, _ = decoder.raw_decode(candidate)
            if isinstance(result_dict, dict):
                return result_dict
            last_error = ValidationError(schema_name, ticker, ["Response JSON was not an object"])
        except Exception as exc:
            last_error = exc

    raise ValidationError(schema_name, ticker, [f"Response is not valid JSON: {last_error}"]) from last_error


def analyze_dip(
    ticker: str,
    mda_text: str,
    risk_factors_current: str,
    risk_factors_prior: str | None = None,
    macro_context: str = "",
) -> DipAnalysisOutput:
    """Request dip classification from MiMo 2.5.

    Raises MimoNotConfiguredError if MIMO_API_KEY is not set.
    Raises schemas.ValidationError if output fails schema validation.
    """
    _assert_configured()

    prompt = (
        f"Stock: {ticker}\n"
        f"Macro context: {macro_context}\n\n"
        f"=== MD&A ===\n{mda_text[:3000]}\n\n"
        f"=== Current Risk Factors ===\n{risk_factors_current[:3000]}\n\n"
    )
    if risk_factors_prior:
        prompt += f"=== Prior Year Risk Factors (for diff) ===\n{risk_factors_prior[:3000]}\n\n"

    prompt += (
        "### Few-shot examples (do NOT copy these; classify the stock above) ###\n\n"
        "Example 1 — TRANSIENT\n"
        "Stock: DEF  Macro: Rates rising moderately\n"
        "MD&A: Revenue grew 8% YoY driven by new product lines.  One-time legal settlement of $120M "
        "depressed net income this quarter.  Core operating margins expanded 60 bps.\n"
        "Risk factors (current vs prior): Unchanged — litigation risk removed after settlement.\n"
        'Expected output: {"ticker":"DEF","classification":"TRANSIENT","confidence":0.82,'
        '"evidence_summary":"One-time legal charge caused the dip; underlying business is healthy.",'
        '"key_risks":["none material"],"transience_argument":"Settlement removes the overhang; '
        'earnings will normalise next quarter.","structural_flags":[]}\n\n'

        "Example 2 — STRUCTURAL\n"
        "Stock: GHI  Macro: Rates elevated\n"
        "MD&A: Revenue declined 18% YoY as key product line faces permanent commoditisation.  "
        "Gross margin compressed from 52% to 31%.  Management is unable to identify a path back "
        "to prior margin levels.  Customer churn accelerated.\n"
        "Risk factors (current vs prior): New risk added: 'Inability to compete with lower-cost '  "
        "'alternatives may permanently impair our business model.'\n"
        'Expected output: {"ticker":"GHI","classification":"STRUCTURAL","confidence":0.91,'
        '"evidence_summary":"Permanent commoditisation and margin collapse indicate structural impairment.",'
        '"key_risks":["commoditisation","customer churn"],'
        '"transience_argument":"No credible path to margin recovery described.",'
        '"structural_flags":["margin collapse","commoditisation risk added"]}\n\n'

        "Example 3 — CYCLICAL_IDIOSYNCRATIC\n"
        "Stock: JKL  Macro: Mild slowdown\n"
        "MD&A: Revenue down 12% due to inventory de-stocking by major retailers following a "
        "demand spike in the prior year.  Long-term demand for the product category is intact.  "
        "Management expects normalisation within 2-3 quarters.\n"
        "Risk factors (current vs prior): No new structural risks added.\n"
        'Expected output: {"ticker":"JKL","classification":"CYCLICAL_IDIOSYNCRATIC","confidence":0.79,'
        '"evidence_summary":"De-stocking cycle is the driver; category demand intact.",'
        '"key_risks":["re-stocking timing uncertain"],'
        '"transience_argument":"Inventory cycles typically resolve in 2-3 quarters; no structural damage.",'
        '"structural_flags":[]}\n\n'

        "Example 4 — CYCLICAL_MACRO (mixed geopolitical + demand signals)\n"
        "Stock: MNO  Macro: RANGING, mild growth slowdown\n"
        "MD&A: Systemwide comparable sales declined 1% driven by a 4% decline in international "
        "markets, primarily China and the Middle East.  US comparable sales were flat.  The company "
        "is investing in value menu and digital ordering to recover traffic.  Long-term franchise "
        "economics remain intact with 95% franchised model.\n"
        "Risk factors (current vs prior): New risk added: 'Consumer boycotts and geopolitical "
        "tensions in certain international markets may continue to pressure comparable sales.' "
        "Prior year had no geopolitical risk language.\n"
        'Expected output: {"ticker":"MNO","classification":"CYCLICAL_MACRO","confidence":0.70,'
        '"evidence_summary":"China/Middle East demand weakness is geopolitical and cyclical, not structural. '
        'US business flat. Franchise model intact.",'
        '"key_risks":["China recovery timing","Middle East boycott duration"],'
        '"transience_argument":"Geopolitical boycotts are transient events; franchise economics '
        'are not impaired. US value menu investments should restore traffic.",'
        '"structural_flags":[]}\n\n'

        "Example 5 — CYCLICAL_IDIOSYNCRATIC (segment weakness that looks structural but isn't)\n"
        "Stock: PQR  Macro: EXPANSION\n"
        "MD&A: Revenue declined 8% YoY driven by a 22% decline in the China segment due to "
        "government hospital spending cuts and a voluntary product recall affecting two product "
        "lines.  Excluding China and the recalled products, revenue grew 3%.  The diabetes and "
        "cardiovascular segments grew 5% and 2% respectively.  Management reaffirmed full-year "
        "guidance excluding China headwinds.\n"
        "Risk factors (current vs prior): New risk: 'Government healthcare spending austerity in "
        "China may persist through FY2026.' Risk on product quality was already present in prior year.\n"
        'Expected output: {"ticker":"PQR","classification":"CYCLICAL_IDIOSYNCRATIC","confidence":0.68,'
        '"evidence_summary":"China spending cuts and product recall are idiosyncratic. Core segments '
        'growing. Management guidance intact ex-China.",'
        '"key_risks":["China spending cuts duration","recall resolution timeline"],'
        '"transience_argument":"China austerity is a government budget cycle issue, not permanent. '
        'Recall is a transient quality event. Core franchise growth confirms business health.",'
        '"structural_flags":[]}\n\n'

        "### End of examples ###\n\n"

        "Classify the nature of this stock's current price decline. "
        "Return JSON matching this schema: "
        '{"ticker":"...","classification":"TRANSIENT|TRANSIENT_MACRO_AMPLIFIED|'
        'CYCLICAL_IDIOSYNCRATIC|CYCLICAL_MACRO|STRUCTURAL|STRUCTURAL_MACRO_EXPOSED|UNKNOWN",'
        '"confidence":0.0-1.0,"evidence_summary":"...","key_risks":[],'
        '"transience_argument":"...","structural_flags":[]}'
        "\n\nReturn only the JSON object. No markdown, no explanation, no code fences."
    )

    raw = _call_api("chat/completions", {
        "model": get_settings().mimo_model,
        "thinking": True,
        "messages": [{"role": "user", "content": prompt}],
    })

    # Extract content from response
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    # Parse JSON from response — MiMo 2.5 should return structured output.
    result_dict = _parse_json_response(content, "DipAnalysisOutput", ticker)

    # MANDATORY schema validation before returning
    try:
        return validate_dip_analysis(result_dict, ticker)
    except ValidationError as first_err:
        # Retry once with corrective prompt
        logger.warning("%s: Schema validation failed (%s), retrying with corrective prompt", ticker, first_err)
        corrective_prompt = (
            f"Your previous response for {ticker} had a validation error: {first_err}\n\n"
            f"Original response:\n{content[:500]}\n\n"
            "Please return a valid JSON object matching the schema exactly. "
            "Ensure all required fields are present and within valid ranges. "
            "Return only the JSON object. No markdown, no explanation, no code fences."
        )
        retry_raw = _call_api("chat/completions", {
            "model": get_settings().mimo_model,
            "thinking": True,
            "messages": [{"role": "user", "content": corrective_prompt}],
        })
        retry_content = retry_raw.get("choices", [{}])[0].get("message", {}).get("content", "")
        retry_dict = _parse_json_response(retry_content, "DipAnalysisOutput", ticker)
        return validate_dip_analysis(retry_dict, ticker)


def extract_filing_data(
    ticker: str,
    filing_text: str,
    fiscal_year: int,
    period_of_report: str,
) -> FilingExtractionOutput:
    """Extract structured data from a 10-K or 10-Q filing using MiMo 2.5.

    Raises MimoNotConfiguredError if MIMO_API_KEY is not set.
    """
    _assert_configured()

    raw = _call_api("chat/completions", {
        "model": get_settings().mimo_model,
        "thinking": True,
        "messages": [{
            "role": "user",
            "content": (
                f"Stock: {ticker}, FY: {fiscal_year}, Period: {period_of_report}\n\n"
                f"{filing_text[:8000]}\n\n"
                "Extract structured data from this SEC filing. Return JSON matching: "
                '{"ticker":"...","fiscal_year":N,"period_of_report":"...","mda_summary":"...",'
                '"key_risks":[],"material_changes":[],"revenue_drivers":[],"cost_headwinds":[]}'
            ),
        }],
    })

    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    result_dict = _parse_json_response(content, "FilingExtractionOutput", ticker)

    return validate_filing_extraction(result_dict, ticker)


# ---------------------------------------------------------------------------
# Filing content hash — used for cache invalidation
# ---------------------------------------------------------------------------


def compute_filing_hash(mda_text: str, risk_current: str, risk_prior: str | None = None) -> str:
    """Compute a SHA-256 hash of the filing content for cache invalidation.

    The hash changes when any of the input texts change, ensuring cached
    MiMo classifications are invalidated when filings are updated.
    """
    content = f"{mda_text}|{risk_current}|{risk_prior or ''}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Batch dip classification — single API call for multiple stocks
# ---------------------------------------------------------------------------


_BATCH_SYSTEM_PROMPT = """You are a financial analyst classifying stock price dips.
For EACH stock below, classify the nature of its current price decline.

Return a JSON ARRAY of objects, one per stock, matching this schema for each:
{"ticker":"...","classification":"TRANSIENT|TRANSIENT_MACRO_AMPLIFIED|CYCLICAL_IDIOSYNCRATIC|CYCLICAL_MACRO|STRUCTURAL|STRUCTURAL_MACRO_EXPOSED|UNKNOWN","confidence":0.0-1.0,"evidence_summary":"...","key_risks":[],"transience_argument":"...","structural_flags":[]}

Rules:
- Return ONLY the JSON array. No markdown, no explanation, no code fences.
- Include exactly one object per input stock.
- confidence must be 0.0-1.0.
- evidence_summary min 10 chars, transience_argument min 5 chars.
- Do NOT copy the examples; classify based on the actual data provided."""


def _build_stock_block(
    ticker: str,
    mda_text: str,
    risk_factors_current: str,
    risk_factors_prior: str | None,
    macro_context: str,
    max_chars: int,
) -> str:
    """Build a single stock's text block for the batch prompt."""
    parts = [f"### Stock: {ticker}"]
    if macro_context:
        parts.append(f"Macro: {macro_context}")
    parts.append(f"MD&A: {mda_text[:max_chars]}")
    parts.append(f"Risk Factors (current): {risk_factors_current[:max_chars]}")
    if risk_factors_prior:
        parts.append(f"Risk Factors (prior year, for diff): {risk_factors_prior[:max_chars]}")
    return "\n".join(parts)


def analyze_dip_batch(
    stock_data: list[dict[str, Any]],
    batch_size: int | None = None,
    max_chars_per_stock: int | None = None,
    max_concurrent: int | None = None,
) -> dict[str, DipAnalysisOutput]:
    """Batch dip classification — processes multiple stocks in fewer API calls.

    Instead of calling MiMo N times (once per stock), this function chunks
    stocks into batches and sends each batch as a single API call. Multiple
    chunks can be processed in parallel using ThreadPoolExecutor.

    Args:
        stock_data: List of dicts, each with keys:
            - ticker: str
            - mda_text: str
            - risk_factors_current: str
            - risk_factors_prior: str | None
            - macro_context: str
        batch_size: Stocks per API call (default from config)
        max_chars_per_stock: Max chars per section (default from config)
        max_concurrent: Max parallel API calls (default from config)

    Returns:
        Dict mapping ticker -> DipAnalysisOutput for successfully classified stocks.
        Stocks that fail validation are logged and skipped (not included in result).

    Raises:
        MimoNotConfiguredError: If MIMO_API_KEY is not set.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _assert_configured()

    cfg = get_settings().mimo_batch
    bs = batch_size or cfg.batch_size
    mc = max_chars_per_stock or cfg.max_chars_per_stock
    concurrency = max_concurrent or cfg.max_concurrent_batches

    # Build all chunks upfront
    chunks: list[list[dict[str, Any]]] = []
    for chunk_start in range(0, len(stock_data), bs):
        chunks.append(stock_data[chunk_start : chunk_start + bs])

    logger.info("Batch MiMo: %d stocks in %d chunks (batch_size=%d, concurrency=%d)",
                len(stock_data), len(chunks), bs, concurrency)

    def _process_chunk(chunk_idx: int, chunk: list[dict[str, Any]]) -> dict[str, DipAnalysisOutput]:
        """Process a single chunk — called from thread pool."""
        chunk_tickers = [s["ticker"] for s in chunk]
        chunk_label = "/".join(chunk_tickers[:5]) + ("..." if len(chunk_tickers) > 5 else "")
        logger.info("Batch MiMo chunk %d/%d [%s]: starting...", chunk_idx + 1, len(chunks), chunk_label)

        # Build the batch prompt
        stock_blocks = []
        for s in chunk:
            block = _build_stock_block(
                ticker=s["ticker"],
                mda_text=s.get("mda_text", ""),
                risk_factors_current=s.get("risk_factors_current", ""),
                risk_factors_prior=s.get("risk_factors_prior"),
                macro_context=s.get("macro_context", ""),
                max_chars=mc,
            )
            stock_blocks.append(block)

        prompt = (
            _BATCH_SYSTEM_PROMPT
            + "\n\n---\n\n"
            + "\n\n---\n\n".join(stock_blocks)
            + "\n\n---\n\n"
            + f"Classify all {len(chunk)} stocks. Return a JSON array with exactly {len(chunk)} objects."
        )

        logger.info("Batch MiMo [%s]: sending prompt (%d chars)...", chunk_label, len(prompt))

        chunk_results: dict[str, DipAnalysisOutput] = {}

        try:
            raw = _call_api("chat/completions", {
                "model": get_settings().mimo_model,
                "thinking": True,
                "messages": [{"role": "user", "content": prompt}],
            })

            content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = _parse_batch_json_response(content, chunk_tickers)

            # Validate each result individually
            for item in parsed:
                ticker = item.get("ticker", "UNKNOWN")
                try:
                    validated = validate_dip_analysis(item, ticker)
                    chunk_results[ticker] = validated
                except ValidationError as ve:
                    logger.warning("Batch MiMo: %s failed schema validation: %s", ticker, ve)
                    # Retry this one individually with corrective prompt
                    try:
                        single_result = analyze_dip(
                            ticker=ticker,
                            mda_text=next(s["mda_text"] for s in chunk if s["ticker"] == ticker),
                            risk_factors_current=next(s["risk_factors_current"] for s in chunk if s["ticker"] == ticker),
                            risk_factors_prior=next((s.get("risk_factors_prior") for s in chunk if s["ticker"] == ticker), None),
                            macro_context=next((s.get("macro_context", "") for s in chunk if s["ticker"] == ticker), ""),
                        )
                        chunk_results[ticker] = single_result
                    except Exception as retry_exc:
                        logger.error("Batch MiMo: individual retry for %s also failed: %s", ticker, retry_exc)

        except Exception as exc:
            logger.error("Batch MiMo chunk %d [%s] failed: %s", chunk_idx + 1, chunk_label, exc)
            # Fall back to individual calls for this chunk
            for s in chunk:
                try:
                    single_result = analyze_dip(
                        ticker=s["ticker"],
                        mda_text=s.get("mda_text", ""),
                        risk_factors_current=s.get("risk_factors_current", ""),
                        risk_factors_prior=s.get("risk_factors_prior"),
                        macro_context=s.get("macro_context", ""),
                    )
                    chunk_results[s["ticker"]] = single_result
                except Exception as single_exc:
                    logger.error("Batch MiMo: fallback individual call for %s failed: %s", s["ticker"], single_exc)

        logger.info("Batch MiMo chunk %d/%d [%s]: done (%d/%d classified)",
                     chunk_idx + 1, len(chunks), chunk_label, len(chunk_results), len(chunk))
        return chunk_results

    # Process chunks in parallel (or sequential if concurrency=1)
    results: dict[str, DipAnalysisOutput] = {}

    if concurrency <= 1:
        # Sequential processing (original behavior)
        for idx, chunk in enumerate(chunks):
            chunk_results = _process_chunk(idx, chunk)
            results.update(chunk_results)
    else:
        # Parallel processing with ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_idx = {
                executor.submit(_process_chunk, idx, chunk): idx
                for idx, chunk in enumerate(chunks)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    chunk_results = future.result()
                    results.update(chunk_results)
                except Exception as exc:
                    logger.error("Batch MiMo chunk %d thread raised: %s", idx + 1, exc)

    logger.info("Batch MiMo complete: %d/%d stocks classified", len(results), len(stock_data))
    return results


def _parse_batch_json_response(content: str, expected_tickers: list[str]) -> list[dict]:
    """Parse a MiMo batch response that should contain a JSON array.

    Handles common response formats: raw array, fenced code block, mixed text.
    """
    stripped = content.strip()
    if not stripped:
        return []

    candidates: list[str] = [stripped]

    # Try fenced code block first
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())

    # Try finding array start
    first_bracket = stripped.find("[")
    if first_bracket != -1:
        candidates.append(stripped[first_bracket:])

    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            result, _ = decoder.raw_decode(candidate)
            if isinstance(result, list):
                return result
        except Exception:
            continue

    # Last resort: try to find individual JSON objects and collect them
    objects = []
    for match in re.finditer(r'\{[^{}]*\}', stripped):
        try:
            obj = decoder.raw_decode(match.group())[0]
            if isinstance(obj, dict) and "ticker" in obj:
                objects.append(obj)
        except Exception:
            continue

    return objects
