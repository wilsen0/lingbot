"""Error types for LLM operations."""

from __future__ import annotations


class LLMError(Exception):
    """Base error for LLM operations."""


class LLMRateLimitError(LLMError):
    """Rate limit exceeded."""

    def __init__(self, message: str = "Rate limit exceeded", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMAuthError(LLMError):
    """Authentication failed."""
