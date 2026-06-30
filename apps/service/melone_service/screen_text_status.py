from __future__ import annotations

import importlib.util
import sqlite3
import sys
from dataclasses import dataclass
from typing import Literal

from melone_service.config import (
    ServiceConfig,
    is_screen_search_workers_enabled,
    is_screenshot_collection_enabled,
)
from melone_service.permissions import (
    PermissionSnapshot,
    StatusCheck,
    check_permission_status,
)
from melone_service.pipeline.screen_search_scheduler import (
    RUNNING_BACKLOG_STATUSES,
    get_last_embedding_indexing_error,
)
from melone_service.settings import app_settings_path, load_app_settings
from melone_service.store.db import connect_readonly
from melone_service.store.embeddings import EmbeddingRepository
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.ocr_jobs import OcrJobRepository
from melone_service.ocr.errors import OcrUnavailableError
from melone_service.ocr.local_vllm import LOCAL_VLLM_PROVIDER
from melone_service.ocr.apple_vision import APPLE_VISION_PROVIDER
from melone_service.ocr.worker import PROVIDER_UNAVAILABLE_ERROR_SYMBOL


ScreenTextState = Literal["off", "blocked", "ready", "indexing", "error"]

_APPLE_VISION_PROVIDERS = {
    APPLE_VISION_PROVIDER,
    "macos_vision",
    "macos-vision",
    "apple-vision",
    "vision",
}
_LOCAL_OPENAI_COMPATIBLE_PROVIDERS = {
    LOCAL_VLLM_PROVIDER,
    "local-vllm",
    "vllm",
    "local_mlx",
    "local-mlx",
    "mlx",
    "mlx_vlm",
    "mlx-vlm",
    "local_openai",
    "local-openai",
    "openai_compatible",
    "openai-compatible",
}
_PROVIDER_UNAVAILABLE_ERROR_TYPES = {
    OcrUnavailableError.__name__,
    "VlmOcrUnavailableError",
}


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    available: bool
    reason: str | None = None
    detail: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "available": self.available,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class IndexingMetrics:
    backlog_count: int = 0
    pending_job_count: int = 0
    running_job_count: int = 0
    retryable_job_count: int = 0
    dead_job_count: int = 0
    latest_indexed_at: str | None = None
    last_error: dict[str, object] | None = None
    semantic_total_ocr_chunks: int = 0
    semantic_embedded_chunks: int = 0
    last_embedding_error: dict[str, object] | None = None
    status_error: str | None = None


def build_screen_text_status(
    config: ServiceConfig,
    *,
    permission_snapshot: PermissionSnapshot | None = None,
    platform_name: str | None = None,
) -> dict[str, object]:
    settings_path = config.settings_path or app_settings_path(config.data_dir)
    settings = load_app_settings(settings_path)
    screenshot_collector_enabled = is_screenshot_collection_enabled(config)
    workers_enabled = is_screen_search_workers_enabled(config)
    effective_enabled = screenshot_collector_enabled or workers_enabled
    snapshot = (
        check_permission_status()
        if permission_snapshot is None
        else permission_snapshot
    )
    screen_recording = snapshot.permissions.get(
        "screen_recording",
        StatusCheck("unsupported", "Screen Recording permission status unavailable"),
    )
    provider = _provider_status(config, platform_name=platform_name)
    metrics = (
        _read_indexing_metrics(config)
        if effective_enabled
        else IndexingMetrics()
    )

    state, reason, required_permissions = _classify_state(
        effective_enabled=effective_enabled,
        screen_recording=screen_recording,
        provider=provider,
        metrics=metrics,
    )

    return {
        "state": state,
        "reason": reason,
        "settings": {
            **settings.screen_text.to_payload(),
            "retainScreenshots": config.screen_text_retain_screenshots,
        },
        "enabled": settings.screen_text.enabled,
        "effectiveEnabled": effective_enabled,
        "screenshotCollectorEnabled": screenshot_collector_enabled,
        "workersEnabled": workers_enabled,
        "developmentOverrides": {
            "screenshotCollector": (
                config.screenshot_collector_development_override_enabled
            ),
            "workers": config.screen_search_workers_development_override_enabled,
        },
        "screenRecordingPermission": _status_check_payload(screen_recording),
        "requiredPermissions": required_permissions,
        "provider": provider.to_payload(),
        "backlogCount": metrics.backlog_count,
        "pendingJobCount": metrics.pending_job_count,
        "runningJobCount": metrics.running_job_count,
        "retryableJobCount": metrics.retryable_job_count,
        "deadJobCount": metrics.dead_job_count,
        "latestOcrAt": metrics.latest_indexed_at,
        "latestIndexedAt": metrics.latest_indexed_at,
        "lastError": metrics.last_error,
        "semanticIndexing": {
            "enabled": config.semantic_search_enabled,
            "model": config.embedding_model,
            "dimension": config.embedding_dimension,
            "totalOcrChunks": metrics.semantic_total_ocr_chunks,
            "embeddedChunks": metrics.semantic_embedded_chunks,
            "lastError": metrics.last_embedding_error,
        },
        "statusError": metrics.status_error,
        "screenshotRetention": (
            "retain" if config.screen_text_retain_screenshots else "delete_after_indexing"
        ),
    }


