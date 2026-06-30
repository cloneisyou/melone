from __future__ import annotations


class EmbeddingUnavailableError(RuntimeError):
    """Raised when an embedding provider cannot be loaded or used."""
