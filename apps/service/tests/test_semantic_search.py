from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
import pytest

from melone_service.embeddings.fake import FakeEmbeddingModel
from melone_service.embeddings.model import EmbeddingModelInfo
from melone_service.search import ScreenSearchService
from melone_service.search.semantic_search import EmbeddingSemanticSearchProvider
from melone_service.search.screen_search import (
    HYBRID_BM25_WEIGHT,
    HYBRID_CONTEXT_RANK_WEIGHT,
    HYBRID_EMBEDDING_WEIGHT,
)
from melone_service.search.vector_index import (
    SemanticSearchCandidate,
    SqliteExactVectorIndex,
    clamp_cosine_similarity,
    cosine_similarity_to_relevance,
)
from melone_service.store.db import connect, initialize_database
from melone_service.store.embeddings import EmbeddingRepository
from melone_service.store.ocr import OcrChunk, OcrChunkRepository
from melone_service.store.screen import ScreenRepository
from melone_service.store.search import ScreenSearchCandidate, ScreenSearchRepository


NOW = "2026-06-09T06:00:00.000Z"
MODEL = "fake-semantic-model"
DIMENSION = 2
RANKING_FIXTURE_MODEL = "fake-hybrid-ranking-fixture-model"
RANKING_FIXTURE_DIMENSION = 4
RANKING_FIXTURE_QUERIES = {
    "who greenlit the renewal after the call": [1.0, 0.0, 0.0, 0.0],
    "AUTH_TOKEN_X7Q": [0.0, 1.0, 0.0, 0.0],
    "결제 failure RCA": [0.0, 0.0, 1.0, 0.0],
}


@dataclass(frozen=True)
class _HybridRankingFixturePage:
    chunk_id: str
    text: str
    vector: tuple[float, ...]
    context_rank: float


@dataclass(frozen=True)
class _ExpectedHybridRanking:
    chunk_id: str
    bm25_relevance: float
    embedding_relevance: float
    context_rank: float
    final_score: float
    match_signals: tuple[str, ...]


RANKING_FIXTURE_PAGES = (
    _HybridRankingFixturePage(
        chunk_id="ocr_chunk_project_approval",
        text=(
            "Customer success approved Project Phoenix renewal after the "
            "quarterly business review."
        ),
        vector=(1.0, 0.0, 0.0, 0.0),
        context_rank=0.25,
    ),
    _HybridRankingFixturePage(
        chunk_id="ocr_chunk_exact_identifier",
        text=(
            "Staging webhook secret AUTH_TOKEN_X7Q appears in the deployment "
            "checklist."
        ),
        vector=(0.0, 0.0, 0.0, 1.0),
        context_rank=0.0,
    ),
    _HybridRankingFixturePage(
        chunk_id="ocr_chunk_wrong_high_rank_token_policy",
        text=(
            "Token rotation policy overview for staging credentials and "
            "webhooks."
        ),
        vector=(0.0, 1.0, 0.0, 0.0),
        context_rank=1.0,
    ),
    _HybridRankingFixturePage(
        chunk_id="ocr_chunk_payment_failure_rca",
        text=(
            "Payment failure root cause analysis: gateway retries exhausted "
            "during checkout."
        ),
        vector=(0.0, 0.0, 1.0, 0.0),
        context_rank=0.25,
    ),
    _HybridRankingFixturePage(
        chunk_id="ocr_chunk_unrelated_lunch_menu",
        text="Team lunch menu and office snack preferences.",
        vector=(-1.0, 0.0, 0.0, 0.0),
        context_rank=0.0,
    ),
)


