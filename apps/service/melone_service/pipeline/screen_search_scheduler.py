from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from melone_service.config import ServiceConfig, is_screen_search_workers_enabled
from melone_service.embeddings.indexer import (
    EmbeddingIndexer,
    EmbeddingIndexingResult,
)
from melone_service.embeddings.model import EmbeddingModel
from melone_service.embeddings.sentence_transformers import (
    get_sentence_transformer_embedding_model,
)
from melone_service.pipeline.context_pages import CONTEXT_GRAPH_EVENT_TYPES
from melone_service.pipeline.context_rank_cache import ContextRankCacheRefresher
from melone_service.pipeline.screen_finalize import ScreenFinalizeResult, SessionFinalizer
from melone_service.pipeline.screen_sessions import ScreenSessionizer
from melone_service.store.context_rank import ContextRankRepository
from melone_service.store.db import connect
from melone_service.store.embeddings import EmbeddingRepository
from melone_service.store.events import EventRepository
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository
from melone_service.store.ocr_jobs import OcrJobRepository
from melone_service.ocr.errors import OcrTimeoutError, OcrUnavailableError
from melone_service.ocr.factory import create_ocr_client
from melone_service.ocr.worker import OcrJobProcessingResult, OcrWorker


RUNNING_BACKLOG_STATUSES = ("pending", "retryable_failed", "running")
STATUS_ERROR_MESSAGE_MAX_LENGTH = 240
_LAST_EMBEDDING_ERRORS: dict[tuple[str, str, int], tuple[str, str]] = {}
_LAST_EMBEDDING_ERRORS_LOCK = threading.Lock()


@dataclass(frozen=True)
class ScreenshotCapturePolicy:
    backlog_count: int
    level: str
    min_interval_seconds: float
    transition_frame_only: bool = False


@dataclass(frozen=True)
class ScreenSearchWorkerTickResult:
    context_session_updated: bool = False
    finalize_jobs_processed: int = 0
    ocr_jobs_processed: int = 0
    context_rank_refreshed: bool = False
    indexed_chunks: int = 0
    duplicate_chunks_skipped: int = 0
    embedding_indexed_chunks: int = 0
    embedding_skipped_chunks: int = 0
    embedding_error_count: int = 0
    semantic_total_ocr_chunks: int = 0
    semantic_embedded_chunks: int = 0
    provider_unavailable: bool = False
    last_ocr_error: str | None = None
    last_ocr_error_type: str | None = None
    last_ocr_error_symbol: str | None = None
    last_embedding_error: str | None = None
    last_embedding_error_type: str | None = None

    @property
    def jobs_processed(self) -> int:
        return self.finalize_jobs_processed + self.ocr_jobs_processed


def capture_policy_for_backlog(
    config: ServiceConfig,
    *,
    backlog_count: int,
) -> ScreenshotCapturePolicy:
    base_interval = float(config.screenshot_min_interval_seconds)
    if backlog_count >= config.screen_search_very_high_backlog_threshold:
        return ScreenshotCapturePolicy(
            backlog_count=backlog_count,
            level="very_high",
            min_interval_seconds=base_interval * 4,
            transition_frame_only=True,
        )
    if backlog_count >= config.screen_search_high_backlog_threshold:
        return ScreenshotCapturePolicy(
            backlog_count=backlog_count,
            level="high",
            min_interval_seconds=base_interval * 2,
        )
    return ScreenshotCapturePolicy(
        backlog_count=backlog_count,
        level="normal",
        min_interval_seconds=base_interval,
    )


def count_screen_search_backlog(database_path: Path) -> int:
    connection = connect(database_path)
    try:
        repository = OcrJobRepository(connection)
        return sum(
            repository.count_jobs(status=status)
            for status in RUNNING_BACKLOG_STATUSES
        )
    finally:
        connection.close()