def _classify_state(
    *,
    effective_enabled: bool,
    screen_recording: StatusCheck,
    provider: ProviderStatus,
    metrics: IndexingMetrics,
) -> tuple[ScreenTextState, str | None, list[str]]:
    if not effective_enabled:
        return "off", "disabled", []
    if screen_recording.status != "granted":
        return "blocked", "screen_recording_permission_required", [
            "screen_recording"
        ]
    if not provider.available:
        return "blocked", "provider_unavailable", []
    if metrics.status_error is not None:
        return "error", "status_unavailable", []
    current_error = _last_error_is_current(metrics)
    if (
        current_error
        and _last_error_symbol(metrics.last_error) == PROVIDER_UNAVAILABLE_ERROR_SYMBOL
    ):
        return "error", PROVIDER_UNAVAILABLE_ERROR_SYMBOL, []
    if metrics.dead_job_count > 0 and current_error:
        return "error", "indexing_error", []
    if metrics.backlog_count > 0:
        return "indexing", "backlog_pending", []
    return "ready", None, []


def _provider_status(
    config: ServiceConfig,
    *,
    platform_name: str | None,
) -> ProviderStatus:
    provider = config.ocr_provider.strip().lower()
    if provider == "mock" or provider in _LOCAL_OPENAI_COMPATIBLE_PROVIDERS:
        return ProviderStatus(name=config.ocr_provider, available=True)
    if provider in _APPLE_VISION_PROVIDERS:
        platform_value = sys.platform if platform_name is None else platform_name
        if platform_value != "darwin":
            return ProviderStatus(
                name=config.ocr_provider,
                available=False,
                reason="apple_vision_requires_macos",
                detail="Screen Text Search requires Apple Vision on this provider.",
            )
        if (
            importlib.util.find_spec("Foundation") is None
            or importlib.util.find_spec("Vision") is None
        ):
            return ProviderStatus(
                name=config.ocr_provider,
                available=False,
                reason="apple_vision_missing_pyobjc",
                detail="Screen Text Search requires PyObjC Vision support.",
            )
        return ProviderStatus(name=config.ocr_provider, available=True)
    return ProviderStatus(
        name=config.ocr_provider,
        available=False,
        reason="unsupported_provider",
        detail=f"Unsupported Screen Text Search provider: {config.ocr_provider}",
    )


def _read_indexing_metrics(config: ServiceConfig) -> IndexingMetrics:
    database_path = config.database_path
    if not database_path.exists():
        return IndexingMetrics()

    try:
        connection = connect_readonly(database_path)
    except sqlite3.Error as exc:
        return IndexingMetrics(status_error=f"{exc.__class__.__name__}: {exc}")

    try:
        jobs = OcrJobRepository(connection)
        ocr = OcrChunkRepository(connection)
        pending = jobs.count_jobs_by_statuses(("pending",))
        running = jobs.count_jobs_by_statuses(("running",))
        retryable = jobs.count_jobs_by_statuses(("retryable_failed",))
        dead = jobs.count_jobs_by_statuses(("dead",))
        backlog = jobs.count_jobs_by_statuses(RUNNING_BACKLOG_STATUSES)
        latest_error_job = jobs.latest_error_job()
        semantic_total_chunks = 0
        semantic_embedded_chunks = 0
        last_embedding_error = None
        if config.semantic_search_enabled:
            embeddings = EmbeddingRepository(connection)
            semantic_total_chunks = ocr.count_chunks()
            semantic_embedded_chunks = embeddings.count_current_chunk_embeddings(
                model=config.embedding_model,
                dimension=config.embedding_dimension,
            )
            last_embedding_error = get_last_embedding_indexing_error(
                database_path=config.database_path,
                model=config.embedding_model,
                dimension=config.embedding_dimension,
            )

        return IndexingMetrics(
            backlog_count=backlog,
            pending_job_count=pending,
            running_job_count=running,
            retryable_job_count=retryable,
            dead_job_count=dead,
            latest_indexed_at=ocr.latest_created_at(),
            last_error=(
                _last_error_payload(latest_error_job)
                if latest_error_job is not None
                else None
            ),
            semantic_total_ocr_chunks=semantic_total_chunks,
            semantic_embedded_chunks=semantic_embedded_chunks,
            last_embedding_error=last_embedding_error,
        )
    except sqlite3.Error as exc:
        return IndexingMetrics(status_error=f"{exc.__class__.__name__}: {exc}")
    finally:
        connection.close()


def _status_check_payload(check: StatusCheck) -> dict[str, object]:
    return {"status": check.status, "detail": check.detail}


def _last_error_payload(job) -> dict[str, object]:
    message = job.last_error or ""
    error_type = _error_type_from_message(message)
    symbol = (
        PROVIDER_UNAVAILABLE_ERROR_SYMBOL
        if error_type in _PROVIDER_UNAVAILABLE_ERROR_TYPES
        else None
    )
    return {
        "jobId": job.id,
        "jobType": job.job_type,
        "status": job.status,
        "message": message,
        "type": error_type,
        "symbol": symbol,
        "updatedAt": job.updated_at,
    }


def _error_type_from_message(message: str) -> str | None:
    prefix = message.split(":", 1)[0].strip()
    if prefix.endswith("Error") and prefix.replace("_", "").isalnum():
        return prefix
    return None


def _last_error_symbol(last_error: dict[str, object] | None) -> str | None:
    if last_error is None:
        return None
    value = last_error.get("symbol")
    return value if isinstance(value, str) else None


def _last_error_is_current(metrics: IndexingMetrics) -> bool:
    if metrics.last_error is None:
        return metrics.latest_indexed_at is None
    if metrics.latest_indexed_at is None:
        return True

    updated_at = metrics.last_error.get("updatedAt")
    if not isinstance(updated_at, str) or not updated_at:
        return True
    return updated_at > metrics.latest_indexed_at