def test_semantic_candidate_lookup_orders_by_cosine_similarity(tmp_path):
    connection = _connection(tmp_path)
    try:
        best = _seed_chunk(
            connection,
            chunk_id="ocr_chunk_best",
            text="calendar planning details",
            created_at="2026-06-09T06:00:03.000Z",
        )
        middle = _seed_chunk(
            connection,
            chunk_id="ocr_chunk_middle",
            text="schedule notes",
            created_at="2026-06-09T06:00:02.000Z",
        )
        opposite = _seed_chunk(
            connection,
            chunk_id="ocr_chunk_opposite",
            text="unrelated invoice",
            created_at="2026-06-09T06:00:01.000Z",
        )
        repository = EmbeddingRepository(connection)
        _upsert_embedding(repository, best, [1.0, 0.0])
        _upsert_embedding(repository, middle, [0.6, 0.8])
        _upsert_embedding(repository, opposite, [-1.0, 0.0])

        model = FakeEmbeddingModel(
            model=MODEL,
            dimension=DIMENSION,
            query_vectors={"calendar summary": [1.0, 0.0]},
        )
        provider = EmbeddingSemanticSearchProvider(
            model=model,
            vector_index=SqliteExactVectorIndex(repository),
            candidate_limit=10,
        )

        candidates = provider.search_candidates("  calendar summary  ", limit=2)

        assert model.query_calls == ["calendar summary"]
        assert model.document_calls == []
        assert [candidate.chunk_id for candidate in candidates] == [
            best.id,
            middle.id,
        ]
        assert candidates[0].embedding_similarity == pytest.approx(1.0)
        assert candidates[0].embedding_relevance == pytest.approx(1.0)
        assert candidates[1].embedding_similarity == pytest.approx(0.6)
        assert candidates[1].embedding_relevance == pytest.approx(0.8)
    finally:
        connection.close()


def test_semantic_since_filter_matches_bm25_session_window(tmp_path):
    connection = _connection(tmp_path)
    try:
        old = _seed_chunk(
            connection,
            chunk_id="ocr_chunk_old",
            text="screen search stale result",
            started_at="2026-06-09T04:00:00.000Z",
            ended_at="2026-06-09T04:30:00.000Z",
        )
        recent = _seed_chunk(
            connection,
            chunk_id="ocr_chunk_recent",
            text="screen search recent result",
            started_at="2026-06-09T05:50:00.000Z",
        )
        repository = EmbeddingRepository(connection)
        _upsert_embedding(repository, old, [1.0, 0.0])
        _upsert_embedding(repository, recent, [1.0, 0.0])
        since = "2026-06-09T05:00:00.000Z"

        bm25_candidates = ScreenSearchRepository(connection).search_chunks(
            "screen search",
            since=since,
        )
        semantic_candidates = EmbeddingSemanticSearchProvider(
            model=FakeEmbeddingModel(
                model=MODEL,
                dimension=DIMENSION,
                query_vectors={"screen search": [1.0, 0.0]},
            ),
            vector_index=SqliteExactVectorIndex(repository),
            candidate_limit=10,
        ).search_candidates("screen search", since=since, limit=10)

        assert [candidate.chunk_id for candidate in bm25_candidates] == [recent.id]
        assert [candidate.chunk_id for candidate in semantic_candidates] == [
            recent.id
        ]
    finally:
        connection.close()


def test_semantic_candidate_lookup_returns_empty_when_embeddings_are_missing(
    tmp_path,
):
    connection = _connection(tmp_path)
    try:
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_without_embedding",
            text="screen text exists without a semantic vector",
        )
        repository = EmbeddingRepository(connection)
        provider = EmbeddingSemanticSearchProvider(
            model=FakeEmbeddingModel(
                model=MODEL,
                dimension=DIMENSION,
                query_vectors={"screen text": [1.0, 0.0]},
            ),
            vector_index=SqliteExactVectorIndex(repository),
            candidate_limit=10,
        )

        assert provider.search_candidates("screen text", limit=10) == []
    finally:
        connection.close()


def test_semantic_candidate_lookup_returns_empty_when_query_vector_is_invalid(
    tmp_path,
):
    connection = _connection(tmp_path)
    try:
        chunk = _seed_chunk(
            connection,
            chunk_id="ocr_chunk_with_embedding",
            text="screen text with a semantic vector",
        )
        repository = EmbeddingRepository(connection)
        _upsert_embedding(repository, chunk, [1.0, 0.0])
        provider = EmbeddingSemanticSearchProvider(
            model=_MalformedQueryEmbeddingModel(),
            vector_index=SqliteExactVectorIndex(repository),
            candidate_limit=10,
        )

        assert provider.search_candidates("screen text", limit=10) == []
    finally:
        connection.close()


