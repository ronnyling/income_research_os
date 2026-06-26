"""EDGAR full-text filing client — rule-based, no LLM.

Downloads and parses 10-K / 10-Q primary documents for:
  - MD&A  (Item 7 in 10-K, Item 2 in 10-Q)
  - Risk Factors (Item 1A in 10-K)
  - Recent 8-K event metadata (for KIV demotion triggers)

Section extraction uses HTML-to-text conversion + regex section-header matching.
Works for the majority of EDGAR HTM filings.  Complex multi-document iXBRL
submissions may return shorter or partial sections — the `truncated` flag signals this.

Per architecture:
  - This module is rule-based.  MiMo receives the extracted text; it does NOT
    decide whether to fetch it.
  - YoY Risk Factor diff (prior vs current 10-K) is the highest-value MiMo use case.
    Both years are fetched before calling llm.mimo.analyze_risk_factor_diff().
  - 8-K events are checked at Stage 1→2 to trigger immediate KIV demotion for
    material negative events (fraud, dividend suspension, restatement).
"""

from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass, field

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
_EDGAR_DATA = "https://data.sec.gov"
_HEADERS = {"User-Agent": "IncomOS research@incomos.local"}
_RATE_DELAY = 0.2            # 200 ms between requests → 5 req/s (EDGAR policy)
_MAX_SECTION_CHARS = 40_000  # ~10 k tokens at 4 chars/token


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class FilingSection:
    ticker: str
    cik: str
    form_type: str        # 10-K | 10-Q | 8-K
    filing_date: str      # YYYY-MM-DD
    accession_number: str
    section_name: str     # RISK_FACTORS | MDAA | FULL_TEXT
    text: str
    word_count: int = field(init=False)
    truncated: bool = False

    def __post_init__(self) -> None:
        self.word_count = len(self.text.split())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_submissions(cik: str) -> dict:
    cik_padded = cik.zfill(10)
    url = f"{_EDGAR_DATA}/submissions/CIK{cik_padded}.json"
    time.sleep(_RATE_DELAY)
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_HEADERS)
        resp.raise_for_status()
    return resp.json()


def _find_recent_filings(submissions: dict, form_type: str, limit: int = 2) -> list[dict]:
    """Return metadata for the most recent N filings of the given form type."""
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    results: list[dict] = []
    for i, form in enumerate(forms):
        if form in (form_type, f"{form_type}/A"):
            results.append({
                "form_type": form,
                "filing_date": dates[i] if i < len(dates) else "",
                "accession_number": accessions[i] if i < len(accessions) else "",
                "primary_document": primary_docs[i] if i < len(primary_docs) else "",
            })
            if len(results) >= limit:
                break
    return results


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def _fetch_document(cik: str, accession_number: str, document_name: str) -> str:
    """Fetch a filing primary document from EDGAR Archives."""
    acc_clean = accession_number.replace("-", "")
    url = f"{_EDGAR_ARCHIVES}/{cik}/{acc_clean}/{document_name}"
    time.sleep(_RATE_DELAY)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resp = client.get(url, headers=_HEADERS)
        resp.raise_for_status()
    return resp.text


