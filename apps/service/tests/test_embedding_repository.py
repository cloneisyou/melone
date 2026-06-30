from __future__ import annotations

import hashlib

import numpy as np
import pytest

from melone_service.store.db import connect, initialize_database
from melone_service.store.embeddings import (
    EmbeddingRepository,
    decode_embedding_blob,
    encode_embedding_blob,
)
from melone_service.store.ocr import OcrChunk, OcrChunkRepository
from melone_service.store.screen import ScreenRepository


def test_embedding_repository_upserts_and_fetches_chunk_embedding(tmp_path):
    connection = _connect_initialized(tmp_path)
    try:
        chunk = _seed_ocr_chunk(connection, chunk_id="ocr_chunk_1")
        repository = EmbeddingRepository(connection)

        stored = repository.upsert_chunk_embedding(
            chunk_id=chunk.id,
            model="test-model",
            dimension=2,
            text_hash=chunk.text_hash,
            embedding=np.asarray([0.6, 0.8], dtype=np.float32),
            now="2026-06-09T06:00:10.000Z",
        )
        fetched = repository.get_chunk_embedding(
            chunk_id=chunk.id,
            model="test-model",
            dimension=2,
        )

        assert fetched is not None
        assert stored.chunk_id == fetched.chunk_id
        assert stored.model == fetched.model
        assert stored.dimension == fetched.dimension
        assert stored.text_hash == fetched.text_hash
        assert fetched.chunk_id == chunk.id
        assert fetched.model == "test-model"
        assert fetched.dimension == 2
        assert fetched.text_hash == chunk.text_hash
        assert fetched.created_at == "2026-06-09T06:00:10.000Z"
        assert fetched.updated_at == "2026-06-09T06:00:10.000Z"
        np.testing.assert_allclose(
            fetched.embedding,
            np.asarray([0.6, 0.8], dtype=np.float32),
        )
    finally:
        connection.close()


def test_embedding_repository_replaces_stale_text_hash_for_same_identity(tmp_path):
    connection = _connect_initialized(tmp_path)
    try:
        chunk = _seed_ocr_chunk(connection, chunk_id="ocr_chunk_1")
        repository = EmbeddingRepository(connection)

        repository.upsert_chunk_embedding(
            chunk_id=chunk.id,
            model="test-model",
            dimension=2,
            text_hash="old-text-hash",
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
            now="2026-06-09T06:00:10.000Z",
        )
        replaced = repository.upsert_chunk_embedding(
            chunk_id=chunk.id,
            model="test-model",
            dimension=2,
            text_hash="new-text-hash",
            embedding=np.asarray([0.0, 1.0], dtype=np.float32),
            now="2026-06-09T06:00:20.000Z",
        )
        row_count = connection.execute(
            "SELECT COUNT(*) FROM ocr_chunk_embeddings"
        ).fetchone()[0]

        assert row_count == 1
        assert replaced.text_hash == "new-text-hash"
        assert replaced.created_at == "2026-06-09T06:00:10.000Z"
        assert replaced.updated_at == "2026-06-09T06:00:20.000Z"
        np.testing.assert_allclose(
            replaced.embedding,
            np.asarray([0.0, 1.0], dtype=np.float32),
        )
    finally:
        connection.close()


def test_embedding_repository_upsert_leaves_transaction_control_to_caller(tmp_path):
    connection = _connect_initialized(tmp_path)
    try:
        chunk = _seed_ocr_chunk(connection, chunk_id="ocr_chunk_1")
        repository = EmbeddingRepository(connection)

        repository.upsert_chunk_embedding(
            chunk_id=chunk.id,
            model="test-model",
            dimension=2,
            text_hash=chunk.text_hash,
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )

        connection.rollback()

        assert (
            repository.get_chunk_embedding(
                chunk_id=chunk.id,
                model="test-model",
                dimension=2,
            )
            is None
        )
    finally:
        connection.close()


def test_embedding_repository_lists_missing_and_stale_ocr_chunks(tmp_path):
    connection = _connect_initialized(tmp_path)
    try:
        embedded = _seed_ocr_chunk(
            connection,
            chunk_id="ocr_chunk_embedded",
            text="already embedded",
            text_hash="hash-embedded",
            created_at="2026-06-09T06:00:01.000Z",
        )
        stale = _seed_ocr_chunk(
            connection,
            chunk_id="ocr_chunk_stale",
            text="changed after embedding",
            text_hash="hash-current",
            created_at="2026-06-09T06:00:02.000Z",
        )
        missing = _seed_ocr_chunk(
            connection,
            chunk_id="ocr_chunk_missing",
            text="not embedded yet",
            text_hash="hash-missing",
            created_at="2026-06-09T06:00:03.000Z",
        )
        repository = EmbeddingRepository(connection)
        repository.upsert_chunk_embedding(
            chunk_id=embedded.id,
            model="test-model",
            dimension=2,
            text_hash=embedded.text_hash,
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )
        repository.upsert_chunk_embedding(
            chunk_id=stale.id,
            model="test-model",
            dimension=2,
            text_hash="hash-before-change",
            embedding=np.asarray([0.0, 1.0], dtype=np.float32),
        )

        chunks = repository.list_missing_ocr_chunks(
            model="test-model",
            dimension=2,
            limit=10,
        )

        assert [chunk.id for chunk in chunks] == [stale.id, missing.id]
    finally:
        connection.close()