def test_cosine_similarity_relevance_maps_clamped_similarity_to_unit_interval():
    # PR 5 normalizes semantic relevance with a stable cosine mapping:
    # -1 -> 0, 0 -> 0.5, 1 -> 1. Non-finite values are treated as no signal.
    assert cosine_similarity_to_relevance(-1.0) == pytest.approx(0.0)
    assert cosine_similarity_to_relevance(0.0) == pytest.approx(0.5)
    assert cosine_similarity_to_relevance(1.0) == pytest.approx(1.0)
    assert cosine_similarity_to_relevance(float("nan")) == pytest.approx(0.0)
    assert cosine_similarity_to_relevance(float("inf")) == pytest.approx(0.0)
    assert clamp_cosine_similarity(1.5) == pytest.approx(1.0)
    assert clamp_cosine_similarity(-1.5) == pytest.approx(-1.0)


def test_hybrid_search_returns_semantic_only_sentence_match():
    provider = _FakeSemanticProvider(
        _semantic_candidate(
            "ocr_chunk_project_approval",
            text=(
                "Project Phoenix renewal was approved after the customer "
                "success review."
            ),
            embedding_similarity=0.8,
            embedding_relevance=0.9,
        )
    )

    results = ScreenSearchService(
        _FakeScreenSearchRepository(),
        semantic_candidate_provider=provider,
        preview_length=48,
    ).search("what renewal did customer success approve")

    assert provider.calls == [
        ("what renewal did customer success approve", 20, None)
    ]
    assert len(results) == 1
    assert results[0].chunks[0].chunk_id == "ocr_chunk_project_approval"
    assert results[0].bm25_relevance == 0.0
    assert results[0].embedding_relevance == pytest.approx(0.9)
    assert results[0].final_score == pytest.approx(HYBRID_EMBEDDING_WEIGHT * 0.9)
    assert results[0].match_signals == ("embedding",)
    assert len(results[0].preview) <= 48


def test_exact_bm25_match_outranks_high_semantic_only_match():
    exact = _candidate(
        "ocr_chunk_exact_identifier",
        raw_bm25=-8.0,
        bm25_relevance=1.0,
    )
    provider = _FakeSemanticProvider(
        _semantic_candidate(
            "ocr_chunk_exact_identifier",
            embedding_similarity=-0.6,
            embedding_relevance=0.2,
        ),
        _semantic_candidate(
            "ocr_chunk_semantic_only",
            text="A related settings page mentions token rotation.",
            embedding_similarity=1.0,
            embedding_relevance=1.0,
        ),
    )

    results = ScreenSearchService(
        _FakeScreenSearchRepository(exact),
        semantic_candidate_provider=provider,
    ).search("AUTH_TOKEN_X7Q")

    assert [result.chunks[0].chunk_id for result in results] == [
        "ocr_chunk_exact_identifier",
        "ocr_chunk_semantic_only",
    ]
    assert results[0].bm25_relevance == pytest.approx(1.0)
    assert results[0].embedding_relevance == pytest.approx(0.2)
    assert results[0].match_signals == ("bm25", "embedding")
    assert results[0].final_score == pytest.approx(
        HYBRID_BM25_WEIGHT + HYBRID_EMBEDDING_WEIGHT * 0.2
    )
    assert results[1].final_score == pytest.approx(HYBRID_EMBEDDING_WEIGHT)


def test_hybrid_search_uses_pagerank_as_tie_breaking_prior():
    low_context = _candidate(
        "ocr_chunk_low_context",
        raw_bm25=-2.0,
        bm25_relevance=0.5,
    )
    high_context = _candidate(
        "ocr_chunk_high_context",
        raw_bm25=-2.0,
        bm25_relevance=0.5,
    )
    provider = _FakeSemanticProvider(
        _semantic_candidate(
            "ocr_chunk_low_context",
            embedding_similarity=0.0,
            embedding_relevance=0.5,
        ),
        _semantic_candidate(
            "ocr_chunk_high_context",
            embedding_similarity=0.0,
            embedding_relevance=0.5,
        ),
    )

    results = ScreenSearchService(
        _FakeScreenSearchRepository(low_context, high_context),
        context_rank_overrides={
            low_context.source_key: 0.0,
            high_context.source_key: 1.0,
        },
        semantic_candidate_provider=provider,
    ).search("screen search")

    assert [result.chunks[0].chunk_id for result in results] == [
        "ocr_chunk_high_context",
        "ocr_chunk_low_context",
    ]
    assert results[0].context_rank == pytest.approx(1.0)
    assert results[1].context_rank == pytest.approx(0.0)
    assert results[0].final_score == pytest.approx(
        HYBRID_BM25_WEIGHT * 0.5
        + HYBRID_EMBEDDING_WEIGHT * 0.5
        + HYBRID_CONTEXT_RANK_WEIGHT
    )


