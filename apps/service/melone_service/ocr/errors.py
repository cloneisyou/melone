from __future__ import annotations


class OcrError(Exception):
    """Base error for provider-neutral OCR failures."""


class OcrTimeoutError(OcrError):
    """Raised when the OCR provider does not respond before the request timeout."""


class OcrUnavailableError(OcrError):
    """Raised when the OCR provider is temporarily unavailable."""
