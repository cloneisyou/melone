from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from melone_service.store.context_rank import ContextRankRepository
from melone_service.store.search import (
    DEFAULT_SCREEN_SEARCH_LIMIT,
    ScreenSearchCandidate,
    ScreenSearchRepository,
    extract_query_terms,
    normalize_search_query,
)

from .semantic_search import SemanticSearchCandidateProvider
from .vector_index import SemanticSearchCandidate


DEFAULT_PREVIEW_LENGTH = 180
HYBRID_BM25_WEIGHT = 0.55
HYBRID_EMBEDDING_WEIGHT = 0.30
HYBRID_CONTEXT_RANK_WEIGHT = 0.15
FALLBACK_BM25_WEIGHT = 0.80
FALLBACK_CONTEXT_RANK_WEIGHT = 0.20
BM25_RELEVANCE_WEIGHT = FALLBACK_BM25_WEIGHT
CONTEXT_RANK_WEIGHT = FALLBACK_CONTEXT_RANK_WEIGHT


@dataclass(frozen=True)
class ScoreWeights:
    bm25: float
    embedding: float
    context_rank: float


HYBRID_SCORE_WEIGHTS = ScoreWeights(
    bm25=HYBRID_BM25_WEIGHT,
    embedding=HYBRID_EMBEDDING_WEIGHT,
    context_rank=HYBRID_CONTEXT_RANK_WEIGHT,
)
FALLBACK_SCORE_WEIGHTS = ScoreWeights(
    bm25=FALLBACK_BM25_WEIGHT,
    embedding=0.0,
    context_rank=FALLBACK_CONTEXT_RANK_WEIGHT,
)


@dataclass(frozen=True)
class ScreenSearchChunkMatch:
    chunk_id: str
    frame_id: str
    source_key: str
    retrieval_locator: str
    raw_bm25: float
    bm25_relevance: float
    embedding_similarity: float | None
    embedding_relevance: float
    context_rank: float
    final_score: float
    preview: str
    match_signals: tuple[str, ...]


@dataclass(frozen=True)
class ScreenSearchResult:
    group_key: str
    session_id: str
    source_key: str
    retrieval_locator: str | None
    app_name: str | None
    window_title: str | None
    url: str | None
    started_at: str
    ended_at: str | None
    raw_bm25: float
    bm25_relevance: float
    embedding_similarity: float | None
    embedding_relevance: float
    context_rank: float
    final_score: float
    preview: str
    match_signals: tuple[str, ...]
    chunks: tuple[ScreenSearchChunkMatch, ...] = field(default_factory=tuple)


