from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray


EmbeddingVector = NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class EmbeddingModelInfo:
    provider: str
    model: str
    dimension: int


@runtime_checkable
class EmbeddingModel(Protocol):
    @property
    def info(self) -> EmbeddingModelInfo:
        """Return provider-neutral model metadata for cache identity."""

    def encode_query(self, query: str) -> EmbeddingVector:
        """Encode a search query using the model's query path."""

    def encode_document(self, text: str) -> EmbeddingVector:
        """Encode OCR chunk text using the model's document path."""


def normalize_embedding_vector(
    vector: Sequence[float] | NDArray[np.floating],
    *,
    dimension: int,
) -> EmbeddingVector:
    if dimension <= 0:
        raise ValueError("embedding dimension must be greater than zero")

    try:
        array = np.asarray(vector, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("embedding vector must contain numeric values") from exc

    if array.ndim != 1:
        raise ValueError("embedding vector must be one-dimensional")
    if array.shape[0] < dimension:
        raise ValueError(
            "embedding vector has "
            f"{array.shape[0]} dimensions; expected at least {dimension}"
        )
    if not np.all(np.isfinite(array)):
        raise ValueError("embedding vector must contain only finite values")

    normalized = array.astype(np.float32, copy=True)
    full_norm = float(np.linalg.norm(normalized))
    if not np.isfinite(full_norm) or full_norm <= 0.0:
        raise ValueError("embedding vector norm must be greater than zero")
    normalized /= full_norm

    truncated = np.ascontiguousarray(normalized[:dimension], dtype=np.float32)
    truncated_norm = float(np.linalg.norm(truncated))
    if not np.isfinite(truncated_norm) or truncated_norm <= 0.0:
        raise ValueError(
            "truncated embedding vector norm must be greater than zero"
        )
    truncated /= truncated_norm
    return truncated