def test_hybrid_grouped_results_choose_best_chunk_across_signal_types():
    locator = "url:https://example.test/grouped"
    bm25_chunk = _candidate(
        "ocr_chunk_grouped_bm25",
        raw_bm25=-1.0,
        bm25_relevance=0.2,
        source_key=locator,
        retrieval_locator=locator,
        text="screen search literal but weak",
    )
    semantic_chunk = _semantic_candidate(
        "ocr_chunk_grouped_semantic",
        source_key=locator,
        retrieval_locator=locator,
        text="The renewal approval discussion is summarized here.",
        embedding_similarity=1.0,
        embedding_relevance=1.0,
    )

    results = ScreenSearchService(
        _FakeScreenSearchRepository(bm25_chunk),
        semantic_candidate_provider=_FakeSemanticProvider(semantic_chunk),
    ).search("what renewal was approved")

    assert len(results) == 1
    assert results[0].group_key == locator
    assert results[0].chunks[0].chunk_id == "ocr_chunk_grouped_semantic"
    assert results[0].chunks[1].chunk_id == "ocr_chunk_grouped_bm25"
    assert results[0].preview == results[0].chunks[0].preview
    assert results[0].embedding_relevance == pytest.approx(1.0)


def test_screen_search_falls_back_to_bm25_when_semantic_provider_fails():
    results = ScreenSearchService(
        _FakeScreenSearchRepository(
            _candidate(
                "ocr_chunk_high_bm25",
                raw_bm25=-3.0,
                bm25_relevance=1.0,
            ),
            _candidate(
                "ocr_chunk_low_bm25",
                raw_bm25=-1.0,
                bm25_relevance=0.0,
            ),
        ),
        semantic_candidate_provider=_UnavailableSemanticProvider(),
    ).search("screen search")

    assert [result.chunks[0].chunk_id for result in results] == [
        "ocr_chunk_high_bm25",
        "ocr_chunk_low_bm25",
    ]
    assert [result.final_score for result in results] == pytest.approx([1.0, 0.0])


