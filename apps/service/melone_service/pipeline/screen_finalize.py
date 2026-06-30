from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from melone_service.models import utc_timestamp
from melone_service.pipeline.image_diff import (
    IMAGE_LOAD_ERRORS,
    CropBBox,
    FrameComparer,
    ImageDiff,
    are_near_duplicate_hashes,
)
from melone_service.store.screen import ScreenFrame, ScreenRepository
from melone_service.store.ocr_jobs import (
    DEFAULT_RETRY_DELAY_SECONDS,
    OcrJob,
    OcrJobRepository,
)


DEFAULT_KEYFRAME_DIFF_THRESHOLD = 0.20
DEFAULT_CROP_DIFF_THRESHOLD = 0.02


@dataclass(frozen=True)
class ScreenFinalizePolicy:
    near_duplicate_phash_distance: int = 4
    keyframe_diff_threshold: float = DEFAULT_KEYFRAME_DIFF_THRESHOLD
    crop_diff_threshold: float = DEFAULT_CROP_DIFF_THRESHOLD


@dataclass(frozen=True)
class ScreenFinalizeResult:
    session_id: str
    total_frames: int
    frame_ocr_jobs_created: int
    crop_ocr_jobs_created: int
    exact_duplicates_skipped: int
    near_duplicates_skipped: int
    low_change_frames_skipped: int
    finalized_at: str


