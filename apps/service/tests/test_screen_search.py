import hashlib

import pytest

from melone_service.cli import main
from melone_service.search import ScreenSearchService
from melone_service.search.screen_search import (
    BM25_RELEVANCE_WEIGHT,
    CONTEXT_RANK_WEIGHT,
    make_text_preview,
)
from melone_service.store.context_rank import ContextRankRepository, ContextRankScore
from melone_service.store.db import connect, initialize_database
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import (
    IMAGE_RETENTION_DELETED_AFTER_INDEXING,
    ScreenRepository,
)
from melone_service.store.search import (
    ScreenSearchCandidate,
    ScreenSearchRepository,
    build_fts_query,
)


NOW = "2026-06-09T06:00:00.000Z"


def test_search_returns_ocr_fixture_chunks_for_matching_query(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_match",
            session_id="screen_session_match",
            frame_id="screen_frame_match",
            source_key="url:https://example.com/docs",
            retrieval_locator="url:https://example.com/docs",
            text="review screen search schema",
        )

        candidates = ScreenSearchRepository(connection).search_chunks("screen search")

        assert [candidate.chunk_id for candidate in candidates] == ["ocr_chunk_match"]
        assert candidates[0].raw_bm25 < 0
        assert candidates[0].bm25_relevance == 1.0
    finally:
        connection.close()


def test_search_returns_chunks_after_frame_png_was_deleted(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_deleted",
            session_id="screen_session_deleted",
            frame_id="screen_frame_deleted",
            source_key="url:https://example.com/deleted",
            retrieval_locator="url:https://example.com/deleted",
            text="deleted image still searchable",
        )
        frame = ScreenRepository(connection).mark_frame_image_retention(
            "screen_frame_deleted",
            state=IMAGE_RETENTION_DELETED_AFTER_INDEXING,
            updated_at=NOW,
        )

        candidates = ScreenSearchRepository(connection).search_chunks("searchable")

        assert frame is not None
        assert candidates[0].chunk_id == "ocr_chunk_deleted"
        assert candidates[0].frame_id == "screen_frame_deleted"
    finally:
        connection.close()


def test_search_chunks_filters_by_session_window(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_old",
            session_id="screen_session_old",
            frame_id="screen_frame_old",
            source_key="url:https://example.com/old",
            retrieval_locator="url:https://example.com/old",
            text="screen search stale result",
            started_at="2026-06-09T04:00:00.000Z",
            ended_at="2026-06-09T04:30:00.000Z",
        )
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_recent",
            session_id="screen_session_recent",
            frame_id="screen_frame_recent",
            source_key="url:https://example.com/recent",
            retrieval_locator="url:https://example.com/recent",
            text="screen search recent result",
            started_at="2026-06-09T05:50:00.000Z",
        )

        candidates = ScreenSearchRepository(connection).search_chunks(
            "screen search",
            since="2026-06-09T05:00:00.000Z",
        )

        assert [candidate.chunk_id for candidate in candidates] == [
            "ocr_chunk_recent"
        ]
    finally:
        connection.close()


def test_bm25_relevance_normalization_ranks_stronger_matches_higher(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_weak",
            session_id="screen_session_weak",
            frame_id="screen_frame_weak",
            source_key="url:https://example.com/weak",
            retrieval_locator="url:https://example.com/weak",
            text="screen search",
        )
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_strong",
            session_id="screen_session_strong",
            frame_id="screen_frame_strong",
            source_key="url:https://example.com/strong",
            retrieval_locator="url:https://example.com/strong",
            text="screen screen screen search search",
        )

        candidates = ScreenSearchRepository(connection).search_chunks("screen search")

        assert [candidate.chunk_id for candidate in candidates] == [
            "ocr_chunk_strong",
            "ocr_chunk_weak",
        ]
        assert candidates[0].raw_bm25 < candidates[1].raw_bm25
        assert candidates[0].bm25_relevance == 1.0
        assert candidates[1].bm25_relevance == 0.0
    finally:
        connection.close()


