from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


DEFAULT_RETRY_DELAY_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 3
MAX_LAST_ERROR_LENGTH = 2000
DUE_JOB_STATUSES = ("pending", "retryable_failed")
ACTIVE_IMAGE_INPUT_STATUSES = ("pending", "running", "retryable_failed")

_OCR_JOB_COLUMNS = """
  id,
  type,
  target_id,
  session_id,
  frame_id,
  source_key,
  retrieval_locator,
  priority,
  status,
  attempts,
  next_run_at,
  locked_at,
  last_error,
  metadata_json,
  created_at,
  updated_at
"""


@dataclass(frozen=True)
class OcrJob:
    id: str
    job_type: str
    target_id: str
    session_id: str | None
    frame_id: str | None
    source_key: str | None
    retrieval_locator: str | None
    priority: int
    status: str
    attempts: int
    next_run_at: str
    locked_at: str | None
    last_error: str | None
    metadata: dict[str, object]
    created_at: str
    updated_at: str


class OcrJobRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def count_jobs(self, *, status: str | None = None) -> int:
        if status is None:
            row = self.connection.execute("SELECT COUNT(*) FROM ocr_jobs").fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM ocr_jobs WHERE status = ?",
                (status,),
            ).fetchone()
        return int(row[0])

    def count_jobs_by_statuses(
        self,
        statuses: Sequence[str],
        *,
        job_types: Sequence[str] | None = None,
    ) -> int:
        unique_statuses = tuple(value for value in dict.fromkeys(statuses) if value)
        if not unique_statuses:
            return 0

        status_placeholders = ", ".join("?" for _ in unique_statuses)
        type_clause, type_params = _job_type_filter(job_types)
        if type_clause is None:
            return 0

        row = self.connection.execute(
            f"""
            SELECT COUNT(*)
            FROM ocr_jobs
            WHERE status IN ({status_placeholders})
              {type_clause}
            """,
            [*unique_statuses, *type_params],
        ).fetchone()
        return int(row[0])

    def latest_error_job(
        self,
        *,
        job_types: Sequence[str] | None = None,
    ) -> OcrJob | None:
        type_clause, type_params = _job_type_filter(job_types)
        if type_clause is None:
            return None

        row = self.connection.execute(
            f"""
            SELECT {_OCR_JOB_COLUMNS}
            FROM ocr_jobs
            WHERE last_error IS NOT NULL
              AND last_error != ''
              {type_clause}
            ORDER BY updated_at DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            type_params,
        ).fetchone()
        return None if row is None else _row_to_job(row)

    def count_active_ocr_jobs_for_frame(
        self,
        *,
        frame_id: str,
        exclude_job_id: str | None = None,
    ) -> int:
        params: list[object] = [
            frame_id,
            *ACTIVE_IMAGE_INPUT_STATUSES,
            "frame_ocr",
            "crop_ocr",
        ]
        exclude_clause = ""
        if exclude_job_id is not None:
            exclude_clause = "AND id != ?"
            params.append(exclude_job_id)

        row = self.connection.execute(
            f"""
            SELECT COUNT(*)
            FROM ocr_jobs
            WHERE frame_id = ?
              AND status IN (?, ?, ?)
              AND type IN (?, ?)
              {exclude_clause}
            """,
            params,
        ).fetchone()
        return int(row[0])

    def get_job(self, job_id: str) -> OcrJob | None:
        row = self.connection.execute(
            f"""
            SELECT {_OCR_JOB_COLUMNS}
            FROM ocr_jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        return None if row is None else _row_to_job(row)

    def create_pending_job(
        self,
        *,
        job_type: str,
        target_id: str,
        job_id: str | None = None,
        session_id: str | None = None,
        frame_id: str | None = None,
        source_key: str | None = None,
        retrieval_locator: str | None = None,
        priority: int = 0,
        next_run_at: datetime | str | None = None,
        metadata: Mapping[str, object] | None = None,
        now: datetime | str | None = None,
    ) -> OcrJob:
        now_dt = _coerce_utc_datetime(now)
        now_iso = _format_utc(now_dt)
        next_run_at_iso = (
            _format_utc(_coerce_utc_datetime(next_run_at))
            if next_run_at is not None
            else now_iso
        )
        metadata_json = json.dumps(dict(metadata or {}), sort_keys=True)

        with self.connection:
            row = self.connection.execute(
                f"""
                INSERT INTO ocr_jobs (
                  id,
                  type,
                  target_id,
                  session_id,
                  frame_id,
                  source_key,
                  retrieval_locator,
                  priority,
                  status,
                  attempts,
                  next_run_at,
                  locked_at,
                  last_error,
                  metadata_json,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?, ?)
                RETURNING {_OCR_JOB_COLUMNS}
                """,
                (
                    job_id or _new_job_id(),
                    job_type,
                    target_id,
                    session_id,
                    frame_id,
                    source_key,
                    retrieval_locator,
                    priority,
                    next_run_at_iso,
                    metadata_json,
                    now_iso,
                    now_iso,
                ),
            ).fetchone()

        return _row_to_job(row)

    def lock_due_job(
        self,
        *,
        now: datetime | str | None = None,
        job_types: Sequence[str] | None = None,
    ) -> OcrJob | None:
        now_iso = _format_utc(_coerce_utc_datetime(now))
        type_clause, type_params = _job_type_filter(job_types)
        if type_clause is None:
            return None

        params: list[object] = [
            now_iso,
            now_iso,
            *DUE_JOB_STATUSES,
            now_iso,
            *type_params,
        ]

        with self.connection:
            row = self.connection.execute(
                f"""
                UPDATE ocr_jobs
                SET
                  status = 'running',
                  locked_at = ?,
                  updated_at = ?
                WHERE id = (
                  SELECT id
                  FROM ocr_jobs
                  WHERE status IN (?, ?)
                    AND next_run_at <= ?
                    {type_clause}
                  ORDER BY next_run_at ASC, priority DESC, created_at ASC, id ASC
                  LIMIT 1
                )
                RETURNING {_OCR_JOB_COLUMNS}
                """,
                params,
            ).fetchone()

        return None if row is None else _row_to_job(row)

    def mark_done(
        self,
        job_id: str,
        *,
        now: datetime | str | None = None,
    ) -> OcrJob | None:
        now_iso = _format_utc(_coerce_utc_datetime(now))

        with self.connection:
            row = self.connection.execute(
                f"""
                UPDATE ocr_jobs
                SET
                  status = 'done',
                  locked_at = NULL,
                  last_error = NULL,
                  updated_at = ?
                WHERE id = ?
                  AND status = 'running'
                RETURNING {_OCR_JOB_COLUMNS}
                """,
                (now_iso, job_id),
            ).fetchone()

        return None if row is None else _row_to_job(row)

    def mark_retryable_failure(
        self,
        job_id: str,
        *,
        error: str | BaseException,
        now: datetime | str | None = None,
        next_run_at: datetime | str | None = None,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> OcrJob | None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        now_dt = _coerce_utc_datetime(now)
        now_iso = _format_utc(now_dt)
        retry_at_iso = (
            _format_utc(_coerce_utc_datetime(next_run_at))
            if next_run_at is not None
            else _format_utc(now_dt + timedelta(seconds=retry_delay_seconds))
        )
        error_text = _last_error_text(error)

        with self.connection:
            row = self.connection.execute(
                f"""
                UPDATE ocr_jobs
                SET
                  attempts = attempts + 1,
                  status = CASE
                    WHEN attempts + 1 >= ? THEN 'dead'
                    ELSE 'retryable_failed'
                  END,
                  next_run_at = CASE
                    WHEN attempts + 1 >= ? THEN ?
                    ELSE ?
                  END,
                  locked_at = NULL,
                  last_error = ?,
                  updated_at = ?
                WHERE id = ?
                  AND status = 'running'
                RETURNING {_OCR_JOB_COLUMNS}
                """,
                (
                    max_attempts,
                    max_attempts,
                    now_iso,
                    retry_at_iso,
                    error_text,
                    now_iso,
                    job_id,
                ),
            ).fetchone()

        return None if row is None else _row_to_job(row)

    def mark_dead(
        self,
        job_id: str,
        *,
        error: str | BaseException,
        now: datetime | str | None = None,
        increment_attempts: bool = True,
    ) -> OcrJob | None:
        now_iso = _format_utc(_coerce_utc_datetime(now))
        error_text = _last_error_text(error)
        attempt_increment = 1 if increment_attempts else 0

        with self.connection:
            row = self.connection.execute(
                f"""
                UPDATE ocr_jobs
                SET
                  attempts = attempts + ?,
                  status = 'dead',
                  next_run_at = ?,
                  locked_at = NULL,
                  last_error = ?,
                  updated_at = ?
                WHERE id = ?
                RETURNING {_OCR_JOB_COLUMNS}
                """,
                (
                    attempt_increment,
                    now_iso,
                    error_text,
                    now_iso,
                    job_id,
                ),
            ).fetchone()

        return None if row is None else _row_to_job(row)


def _new_job_id() -> str:
    return f"ocr_job_{uuid.uuid4().hex}"


def _row_to_job(row: sqlite3.Row) -> OcrJob:
    metadata = json.loads(row["metadata_json"])
    if not isinstance(metadata, dict):
        raise ValueError("ocr_jobs metadata_json must contain a JSON object")

    return OcrJob(
        id=row["id"],
        job_type=row["type"],
        target_id=row["target_id"],
        session_id=row["session_id"],
        frame_id=row["frame_id"],
        source_key=row["source_key"],
        retrieval_locator=row["retrieval_locator"],
        priority=int(row["priority"]),
        status=row["status"],
        attempts=int(row["attempts"]),
        next_run_at=row["next_run_at"],
        locked_at=row["locked_at"],
        last_error=row["last_error"],
        metadata=metadata,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _job_type_filter(job_types: Sequence[str] | None) -> tuple[str | None, list[str]]:
    if job_types is None:
        return "", []

    unique_job_types = tuple(value for value in dict.fromkeys(job_types) if value)
    if not unique_job_types:
        return None, []

    placeholders = ", ".join("?" for _ in unique_job_types)
    return f"AND type IN ({placeholders})", list(unique_job_types)


def _coerce_utc_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)

    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    else:
        parsed = value

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _last_error_text(error: str | BaseException) -> str:
    if isinstance(error, BaseException):
        message = f"{error.__class__.__name__}: {error}"
    else:
        message = error

    return str(message)[:MAX_LAST_ERROR_LENGTH]