def get_last_embedding_indexing_error(
    *,
    database_path: Path,
    model: str,
    dimension: int,
) -> dict[str, str] | None:
    key = _embedding_error_key(
        database_path=database_path,
        model=model,
        dimension=dimension,
    )
    with _LAST_EMBEDDING_ERRORS_LOCK:
        value = _LAST_EMBEDDING_ERRORS.get(key)
    if value is None:
        return None

    error_type, message = value
    return {"type": error_type, "message": _status_safe_error_message(message)}


def run_screen_search_workers_once(
    config: ServiceConfig,
    *,
    stop_event: threading.Event | None = None,
    logger: logging.Logger | None = None,
    embedding_model: EmbeddingModel | None = None,
) -> ScreenSearchWorkerTickResult:
    if not is_screen_search_workers_enabled(config):
        return ScreenSearchWorkerTickResult()

    logger = logging.getLogger(__name__) if logger is None else logger
    if _stop_requested(stop_event):
        return ScreenSearchWorkerTickResult()

    context_session_updated = _accept_latest_screen_context(config, logger=logger)
    finalize_jobs_processed = 0
    ocr_jobs_processed = 0
    indexed_chunks = 0
    duplicate_chunks_skipped = 0
    embedding_indexed_chunks = 0
    embedding_skipped_chunks = 0
    embedding_error_count = 0
    semantic_total_ocr_chunks = 0
    semantic_embedded_chunks = 0
    provider_unavailable = False
    last_ocr_error: str | None = None
    last_ocr_error_type: str | None = None
    last_ocr_error_symbol: str | None = None
    last_embedding_error: str | None = None
    last_embedding_error_type: str | None = None

    max_jobs = config.screen_search_max_jobs_per_tick
    while (
        finalize_jobs_processed + ocr_jobs_processed < max_jobs
        and not _stop_requested(stop_event)
    ):
        finalize_result = _finalize_one_session(config, logger=logger)
        if finalize_result is not None:
            finalize_jobs_processed += 1
            _log_finalize_result(finalize_result, logger)
            continue

        ocr_result = _process_one_ocr_job(config, logger=logger)
        if ocr_result is not None:
            ocr_jobs_processed += 1
            indexed_chunks += ocr_result.chunks_inserted
            duplicate_chunks_skipped += ocr_result.duplicate_chunks_skipped
            provider_unavailable = (
                provider_unavailable or ocr_result.provider_unavailable
            )
            if ocr_result.error:
                last_ocr_error = ocr_result.error
                last_ocr_error_type = ocr_result.error_type
                last_ocr_error_symbol = ocr_result.error_symbol
            _log_ocr_result(ocr_result, logger)
            continue

        break

    context_rank_refreshed = False
    if not _stop_requested(stop_event):
        context_rank_refreshed = _refresh_context_rank_cache(config, logger=logger)

    if config.semantic_search_enabled and not _stop_requested(stop_event):
        embedding_result = _index_ocr_embeddings(
            config,
            logger=logger,
            embedding_model=embedding_model,
        )
        embedding_indexed_chunks = embedding_result.indexed_count
        embedding_skipped_chunks = embedding_result.skipped_count
        embedding_error_count = embedding_result.error_count
        semantic_total_ocr_chunks = embedding_result.total_ocr_chunks
        semantic_embedded_chunks = embedding_result.embedded_chunks
        last_embedding_error = embedding_result.last_error_message
        last_embedding_error_type = embedding_result.last_error_type
        _record_embedding_indexing_result(config, embedding_result)
        _log_embedding_result(embedding_result, logger)

    return ScreenSearchWorkerTickResult(
        context_session_updated=context_session_updated,
        finalize_jobs_processed=finalize_jobs_processed,
        ocr_jobs_processed=ocr_jobs_processed,
        context_rank_refreshed=context_rank_refreshed,
        indexed_chunks=indexed_chunks,
        duplicate_chunks_skipped=duplicate_chunks_skipped,
        embedding_indexed_chunks=embedding_indexed_chunks,
        embedding_skipped_chunks=embedding_skipped_chunks,
        embedding_error_count=embedding_error_count,
        semantic_total_ocr_chunks=semantic_total_ocr_chunks,
        semantic_embedded_chunks=semantic_embedded_chunks,
        provider_unavailable=provider_unavailable,
        last_ocr_error=last_ocr_error,
        last_ocr_error_type=last_ocr_error_type,
        last_ocr_error_symbol=last_ocr_error_symbol,
        last_embedding_error=last_embedding_error,
        last_embedding_error_type=last_embedding_error_type,
    )