def test_screen_search_service_groups_multiple_chunks_by_locator(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_1",
            session_id="screen_session_docs",
            frame_id="screen_frame_1",
            source_key="url:https://example.com/docs",
            retrieval_locator="url:https://example.com/docs",
            text="screen search introduction",
        )
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_2",
            session_id="screen_session_docs",
            frame_id="screen_frame_2",
            source_key="url:https://example.com/docs",
            retrieval_locator="url:https://example.com/docs",
            text="screen search advanced usage",
        )

        results = ScreenSearchService(
            ScreenSearchRepository(connection),
            preview_length=80,
        ).search("screen search")

        assert len(results) == 1
        assert results[0].group_key == "url:https://example.com/docs"
        assert results[0].retrieval_locator == "url:https://example.com/docs"
        assert {chunk.chunk_id for chunk in results[0].chunks} == {
            "ocr_chunk_1",
            "ocr_chunk_2",
        }
        assert results[0].final_score == results[0].bm25_relevance
    finally:
        connection.close()


def test_screen_search_service_falls_back_to_session_id_group(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_fallback",
            session_id="screen_session_fallback",
            frame_id="screen_frame_fallback",
            source_key="app:Preview",
            retrieval_locator="",
            text="screen search fallback locator",
        )

        results = ScreenSearchService(ScreenSearchRepository(connection)).search(
            "screen search"
        )

        assert len(results) == 1
        assert results[0].group_key == "session:screen_session_fallback"
        assert results[0].retrieval_locator is None
    finally:
        connection.close()