def test_hybrid_ranking_evaluation_fixtures_preserve_expected_score_breakdowns(
    tmp_path,
):
    assert HYBRID_BM25_WEIGHT == pytest.approx(0.55)
    assert HYBRID_EMBEDDING_WEIGHT == pytest.approx(0.30)
    assert HYBRID_CONTEXT_RANK_WEIGHT == pytest.approx(0.15)

    connection = _connection(tmp_path)
    try:
        model = _seed_hybrid_ranking_fixture_corpus(connection)
        service = ScreenSearchService(
            ScreenSearchRepository(connection),
            context_rank_overrides=_hybrid_ranking_fixture_context_ranks(),
            semantic_candidate_provider=EmbeddingSemanticSearchProvider(
                model=model,
                vector_index=SqliteExactVectorIndex(EmbeddingRepository(connection)),
                candidate_limit=10,
            ),
        )

        cases = (
            (
                "who greenlit the renewal after the call",
                (
                    _ExpectedHybridRanking(
                        "ocr_chunk_project_approval",
                        bm25_relevance=0.0,
                        embedding_relevance=1.0,
                        context_rank=0.25,
                        final_score=0.3375,
                        match_signals=("embedding",),
                    ),
                    _ExpectedHybridRanking(
                        "ocr_chunk_wrong_high_rank_token_policy",
                        bm25_relevance=0.0,
                        embedding_relevance=0.5,
                        context_rank=1.0,
                        final_score=0.30,
                        match_signals=("embedding",),
                    ),
                ),
            ),
            (
                "AUTH_TOKEN_X7Q",
                (
                    _ExpectedHybridRanking(
                        "ocr_chunk_exact_identifier",
                        bm25_relevance=1.0,
                        embedding_relevance=0.5,
                        context_rank=0.0,
                        final_score=0.70,
                        match_signals=("bm25", "embedding"),
                    ),
                    _ExpectedHybridRanking(
                        "ocr_chunk_wrong_high_rank_token_policy",
                        bm25_relevance=0.0,
                        embedding_relevance=1.0,
                        context_rank=1.0,
                        final_score=0.45,
                        match_signals=("embedding",),
                    ),
                ),
            ),
            (
                "결제 failure RCA",
                (
                    _ExpectedHybridRanking(
                        "ocr_chunk_payment_failure_rca",
                        bm25_relevance=0.0,
                        embedding_relevance=1.0,
                        context_rank=0.25,
                        final_score=0.3375,
                        match_signals=("embedding",),
                    ),
                    _ExpectedHybridRanking(
                        "ocr_chunk_wrong_high_rank_token_policy",
                        bm25_relevance=0.0,
                        embedding_relevance=0.5,
                        context_rank=1.0,
                        final_score=0.30,
                        match_signals=("embedding",),
                    ),
                ),
            ),
        )

        for query, expected_rankings in cases:
            results = service.search(query, limit=10)

            assert [result.chunks[0].chunk_id for result in results[:2]] == [
                expected.chunk_id for expected in expected_rankings
            ]
            for result, expected in zip(results[:2], expected_rankings, strict=True):
                assert result.bm25_relevance == pytest.approx(
                    expected.bm25_relevance
                )
                assert result.embedding_relevance == pytest.approx(
                    expected.embedding_relevance
                )
                assert result.context_rank == pytest.approx(expected.context_rank)
                assert result.final_score == pytest.approx(expected.final_score)
                assert result.match_signals == expected.match_signals

        assert model.query_calls == list(RANKING_FIXTURE_QUERIES)
        assert model.document_calls == [page.text for page in RANKING_FIXTURE_PAGES]
    finally:
        connection.close()


