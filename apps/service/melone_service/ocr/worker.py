from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image

from melone_service.models import utc_timestamp
from melone_service.pipeline.image_diff import IMAGE_LOAD_ERRORS, CropBBox
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import (
    IMAGE_RETENTION_DELETE_FAILED_AFTER_INDEXING,
    IMAGE_RETENTION_DELETE_PENDING_AFTER_INDEXING,
    IMAGE_RETENTION_DELETED_AFTER_INDEXING,
    IMAGE_RETENTION_MISSING_AFTER_INDEXING,
    IMAGE_RETENTION_RETAINED,
    IMAGE_RETENTION_RETAINED_AFTER_DEAD_JOB,
    IMAGE_RETENTION_RETAINED_FOR_OCR,
    IMAGE_RETENTION_RETAINED_FOR_RETRY,
    ScreenFrame,
    ScreenRepository,
    ScreenSession,
)
from melone_service.store.ocr_jobs import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_DELAY_SECONDS,
    OcrJob,
    OcrJobRepository,
)
from melone_service.ocr.client import OcrClient, OcrRequest, OcrResult
from melone_service.ocr.errors import OcrError, OcrUnavailableError


OCR_JOB_TYPES = ("frame_ocr", "crop_ocr")
PROVIDER_UNAVAILABLE_ERROR_SYMBOL = "provider_unavailable"
SCENE_PREVIEW_JOB_REASON = "initial_keyframe"


@dataclass(frozen=True)
class OcrJobProcessingResult:
    job_id: str
    job_type: str
    status: str
    chunks_inserted: int = 0
    duplicate_chunks_skipped: int = 0
    empty_text: bool = False
    text_hash: str | None = None
    error: str | None = None
    error_type: str | None = None
    error_symbol: str | None = None
    provider_unavailable: bool = False


class OcrJobValidationError(ValueError):
    """Raised when an OCR job points at invalid local data."""


