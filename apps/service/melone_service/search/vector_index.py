from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from melone_service.embeddings.model import EmbeddingVector
from melone_service.store.embeddings import (
    EmbeddingRepository,
    OcrChunkEmbeddingSearchRow,
)
from melone_service.store.search import DEFAULT_SCREEN_SEARCH_LIMIT


DEFAULT_VECTOR_INDEX_SCAN_BATCH_SIZE = 256


@dataclass(frozen=True, slots=True)
class SemanticSearchCandidate:
    chunk_id: str
    session_id: str
    frame_id: str
    source_key: str
    retrieval_locator: str
    app_name: str | None
    window_title: str | None
    url: str | None
    session_started_at: str
    session_ended_at: str | None
    chunk_created_at: str
    text: str
    embedding_similarity: float
    embedding_relevance: float


@runtime_checkable
class VectorIndex(Protocol):
    def search(
        self,
        query_embedding: EmbeddingVector,
        *,
        model: str,
        dimension: int,
        limit: int = DEFAULT_SCREEN_SEARCH_LIMIT,
        since: str | None = None,
    ) -> list[SemanticSearchCandidate]:
        """Return nearest OCR chunk candidates for a normalized query vector."""


class SqliteExactVectorIndex:
    def __init__(
        self,
        repository: EmbeddingRepository,
        *,
        scan_batch_size: int = DEFAULT_VECTOR_INDEX_SCAN_BATCH_SIZE,
    ) -> None:
        if scan_batch_size <= 0:
            raise ValueError("vector index scan batch size must be greater than zero")

        self.repository = repository
        self.scan_batch_size = scan_batch_size

    def search(
        self,
        query_embedding: EmbeddingVector,
        *,
        model: str,
        dimension: int,
        limit: int = DEFAULT_SCREEN_SEARCH_LIMIT,
        since: str | None = None,
    ) -> list[SemanticSearchCandidate]:
        if limit <= 0:
            return []

        query_vector = _coerce_query_embedding(query_embedding, dimension=dimension)
        heap: list[tuple[tuple[float, float, int], SemanticSearchCandidate]] = []
        # TODO: Replace the exact SQLite scan with an approximate/vector-index
        # backend once packaging for sqlite-vec, FAISS, or LanceDB is settled.
        for row_index, row in enumerate(
            self.repository.iter_embeddings_for_search(
                model=model,
                dimension=dimension,
                since=since,
                batch_size=self.scan_batch_size,
            )
        ):
            candidate = _semantic_candidate_from_row(row, query_vector=query_vector)
            rank_key = (
                candidate.embedding_relevance,
                candidate.embedding_similarity,
                -row_index,
            )
            item = (rank_key, candidate)
            if len(heap) < limit:
                heapq.heappush(heap, item)
            else:
                heapq.heappushpop(heap, item)

        return [
            candidate
            for _, candidate in sorted(
                heap,
                key=lambda item: item[0],
                reverse=True,
            )
        ]


def cosine_similarity_to_relevance(similarity: float) -> float:
    """Map cosine similarity from [-1, 1] to a [0, 1] relevance score."""
    return (clamp_cosine_similarity(similarity) + 1.0) / 2.0


def clamp_cosine_similarity(similarity: float) -> float:
    value = float(similarity)
    if not np.isfinite(value):
        return -1.0
    return max(-1.0, min(1.0, value))


def _semantic_candidate_from_row(
    row: OcrChunkEmbeddingSearchRow,
    *,
    query_vector: EmbeddingVector,
) -> SemanticSearchCandidate:
    similarity = clamp_cosine_similarity(np.dot(query_vector, row.embedding))
    return SemanticSearchCandidate(
        chunk_id=row.chunk_id,
        session_id=row.session_id,
        frame_id=row.frame_id,
        source_key=row.source_key,
        retrieval_locator=row.retrieval_locator,
        app_name=row.app_name,
        window_title=row.window_title,
        url=row.url,
        session_started_at=row.session_started_at,
        session_ended_at=row.session_ended_at,
        chunk_created_at=row.chunk_created_at,
        text=row.text,
        embedding_similarity=similarity,
        embedding_relevance=cosine_similarity_to_relevance(similarity),
    )


def _coerce_query_embedding(
    query_embedding: EmbeddingVector,
    *,
    dimension: int,
) -> EmbeddingVector:
    query_vector = np.asarray(query_embedding, dtype=np.float32)
    if query_vector.ndim != 1:
        raise ValueError("query embedding vector must be one-dimensional")
    if query_vector.shape[0] != dimension:
        raise ValueError(
            f"query embedding vector has {query_vector.shape[0]} dimensions; "
            f"expected {dimension}"
        )
    if not np.all(np.isfinite(query_vector)):
        raise ValueError("query embedding vector must contain only finite values")

    norm = float(np.linalg.norm(query_vector))
    if not np.isfinite(norm) or not np.isclose(norm, 1.0, rtol=1e-4, atol=1e-6):
        raise ValueError("query embedding vector must be normalized")

    return np.ascontiguousarray(query_vector, dtype=np.float32)