class ScreenSearchService:
    def __init__(
        self,
        repository: ScreenSearchRepository,
        context_rank_repository: ContextRankRepository | None = None,
        *,
        context_rank_overrides: Mapping[str, float] | None = None,
        semantic_candidate_provider: SemanticSearchCandidateProvider | None = None,
        preview_length: int = DEFAULT_PREVIEW_LENGTH,
    ) -> None:
        if preview_length <= 0:
            raise ValueError("preview_length must be greater than zero")

        self.repository = repository
        self.context_rank_repository = context_rank_repository
        # Request-local callers can pass already-normalized PageRank scores so
        # OCR reranking does not depend on the background cache.
        self.context_rank_overrides = context_rank_overrides
        self.semantic_candidate_provider = semantic_candidate_provider
        self.preview_length = preview_length

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SCREEN_SEARCH_LIMIT,
        since: str | None = None,
    ) -> list[ScreenSearchResult]:
        candidates = self.repository.search_chunks(query, limit=limit, since=since)
        semantic_candidates = self.semantic_candidates(query, limit=limit, since=since)
        scored_candidates = self._score_candidates(
            candidates,
            semantic_candidates=semantic_candidates,
        )
        groups: OrderedDict[str, _MutableScreenSearchGroup] = OrderedDict()

        for scored_candidate in scored_candidates:
            candidate = scored_candidate.candidate
            group_key = _candidate_group_key(candidate)
            match = _chunk_match(
                scored_candidate,
                query=query,
                preview_length=self.preview_length,
            )
            group = groups.get(group_key)
            if group is None:
                groups[group_key] = _MutableScreenSearchGroup(
                    candidate=scored_candidate,
                    group_key=group_key,
                    chunks=[match],
                )
            else:
                group.chunks.append(match)
                if _scored_candidate_sort_key(scored_candidate) > (
                    _scored_candidate_sort_key(group.candidate)
                ):
                    group.candidate = scored_candidate

        results = [
            group.to_result(query=query, preview_length=self.preview_length)
            for group in groups.values()
        ]
        return sorted(
            results,
            key=_screen_search_result_sort_key,
        )

    def semantic_candidates(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SCREEN_SEARCH_LIMIT,
        since: str | None = None,
    ) -> list[SemanticSearchCandidate]:
        if self.semantic_candidate_provider is None:
            return []
        try:
            return self.semantic_candidate_provider.search_candidates(
                query,
                limit=limit,
                since=since,
            )
        except Exception:
            return []

    def _score_candidates(
        self,
        candidates: list[ScreenSearchCandidate],
        *,
        semantic_candidates: list[SemanticSearchCandidate] | None = None,
    ) -> list["_ScoredScreenSearchCandidate"]:
        has_semantic_signal = bool(semantic_candidates)
        weights = HYBRID_SCORE_WEIGHTS if has_semantic_signal else FALLBACK_SCORE_WEIGHTS
        combined_candidates = _union_search_candidates(
            candidates,
            semantic_candidates or [],
        )
        normalized_context_ranks = self._normalized_context_ranks(combined_candidates)
        scored_candidates: list[_ScoredScreenSearchCandidate] = []

        for candidate in combined_candidates:
            if normalized_context_ranks is None:
                # Keep the historic BM25-only score surface when no semantic signal
                # is active. In hybrid mode, missing context rank is a zero prior.
                context_rank = (
                    candidate.bm25_relevance if not has_semantic_signal else 0.0
                )
            else:
                context_rank = normalized_context_ranks.get(candidate.source_key, 0.0)

            scored_candidates.append(
                _ScoredScreenSearchCandidate(
                    candidate=candidate,
                    context_rank=context_rank,
                    final_score=(
                        weights.bm25 * candidate.bm25_relevance
                        + weights.embedding * candidate.embedding_relevance
                        + weights.context_rank * context_rank
                    ),
                )
            )

        return scored_candidates

    def _normalized_context_ranks(
        self,
        candidates: list["_CombinedScreenSearchCandidate"],
    ) -> dict[str, float] | None:
        if not candidates:
            return None

        source_keys = tuple(
            dict.fromkeys(candidate.source_key for candidate in candidates)
        )
        if self.context_rank_overrides is not None:
            return {
                source_key: _clamp_context_score(
                    self.context_rank_overrides.get(source_key, 0.0)
                )
                for source_key in source_keys
            }

        if self.context_rank_repository is None:
            return None

        scores = self.context_rank_repository.list_scores_for_source_keys(source_keys)
        if not scores:
            return None

        raw_scores = {
            source_key: scores[source_key].score if source_key in scores else 0.0
            for source_key in source_keys
        }
        return normalize_context_scores(raw_scores)


@dataclass(frozen=True)
class _CombinedScreenSearchCandidate:
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
    raw_bm25: float = 0.0
    bm25_relevance: float = 0.0
    embedding_similarity: float | None = None
    embedding_relevance: float = 0.0
    match_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ScoredScreenSearchCandidate:
    candidate: _CombinedScreenSearchCandidate
    context_rank: float
    final_score: float