def test_embedding_repository_lists_current_embeddings_for_search(tmp_path):
    connection = _connect_initialized(tmp_path)
    try:
        recent = _seed_ocr_chunk(
            connection,
            chunk_id="ocr_chunk_recent",
            text="semantic screen text",
            text_hash="hash-recent",
            started_at="2026-06-09T06:00:00.000Z",
            created_at="2026-06-09T06:00:05.000Z",
        )
        old = _seed_ocr_chunk(
            connection,
            chunk_id="ocr_chunk_old",
            text="older screen text",
            text_hash="hash-old",
            started_at="2026-06-09T05:00:00.000Z",
            ended_at="2026-06-09T05:30:00.000Z",
            created_at="2026-06-09T05:00:05.000Z",
        )
        stale = _seed_ocr_chunk(
            connection,
            chunk_id="ocr_chunk_stale",
            text="stale screen text",
            text_hash="hash-stale-old",
            started_at="2026-06-09T06:05:00.000Z",
            created_at="2026-06-09T06:05:05.000Z",
        )
        repository = EmbeddingRepository(connection)
        repository.upsert_chunk_embedding(
            chunk_id=recent.id,
            model="test-model",
            dimension=2,
            text_hash=recent.text_hash,
            embedding=np.asarray([0.6, 0.8], dtype=np.float32),
        )
        repository.upsert_chunk_embedding(
            chunk_id=old.id,
            model="test-model",
            dimension=2,
            text_hash=old.text_hash,
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )
        repository.upsert_chunk_embedding(
            chunk_id=stale.id,
            model="test-model",
            dimension=2,
            text_hash=stale.text_hash,
            embedding=np.asarray([0.0, 1.0], dtype=np.float32),
        )
        connection.execute(
            """
            UPDATE ocr_chunks
            SET text_hash = ?
            WHERE id = ?
            """,
            ("hash-stale-new", stale.id),
        )
        connection.commit()

        rows = repository.list_embeddings_for_search(
            model="test-model",
            dimension=2,
            since="2026-06-09T05:45:00.000Z",
        )

        assert [row.chunk_id for row in rows] == [recent.id]
        assert rows[0].session_id == recent.session_id
        assert rows[0].frame_id == recent.frame_id
        assert rows[0].source_key == recent.source_key
        assert rows[0].retrieval_locator == recent.retrieval_locator
        assert rows[0].text == recent.text
        assert rows[0].text_hash == recent.text_hash
        np.testing.assert_allclose(
            rows[0].embedding,
            np.asarray([0.6, 0.8], dtype=np.float32),
        )
    finally:
        connection.close()


def test_embedding_blob_helpers_validate_dimension_and_length():
    blob = encode_embedding_blob(
        np.asarray([0.6, 0.8], dtype=np.float32),
        dimension=2,
    )

    assert len(blob) == 8
    np.testing.assert_allclose(
        decode_embedding_blob(blob, dimension=2),
        np.asarray([0.6, 0.8], dtype=np.float32),
    )

    with pytest.raises(ValueError, match="expected 8"):
        decode_embedding_blob(blob[:4], dimension=2)

    with pytest.raises(ValueError, match="expected 3"):
        encode_embedding_blob([0.6, 0.8], dimension=3)

    with pytest.raises(ValueError, match="normalized"):
        encode_embedding_blob([2.0, 0.0], dimension=2)


def test_embedding_cache_cascades_when_ocr_chunk_is_deleted(tmp_path):
    connection = _connect_initialized(tmp_path)
    try:
        chunk = _seed_ocr_chunk(connection, chunk_id="ocr_chunk_1")
        repository = EmbeddingRepository(connection)
        repository.upsert_chunk_embedding(
            chunk_id=chunk.id,
            model="test-model",
            dimension=2,
            text_hash=chunk.text_hash,
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )

        with connection:
            connection.execute("DELETE FROM ocr_chunks WHERE id = ?", (chunk.id,))

        assert (
            repository.get_chunk_embedding(
                chunk_id=chunk.id,
                model="test-model",
                dimension=2,
            )
            is None
        )
    finally:
        connection.close()


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
    started_at: str = "2026-06-09T06:00:00.000Z",
    ended_at: str | None = None,
    created_at: str = "2026-06-09T06:00:01.000Z",
) -> OcrChunk:
    session_id = f"screen_session_{chunk_id}"
    frame_id = f"screen_frame_{chunk_id}"
    source_key = f"test:source:{chunk_id}"
    retrieval_locator = f"url:https://example.test/{chunk_id}"
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