class OcrWorker:
    def __init__(
        self,
        *,
        client: OcrClient,
        job_repository: OcrJobRepository,
        screen_repository: ScreenRepository,
        ocr_repository: OcrChunkRepository,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retain_screenshots: bool = False,
    ) -> None:
        self.client = client
        self.job_repository = job_repository
        self.screen_repository = screen_repository
        self.ocr_repository = ocr_repository
        self.retry_delay_seconds = retry_delay_seconds
        self.max_attempts = max_attempts
        self.retain_screenshots = retain_screenshots

    def process_next_due_job(
        self,
        *,
        now: datetime | str | None = None,
    ) -> OcrJobProcessingResult | None:
        job = self.job_repository.lock_due_job(
            now=now,
            job_types=OCR_JOB_TYPES,
        )
        if job is None:
            return None

        prepared: _PreparedOcrJob | None = None
        try:
            try:
                prepared = self._prepare_job(job)
                result = self._extract_text(prepared)
            except OcrJobValidationError as exc:
                dead_job = self.job_repository.mark_dead(job.id, error=exc, now=now)
                return OcrJobProcessingResult(
                    job_id=job.id,
                    job_type=job.job_type,
                    status=dead_job.status if dead_job is not None else "dead",
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )
            except OcrError as exc:
                provider_unavailable = isinstance(exc, OcrUnavailableError)
                failed_job = self.job_repository.mark_retryable_failure(
                    job.id,
                    error=exc,
                    now=now,
                    retry_delay_seconds=self.retry_delay_seconds,
                    max_attempts=self.max_attempts,
                )
                self._record_failed_image_retention(prepared, failed_job, now=now)
                return OcrJobProcessingResult(
                    job_id=job.id,
                    job_type=job.job_type,
                    status=(
                        failed_job.status
                        if failed_job is not None
                        else "retryable_failed"
                    ),
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                    error_symbol=(
                        PROVIDER_UNAVAILABLE_ERROR_SYMBOL
                        if provider_unavailable
                        else None
                    ),
                    provider_unavailable=provider_unavailable,
                )
            except Exception as exc:
                self.job_repository.mark_retryable_failure(
                    job.id,
                    error=exc,
                    now=now,
                    retry_delay_seconds=self.retry_delay_seconds,
                    max_attempts=self.max_attempts,
                )
                raise

            try:
                return self._store_result(job, prepared, result, now=now)
            except Exception as exc:
                self.job_repository.mark_retryable_failure(
                    job.id,
                    error=exc,
                    now=now,
                    retry_delay_seconds=self.retry_delay_seconds,
                    max_attempts=self.max_attempts,
                )
                raise
        finally:
            if prepared is not None:
                _delete_temporary_request_image(prepared)

    def _prepare_job(self, job: OcrJob) -> "_PreparedOcrJob":
        if job.job_type not in OCR_JOB_TYPES:
            raise OcrJobValidationError(f"unsupported OCR job type: {job.job_type}")

        frame_id = job.frame_id or job.target_id
        frame = self.screen_repository.get_frame(frame_id)
        if frame is None:
            raise OcrJobValidationError(f"screen frame not found: {frame_id}")

        session_id = job.session_id or frame.session_id
        session = self.screen_repository.get_session(session_id)
        if session is None:
            raise OcrJobValidationError(f"screen session not found: {session_id}")

        source_key = job.source_key or session.source_key
        retrieval_locator = job.retrieval_locator or session.retrieval_locator
        if not source_key:
            raise OcrJobValidationError(f"OCR job missing source_key: {job.id}")
        if not retrieval_locator:
            raise OcrJobValidationError(f"OCR job missing retrieval_locator: {job.id}")

        image_path = Path(frame.image_path)
        if not image_path.is_file():
            raise OcrJobValidationError(f"OCR image path is not a file: {image_path}")

        crop_bbox_json: str | None = None
        request_image_path = image_path
        crop_bbox: CropBBox | None = None
        if job.job_type == "crop_ocr":
            crop_bbox = _parse_crop_bbox(job.metadata.get("crop_bbox_json"), frame)
            crop_bbox_json = crop_bbox.to_json()
            _validate_source_frame_id(job, frame)
            try:
                request_image_path = _prepare_crop_image(
                    image_path=image_path,
                    job_id=job.id,
                    crop_bbox=crop_bbox,
                )
            except IMAGE_LOAD_ERRORS as exc:
                raise OcrJobValidationError(
                    f"failed to prepare crop image: {exc}"
                ) from exc

        return _PreparedOcrJob(
            job=job,
            frame=frame,
            session=session,
            source_key=source_key,
            retrieval_locator=retrieval_locator,
            image_path=request_image_path,
            original_image_path=image_path,
            crop_bbox=crop_bbox,
            crop_bbox_json=crop_bbox_json,
        )

    def _extract_text(self, prepared: "_PreparedOcrJob") -> OcrResult:
        request = OcrRequest(
            image_path=prepared.image_path,
            request_id=prepared.job.id,
            metadata=_request_metadata(prepared),
        )
        try:
            result = self.client.extract_text(request)
        except OcrError:
            raise
        except Exception as exc:
            raise OcrUnavailableError(f"OCR provider failed: {exc}") from exc

        return _validate_ocr_result(result)

    def _store_result(
        self,
        job: OcrJob,
        prepared: "_PreparedOcrJob",
        result: OcrResult,
        *,
        now: datetime | str | None,
    ) -> OcrJobProcessingResult:
        normalized_text = normalize_ocr_text(result.text)
        now_iso = _timestamp(now)

        with self.job_repository.connection:
            chunks_inserted = 0
            duplicates_skipped = 0
            text_hash: str | None = None
            if normalized_text:
                text_hash = hash_ocr_text(normalized_text)
                if self.ocr_repository.text_hash_exists(text_hash):
                    duplicates_skipped = 1
                else:
                    chunk = self.ocr_repository.insert_chunk_with_fts(
                        session_id=prepared.session.id,
                        frame_id=prepared.frame.id,
                        source_key=prepared.source_key,
                        retrieval_locator=prepared.retrieval_locator,
                        app_name=prepared.session.app_name,
                        window_title=prepared.session.window_title,
                        url=prepared.session.url,
                        crop_bbox_json=prepared.crop_bbox_json,
                        text=normalized_text,
                        text_hash=text_hash,
                        provider=result.provider,
                        model=result.model,
                        latency_ms=result.latency_ms,
                        created_at=now_iso,
                    )
                    chunks_inserted = 1 if chunk is not None else 0

            _mark_job_done_in_current_transaction(
                self.job_repository.connection,
                job_id=job.id,
                now=now_iso,
            )

        self._apply_successful_image_retention(job, prepared, now=now_iso)
        return OcrJobProcessingResult(
            job_id=job.id,
            job_type=job.job_type,
            status="done",
            chunks_inserted=chunks_inserted,
            duplicate_chunks_skipped=duplicates_skipped,
            empty_text=not normalized_text,
            text_hash=text_hash,
        )

    def _apply_successful_image_retention(
        self,
        job: OcrJob,
        prepared: "_PreparedOcrJob",
        *,
        now: str,
    ) -> None:
        if self.retain_screenshots:
            self._mark_frame_image_retention(
                prepared.frame.id,
                IMAGE_RETENTION_RETAINED,
                now=now,
            )
            return

        # Keep the first screenshot of each scene as a preview, even when
        # screenshots are otherwise deleted after indexing.
        if job.metadata.get("reason") == SCENE_PREVIEW_JOB_REASON:
            self._mark_frame_image_retention(
                prepared.frame.id,
                IMAGE_RETENTION_RETAINED,
                now=now,
            )
            return

        # A retryable or pending OCR job for the same frame still needs the
        # original PNG. Deletion is tied to successful indexing and waits until
        # no other active OCR job depends on this frame image.
        active_jobs = self.job_repository.count_active_ocr_jobs_for_frame(
            frame_id=prepared.frame.id,
            exclude_job_id=job.id,
        )
        if active_jobs > 0:
            self._mark_frame_image_retention(
                prepared.frame.id,
                IMAGE_RETENTION_RETAINED_FOR_OCR,
                now=now,
            )
            return

        self._mark_frame_image_retention(
            prepared.frame.id,
            IMAGE_RETENTION_DELETE_PENDING_AFTER_INDEXING,
            now=now,
        )
        state = _delete_indexed_frame_image(prepared.original_image_path)
        self._mark_frame_image_retention(prepared.frame.id, state, now=now)

    def _record_failed_image_retention(
        self,
        prepared: "_PreparedOcrJob | None",
        failed_job: OcrJob | None,
        *,
        now: datetime | str | None,
    ) -> None:
        if prepared is None:
            return

        # Failed OCR jobs keep the original frame PNG: retryable jobs need it
        # for the next attempt, and dead jobs retain deterministic debug input.
        state = (
            IMAGE_RETENTION_RETAINED_AFTER_DEAD_JOB
            if failed_job is not None and failed_job.status == "dead"
            else IMAGE_RETENTION_RETAINED_FOR_RETRY
        )
        self._mark_frame_image_retention(prepared.frame.id, state, now=now)

    def _mark_frame_image_retention(
        self,
        frame_id: str,
        state: str,
        *,
        now: datetime | str | None,
    ) -> None:
        self.screen_repository.mark_frame_image_retention(
            frame_id,
            state=state,
            updated_at=now,
        )