def _accept_latest_screen_context(
    config: ServiceConfig,
    *,
    logger: logging.Logger,
) -> bool:
    connection = connect(config.database_path)
    try:
        event_repository = EventRepository(connection)
        latest_context = event_repository.latest_by_types(CONTEXT_GRAPH_EVENT_TYPES)
        if latest_context is None:
            return False

        ScreenSessionizer(
            screen_repository=ScreenRepository(connection),
            job_repository=OcrJobRepository(connection),
        ).accept_latest_context(latest_context)
        return True
    except Exception as exc:  # pragma: no cover - service boundary
        logger.exception("screen sessionizer failed: %s", exc)
        return False
    finally:
        connection.close()


def _finalize_one_session(
    config: ServiceConfig,
    *,
    logger: logging.Logger,
) -> ScreenFinalizeResult | None:
    connection = connect(config.database_path)
    try:
        return SessionFinalizer(
            screen_repository=ScreenRepository(connection),
            job_repository=OcrJobRepository(connection),
        ).finalize_next_due_job()
    except Exception as exc:  # pragma: no cover - service boundary
        logger.exception("screen session finalize worker failed: %s", exc)
        return None
    finally:
        connection.close()


def _process_one_ocr_job(
    config: ServiceConfig,
    *,
    logger: logging.Logger,
) -> OcrJobProcessingResult | None:
    connection = connect(config.database_path)
    try:
        return OcrWorker(
            client=create_ocr_client(config),
            job_repository=OcrJobRepository(connection),
            screen_repository=ScreenRepository(connection),
            ocr_repository=OcrChunkRepository(connection),
            retry_delay_seconds=config.screen_search_retry_backoff_seconds,
            retain_screenshots=config.screen_text_retain_screenshots,
        ).process_next_due_job()
    except Exception as exc:  # pragma: no cover - service boundary
        logger.exception("OCR worker failed: %s", exc)
        return None
    finally:
        connection.close()


def _refresh_context_rank_cache(
    config: ServiceConfig,
    *,
    logger: logging.Logger,
) -> bool:
    connection = connect(config.database_path)
    try:
        result = ContextRankCacheRefresher(
            EventRepository(connection),
            ContextRankRepository(connection),
        ).refresh_if_due(
            min_interval_seconds=config.context_rank_refresh_min_interval_seconds,
        )
        if result.recomputed:
            logger.info(
                "context rank cache refreshed: rows=%s events=%s computed_at=%s",
                result.upserted_count,
                result.event_count,
                result.computed_at,
            )
        return result.recomputed
    except Exception as exc:  # pragma: no cover - service boundary
        logger.exception("context rank cache refresh failed: %s", exc)
        return False
    finally:
        connection.close()


def _index_ocr_embeddings(
    config: ServiceConfig,
    *,
    logger: logging.Logger,
    embedding_model: EmbeddingModel | None,
) -> EmbeddingIndexingResult:
    model = (
        get_sentence_transformer_embedding_model(config)
        if embedding_model is None
        else embedding_model
    )
    info = model.info

    try:
        connection = connect(config.database_path)
    except Exception as exc:  # pragma: no cover - service boundary
        logger.exception("embedding indexer could not open database: %s", exc)
        return EmbeddingIndexingResult(
            model=info.model,
            dimension=info.dimension,
            error_count=1,
            last_error_type=exc.__class__.__name__,
            last_error_message=str(exc),
        )

    try:
        return EmbeddingIndexer(
            repository=EmbeddingRepository(connection),
            ocr_repository=OcrChunkRepository(connection),
            model=model,
            batch_size=config.embedding_batch_size,
        ).index_once()
    except Exception as exc:  # pragma: no cover - service boundary
        logger.exception("embedding indexer failed: %s", exc)
        return EmbeddingIndexingResult(
            model=info.model,
            dimension=info.dimension,
            error_count=1,
            last_error_type=exc.__class__.__name__,
            last_error_message=str(exc),
        )
    finally:
        connection.close()