def _connection(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    return connect(database_path)


def _seed_chunk(
    connection,
    *,
    chunk_id: str,
    text: str,
    started_at: str = NOW,
    ended_at: str | None = None,
    created_at: str = NOW,
) -> OcrChunk:
    session_id = f"screen_session_{chunk_id}"
    frame_id = f"screen_frame_{chunk_id}"
    source_key = f"url:https://example.test/{chunk_id}"
    screen_repository = ScreenRepository(connection)
    screen_repository.create_session(
        session_id=session_id,
        source_key=source_key,
        retrieval_locator=source_key,
        app_name="Google Chrome",
        bundle_id="com.google.Chrome",
        window_title="Docs",
        url=source_key.removeprefix("url:"),
        started_at=started_at,
        now=started_at,
    )
    if ended_at is not None:
        screen_repository.close_session(
            session_id,
            ended_at=ended_at,
            now=ended_at,
        )
    screen_repository.insert_frame(
        frame_id=frame_id,
        session_id=session_id,
        captured_at=started_at,
        image_path=f"/tmp/{frame_id}.png",
        sha256=hashlib.sha256(frame_id.encode()).hexdigest(),
        width=1280,
        height=720,
    )
    chunk = OcrChunkRepository(connection).insert_chunk_with_fts(
        chunk_id=chunk_id,
        session_id=session_id,
        frame_id=frame_id,
        source_key=source_key,
        retrieval_locator=source_key,
        app_name="Google Chrome",
        window_title="Docs",
        url=source_key.removeprefix("url:"),
        text=text,
        text_hash=hashlib.sha256(f"{chunk_id}:{text}".encode()).hexdigest(),
        created_at=created_at,
    )
    connection.commit()
    return chunk


def _upsert_embedding(
    repository: EmbeddingRepository,
    chunk: OcrChunk,
    vector: list[float],
    *,
    model: str = MODEL,
    dimension: int = DIMENSION,
) -> None:
    repository.upsert_chunk_embedding(
        chunk_id=chunk.id,
        model=model,
        dimension=dimension,
        text_hash=chunk.text_hash,
        embedding=np.asarray(vector, dtype=np.float32),
    )
    repository.connection.commit()


def _seed_hybrid_ranking_fixture_corpus(connection) -> FakeEmbeddingModel:
    model = FakeEmbeddingModel(
        model=RANKING_FIXTURE_MODEL,
        dimension=RANKING_FIXTURE_DIMENSION,
        query_vectors=RANKING_FIXTURE_QUERIES,
        document_vectors={
            page.text: page.vector for page in RANKING_FIXTURE_PAGES
        },
    )
    repository = EmbeddingRepository(connection)

    for page in RANKING_FIXTURE_PAGES:
        chunk = _seed_chunk(connection, chunk_id=page.chunk_id, text=page.text)
        _upsert_embedding(
            repository,
            chunk,
            model.encode_document(page.text),
            model=RANKING_FIXTURE_MODEL,
            dimension=RANKING_FIXTURE_DIMENSION,
        )

    return model


def _hybrid_ranking_fixture_context_ranks() -> dict[str, float]:
    return {
        f"url:https://example.test/{page.chunk_id}": page.context_rank
        for page in RANKING_FIXTURE_PAGES
    }


def _candidate(
    chunk_id: str,
    *,
    raw_bm25: float,
    bm25_relevance: float,
    source_key: str | None = None,
    retrieval_locator: str | None = None,
    text: str = "screen search result",
) -> ScreenSearchCandidate:
    source_key = source_key or f"url:https://example.test/{chunk_id}"
    retrieval_locator = retrieval_locator or source_key
    return ScreenSearchCandidate(
        chunk_id=chunk_id,
        session_id=f"screen_session_{chunk_id}",
        frame_id=f"screen_frame_{chunk_id}",
        source_key=source_key,
        retrieval_locator=retrieval_locator,
        app_name="Google Chrome",
        window_title="Docs",
        url=retrieval_locator.removeprefix("url:"),
        session_started_at=NOW,
        session_ended_at=None,
        chunk_created_at=NOW,
        text=text,
        raw_bm25=raw_bm25,
        bm25_relevance=bm25_relevance,
    )


def _semantic_candidate(
    chunk_id: str,
    *,
    source_key: str | None = None,
    retrieval_locator: str | None = None,
    text: str = "semantic screen text result",
    embedding_similarity: float,
    embedding_relevance: float,
) -> SemanticSearchCandidate:
    source_key = source_key or f"url:https://example.test/{chunk_id}"
    retrieval_locator = retrieval_locator or source_key
    return SemanticSearchCandidate(
        chunk_id=chunk_id,
        session_id=f"screen_session_{chunk_id}",
        frame_id=f"screen_frame_{chunk_id}",
        source_key=source_key,
        retrieval_locator=retrieval_locator,
        app_name="Google Chrome",
        window_title="Docs",
        url=retrieval_locator.removeprefix("url:"),
        session_started_at=NOW,
        session_ended_at=None,
        chunk_created_at=NOW,
        text=text,
        embedding_similarity=embedding_similarity,
        embedding_relevance=embedding_relevance,
    )


class _FakeScreenSearchRepository:
    def __init__(self, *candidates: ScreenSearchCandidate) -> None:
        self.candidates = list(candidates)

    def search_chunks(
        self,
        query: str,
        *,
        limit: int,
        since: str | None = None,
    ) -> list[ScreenSearchCandidate]:
        return self.candidates[:limit]


class _FakeSemanticProvider:
    def __init__(self, *candidates: SemanticSearchCandidate) -> None:
        self.candidates = list(candidates)
        self.calls: list[tuple[str, int, str | None]] = []

    def search_candidates(
        self,
        query: str,
        *,
        limit: int,
        since: str | None = None,
    ) -> list[SemanticSearchCandidate]:
        self.calls.append((query, limit, since))
        return self.candidates[:limit]


class _UnavailableSemanticProvider:
    def search_candidates(self, query: str, *, limit: int, since: str | None = None):
        raise RuntimeError("embedding model unavailable")


class _MalformedQueryEmbeddingModel:
    @property
    def info(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(
            provider="test",
            model=MODEL,
            dimension=DIMENSION,
        )

    def encode_query(self, query: str):
        return np.asarray([2.0, 0.0], dtype=np.float32)

    def encode_document(self, text: str):
        raise AssertionError("semantic search should use encode_query")
