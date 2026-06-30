from __future__ import annotations

import hashlib

import numpy as np

from melone_service.embeddings import (
    EmbeddingModelInfo,
    EmbeddingUnavailableError,
    FakeEmbeddingModel,
)
from melone_service.embeddings.indexer import EmbeddingIndexer
from melone_service.store.db import connect, initialize_database
from melone_service.store.embeddings import EmbeddingRepository
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository


def test_embedding_indexer_populates_missing_embeddings_in_batches(tmp_path):
    connection = _connect_initialized(tmp_path)
    try:
        first = _seed_ocr_chunk(
            connection,
            chunk_id="ocr_chunk_1",
            text="alpha screen text",
            created_at="2026-06-09T06:00:01.000Z",
        )
        second = _seed_ocr_chunk(
            connection,
            chunk_id="ocr_chunk_2",
            text="beta screen text",
            created_at="2026-06-09T06:00:02.000Z",
        )
        model = FakeEmbeddingModel(
            model="test-embedding-model",
            dimension=2,
            document_vectors={
                "alpha screen text": [1.0, 0.0],
                "beta screen text": [0.0, 1.0],
            },
        )
        repository = EmbeddingRepository(connection)
        indexer = EmbeddingIndexer(
            repository=repository,
            ocr_repository=OcrChunkRepository(connection),
            model=model,
            batch_size=1,
        )

        first_result = indexer.index_once()
        second_result = indexer.index_once()

        assert first_result.indexed_count == 1
        assert first_result.skipped_count == 0
        assert first_result.error_count == 0
        assert first_result.total_ocr_chunks == 2
        assert first_result.embedded_chunks == 1
        assert second_result.indexed_count == 1
        assert second_result.embedded_chunks == 2
        assert model.query_calls == []
        assert model.document_calls == ["alpha screen text", "beta screen text"]
        assert repository.count_current_chunk_embeddings(
            model="test-embedding-model",
            dimension=2,
        ) == 2
        first_stored = repository.get_chunk_embedding(
            chunk_id=first.id,
            model="test-embedding-model",
            dimension=2,
        )
        second_stored = repository.get_chunk_embedding(
            chunk_id=second.id,
            model="test-embedding-model",
            dimension=2,
        )
        assert first_stored is not None
        assert second_stored is not None
        np.testing.assert_allclose(
            first_stored.embedding,
            np.asarray([1.0, 0.0], dtype=np.float32),
        )
        np.testing.assert_allclose(
            second_stored.embedding,
            np.asarray([0.0, 1.0], dtype=np.float32),
        )
    finally:
        connection.close()


def test_embedding_indexer_replaces_stale_embeddings(tmp_path):
    connection = _connect_initialized(tmp_path)
    try:
        chunk = _seed_ocr_chunk(
            connection,
            chunk_id="ocr_chunk_stale",
            text="updated screen text",
            text_hash="hash-current",
        )
        repository = EmbeddingRepository(connection)
        repository.upsert_chunk_embedding(
            chunk_id=chunk.id,
            model="test-embedding-model",
            dimension=2,
            text_hash="hash-before-update",
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )
        connection.commit()
        model = FakeEmbeddingModel(
            model="test-embedding-model",
            dimension=2,
            document_vectors={"updated screen text": [0.0, 1.0]},
        )

        result = EmbeddingIndexer(
            repository=repository,
            ocr_repository=OcrChunkRepository(connection),
            model=model,
            batch_size=10,
        ).index_once()

        stored = repository.get_chunk_embedding(
            chunk_id=chunk.id,
            model="test-embedding-model",
            dimension=2,
        )
        assert result.indexed_count == 1
        assert result.embedded_chunks == 1
        assert stored is not None
        assert stored.text_hash == "hash-current"
        np.testing.assert_allclose(
            stored.embedding,
            np.asarray([0.0, 1.0], dtype=np.float32),
        )
    finally:
        connection.close()


def test_embedding_indexer_reports_model_unavailable_without_upserting(tmp_path):
    connection = _connect_initialized(tmp_path)
    try:
        _seed_ocr_chunk(connection, chunk_id="ocr_chunk_1")
        repository = EmbeddingRepository(connection)

        result = EmbeddingIndexer(
            repository=repository,
            ocr_repository=OcrChunkRepository(connection),
            model=_UnavailableEmbeddingModel(),
            batch_size=10,
        ).index_once()

        assert result.indexed_count == 0
        assert result.skipped_count == 0
        assert result.error_count == 1
        assert result.total_ocr_chunks == 1
        assert result.embedded_chunks == 0
        assert result.last_error_type == EmbeddingUnavailableError.__name__
        assert "embedding unavailable for test" in result.last_error_message
        assert repository.count_current_chunk_embeddings(
            model="unavailable-test-model",
            dimension=2,
        ) == 0
    finally:
        connection.close()


class _UnavailableEmbeddingModel:
    @property
    def info(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(
            provider="fake",
            model="unavailable-test-model",
            dimension=2,
        )

    def encode_query(self, query: str):
        raise AssertionError("query path should not be used by the indexer")

    def encode_document(self, text: str):
        raise EmbeddingUnavailableError("embedding unavailable for test")


def _connect_initialized(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    return connect(database_path)


def _seed_ocr_chunk(
    connection,
    *,
    chunk_id: str,
    text: str = "screen text",
    text_hash: str | None = None,
    created_at: str = "2026-06-09T06:00:01.000Z",
):
    session_id = f"screen_session_{chunk_id}"
    frame_id = f"screen_frame_{chunk_id}"
    source_key = f"test:source:{chunk_id}"
    retrieval_locator = f"url:https://example.test/{chunk_id}"
    started_at = "2026-06-09T06:00:00.000Z"
    screen_repository = ScreenRepository(connection)
    screen_repository.create_session(
        session_id=session_id,
        source_key=source_key,
        retrieval_locator=retrieval_locator,
        app_name="Chrome",
        bundle_id="com.google.Chrome",
        window_title="Docs",
        url=retrieval_locator.removeprefix("url:"),
        started_at=started_at,
        now=started_at,
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
    chunk = OcrChunkRepository(connection).insert_chunk(
        chunk_id=chunk_id,
        session_id=session_id,
        frame_id=frame_id,
        source_key=source_key,
        retrieval_locator=retrieval_locator,
        app_name="Chrome",
        window_title="Docs",
        url=retrieval_locator.removeprefix("url:"),
        text=text,
        text_hash=text_hash or hashlib.sha256(text.encode()).hexdigest(),
        created_at=created_at,
    )
    connection.commit()
    return chunk