@dataclass
class _MutableScreenSearchGroup:
    candidate: _ScoredScreenSearchCandidate
    group_key: str
    chunks: list[ScreenSearchChunkMatch]

    def to_result(
        self,
        *,
        query: str,
        preview_length: int,
    ) -> ScreenSearchResult:
        scored_candidate = self.candidate
        candidate = scored_candidate.candidate
        locator = _nonblank(candidate.retrieval_locator)
        return ScreenSearchResult(
            group_key=self.group_key,
            session_id=candidate.session_id,
            source_key=candidate.source_key,
            retrieval_locator=locator,
            app_name=candidate.app_name,
            window_title=candidate.window_title,
            url=candidate.url,
            started_at=candidate.session_started_at,
            ended_at=candidate.session_ended_at,
            raw_bm25=candidate.raw_bm25,
            bm25_relevance=candidate.bm25_relevance,
            embedding_similarity=candidate.embedding_similarity,
            embedding_relevance=candidate.embedding_relevance,
            context_rank=scored_candidate.context_rank,
            final_score=scored_candidate.final_score,
            preview=make_text_preview(
                candidate.text,
                query=query,
                max_length=preview_length,
            ),
            match_signals=candidate.match_signals,
            chunks=tuple(
                sorted(
                    self.chunks,
                    key=_chunk_match_sort_key,
                )
            ),
        )


