"""Domain-specific exceptions for IncomOS.

Raising a typed exception rather than returning a sentinel (None, empty list,
neutral score) is the production contract for all modules.  Callers decide
whether to retry, alert, or halt — they are never silently given inaccurate data.
"""

from __future__ import annotations


class IncomOSError(Exception):
    """Base class for all IncomOS domain errors."""


class MimoNotConfiguredError(IncomOSError):
    """Raised when MiMo 2.5 analysis is required but MIMO_API_KEY is not set."""
    def __init__(self) -> None:
        super().__init__(
            "MIMO_API_KEY not configured.  "
            "MiMo 2.5 is required for dip quality scoring (Stage 2→3+).  "
            "Set MIMO_API_KEY in .env to enable full DD."
        )


class MimoAnalysisRequiredError(IncomOSError):
    """Raised when score() is called without a validated MiMo dip analysis result."""
    def __init__(self, ticker: str) -> None:
        super().__init__(
            f"{ticker}: mimo_dip_result is required for scoring.  "
            "Call llm.mimo.analyze_dip() and pass the result to score()."
        )


class MacroDataUnavailableError(IncomOSError):
    """Raised when a FRED series cannot be fetched after retries."""
    def __init__(self, series_id: str, cause: str) -> None:
        self.series_id = series_id
        super().__init__(
            f"FRED series '{series_id}' unavailable after retries: {cause}.  "
            "Macro regime cannot be determined — pipeline halted."
        )


class FXRateUnavailableError(IncomOSError):
    """Raised when USD/MYR rate from FRED DEXMAUS is unavailable (Gap D rule)."""
    def __init__(self) -> None:
        super().__init__(
            "USD/MYR rate unavailable from FRED DEXMAUS.  "
            "Position sizing for USD-denominated assets is blocked (Gap D rule).  "
            "Do not use a hardcoded fallback rate."
        )


class PriceDataUnavailableError(IncomOSError):
    """Raised when yfinance returns no usable price history for a ticker."""
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        super().__init__(
            f"No price data available for '{ticker}'.  "
            "The ticker may be delisted, mistyped, or temporarily unavailable."
        )


class DatabaseUnavailableError(IncomOSError):
    """Raised when the PostgreSQL database cannot be reached."""
    def __init__(self, cause: str) -> None:
        super().__init__(
            f"Database unavailable: {cause}.  "
            "Run PostgreSQL and verify DATABASE_URL in .env."
        )


class FilingFetchError(IncomOSError):
    """Raised when an EDGAR filing document cannot be fetched after retries."""
    def __init__(self, ticker: str, cause: str) -> None:
        self.ticker = ticker
        super().__init__(f"{ticker}: EDGAR filing fetch failed: {cause}")
