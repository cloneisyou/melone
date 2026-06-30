from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from numpy.typing import NDArray

from melone_service.models import utc_timestamp
from melone_service.store.ocr import OcrChunk, _row_to_chunk

EmbeddingVector = NDArray[np.float32]


_OCR_CHUNK_COLUMNS = """
  id,
  session_id,
  frame_id,
  source_key,
  retrieval_locator,
  app_name,
  window_title,
  url,
  crop_bbox_json,
  text,
  text_hash,
  provider,
  model,
  latency_ms,
  created_at
"""

_OCR_CHUNK_EMBEDDING_COLUMNS = """
  chunk_id,
  model,
  dimension,
  text_hash,
  embedding,
  created_at,
  updated_at
"""


@dataclass(frozen=True)
class OcrChunkEmbedding:
    chunk_id: str
    model: str
    dimension: int
    text_hash: str
    embedding: EmbeddingVector
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OcrChunkEmbeddingSearchRow:
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
    text_hash: str
    embedding: EmbeddingVector


class EmbeddingRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def upsert_chunk_embedding(
        self,
        *,
        chunk_id: str,
        model: str,
        dimension: int,
        text_hash: str,
        embedding: Sequence[float] | NDArray[np.floating],
        now: datetime | str | None = None,
    ) -> OcrChunkEmbedding:
        _validate_cache_identity(model=model, dimension=dimension)
        blob = encode_embedding_blob(embedding, dimension=dimension)
        now_iso = _format_timestamp(now)

        row = self.connection.execute(
            f"""
            INSERT INTO ocr_chunk_embeddings (
              chunk_id,
              model,
              dimension,
              text_hash,
              embedding,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chunk_id, model, dimension) DO UPDATE SET
              text_hash = excluded.text_hash,
              embedding = excluded.embedding,
              updated_at = excluded.updated_at
            RETURNING {_OCR_CHUNK_EMBEDDING_COLUMNS}
            """,
            (
                chunk_id,
                model,
                dimension,
                text_hash,
                blob,
                now_iso,
                now_iso,
            ),
        ).fetchone()

        return _row_to_chunk_embedding(row)

    def get_chunk_embedding(
        self,
        *,
        chunk_id: str,
        model: str,
        dimension: int,
    ) -> OcrChunkEmbedding | None:
        _validate_cache_identity(model=model, dimension=dimension)
        row = self.connection.execute(
            f"""
            SELECT {_OCR_CHUNK_EMBEDDING_COLUMNS}
            FROM ocr_chunk_embeddings
            WHERE chunk_id = ?
              AND model = ?
              AND dimension = ?
            """,
            (chunk_id, model, dimension),
        ).fetchone()
        return None if row is None else _row_to_chunk_embedding(row)

    def count_current_chunk_embeddings(
        self,
        *,
        model: str,
        dimension: int,
    ) -> int:
        _validate_cache_identity(model=model, dimension=dimension)
        row = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM ocr_chunk_embeddings AS e
            JOIN ocr_chunks AS c
              ON c.id = e.chunk_id
            WHERE e.model = ?
              AND e.dimension = ?
              AND e.text_hash = c.text_hash
            """,
            (model, dimension),
        ).fetchone()
        return int(row[0])

    def list_missing_ocr_chunks(
        self,
        *,
        model: str,
        dimension: int,
        limit: int,
    ) -> list[OcrChunk]:
        return self.list_missing_or_stale_ocr_chunks(
            model=model,
            dimension=dimension,
            limit=limit,
        )

    def list_missing_or_stale_ocr_chunks(
        self,
        *,
        model: str,
        dimension: int,
        limit: int,
    ) -> list[OcrChunk]:
        _validate_cache_identity(model=model, dimension=dimension)
        _validate_limit(limit)
        rows = self.connection.execute(
            f"""
            SELECT {_select_ocr_chunk_columns("c")}
            FROM ocr_chunks AS c
            LEFT JOIN ocr_chunk_embeddings AS e
              ON e.chunk_id = c.id
             AND e.model = ?
             AND e.dimension = ?
            WHERE e.chunk_id IS NULL
               OR e.text_hash != c.text_hash
            ORDER BY c.created_at ASC, c.id ASC
            LIMIT ?
            """,
            (model, dimension, limit),
        ).fetchall()
        return [_row_to_chunk(row) for row in rows]

    def list_embeddings_for_search(
        self,
        *,
        model: str,
        dimension: int,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[OcrChunkEmbeddingSearchRow]:
        _validate_cache_identity(model=model, dimension=dimension)
        clauses = [
            "e.model = ?",
            "e.dimension = ?",
            "e.text_hash = c.text_hash",
        ]
        params: list[object] = [model, dimension]

        if since is not None:
            clauses.append("COALESCE(s.ended_at, s.started_at) >= ?")
            params.append(since)

        limit_sql = ""
        if limit is not None:
            _validate_limit(limit)
            limit_sql = "LIMIT ?"
            params.append(limit)

        rows = self.connection.execute(
            f"""
            SELECT
              c.id AS chunk_id,
              c.session_id,
              c.frame_id,
              c.source_key,
              c.retrieval_locator,
              c.app_name,
              c.window_title,
              c.url,
              s.started_at AS session_started_at,
              s.ended_at AS session_ended_at,
              c.created_at AS chunk_created_at,
              c.text,
              c.text_hash,
              e.embedding
            FROM ocr_chunk_embeddings AS e
            JOIN ocr_chunks AS c
              ON c.id = e.chunk_id
            JOIN screen_sessions AS s
              ON s.id = c.session_id
            WHERE {" AND ".join(clauses)}
            ORDER BY c.created_at DESC, c.id ASC
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [
            OcrChunkEmbeddingSearchRow(
                **_embedding_search_row_values(row, dimension=dimension)
            )
            for row in rows
        ]

    def iter_embeddings_for_search(
        self,
        *,
        model: str,
        dimension: int,
        since: str | None = None,
        batch_size: int = 256,
    ) -> Iterator[OcrChunkEmbeddingSearchRow]:
        _validate_cache_identity(model=model, dimension=dimension)
        _validate_limit(batch_size)
        clauses = [
            "e.model = ?",
            "e.dimension = ?",
            "e.text_hash = c.text_hash",
        ]
        params: list[object] = [model, dimension]

        if since is not None:
            clauses.append("COALESCE(s.ended_at, s.started_at) >= ?")
            params.append(since)

        cursor = self.connection.execute(
            f"""
            SELECT
              c.id AS chunk_id,
              c.session_id,
              c.frame_id,
              c.source_key,
              c.retrieval_locator,
              c.app_name,
              c.window_title,
              c.url,
              s.started_at AS session_started_at,
              s.ended_at AS session_ended_at,
              c.created_at AS chunk_created_at,
              c.text,
              c.text_hash,
              e.embedding
            FROM ocr_chunk_embeddings AS e
            JOIN ocr_chunks AS c
              ON c.id = e.chunk_id
            JOIN screen_sessions AS s
              ON s.id = c.session_id
            WHERE {" AND ".join(clauses)}
            ORDER BY c.created_at DESC, c.id ASC
            """,
            params,
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                yield OcrChunkEmbeddingSearchRow(
                    **_embedding_search_row_values(row, dimension=dimension)
                )


def encode_embedding_blob(
    vector: Sequence[float] | NDArray[np.floating],
    *,
    dimension: int,
) -> bytes:
    array = _coerce_embedding_array(vector, dimension=dimension)
    blob = array.astype("<f4", copy=False).tobytes(order="C")
    expected_bytes = _expected_blob_bytes(dimension)
    if len(blob) != expected_bytes:
        raise ValueError(
            f"embedding blob is {len(blob)} bytes; expected {expected_bytes}"
        )
    return blob


def decode_embedding_blob(blob: bytes, *, dimension: int) -> EmbeddingVector:
    expected_bytes = _expected_blob_bytes(dimension)
    if len(blob) != expected_bytes:
        raise ValueError(
            f"embedding blob is {len(blob)} bytes; expected {expected_bytes}"
        )

    array = np.frombuffer(blob, dtype="<f4")
    return _coerce_embedding_array(array, dimension=dimension)


def _row_to_chunk_embedding(row: sqlite3.Row) -> OcrChunkEmbedding:
    dimension = int(row["dimension"])
    return OcrChunkEmbedding(
        chunk_id=row["chunk_id"],
        model=row["model"],
        dimension=dimension,
        text_hash=row["text_hash"],
        embedding=decode_embedding_blob(row["embedding"], dimension=dimension),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _embedding_search_row_values(
    row: sqlite3.Row,
    *,
    dimension: int,
) -> dict[str, object]:
    return {
        "chunk_id": row["chunk_id"],
        "session_id": row["session_id"],
        "frame_id": row["frame_id"],
        "source_key": row["source_key"],
        "retrieval_locator": row["retrieval_locator"],
        "app_name": row["app_name"],
        "window_title": row["window_title"],
        "url": row["url"],
        "session_started_at": row["session_started_at"],
        "session_ended_at": row["session_ended_at"],
        "chunk_created_at": row["chunk_created_at"],
        "text": row["text"],
        "text_hash": row["text_hash"],
        "embedding": decode_embedding_blob(
            row["embedding"],
            dimension=dimension,
        ),
    }


def _select_ocr_chunk_columns(alias: str) -> str:
    return ", ".join(
        f"{alias}.{column.strip()}" for column in _OCR_CHUNK_COLUMNS.split(",")
    )


def _coerce_embedding_array(
    vector: Sequence[float] | NDArray[np.floating],
    *,
    dimension: int,
) -> EmbeddingVector:
    _validate_dimension(dimension)
    try:
        array = np.asarray(vector, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("embedding vector must contain numeric values") from exc

    if array.ndim != 1:
        raise ValueError("embedding vector must be one-dimensional")
    if array.shape[0] != dimension:
        raise ValueError(
            f"embedding vector has {array.shape[0]} dimensions; expected {dimension}"
        )
    if not np.all(np.isfinite(array)):
        raise ValueError("embedding vector must contain only finite values")

    norm = float(np.linalg.norm(array))
    if not np.isfinite(norm) or not np.isclose(norm, 1.0, rtol=1e-4, atol=1e-6):
        raise ValueError("embedding vector must be normalized")

    return np.ascontiguousarray(array, dtype=np.float32)


def _validate_cache_identity(*, model: str, dimension: int) -> None:
    if not model.strip():
        raise ValueError("embedding model must not be empty")
    _validate_dimension(dimension)


def _validate_dimension(dimension: int) -> None:
    if dimension <= 0:
        raise ValueError("embedding dimension must be greater than zero")


def _expected_blob_bytes(dimension: int) -> int:
    _validate_dimension(dimension)
    return dimension * np.dtype("<f4").itemsize


def _validate_limit(limit: int) -> None:
    if limit <= 0:
        raise ValueError("limit must be greater than zero")


def _format_timestamp(value: datetime | str | None) -> str:
    if value is None:
        return utc_timestamp()
    if isinstance(value, str):
        return utc_timestamp(_parse_timestamp(value))
    return utc_timestamp(value)


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
