from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from melone_service.models import AppContext, NormalizedEvent, WindowContext


DEFAULT_EVENT_LIMIT = 5000


class EventRepository:
    # normalized event 전용 저장소로 SQL 접근을 한곳에 모읍니다.
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert(self, event: NormalizedEvent) -> None:
        # NormalizedEvent의 중첩 필드를 events 테이블의 평평한 컬럼 구조로 저장합니다.
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO events (
                  id,
                  timestamp,
                  type,
                  app_name,
                  bundle_id,
                  pid,
                  window_title,
                  url,
                  source,
                  metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.timestamp,
                    event.type,
                    event.app_name,
                    event.bundle_id,
                    event.pid,
                    event.window_title,
                    event.url,
                    event.source,
                    json.dumps(event.metadata, sort_keys=True),
                ),
            )

    def list(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        event_type: str | None = None,
        limit: int = DEFAULT_EVENT_LIMIT,
    ) -> list[NormalizedEvent]:
        # CLI 조회를 위해 시간, 타입 필터를 조합하고 저장된 순서대로 이벤트를 복원합니다.
        clauses: list[str] = []
        params: list[object] = []

        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        if until:
            clauses.append("timestamp <= ?")
            params.append(until)
        if event_type:
            clauses.append("type = ?")
            params.append(event_type)

        query = """
            SELECT
              id,
              timestamp,
              type,
              app_name,
              bundle_id,
              pid,
              window_title,
              url,
              source,
              metadata_json
            FROM events
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY timestamp ASC, id ASC LIMIT ?"
        params.append(limit)

        rows = self.connection.execute(query, params).fetchall()
        return [_row_to_event(row) for row in rows]

    def list_by_types(
        self,
        event_types: Sequence[str],
        *,
        since: str | None = None,
        limit: int = DEFAULT_EVENT_LIMIT,
    ) -> list[NormalizedEvent]:
        types = _event_types(event_types)
        if not types:
            return []

        placeholders = ", ".join("?" for _ in types)
        clauses = [f"type IN ({placeholders})"]
        params: list[object] = list(types)

        if since:
            clauses.append("timestamp >= ?")
            params.append(since)

        query = f"""
            SELECT
              id,
              timestamp,
              type,
              app_name,
              bundle_id,
              pid,
              window_title,
              url,
              source,
              metadata_json
            FROM events
            WHERE {" AND ".join(clauses)}
            ORDER BY timestamp ASC, id ASC
            LIMIT ?
        """
        params.append(limit)

        rows = self.connection.execute(query, params).fetchall()
        return [_row_to_event(row) for row in rows]

    def list_recent_by_types(
        self,
        event_types: Sequence[str],
        *,
        since: str | None = None,
        limit: int = DEFAULT_EVENT_LIMIT,
    ) -> list[NormalizedEvent]:
        types = _event_types(event_types)
        if not types:
            return []

        placeholders = ", ".join("?" for _ in types)
        clauses = [f"type IN ({placeholders})"]
        params: list[object] = list(types)

        if since:
            clauses.append("timestamp >= ?")
            params.append(since)

        query = f"""
            SELECT
              id,
              timestamp,
              type,
              app_name,
              bundle_id,
              pid,
              window_title,
              url,
              source,
              metadata_json
            FROM events
            WHERE {" AND ".join(clauses)}
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        """
        params.append(limit)

        rows = self.connection.execute(query, params).fetchall()
        return [_row_to_event(row) for row in reversed(rows)]

    def latest(self, *, event_type: str | None = None) -> NormalizedEvent | None:
        clauses: list[str] = []
        params: list[object] = []

        if event_type:
            clauses.append("type = ?")
            params.append(event_type)

        query = """
            SELECT
              id,
              timestamp,
              type,
              app_name,
              bundle_id,
              pid,
              window_title,
              url,
              source,
              metadata_json
            FROM events
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY timestamp DESC, id DESC LIMIT 1"

        row = self.connection.execute(query, params).fetchone()
        return None if row is None else _row_to_event(row)

    def latest_by_types(
        self,
        event_types: Sequence[str],
    ) -> NormalizedEvent | None:
        types = _event_types(event_types)
        if not types:
            return None

        placeholders = ", ".join("?" for _ in types)
        query = f"""
            SELECT
              id,
              timestamp,
              type,
              app_name,
              bundle_id,
              pid,
              window_title,
              url,
              source,
              metadata_json
            FROM events
            WHERE type IN ({placeholders})
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
        """

        row = self.connection.execute(query, list(types)).fetchone()
        return None if row is None else _row_to_event(row)


def _row_to_event(row: sqlite3.Row) -> NormalizedEvent:
    # DB row를 앱 코드에서 쓰는 NormalizedEvent 객체로 되돌립니다.
    return NormalizedEvent(
        id=row["id"],
        timestamp=row["timestamp"],
        type=row["type"],
        app=AppContext(
            name=row["app_name"],
            bundle_id=row["bundle_id"],
            pid=row["pid"],
        ),
        window=WindowContext(title=row["window_title"]),
        url=row["url"],
        source=row["source"],
        metadata=_metadata_from_json(row["metadata_json"]),
    )


def _metadata_from_json(value: str) -> dict[str, object]:
    # metadata_json은 항상 JSON object여야 하므로 읽는 시점에도 검증합니다.
    metadata = json.loads(value)
    if not isinstance(metadata, dict):
        raise ValueError("event metadata_json must contain a JSON object")

    return metadata


def _event_types(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(value for value in dict.fromkeys(values) if value)