@dataclass(frozen=True)
class _PreparedOcrJob:
    job: OcrJob
    frame: ScreenFrame
    session: ScreenSession
    source_key: str
    retrieval_locator: str
    image_path: Path
    original_image_path: Path
    crop_bbox: CropBBox | None
    crop_bbox_json: str | None


def normalize_ocr_text(text: str) -> str:
    return " ".join(text.split())


def hash_ocr_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_ocr_result(result: object) -> OcrResult:
    if not isinstance(result, OcrResult):
        raise OcrError("OCR provider returned a malformed response")
    if not isinstance(result.text, str):
        raise OcrError("OCR provider returned non-string text")
    if result.provider is not None and not isinstance(result.provider, str):
        raise OcrError("OCR provider returned non-string provider")
    if result.model is not None and not isinstance(result.model, str):
        raise OcrError("OCR provider returned non-string model")
    if result.latency_ms is not None and (
        isinstance(result.latency_ms, bool) or not isinstance(result.latency_ms, int)
    ):
        raise OcrError("OCR provider returned non-integer latency_ms")
    return result


def _request_metadata(prepared: _PreparedOcrJob) -> dict[str, object]:
    metadata: dict[str, object] = {
        "job_id": prepared.job.id,
        "job_type": prepared.job.job_type,
        "session_id": prepared.session.id,
        "frame_id": prepared.frame.id,
        "source_key": prepared.source_key,
        "retrieval_locator": prepared.retrieval_locator,
        "original_image_path": str(prepared.original_image_path),
    }
    if prepared.crop_bbox is not None:
        metadata["crop_bbox"] = {
            "x": prepared.crop_bbox.x,
            "y": prepared.crop_bbox.y,
            "width": prepared.crop_bbox.width,
            "height": prepared.crop_bbox.height,
        }
    metadata.update(prepared.job.metadata)
    return metadata