def test_context_rank_rerank_keeps_strong_bm25_above_weak_match(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_context_score(connection, "url:https://example.com/strong", 0.1)
        _seed_context_score(connection, "url:https://example.com/weak", 0.9)

        results = ScreenSearchService(
            _FakeScreenSearchRepository(
                _candidate(
                    "ocr_chunk_strong_bm25",
                    source_key="url:https://example.com/strong",
                    raw_bm25=-10.0,
                    bm25_relevance=1.0,
                    text="screen search exact match",
                ),
                _candidate(
                    "ocr_chunk_weak_bm25",
                    source_key="url:https://example.com/weak",
                    raw_bm25=-1.0,
                    bm25_relevance=0.0,
                    text="screen search weak match",
                ),
            ),
            ContextRankRepository(connection),
        ).search("screen search")
    finally:
        connection.close()

    assert [result.source_key for result in results] == [
        "url:https://example.com/strong",
        "url:https://example.com/weak",
    ]
    assert results[0].final_score == pytest.approx(0.80)
    assert results[1].final_score == pytest.approx(0.20)


def test_context_rank_breaks_bm25_relevance_ties(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_context_score(connection, "url:https://example.com/low", 0.1)
        _seed_context_score(connection, "url:https://example.com/high", 0.9)

        results = ScreenSearchService(
            _FakeScreenSearchRepository(
                _candidate(
                    "ocr_chunk_low_context",
                    source_key="url:https://example.com/low",
                    raw_bm25=-2.0,
                    bm25_relevance=1.0,
                ),
                _candidate(
                    "ocr_chunk_high_context",
                    source_key="url:https://example.com/high",
                    raw_bm25=-2.0,
                    bm25_relevance=1.0,
                ),
            ),
            ContextRankRepository(connection),
        ).search("screen search")
    finally:
        connection.close()

    assert [result.source_key for result in results] == [
        "url:https://example.com/high",
        "url:https://example.com/low",
    ]
    assert results[0].context_rank == pytest.approx(1.0)
    assert results[1].context_rank == pytest.approx(0.0)
    assert results[0].final_score > results[1].final_score


def test_missing_context_rank_rows_use_zero_prior_when_cache_exists(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_context_score(connection, "url:https://example.com/cached-low", 0.5)
        _seed_context_score(connection, "url:https://example.com/cached-high", 1.0)

        results = ScreenSearchService(
            _FakeScreenSearchRepository(
                _candidate(
                    "ocr_chunk_missing_context",
                    source_key="url:https://example.com/missing",
                    raw_bm25=-2.0,
                    bm25_relevance=0.5,
                ),
                _candidate(
                    "ocr_chunk_cached_low_context",
                    source_key="url:https://example.com/cached-low",
                    raw_bm25=-2.0,
                    bm25_relevance=0.5,
                ),
                _candidate(
                    "ocr_chunk_cached_high_context",
                    source_key="url:https://example.com/cached-high",
                    raw_bm25=-2.0,
                    bm25_relevance=0.5,
                ),
            ),
            ContextRankRepository(connection),
        ).search("screen search")
    finally:
        connection.close()

    assert [result.source_key for result in results] == [
        "url:https://example.com/cached-high",
        "url:https://example.com/cached-low",
        "url:https://example.com/missing",
    ]
    assert results[0].context_rank == pytest.approx(1.0)
    assert results[1].context_rank == pytest.approx(0.5)
    assert results[2].context_rank == pytest.approx(0.0)
    assert results[2].final_score == pytest.approx(BM25_RELEVANCE_WEIGHT * 0.5)


def test_empty_context_rank_cache_preserves_bm25_only_scores(tmp_path):
    connection = _connection(tmp_path)
    try:
        results = ScreenSearchService(
            _FakeScreenSearchRepository(
                _candidate(
                    "ocr_chunk_high_bm25",
                    source_key="url:https://example.com/high-bm25",
                    raw_bm25=-3.0,
                    bm25_relevance=0.75,
                ),
                _candidate(
                    "ocr_chunk_low_bm25",
                    source_key="url:https://example.com/low-bm25",
                    raw_bm25=-1.0,
                    bm25_relevance=0.25,
                ),
            ),
            ContextRankRepository(connection),
        ).search("screen search")
    finally:
        connection.close()

    assert [result.bm25_relevance for result in results] == [0.75, 0.25]
    assert [result.context_rank for result in results] == [0.75, 0.25]
    assert [result.final_score for result in results] == pytest.approx([0.75, 0.25])


def test_context_rank_explain_metadata_matches_final_score_calculation(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_context_score(connection, "url:https://example.com/explain", 1.0)

        results = ScreenSearchService(
            _FakeScreenSearchRepository(
                _candidate(
                    "ocr_chunk_explain",
                    source_key="url:https://example.com/explain",
                    retrieval_locator="url:https://example.com/explain/page",
                    raw_bm25=-4.0,
                    bm25_relevance=0.6,
                    text="prefix screen search explain metadata suffix",
                )
            ),
            ContextRankRepository(connection),
            preview_length=32,
        ).search("screen search")
    finally:
        connection.close()

    result = results[0]
    chunk = result.chunks[0]
    expected_final_score = (
        BM25_RELEVANCE_WEIGHT * chunk.bm25_relevance
        + CONTEXT_RANK_WEIGHT * chunk.context_rank
    )

    assert chunk.chunk_id == "ocr_chunk_explain"
    assert chunk.retrieval_locator == "url:https://example.com/explain/page"
    assert chunk.source_key == "url:https://example.com/explain"
    assert chunk.raw_bm25 == -4.0
    assert chunk.bm25_relevance == pytest.approx(0.6)
    assert chunk.context_rank == pytest.approx(1.0)
    assert chunk.final_score == pytest.approx(expected_final_score)
    assert result.final_score == pytest.approx(chunk.final_score)
    assert len(chunk.preview) <= 32
    assert "screen" in chunk.preview


def test_special_character_queries_do_not_fail_or_inject_sql(tmp_path):
    connection = _connection(tmp_path)
    try:
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_safe",
            session_id="screen_session_safe",
            frame_id="screen_frame_safe",
            source_key="url:https://example.com/safe",
            retrieval_locator="url:https://example.com/safe",
            text="screen search survives unusual query syntax",
        )
        repository = ScreenSearchRepository(connection)

        for query in (
            '"',
            "***",
            "foo:bar",
            "screen -schema",
            'screen" OR *',
            '"; DROP TABLE ocr_chunks; --',
        ):
            repository.search_chunks(query)

        assert OcrChunkRepository(connection).count_chunks() == 1
        assert connection.execute("SELECT COUNT(*) FROM ocr_chunks").fetchone()[0] == 1
    finally:
        connection.close()


def test_build_fts_query_quotes_terms_for_fts_syntax_safety():
    assert build_fts_query("foo:bar screen-search") == '"foo" "bar" "screen" "search"'
    assert build_fts_query('"') is None
    assert build_fts_query("***") == '"***"'


def test_preview_is_bounded_and_keeps_matched_text_nearby():
    text = ("prefix " * 20) + "needle screen term " + ("suffix " * 20)

    preview = make_text_preview(text, query="screen", max_length=60)

    assert len(preview) <= 60
    assert "screen" in preview


def test_search_command_prints_screen_results(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    connection = connect(database_path)
    try:
        _seed_chunk(
            connection,
            chunk_id="ocr_chunk_cli",
            session_id="screen_session_cli",
            frame_id="screen_frame_cli",
            source_key="url:https://example.com/cli",
            retrieval_locator="url:https://example.com/cli",
            text="screen search command result",
        )
        connection.commit()
    finally:
        connection.close()

    exit_code = main(["search", "screen", "search", "--limit", "5"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "score" in output
    assert "bm25" in output
    assert "emb" in output
    assert "rank" in output
    assert "chunks" in output
    assert "url:https://example.com/cli" in output
    assert "screen search command result" in output


def _connection(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    return connect(database_path)


def _seed_chunk(
    connection,
    *,
    chunk_id,
    session_id,
    frame_id,
    source_key,
    retrieval_locator,
    text,
    started_at=NOW,
    ended_at=None,
    created_at=NOW,
):
    screen_repository = ScreenRepository(connection)
    if screen_repository.get_session(session_id) is None:
        screen_repository.create_session(
            session_id=session_id,
            source_key=source_key,
            retrieval_locator=retrieval_locator,
            app_name="Google Chrome",
            bundle_id="com.google.Chrome",
            window_title="Docs",
            url=_url_from_locator(retrieval_locator),
            started_at=started_at,
            now=created_at,
        )
        if ended_at is not None:
            screen_repository.close_session(
                session_id,
                ended_at=ended_at,
                now=created_at,
            )

    frame = screen_repository.get_frame(frame_id)
    if frame is None:
        frame = screen_repository.insert_frame(
            frame_id=frame_id,
            session_id=session_id,
            captured_at=NOW,
            image_path=f"/tmp/{frame_id}.png",
            sha256=hashlib.sha256(frame_id.encode()).hexdigest(),
            width=1280,
            height=720,
        )
        assert frame is not None

    return OcrChunkRepository(connection).insert_chunk_with_fts(
        chunk_id=chunk_id,
        session_id=session_id,
        frame_id=frame_id,
        source_key=source_key,
        retrieval_locator=retrieval_locator,
        app_name="Google Chrome",
        window_title="Docs",
        url=_url_from_locator(retrieval_locator),
        text=text,
        text_hash=hashlib.sha256(f"{chunk_id}:{text}".encode()).hexdigest(),
        created_at=created_at,
    )


def _url_from_locator(locator):
    if locator.startswith("url:"):
        return locator.removeprefix("url:")
    return None


def _seed_context_score(connection, source_key, score):
    ContextRankRepository(connection).upsert_scores(
        [
            ContextRankScore(
                source_key=source_key,
                score=score,
                visits=1,
                retrieval_locators=(source_key,),
                computed_at=NOW,
                model_version="test_model_v1",
            )
        ]
    )


def _candidate(
    chunk_id,
    *,
    source_key,
    raw_bm25,
    bm25_relevance,
    retrieval_locator=None,
    text="screen search result",
):
    locator = retrieval_locator if retrieval_locator is not None else source_key
    return ScreenSearchCandidate(
        chunk_id=chunk_id,
        session_id=f"screen_session_{chunk_id}",
        frame_id=f"screen_frame_{chunk_id}",
        source_key=source_key,
        retrieval_locator=locator,
        app_name="Google Chrome",
        window_title="Docs",
        url=_url_from_locator(locator),
        session_started_at=NOW,
        session_ended_at=None,
        chunk_created_at=NOW,
        text=text,
        raw_bm25=raw_bm25,
        bm25_relevance=bm25_relevance,
    )


class _FakeScreenSearchRepository:
    def __init__(self, *candidates):
        self.candidates = list(candidates)

    def search_chunks(self, query, *, limit, since=None):
        return self.candidates[:limit]