class SessionFinalizer:
    def __init__(
        self,
        *,
        screen_repository: ScreenRepository,
        job_repository: OcrJobRepository,
        policy: ScreenFinalizePolicy | None = None,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self.screen_repository = screen_repository
        self.job_repository = job_repository
        self.policy = policy or ScreenFinalizePolicy()
        self.retry_delay_seconds = retry_delay_seconds

    def finalize_next_due_job(
        self,
        *,
        now: datetime | str | None = None,
    ) -> ScreenFinalizeResult | None:
        job = self.job_repository.lock_due_job(
            now=now,
            job_types=("session_finalize",),
        )
        if job is None:
            return None

        try:
            result = self.finalize_session_job(job, now=now)
        except Exception as exc:
            self.job_repository.mark_retryable_failure(
                job.id,
                error=exc,
                now=now,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            raise

        self.job_repository.mark_done(job.id, now=now)
        return result

    def finalize_session_job(
        self,
        job: OcrJob,
        *,
        now: datetime | str | None = None,
    ) -> ScreenFinalizeResult:
        if job.job_type != "session_finalize":
            raise ValueError(f"cannot finalize unsupported job type: {job.job_type}")
        session_id = job.session_id or job.target_id
        return self.finalize_session(session_id, now=now)

    def finalize_session(
        self,
        session_id: str,
        *,
        now: datetime | str | None = None,
    ) -> ScreenFinalizeResult:
        session = self.screen_repository.get_session(session_id)
        if session is None:
            raise ValueError(f"screen session not found: {session_id}")

        finalized_at = _timestamp(now)
        frames = self.screen_repository.list_session_frames(session_id)
        stats = _FinalizeStats(total_frames=len(frames))

        keyframes: list[ScreenFrame] = []
        kept_frames: list[ScreenFrame] = []
        seen_sha256: set[str] = set()
        # One comparer per session: the baseline keyframe is decoded and
        # downscaled once and reused across candidates, advancing only when a new
        # keyframe is promoted.
        comparer = FrameComparer()

        for frame in frames:
            if frame.sha256 in seen_sha256:
                stats.exact_duplicates_skipped += 1
                self.screen_repository.mark_frame_status(
                    frame.id,
                    status="skipped",
                    diff_score=0.0,
                )
                continue
            seen_sha256.add(frame.sha256)

            if _is_near_duplicate(
                frame,
                kept_frames,
                max_distance=self.policy.near_duplicate_phash_distance,
            ):
                stats.near_duplicates_skipped += 1
                self.screen_repository.mark_frame_status(
                    frame.id,
                    status="skipped",
                    diff_score=0.0,
                )
                continue

            kept_frames.append(frame)

            if not keyframes:
                if self._create_frame_ocr_job(
                    frame=frame,
                    source_key=session.source_key,
                    retrieval_locator=session.retrieval_locator,
                    reason="initial_keyframe",
                    diff_score=1.0,
                    now=finalized_at,
                ):
                    stats.frame_ocr_jobs_created += 1
                keyframes.append(frame)
                comparer.set_baseline(frame.image_path)
                self.screen_repository.mark_frame_status(
                    frame.id,
                    status="selected",
                    diff_score=1.0,
                )
                continue

            previous_keyframe = keyframes[-1]
            try:
                diff = comparer.compare(frame.image_path)
                compared_ok = True
            except IMAGE_LOAD_ERRORS:
                # Unreadable frame: treat as a full change and OCR it, but leave
                # the baseline on the last good keyframe.
                diff = ImageDiff(
                    score=1.0,
                    crop_bbox=CropBBox(
                        x=0, y=0, width=frame.width, height=frame.height
                    ),
                )
                compared_ok = False
            rounded_score = _rounded_score(diff.score)

            if diff.score >= self.policy.keyframe_diff_threshold:
                if self._create_frame_ocr_job(
                    frame=frame,
                    source_key=session.source_key,
                    retrieval_locator=session.retrieval_locator,
                    reason="meaningful_change",
                    diff_score=rounded_score,
                    compared_frame_id=previous_keyframe.id,
                    now=finalized_at,
                ):
                    stats.frame_ocr_jobs_created += 1
                if compared_ok:
                    keyframes.append(frame)
                    comparer.promote_last_to_baseline()
                self.screen_repository.mark_frame_status(
                    frame.id,
                    status="selected",
                    diff_score=rounded_score,
                )
            elif (
                diff.score >= self.policy.crop_diff_threshold
                and diff.crop_bbox is not None
            ):
                if self._create_crop_ocr_job(
                    frame=frame,
                    source_key=session.source_key,
                    retrieval_locator=session.retrieval_locator,
                    diff=diff,
                    compared_frame_id=previous_keyframe.id,
                    now=finalized_at,
                ):
                    stats.crop_ocr_jobs_created += 1
                self.screen_repository.mark_frame_status(
                    frame.id,
                    status="selected",
                    diff_score=rounded_score,
                )
            else:
                stats.low_change_frames_skipped += 1
                self.screen_repository.mark_frame_status(
                    frame.id,
                    status="skipped",
                    diff_score=rounded_score,
                )

        finalized_session = self.screen_repository.mark_session_finalized(
            session_id,
            now=finalized_at,
        )
        if finalized_session is None:
            raise ValueError(f"screen session is not closed: {session_id}")

        return ScreenFinalizeResult(
            session_id=session_id,
            total_frames=stats.total_frames,
            frame_ocr_jobs_created=stats.frame_ocr_jobs_created,
            crop_ocr_jobs_created=stats.crop_ocr_jobs_created,
            exact_duplicates_skipped=stats.exact_duplicates_skipped,
            near_duplicates_skipped=stats.near_duplicates_skipped,
            low_change_frames_skipped=stats.low_change_frames_skipped,
            finalized_at=finalized_at,
        )

    def _create_frame_ocr_job(
        self,
        *,
        frame: ScreenFrame,
        source_key: str,
        retrieval_locator: str,
        reason: str,
        diff_score: float,
        now: str,
        compared_frame_id: str | None = None,
    ) -> bool:
        metadata: dict[str, object] = {
            "reason": reason,
            "diff_score": diff_score,
            "source_frame_id": frame.id,
        }
        if compared_frame_id is not None:
            metadata["compared_frame_id"] = compared_frame_id

        return self._create_ocr_job_once(
            job_type="frame_ocr",
            target_id=frame.id,
            frame=frame,
            source_key=source_key,
            retrieval_locator=retrieval_locator,
            priority=10,
            metadata=metadata,
            now=now,
        )

    def _create_crop_ocr_job(
        self,
        *,
        frame: ScreenFrame,
        source_key: str,
        retrieval_locator: str,
        diff: ImageDiff,
        compared_frame_id: str,
        now: str,
    ) -> bool:
        if diff.crop_bbox is None:
            raise ValueError("crop OCR job requires a crop bbox")

        metadata = {
            "reason": "changed_crop",
            "crop_bbox_json": diff.crop_bbox.to_json(),
            "source_frame_id": frame.id,
            "compared_frame_id": compared_frame_id,
            "diff_score": _rounded_score(diff.score),
        }

        return self._create_ocr_job_once(
            job_type="crop_ocr",
            target_id=frame.id,
            frame=frame,
            source_key=source_key,
            retrieval_locator=retrieval_locator,
            priority=5,
            metadata=metadata,
            now=now,
        )

    def _create_ocr_job_once(
        self,
        *,
        job_type: str,
        target_id: str,
        frame: ScreenFrame,
        source_key: str,
        retrieval_locator: str,
        priority: int,
        metadata: dict[str, object],
        now: str,
    ) -> bool:
        metadata_json = json.dumps(metadata, sort_keys=True)
        existing = self.job_repository.connection.execute(
            """
            SELECT id
            FROM ocr_jobs
            WHERE type = ?
              AND target_id = ?
              AND session_id = ?
              AND frame_id = ?
              AND metadata_json = ?
              AND status != 'dead'
            LIMIT 1
            """,
            (job_type, target_id, frame.session_id, frame.id, metadata_json),
        ).fetchone()
        if existing is not None:
            return False

        self.job_repository.create_pending_job(
            job_type=job_type,
            target_id=target_id,
            session_id=frame.session_id,
            frame_id=frame.id,
            source_key=source_key,
            retrieval_locator=retrieval_locator,
            priority=priority,
            next_run_at=now,
            metadata=metadata,
            now=now,
        )
        return True


@dataclass
class _FinalizeStats:
    total_frames: int
    frame_ocr_jobs_created: int = 0
    crop_ocr_jobs_created: int = 0
    exact_duplicates_skipped: int = 0
    near_duplicates_skipped: int = 0
    low_change_frames_skipped: int = 0


def _is_near_duplicate(
    frame: ScreenFrame,
    kept_frames: list[ScreenFrame],
    *,
    max_distance: int,
) -> bool:
    return any(
        are_near_duplicate_hashes(
            frame.perceptual_hash,
            kept_frame.perceptual_hash,
            max_distance=max_distance,
        )
        for kept_frame in kept_frames
    )


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        return utc_timestamp()
    if isinstance(value, str):
        return value
    return utc_timestamp(value)


def _rounded_score(value: float) -> float:
    return round(float(value), 6)