def make_text_preview(
    text: str,
    *,
    query: str,
    max_length: int = DEFAULT_PREVIEW_LENGTH,
) -> str:
    if max_length <= 0:
        raise ValueError("max_length must be greater than zero")

    normalized_text = " ".join(text.split())
    if len(normalized_text) <= max_length:
        return normalized_text

    match_position = _first_match_position(normalized_text, query)
    start = 0 if match_position is None else max(0, match_position - max_length // 3)
    if start + max_length > len(normalized_text):
        start = max(0, len(normalized_text) - max_length)

    return _bounded_excerpt(normalized_text, start=start, max_length=max_length)


def _chunk_match(
    scored_candidate: _ScoredScreenSearchCandidate,
    *,
    query: str,
    preview_length: int,
) -> ScreenSearchChunkMatch:
    candidate = scored_candidate.candidate
    return ScreenSearchChunkMatch(
        chunk_id=candidate.chunk_id,
        frame_id=candidate.frame_id,
        source_key=candidate.source_key,
        retrieval_locator=candidate.retrieval_locator,
        raw_bm25=candidate.raw_bm25,
        bm25_relevance=candidate.bm25_relevance,
        embedding_similarity=candidate.embedding_similarity,
        embedding_relevance=candidate.embedding_relevance,
        context_rank=scored_candidate.context_rank,
        final_score=scored_candidate.final_score,
        preview=make_text_preview(
            candidate.text,
            query=query,
            max_length=preview_length,
        ),
        match_signals=candidate.match_signals,
    )


def _candidate_group_key(candidate: _CombinedScreenSearchCandidate) -> str:
    locator = _nonblank(candidate.retrieval_locator)
    if locator is not None:
        return locator
    return f"session:{candidate.session_id}"


def normalize_context_scores(raw_scores: Mapping[str, float]) -> dict[str, float]:
    """Normalize raw PageRank-like scores to [0, 1], keeping all-zero scores at 0."""
    if not raw_scores:
        return {}

    best_score = max(raw_scores.values())
    worst_score = min(raw_scores.values())
    score_range = best_score - worst_score

    if best_score <= 0:
        return {source_key: 0.0 for source_key in raw_scores}

    if score_range == 0:
        return {source_key: 1.0 for source_key in raw_scores}

    return {
        source_key: _clamp_context_score((score - worst_score) / score_range)
        for source_key, score in raw_scores.items()
    }


def _clamp_context_score(score: float) -> float:
    return max(0.0, min(1.0, float(score)))


def _scored_candidate_sort_key(
    scored_candidate: _ScoredScreenSearchCandidate,
) -> tuple[float, float, float, float, float, str, str]:
    candidate = scored_candidate.candidate
    return (
        scored_candidate.final_score,
        candidate.bm25_relevance,
        candidate.embedding_relevance,
        scored_candidate.context_rank,
        -candidate.raw_bm25,
        candidate.chunk_created_at,
        candidate.chunk_id,
    )


def _screen_search_result_sort_key(
    result: ScreenSearchResult,
) -> tuple[float, float, float, float, float, str]:
    return (
        -result.final_score,
        -result.bm25_relevance,
        -result.embedding_relevance,
        -result.context_rank,
        result.raw_bm25,
        result.group_key,
    )


def _chunk_match_sort_key(
    chunk: ScreenSearchChunkMatch,
) -> tuple[float, float, float, float, float, str]:
    return (
        -chunk.final_score,
        -chunk.bm25_relevance,
        -chunk.embedding_relevance,
        -chunk.context_rank,
        chunk.raw_bm25,
        chunk.chunk_id,
    )


def _union_search_candidates(
    bm25_candidates: list[ScreenSearchCandidate],
    semantic_candidates: list[SemanticSearchCandidate],
) -> list[_CombinedScreenSearchCandidate]:
    by_chunk_id: OrderedDict[str, _CombinedScreenSearchCandidate] = OrderedDict()

    for candidate in bm25_candidates:
        by_chunk_id[candidate.chunk_id] = _combined_candidate_from_bm25(candidate)

    for candidate in semantic_candidates:
        existing = by_chunk_id.get(candidate.chunk_id)
        if existing is None:
            by_chunk_id[candidate.chunk_id] = _combined_candidate_from_semantic(
                candidate
            )
            continue

        by_chunk_id[candidate.chunk_id] = replace(
            existing,
            embedding_similarity=candidate.embedding_similarity,
            embedding_relevance=candidate.embedding_relevance,
            match_signals=_append_signal(existing.match_signals, "embedding"),
        )

    return list(by_chunk_id.values())


def _combined_candidate_from_bm25(
    candidate: ScreenSearchCandidate,
) -> _CombinedScreenSearchCandidate:
    return _CombinedScreenSearchCandidate(
        chunk_id=candidate.chunk_id,
        session_id=candidate.session_id,
        frame_id=candidate.frame_id,
        source_key=candidate.source_key,
        retrieval_locator=candidate.retrieval_locator,
        app_name=candidate.app_name,
        window_title=candidate.window_title,
        url=candidate.url,
        session_started_at=candidate.session_started_at,
        session_ended_at=candidate.session_ended_at,
        chunk_created_at=candidate.chunk_created_at,
        text=candidate.text,
        raw_bm25=candidate.raw_bm25,
        bm25_relevance=candidate.bm25_relevance,
        match_signals=("bm25",),
    )


def _combined_candidate_from_semantic(
    candidate: SemanticSearchCandidate,
) -> _CombinedScreenSearchCandidate:
    return _CombinedScreenSearchCandidate(
        chunk_id=candidate.chunk_id,
        session_id=candidate.session_id,
        frame_id=candidate.frame_id,
        source_key=candidate.source_key,
        retrieval_locator=candidate.retrieval_locator,
        app_name=candidate.app_name,
        window_title=candidate.window_title,
        url=candidate.url,
        session_started_at=candidate.session_started_at,
        session_ended_at=candidate.session_ended_at,
        chunk_created_at=candidate.chunk_created_at,
        text=candidate.text,
        embedding_similarity=candidate.embedding_similarity,
        embedding_relevance=candidate.embedding_relevance,
        match_signals=("embedding",),
    )


def _append_signal(signals: tuple[str, ...], signal: str) -> tuple[str, ...]:
    if signal in signals:
        return signals
    return (*signals, signal)


def _first_match_position(text: str, query: str) -> int | None:
    folded_text = text.casefold()
    terms = extract_query_terms(query)
    if not terms:
        normalized_query = normalize_search_query(query)
        terms = [normalized_query] if normalized_query else []

    positions = [
        folded_text.find(term.casefold())
        for term in terms
        if term and folded_text.find(term.casefold()) >= 0
    ]
    if not positions:
        return None
    return min(positions)


def _bounded_excerpt(text: str, *, start: int, max_length: int) -> str:
    prefix = "..." if start > 0 else ""
    prefix_length = len(prefix)
    available = max(1, max_length - prefix_length)
    end = min(len(text), start + available)
    suffix = "..." if end < len(text) else ""
    if suffix:
        available = max(1, max_length - prefix_length - len(suffix))
        end = min(len(text), start + available)

    return f"{prefix}{text[start:end]}{suffix}"


def _nonblank(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
