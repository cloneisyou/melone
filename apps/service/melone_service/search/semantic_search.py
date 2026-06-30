from __future__ import annotations

from typing import Protocol, runtime_checkable

from melone_service.embeddings.errors import EmbeddingUnavailableError
from melone_service.embeddings.model import EmbeddingModel
from melone_service.store.search import (
    DEFAULT_SCREEN_SEARCH_LIMIT,
    normalize_search_query,
)

from .vector_index import SemanticSearchCandidate, VectorIndex


@runtime_checkable
class SemanticSearchCandidateProvider(Protocol):
    def search_candidates(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SCREEN_SEARCH_LIMIT,
        since: str | None = None,
    ) -> list[SemanticSearchCandidate]:
        """Return semantic OCR chunk candidates for a search query."""


class EmbeddingSemanticSearchProvider:
    def __init__(
        self,
        *,
        model: EmbeddingModel,
        vector_index: VectorIndex,
        candidate_limit: int = DEFAULT_SCREEN_SEARCH_LIMIT,
    ) -> None:
        if candidate_limit <= 0:
            raise ValueError("semantic search candidate limit must be greater than zero")

        self.model = model
        self.vector_index = vector_index
        self.candidate_limit = candidate_limit

    def search_candidates(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SCREEN_SEARCH_LIMIT,
        since: str | None = None,
    ) -> list[SemanticSearchCandidate]:
        normalized_query = normalize_search_query(query)
        if not normalized_query or limit <= 0:
            return []

        info = self.model.info
        try:
            query_embedding = self.model.encode_query(normalized_query)
            return self.vector_index.search(
                query_embedding,
                model=info.model,
                dimension=info.dimension,
                limit=min(limit, self.candidate_limit),
                since=since,
            )
        except (EmbeddingUnavailableError, ValueError):
            return []