def _clean_html(text: str) -> str:
    """Strip HTML/iXBRL tags and collapse whitespace.

    iXBRL filings embed XBRL context/unit definitions in the document body
    (inside <ix:header> or <head>) that can contain text matching section-header
    patterns.  These blocks must be removed before section extraction.
    """
    # Remove <head> section (metadata, XBRL schema links, context definitions)
    text = re.sub(r"<head[^>]*>.*?</head>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove iXBRL header block (context/unit/reference definitions inside <body>)
    text = re.sub(r"<ix:header[^>]*>.*?</ix:header>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove script/style blocks
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags (including iXBRL namespaced tags like <ix:nonNumeric>)
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode entities like &#8217; and &nbsp; before regex section matching.
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Section header patterns — ordered by specificity (most specific first)
_SECTION_PATTERNS: dict[str, list[str]] = {
    "RISK_FACTORS": [
        r"(?i)item\s*1a[\.\-\s]\s*risk\s+factors",
        r"(?i)risk\s+factors",
    ],
    "MDAA": [
        r"(?i)item\s*7[\.\-\s]\s*management['\u2019s\s]+discussion",
        r"(?i)management['\u2019s\s]+discussion\s+and\s+analysis",
    ],
}

_NEXT_ITEM = re.compile(r"(?i)\bitem\s+\d+[a-z]?\b[\.\-\s]", re.MULTILINE)

def _find_body_document_via_index(cik: str, accession_number: str) -> str | None:
    """Fetch EDGAR filing index to locate the main 10-K body document.

    Used when the primary document doesn\'t yield extractable sections.
    Looks for the document with Type = \'10-K\' in the filing index table.
    Returns the filename (not full URL), or None if not found.
    """
    cik_int = str(int(cik))  # archive URLs use CIK without leading zeros
    acc_clean = accession_number.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data"
        f"/{cik_int}/{acc_clean}/{accession_number}-index.htm"
    )
    time.sleep(_RATE_DELAY)
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(index_url, headers=_HEADERS)
            resp.raise_for_status()
    except Exception as exc:
        logger.debug("Filing index fetch failed (%s): %s", accession_number, exc)
        return None

    # The EDGAR filing index table has rows with the actual 10-K body document,
    # but the link may appear as /ix?doc=/Archives/.../<file>.htm and may carry
    # extra markup in the same cell. Parse rows rather than relying on a fixed
    # column order.
    for row_match in re.finditer(r"(?is)<tr[^>]*>(.*?)</tr>", resp.text):
        row = row_match.group(1)
        if not re.search(r"(?i)\b10-k\b", row):
            continue

        href_match = re.search(r'(?is)<a[^>]+href="([^"]+?)"', row)
        if not href_match:
            continue

        href = html.unescape(href_match.group(1))
        if "doc=" in href:
            href = href.split("doc=", 1)[1]
        filename = href.split("/")[-1]
        if filename.endswith(".htm") or filename.endswith(".html"):
            logger.debug("Filing index resolved body document: %s", filename)
            return filename

    logger.debug("No 10-K body document found in filing index for %s", accession_number)
    return None

def _extract_section(text: str, section_name: str) -> tuple[str, bool]:
    """Locate a section in clean text and return (content, truncated)."""
    patterns = _SECTION_PATTERNS.get(section_name, [])
    best_section = ""
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            section_text = text[m.start():]

            # End of section = next "Item X" heading that is not the current one.
            matches = list(_NEXT_ITEM.finditer(section_text))
            if len(matches) > 1:
                section_text = section_text[: matches[1].start()]

            section_text = section_text.strip()
            if len(section_text) > len(best_section):
                best_section = section_text

    if not best_section:
        return "", False

    truncated = False
    if len(best_section) > _MAX_SECTION_CHARS:
        best_section = best_section[:_MAX_SECTION_CHARS]
        truncated = True

    return best_section, truncated


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------

class FilingsClient:
    """EDGAR full-text filing client.

    Rule-based.  No LLM calls.  Returns FilingSection objects for downstream
    MiMo analysis.
    """

    def get_filing_sections(
        self,
        ticker: str,
        cik: str,
        form_type: str = "10-K",
        sections: tuple[str, ...] = ("RISK_FACTORS", "MDAA"),
        filing_index: int = 0,  # 0 = most recent, 1 = prior year
    ) -> dict[str, FilingSection]:
        """Download and extract named sections from an EDGAR filing.

        Returns a dict keyed by section_name.  Missing sections have empty text
        rather than raising.  An empty dict is returned if the filing cannot be
        downloaded.
        """
        try:
            submissions = _get_submissions(cik)
        except Exception as exc:
            logger.error("%s: failed to fetch EDGAR submissions: %s", ticker, exc)
            return {}

        filings = _find_recent_filings(submissions, form_type, limit=filing_index + 1)
        if filing_index >= len(filings):
            logger.warning(
                "%s: requested filing index %d but only %d %s filings found",
                ticker, filing_index, len(filings), form_type,
            )
            return {}

        filing = filings[filing_index]
        if not filing["primary_document"]:
            logger.warning("%s: no primary_document in %s submission", ticker, form_type)
            return {}

        try:
            raw_html = _fetch_document(cik, filing["accession_number"], filing["primary_document"])
        except Exception as exc:
            logger.error("%s: failed to fetch filing document: %s", ticker, exc)
            return {}

        clean_text = _clean_html(raw_html)

        # Gap F: if the primary document is an iXBRL cover/index page it may yield
        # no recognisable section text.  In that case look up the actual 10-K body
        # document via the filing index and retry.
        extracted = {s: _extract_section(clean_text, s) for s in sections}
        if all(not text for text, _ in extracted.values()):
            body_doc = _find_body_document_via_index(cik, filing["accession_number"])
            if body_doc and body_doc != filing["primary_document"]:
                logger.info(
                    "%s: primary doc yielded no sections; retrying with body doc %s",
                    ticker, body_doc,
                )
                try:
                    raw_html = _fetch_document(cik, filing["accession_number"], body_doc)
                    clean_text = _clean_html(raw_html)
                    extracted = {s: _extract_section(clean_text, s) for s in sections}
                except Exception as exc:
                    logger.warning(
                        "%s: fallback body document %s failed: %s", ticker, body_doc, exc
                    )

        result: dict[str, FilingSection] = {}
        for section in sections:
            text, truncated = extracted[section]
            result[section] = FilingSection(
                ticker=ticker,
                cik=cik,
                form_type=filing["form_type"],
                filing_date=filing["filing_date"],
                accession_number=filing["accession_number"],
                section_name=section,
                text=text,
                truncated=truncated,
            )

        return result

    def get_yoy_sections(
        self,
        ticker: str,
        cik: str,
        sections: tuple[str, ...] = ("RISK_FACTORS", "MDAA"),
    ) -> tuple[dict[str, FilingSection], dict[str, FilingSection]]:
        """Fetch the same sections from the current AND prior year 10-K.

        Returns (current_sections, prior_sections).  Prior may be empty if only
        one 10-K is available on EDGAR.  This is the primary input for MiMo's
        YoY Risk Factor diff — the highest-value filing analysis use case.
        """
        current = self.get_filing_sections(ticker, cik, "10-K", sections, filing_index=0)
        prior = self.get_filing_sections(ticker, cik, "10-K", sections, filing_index=1)
        return current, prior

    def get_recent_8k_events(
        self,
        ticker: str,
        cik: str,
        limit: int = 5,
    ) -> list[dict]:
        """Return metadata for recent 8-K filings.

        Used at Stage 1→2 to detect KIV demotion triggers:
          - Dividend suspension / reduction
          - Material restatement / fraud
          - Going-concern warnings
        The caller is responsible for classifying the 8-K content.
        """
        try:
            submissions = _get_submissions(cik)
        except Exception as exc:
            logger.error("%s: failed to fetch EDGAR submissions for 8-K check: %s", ticker, exc)
            return []

        filings = _find_recent_filings(submissions, "8-K", limit=limit)
        return [
            {
                "ticker": ticker,
                "form_type": f["form_type"],
                "filing_date": f["filing_date"],
                "accession_number": f["accession_number"],
            }
            for f in filings
        ]