def _parse_crop_bbox(value: object, frame: ScreenFrame) -> CropBBox:
    if value is None:
        raise OcrJobValidationError("crop OCR job requires crop_bbox_json metadata")

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise OcrJobValidationError("crop_bbox_json is not valid JSON") from exc
    elif isinstance(value, Mapping):
        decoded = value
    else:
        raise OcrJobValidationError("crop_bbox_json must be a JSON object")

    if not isinstance(decoded, Mapping):
        raise OcrJobValidationError("crop_bbox_json must decode to an object")

    bbox = CropBBox(
        x=_bbox_int(decoded, "x"),
        y=_bbox_int(decoded, "y"),
        width=_bbox_int(decoded, "width"),
        height=_bbox_int(decoded, "height"),
    )
    if bbox.x < 0 or bbox.y < 0:
        raise OcrJobValidationError("crop bbox x and y must be non-negative")
    if bbox.width <= 0 or bbox.height <= 0:
        raise OcrJobValidationError("crop bbox width and height must be positive")
    if bbox.x + bbox.width > frame.width or bbox.y + bbox.height > frame.height:
        raise OcrJobValidationError("crop bbox is outside the frame dimensions")
    return bbox


def _bbox_int(decoded: Mapping[str, object], key: str) -> int:
    value = decoded.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise OcrJobValidationError(f"crop bbox {key} must be an integer")
    return value


def _validate_source_frame_id(job: OcrJob, frame: ScreenFrame) -> None:
    source_frame_id = job.metadata.get("source_frame_id")
    if source_frame_id is not None and source_frame_id != frame.id:
        raise OcrJobValidationError("crop source_frame_id does not match frame_id")


def _prepare_crop_image(
    *,
    image_path: Path,
    job_id: str,
    crop_bbox: CropBBox,
) -> Path:
    with Image.open(image_path) as image:
        if (
            crop_bbox.x + crop_bbox.width > image.width
            or crop_bbox.y + crop_bbox.height > image.height
        ):
            raise OcrJobValidationError("crop bbox is outside the image dimensions")
        cropped = image.crop(
            (
                crop_bbox.x,
                crop_bbox.y,
                crop_bbox.x + crop_bbox.width,
                crop_bbox.y + crop_bbox.height,
            )
        )
        crop_path = image_path.with_name(f"{image_path.stem}.{job_id}.crop.png")
        cropped.save(crop_path, format="PNG")
    return crop_path


def _mark_job_done_in_current_transaction(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    now: str,
) -> None:
    row = connection.execute(
        """
        UPDATE ocr_jobs
        SET
          status = 'done',
          locked_at = NULL,
          last_error = NULL,
          updated_at = ?
        WHERE id = ?
          AND status = 'running'
        RETURNING id
        """,
        (now, job_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"running OCR job not found while marking done: {job_id}")


def _delete_indexed_frame_image(image_path: Path) -> str:
    if not image_path.exists():
        return IMAGE_RETENTION_MISSING_AFTER_INDEXING

    try:
        image_path.unlink()
    except OSError:
        return IMAGE_RETENTION_DELETE_FAILED_AFTER_INDEXING

    return IMAGE_RETENTION_DELETED_AFTER_INDEXING


def _delete_temporary_request_image(prepared: _PreparedOcrJob) -> None:
    if prepared.image_path == prepared.original_image_path:
        return

    try:
        prepared.image_path.unlink(missing_ok=True)
    except OSError:
        return


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        return utc_timestamp()
    if isinstance(value, str):
        return value
    return utc_timestamp(value)
