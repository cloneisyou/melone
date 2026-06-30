from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

import numpy as np

from .model import (
    EmbeddingModelInfo,
    EmbeddingVector,
    normalize_embedding_vector,
)


FAKE_EMBEDDING_PROVIDER = "fake"


class FakeEmbeddingModel:
    def __init__(
        self,
        *,
        model: str = "fake-embedding-model",
        dimension: int = 8,
        seed: str = "melone-fake-embedding",
        query_vectors: Mapping[str, Sequence[float]] | None = None,
        document_vectors: Mapping[str, Sequence[float]] | None = None,
    ) -> None:
        if dimension <= 0:
            raise ValueError("embedding dimension must be greater than zero")

        self.model_name = model
        self.dimension = dimension
        self.seed = seed
        self.query_vectors = dict(query_vectors or {})
        self.document_vectors = dict(document_vectors or {})
        self.query_calls: list[str] = []
        self.document_calls: list[str] = []

    @property
    def info(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(
            provider=FAKE_EMBEDDING_PROVIDER,
            model=self.model_name,
            dimension=self.dimension,
        )

    def encode_query(self, query: str) -> EmbeddingVector:
        self.query_calls.append(query)
        vector = self.query_vectors.get(query)
        if vector is None:
            vector = self._deterministic_vector("query", query)
        return normalize_embedding_vector(vector, dimension=self.dimension)

    def encode_document(self, text: str) -> EmbeddingVector:
        self.document_calls.append(text)
        vector = self.document_vectors.get(text)
        if vector is None:
            vector = self._deterministic_vector("document", text)
        return normalize_embedding_vector(vector, dimension=self.dimension)

    def _deterministic_vector(self, path: str, text: str) -> EmbeddingVector:
        values: list[float] = []
        counter = 0
        while len(values) < self.dimension:
            digest = hashlib.sha256(
                f"{self.seed}\0{path}\0{text}\0{counter}".encode("utf-8")
            ).digest()
            for offset in range(0, len(digest), 4):
                integer = int.from_bytes(digest[offset : offset + 4], "big")
                values.append((integer / 0xFFFFFFFF) * 2.0 - 1.0)
                if len(values) >= self.dimension:
                    break
            counter += 1

        return np.asarray(values, dtype=np.float32)
