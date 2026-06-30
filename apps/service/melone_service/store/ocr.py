from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from melone_service.models import utc_timestamp


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


@dataclass(frozen=True)
class OcrChunk:
    id: str
    session_id: str
    frame_id: str
    source_key: str
    retrieval_locator: str
    app_name: str | None
    window_title: str | None
    url: str | None
    crop_bbox_json: str | None
    text: str
    text_hash: str
    provider: str | None
    model: str | None
    latency_ms: int | None
    created_at: str


class OcrChunkRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def count_chunks(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM ocr_chunks").fetchone()
        return int(row[0])

    def count_for_session(self, session_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM ocr_chunks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0])

    def count_fts_rows(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM ocr_chunks_fts").fetchone()
        return int(row[0])

    def latest_created_at(self) -> str | None:
        row = self.connection.execute(
            """
            SELECT MAX(created_at)
            FROM ocr_chunks
            """
        ).fetchone()
        return None if row[0] is None else str(row[0])

    def get_chunk(self, chunk_id: str) -> OcrChunk | None:
        row = self.connection.execute(
            f"""
            SELECT {_OCR_CHUNK_COLUMNS}
            FROM ocr_chunks
            WHERE id = ?
            """,
            (chunk_id,),
        ).fetchone()
        return None if row is None else _row_to_chunk(row)

    def get_chunk_by_text_hash(self, text_hash: str) -> OcrChunk | None:
        row = self.connection.execute(
            f"""
            SELECT {_OCR_CHUNK_COLUMNS}
            FROM ocr_chunks
            WHERE text_hash = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (text_hash,),
        ).fetchone()
        return None if row is None else _row_to_chunk(row)

    def list_chunks(self) -> list[OcrChunk]:
        rows = self.connection.execute(
            f"""
            SELECT {_OCR_CHUNK_COLUMNS}
            FROM ocr_chunks
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
        return [_row_to_chunk(row) for row in rows]

    def text_hash_exists(self, text_hash: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM ocr_chunks
            WHERE text_hash = ?
            LIMIT 1
            """,
            (text_hash,),
        ).fetchone()
        return row is not None

    def insert_chunk(
        self,
        *,
        session_id: str,
        frame_id: str,
        source_key: str,
        retrieval_locator: str,
        text: str,
        text_hash: str,
        chunk_id: str | None = None,
        app_name: str | None = None,
        window_title: str | None = None,
        url: str | None = None,
        crop_bbox_json: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        latency_ms: int | None = None,
        created_at: datetime | str | None = None,
    ) -> OcrChunk:
        row = self.connection.execute(
            f"""
            INSERT INTO ocr_chunks (
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
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING {_OCR_CHUNK_COLUMNS}
            """,
            (
                chunk_id or _new_chunk_id(),
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
                _format_timestamp(created_at),
            ),
        ).fetchone()
        return _row_to_chunk(row)

    def upsert_fts(self, chunk: OcrChunk) -> None:
        self.connection.execute(
            """
            DELETE FROM ocr_chunks_fts
            WHERE chunk_id = ?
            """,
            (chunk.id,),
        )
        self.connection.execute(
            """
            INSERT INTO ocr_chunks_fts (
              chunk_id,
              source_key,
              retrieval_locator,
              title,
              app_name,
              text
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                chunk.id,
                chunk.source_key,
                chunk.retrieval_locator,
                chunk.window_title,
                chunk.app_name,
                chunk.text,
            ),
        )

    def insert_chunk_with_fts(self, **kwargs: Any) -> OcrChunk:
        chunk = self.insert_chunk(**kwargs)
        self.upsert_fts(chunk)
        return chunk


def _new_chunk_id() -> str:
    return f"ocr_chunk_{uuid.uuid4().hex}"


def _row_to_chunk(row: sqlite3.Row) -> OcrChunk:
    return OcrChunk(
        id=row["id"],
        session_id=row["session_id"],
        frame_id=row["frame_id"],
        source_key=row["source_key"],
        retrieval_locator=row["retrieval_locator"],
        app_name=row["app_name"],
        window_title=row["window_title"],
        url=row["url"],
        crop_bbox_json=row["crop_bbox_json"],
        text=row["text"],
        text_hash=row["text_hash"],
        provider=row["provider"],
        model=row["model"],
        latency_ms=row["latency_ms"],
        created_at=row["created_at"],
    )


def _format_timestamp(value: datetime | str | None) -> str:
    if value is None:
        return utc_timestamp()
    if isinstance(value, str):
        return value
    return utc_timestamp(value)
