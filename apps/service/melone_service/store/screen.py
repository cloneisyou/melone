from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from melone_service.models import utc_timestamp


_SCREEN_SESSION_COLUMNS = """
  id,
  source_key,
  retrieval_locator,
  app_name,
  bundle_id,
  window_title,
  url,
  started_at,
  ended_at,
  status,
  created_at,
  updated_at
"""


_SCREEN_FRAME_COLUMNS = """
  id,
  session_id,
  captured_at,
  image_path,
  sha256,
  perceptual_hash,
  diff_score,
  width,
  height,
  status,
  created_at,
  image_retention_state,
  image_retention_updated_at
"""

IMAGE_RETENTION_RETAINED = "retained"
IMAGE_RETENTION_RETAINED_FOR_OCR = "retained_for_ocr"
IMAGE_RETENTION_RETAINED_FOR_RETRY = "retained_for_retry"
IMAGE_RETENTION_RETAINED_AFTER_DEAD_JOB = "retained_after_dead_job"
IMAGE_RETENTION_DELETE_PENDING_AFTER_INDEXING = "delete_pending_after_indexing"
IMAGE_RETENTION_DELETED_AFTER_INDEXING = "deleted_after_indexing"
IMAGE_RETENTION_MISSING_AFTER_INDEXING = "missing_after_indexing"
IMAGE_RETENTION_DELETE_FAILED_AFTER_INDEXING = "delete_failed_after_indexing"

IMAGE_RETENTION_STATES = frozenset(
    {
        IMAGE_RETENTION_RETAINED,
        IMAGE_RETENTION_RETAINED_FOR_OCR,
        IMAGE_RETENTION_RETAINED_FOR_RETRY,
        IMAGE_RETENTION_RETAINED_AFTER_DEAD_JOB,
        IMAGE_RETENTION_DELETE_PENDING_AFTER_INDEXING,
        IMAGE_RETENTION_DELETED_AFTER_INDEXING,
        IMAGE_RETENTION_MISSING_AFTER_INDEXING,
        IMAGE_RETENTION_DELETE_FAILED_AFTER_INDEXING,
    }
)


@dataclass(frozen=True)
class ScreenSession:
    id: str
    source_key: str
    retrieval_locator: str
    app_name: str | None
    bundle_id: str | None
    window_title: str | None
    url: str | None
    started_at: str
    ended_at: str | None
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ScreenFrame:
    id: str
    session_id: str
    captured_at: str
    image_path: str
    sha256: str
    perceptual_hash: str | None
    diff_score: float | None
    width: int
    height: int
    status: str
    created_at: str
    image_retention_state: str
    image_retention_updated_at: str | None


@dataclass(frozen=True)
class ScenePreview:
    """First retained screenshot of a scene (session), for display to the user."""

    session_id: str
    app_name: str | None
    window_title: str | None
    url: str | None
    started_at: str
    ended_at: str | None
    frame_id: str
    captured_at: str
    image_path: str


class ScreenRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def count_sessions(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM screen_sessions"
        ).fetchone()
        return int(row[0])

    def count_frames(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM screen_frames").fetchone()
        return int(row[0])

    def get_session(self, session_id: str) -> ScreenSession | None:
        row = self.connection.execute(
            f"""
            SELECT {_SCREEN_SESSION_COLUMNS}
            FROM screen_sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        return None if row is None else _row_to_session(row)

    def get_frame(self, frame_id: str) -> ScreenFrame | None:
        row = self.connection.execute(
            f"""
            SELECT {_SCREEN_FRAME_COLUMNS}
            FROM screen_frames
            WHERE id = ?
            """,
            (frame_id,),
        ).fetchone()
        return None if row is None else _row_to_frame(row)

    def get_frame_by_sha256(
        self,
        *,
        session_id: str,
        sha256: str,
    ) -> ScreenFrame | None:
        row = self.connection.execute(
            f"""
            SELECT {_SCREEN_FRAME_COLUMNS}
            FROM screen_frames
            WHERE session_id = ?
              AND sha256 = ?
            ORDER BY captured_at ASC, id ASC
            LIMIT 1
            """,
            (session_id, sha256),
        ).fetchone()
        return None if row is None else _row_to_frame(row)

    def list_session_frames(self, session_id: str) -> list[ScreenFrame]:
        rows = self.connection.execute(
            f"""
            SELECT {_SCREEN_FRAME_COLUMNS}
            FROM screen_frames
            WHERE session_id = ?
            ORDER BY captured_at ASC, created_at ASC, id ASC
            """,
            (session_id,),
        ).fetchall()
        return [_row_to_frame(row) for row in rows]

    def get_latest_open_session(self) -> ScreenSession | None:
        row = self.connection.execute(
            f"""
            SELECT {_SCREEN_SESSION_COLUMNS}
            FROM screen_sessions
            WHERE status = 'open'
            ORDER BY started_at DESC, created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        return None if row is None else _row_to_session(row)

    def list_top_scene_previews(self, *, limit: int = 12) -> list[ScenePreview]:
        # One preview per context (source_key): the latest session that has a
        # retained first screenshot, ordered by context rank (high-ranked first),
        # falling back to recency. Powers the home carousel.
        if limit <= 0:
            return []

        rows = self.connection.execute(
            """
            SELECT
              s.id AS session_id,
              s.app_name AS app_name,
              s.window_title AS window_title,
              s.url AS url,
              s.started_at AS started_at,
              s.ended_at AS ended_at,
              f.id AS frame_id,
              f.captured_at AS captured_at,
              f.image_path AS image_path
            FROM screen_sessions s
            JOIN screen_frames f ON f.id = (
              SELECT cf.id
              FROM screen_frames cf
              WHERE cf.session_id = s.id
                AND cf.status = 'selected'
                AND cf.image_retention_state = ?
              ORDER BY cf.captured_at ASC, cf.created_at ASC, cf.id ASC
              LIMIT 1
            )
            LEFT JOIN context_rank_scores r ON r.source_key = s.source_key
            WHERE s.id = (
              SELECT s2.id
              FROM screen_sessions s2
              WHERE s2.source_key = s.source_key
                AND EXISTS (
                  SELECT 1
                  FROM screen_frames cf2
                  WHERE cf2.session_id = s2.id
                    AND cf2.status = 'selected'
                    AND cf2.image_retention_state = ?
                )
              ORDER BY s2.started_at DESC, s2.created_at DESC, s2.id DESC
              LIMIT 1
            )
            ORDER BY COALESCE(r.score, 0) DESC, s.started_at DESC, s.id DESC
            LIMIT ?
            """,
            (IMAGE_RETENTION_RETAINED, IMAGE_RETENTION_RETAINED, limit),
        ).fetchall()
        return [_row_to_scene_preview(row) for row in rows]

    def list_recent_sessions(
        self, *, before: str | None = None, limit: int = 40
    ) -> list[ScreenSession]:
        # Finalized scenes for the timeline, most-recent first. `before` is a
        # keyset cursor (started_at) for paginating into older scenes.
        if limit <= 0:
            return []
        clauses = ["status = 'finalized'"]
        params: list[object] = []
        if before is not None:
            clauses.append("started_at < ?")
            params.append(before)
        params.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT {_SCREEN_SESSION_COLUMNS}
            FROM screen_sessions
            WHERE {" AND ".join(clauses)}
            ORDER BY started_at DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def get_session_keyframe(self, session_id: str) -> ScreenFrame | None:
        # The scene's representative screenshot: its first selected, retained frame.
        row = self.connection.execute(
            f"""
            SELECT {_SCREEN_FRAME_COLUMNS}
            FROM screen_frames
            WHERE session_id = ?
              AND status = 'selected'
              AND image_retention_state = ?
            ORDER BY captured_at ASC, created_at ASC, id ASC
            LIMIT 1
            """,
            (session_id, IMAGE_RETENTION_RETAINED),
        ).fetchone()
        return None if row is None else _row_to_frame(row)

    def get_scene_preview_for_source_key(
        self, source_key: str
    ) -> ScenePreview | None:
        # The latest session for this context that still has a retained first
        # screenshot — the representative thumbnail for a search result.
        row = self.connection.execute(
            """
            SELECT
              s.id AS session_id,
              s.app_name AS app_name,
              s.window_title AS window_title,
              s.url AS url,
              s.started_at AS started_at,
              s.ended_at AS ended_at,
              f.id AS frame_id,
              f.captured_at AS captured_at,
              f.image_path AS image_path
            FROM screen_sessions s
            JOIN screen_frames f ON f.id = (
              SELECT cf.id
              FROM screen_frames cf
              WHERE cf.session_id = s.id
                AND cf.status = 'selected'
                AND cf.image_retention_state = ?
              ORDER BY cf.captured_at ASC, cf.created_at ASC, cf.id ASC
              LIMIT 1
            )
            WHERE s.source_key = ?
            ORDER BY s.started_at DESC, s.created_at DESC, s.id DESC
            LIMIT 1
            """,
            (IMAGE_RETENTION_RETAINED, source_key),
        ).fetchone()
        if row is not None:
            return _row_to_scene_preview(row)
        # Brief visits (window/tab switches) often close before a screenshot is
        # captured, so a context can have no frame of its own. Fall back to a
        # recent retained screenshot from the same app (matched by bundle_id,
        # then app_name) so the search card still shows something representative.
        return self._get_app_fallback_preview(source_key)

    def _get_app_fallback_preview(self, source_key: str) -> ScenePreview | None:
        row = self.connection.execute(
            """
            WITH ctx AS (
              SELECT bundle_id, app_name
              FROM screen_sessions
              WHERE source_key = ?
              ORDER BY started_at DESC, created_at DESC, id DESC
              LIMIT 1
            )
            SELECT
              s.id AS session_id,
              s.app_name AS app_name,
              s.window_title AS window_title,
              s.url AS url,
              s.started_at AS started_at,
              s.ended_at AS ended_at,
              f.id AS frame_id,
              f.captured_at AS captured_at,
              f.image_path AS image_path
            FROM screen_sessions s
            JOIN ctx ON (
              (ctx.bundle_id IS NOT NULL AND s.bundle_id = ctx.bundle_id)
              OR (
                ctx.bundle_id IS NULL
                AND ctx.app_name IS NOT NULL
                AND s.app_name = ctx.app_name
              )
            )
            JOIN screen_frames f ON f.id = (
              SELECT cf.id
              FROM screen_frames cf
              WHERE cf.session_id = s.id
                AND cf.status = 'selected'
                AND cf.image_retention_state = ?
              ORDER BY cf.captured_at ASC, cf.created_at ASC, cf.id ASC
              LIMIT 1
            )
            ORDER BY s.started_at DESC, s.created_at DESC, s.id DESC
            LIMIT 1
            """,
            (source_key, IMAGE_RETENTION_RETAINED),
        ).fetchone()
        return None if row is None else _row_to_scene_preview(row)

    def create_session(
        self,
        *,
        source_key: str,
        retrieval_locator: str,
        started_at: datetime | str | None = None,
        session_id: str | None = None,
        app_name: str | None = None,
        bundle_id: str | None = None,
        window_title: str | None = None,
        url: str | None = None,
        now: datetime | str | None = None,
    ) -> ScreenSession:
        started_at_iso = _format_timestamp(started_at)
        now_iso = _format_timestamp(now) if now is not None else started_at_iso

        with self.connection:
            row = self.connection.execute(
                f"""
                INSERT INTO screen_sessions (
                  id,
                  source_key,
                  retrieval_locator,
                  app_name,
                  bundle_id,
                  window_title,
                  url,
                  started_at,
                  ended_at,
                  status,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'open', ?, ?)
                RETURNING {_SCREEN_SESSION_COLUMNS}
                """,
                (
                    session_id or _new_session_id(),
                    source_key,
                    retrieval_locator,
                    app_name,
                    bundle_id,
                    window_title,
                    url,
                    started_at_iso,
                    now_iso,
                    now_iso,
                ),
            ).fetchone()

        return _row_to_session(row)

    def touch_session(
        self,
        session_id: str,
        *,
        app_name: str | None = None,
        bundle_id: str | None = None,
        window_title: str | None = None,
        url: str | None = None,
        now: datetime | str | None = None,
    ) -> ScreenSession | None:
        now_iso = _format_timestamp(now)

        with self.connection:
            row = self.connection.execute(
                f"""
                UPDATE screen_sessions
                SET
                  app_name = ?,
                  bundle_id = ?,
                  window_title = ?,
                  url = ?,
                  updated_at = ?
                WHERE id = ?
                  AND status = 'open'
                RETURNING {_SCREEN_SESSION_COLUMNS}
                """,
                (
                    app_name,
                    bundle_id,
                    window_title,
                    url,
                    now_iso,
                    session_id,
                ),
            ).fetchone()

        return None if row is None else _row_to_session(row)

    def close_session(
        self,
        session_id: str,
        *,
        ended_at: datetime | str | None = None,
        now: datetime | str | None = None,
    ) -> ScreenSession | None:
        ended_at_iso = _format_timestamp(ended_at)
        now_iso = _format_timestamp(now) if now is not None else ended_at_iso

        with self.connection:
            row = self.connection.execute(
                f"""
                UPDATE screen_sessions
                SET
                  ended_at = ?,
                  status = 'closed',
                  updated_at = ?
                WHERE id = ?
                  AND status = 'open'
                RETURNING {_SCREEN_SESSION_COLUMNS}
                """,
                (ended_at_iso, now_iso, session_id),
            ).fetchone()

        return None if row is None else _row_to_session(row)

    def mark_session_finalized(
        self,
        session_id: str,
        *,
        now: datetime | str | None = None,
    ) -> ScreenSession | None:
        now_iso = _format_timestamp(now)

        with self.connection:
            row = self.connection.execute(
                f"""
                UPDATE screen_sessions
                SET
                  status = 'finalized',
                  updated_at = ?
                WHERE id = ?
                  AND status IN ('closed', 'finalized')
                RETURNING {_SCREEN_SESSION_COLUMNS}
                """,
                (now_iso, session_id),
            ).fetchone()

        return None if row is None else _row_to_session(row)

    def insert_frame(
        self,
        *,
        session_id: str,
        captured_at: datetime | str | None,
        image_path: str,
        sha256: str,
        width: int,
        height: int,
        frame_id: str | None = None,
        perceptual_hash: str | None = None,
        diff_score: float | None = None,
    ) -> ScreenFrame | None:
        captured_at_iso = _format_timestamp(captured_at)

        with self.connection:
            row = self.connection.execute(
                f"""
                INSERT INTO screen_frames (
                  id,
                  session_id,
                  captured_at,
                  image_path,
                  sha256,
                  perceptual_hash,
                  diff_score,
                  width,
                  height
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, sha256) DO NOTHING
                RETURNING {_SCREEN_FRAME_COLUMNS}
                """,
                (
                    frame_id or _new_frame_id(),
                    session_id,
                    captured_at_iso,
                    image_path,
                    sha256,
                    perceptual_hash,
                    diff_score,
                    width,
                    height,
                ),
            ).fetchone()

        return None if row is None else _row_to_frame(row)

    def mark_frame_status(
        self,
        frame_id: str,
        *,
        status: str,
        diff_score: float | None = None,
    ) -> ScreenFrame | None:
        with self.connection:
            row = self.connection.execute(
                f"""
                UPDATE screen_frames
                SET
                  status = ?,
                  diff_score = ?
                WHERE id = ?
                RETURNING {_SCREEN_FRAME_COLUMNS}
                """,
                (status, diff_score, frame_id),
            ).fetchone()

        return None if row is None else _row_to_frame(row)

    def mark_frame_image_retention(
        self,
        frame_id: str,
        *,
        state: str,
        updated_at: datetime | str | None = None,
    ) -> ScreenFrame | None:
        if state not in IMAGE_RETENTION_STATES:
            raise ValueError(f"unsupported image retention state: {state}")

        updated_at_iso = _format_timestamp(updated_at)
        with self.connection:
            row = self.connection.execute(
                f"""
                UPDATE screen_frames
                SET
                  image_retention_state = ?,
                  image_retention_updated_at = ?
                WHERE id = ?
                RETURNING {_SCREEN_FRAME_COLUMNS}
                """,
                (state, updated_at_iso, frame_id),
            ).fetchone()

        return None if row is None else _row_to_frame(row)


def _new_session_id() -> str:
    return f"screen_session_{uuid.uuid4().hex}"


def _new_frame_id() -> str:
    return f"screen_frame_{uuid.uuid4().hex}"


def _row_to_session(row: sqlite3.Row) -> ScreenSession:
    return ScreenSession(
        id=row["id"],
        source_key=row["source_key"],
        retrieval_locator=row["retrieval_locator"],
        app_name=row["app_name"],
        bundle_id=row["bundle_id"],
        window_title=row["window_title"],
        url=row["url"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_frame(row: sqlite3.Row) -> ScreenFrame:
    return ScreenFrame(
        id=row["id"],
        session_id=row["session_id"],
        captured_at=row["captured_at"],
        image_path=row["image_path"],
        sha256=row["sha256"],
        perceptual_hash=row["perceptual_hash"],
        diff_score=row["diff_score"],
        width=row["width"],
        height=row["height"],
        status=row["status"],
        created_at=row["created_at"],
        image_retention_state=row["image_retention_state"],
        image_retention_updated_at=row["image_retention_updated_at"],
    )


def _row_to_scene_preview(row: sqlite3.Row) -> ScenePreview:
    return ScenePreview(
        session_id=row["session_id"],
        app_name=row["app_name"],
        window_title=row["window_title"],
        url=row["url"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        frame_id=row["frame_id"],
        captured_at=row["captured_at"],
        image_path=row["image_path"],
    )


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