def _record_embedding_indexing_result(
    config: ServiceConfig,
    result: EmbeddingIndexingResult,
) -> None:
    key = _embedding_error_key(
        database_path=config.database_path,
        model=result.model,
        dimension=result.dimension,
    )
    with _LAST_EMBEDDING_ERRORS_LOCK:
        if result.last_error_type is None or result.last_error_message is None:
            _LAST_EMBEDDING_ERRORS.pop(key, None)
            return

        _LAST_EMBEDDING_ERRORS[key] = (
            result.last_error_type,
            result.last_error_message,
        )


def _embedding_error_key(
    *,
    database_path: Path,
    model: str,
    dimension: int,
) -> tuple[str, str, int]:
    return (str(database_path.resolve()), model, dimension)


def _status_safe_error_message(message: str) -> str:
    lines = [line.strip() for line in str(message).splitlines() if line.strip()]
    if not lines:
        return ""

    safe_line = lines[0]
    for line in lines:
        if not line.startswith(("Traceback", 'File "')):
            safe_line = line
            break

    safe_line = " ".join(safe_line.split())
    if len(safe_line) <= STATUS_ERROR_MESSAGE_MAX_LENGTH:
        return safe_line
    return safe_line[: STATUS_ERROR_MESSAGE_MAX_LENGTH - 3] + "..."


def _log_embedding_result(
    result: EmbeddingIndexingResult,
    logger: logging.Logger,
) -> None:
    if result.error_count:
        logger.warning(
            "embedding indexing failed: model=%s dimension=%s type=%s error=%s",
            result.model,
            result.dimension,
            result.last_error_type,
            result.last_error_message,
        )
        return

    if result.indexed_count or result.skipped_count:
        logger.info(
            "embedding indexing pass: model=%s dimension=%s indexed=%s "
            "skipped=%s embedded=%s total=%s",
            result.model,
            result.dimension,
            result.indexed_count,
            result.skipped_count,
            result.embedded_chunks,
            result.total_ocr_chunks,
        )


def _log_finalize_result(
    result: ScreenFinalizeResult,
    logger: logging.Logger,
) -> None:
    logger.info(
        "screen session finalized: session_id=%s frames=%s "
        "frame_ocr_jobs=%s crop_ocr_jobs=%s exact_dedupe=%s near_dedupe=%s",
        result.session_id,
        result.total_frames,
        result.frame_ocr_jobs_created,
        result.crop_ocr_jobs_created,
        result.exact_duplicates_skipped,
        result.near_duplicates_skipped,
    )


def _log_ocr_result(
    result: OcrJobProcessingResult,
    logger: logging.Logger,
) -> None:
    if result.error_type == OcrTimeoutError.__name__:
        logger.warning(
            "OCR timeout: job_id=%s status=%s error=%s",
            result.job_id,
            result.status,
            result.error,
        )
    elif (
        result.provider_unavailable
        or result.error_type == OcrUnavailableError.__name__
    ):
        logger.warning(
            "OCR provider unavailable: job_id=%s status=%s error=%s",
            result.job_id,
            result.status,
            result.error,
        )
    elif result.error:
        logger.warning(
            "OCR job failed: job_id=%s status=%s error=%s",
            result.job_id,
            result.status,
            result.error,
        )

    logger.info(
        "OCR job processed: job_id=%s status=%s indexed_chunks=%s "
        "duplicate_chunks=%s empty_text=%s",
        result.job_id,
        result.status,
        result.chunks_inserted,
        result.duplicate_chunks_skipped,
        result.empty_text,
    )


def _stop_requested(stop_event: threading.Event | None) -> bool:
    return stop_event is not None and stop_event.is_set()
