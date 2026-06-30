from __future__ import annotations

import hashlib

from melone_service.config import load_config
from melone_service.permissions import PermissionSnapshot, StatusCheck
from melone_service.rpc.methods import dispatch
from melone_service.screen_text_status import build_screen_text_status
from melone_service.settings import app_settings_path, update_screen_text_settings
from melone_service.store.db import connect, initialize_database
from melone_service.store.embeddings import EmbeddingRepository
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository
from melone_service.store.ocr_jobs import OcrJobRepository
from melone_service.ocr.errors import OcrUnavailableError


INDEXED_AT = "2026-06-19T01:02:03.000Z"


def test_off_settings_produce_off_status(tmp_path):
    config = _config(tmp_path)

    status = build_screen_text_status(
        config,
        permission_snapshot=_permissions(),
    )

    assert status["state"] == "off"
    assert status["reason"] == "disabled"
    assert status["settings"]["enabled"] is False
    assert status["effectiveEnabled"] is False
    assert status["backlogCount"] == 0


def test_missing_screen_recording_permission_blocks_status(tmp_path):
    config = _enabled_config(tmp_path)

    status = build_screen_text_status(
        config,
        permission_snapshot=_permissions(screen_recording="denied"),
    )

    assert status["state"] == "blocked"
    assert status["reason"] == "screen_recording_permission_required"
    assert status["requiredPermissions"] == ["screen_recording"]
    assert status["screenRecordingPermission"]["status"] == "denied"


def test_unavailable_provider_blocks_with_stable_reason(tmp_path):
    config = _enabled_config(tmp_path, provider="apple_vision")

    status = build_screen_text_status(
        config,
        permission_snapshot=_permissions(),
        platform_name="linux",
    )

    assert status["state"] == "blocked"
    assert status["reason"] == "provider_unavailable"
    assert status["provider"]["available"] is False
    assert status["provider"]["reason"] == "apple_vision_requires_macos"


def test_pending_ocr_backlog_reports_indexing(tmp_path):
    config = _enabled_config(tmp_path)
    initialize_database(config.database_path)
    connection = connect(config.database_path)
    try:
        OcrJobRepository(connection).create_pending_job(
            job_id="ocr_job_pending",
            job_type="frame_ocr",
            target_id="screen_frame_1",
        )
    finally:
        connection.close()

    status = build_screen_text_status(
        config,
        permission_snapshot=_permissions(),
    )

    assert status["state"] == "indexing"
    assert status["reason"] == "backlog_pending"
    assert status["backlogCount"] == 1
    assert status["pendingJobCount"] == 1


def test_pending_index_backlog_reports_indexing(tmp_path):
    config = _enabled_config(tmp_path)
    initialize_database(config.database_path)
    connection = connect(config.database_path)
    try:
        OcrJobRepository(connection).create_pending_job(
            job_id="ocr_job_finalize",
            job_type="session_finalize",
            target_id="screen_session_1",
        )
    finally:
        connection.close()

    status = build_screen_text_status(
        config,
        permission_snapshot=_permissions(),
    )

    assert status["state"] == "indexing"
    assert status["reason"] == "backlog_pending"
    assert status["backlogCount"] == 1
    assert status["pendingJobCount"] == 1


def test_no_blockers_and_no_backlog_reports_ready_with_latest_index_time(tmp_path):
    config = _enabled_config(tmp_path)
    initialize_database(config.database_path)
    _seed_indexed_chunk(config.database_path)

    status = build_screen_text_status(
        config,
        permission_snapshot=_permissions(),
    )

    assert status["state"] == "ready"
    assert status["reason"] is None
    assert status["backlogCount"] == 0
    assert status["latestOcrAt"] == INDEXED_AT
    assert status["latestIndexedAt"] == INDEXED_AT


def test_semantic_indexing_status_reports_active_model_coverage(tmp_path):
    update_screen_text_settings(app_settings_path(tmp_path), enabled=True)
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_OCR_PROVIDER": "mock",
            "MELONE_SEMANTIC_SEARCH_ENABLED": "true",
            "MELONE_EMBEDDING_MODEL": "status-test-model",
            "MELONE_EMBEDDING_DIMENSION": "128",
        }
    )
    initialize_database(config.database_path)
    _seed_indexed_chunk(config.database_path)
    connection = connect(config.database_path)
    try:
        chunk = OcrChunkRepository(connection).list_chunks()[0]
        EmbeddingRepository(connection).upsert_chunk_embedding(
            chunk_id=chunk.id,
            model="status-test-model",
            dimension=128,
            text_hash=chunk.text_hash,
            embedding=[1.0, *([0.0] * 127)],
        )
        connection.commit()
    finally:
        connection.close()

    status = build_screen_text_status(
        config,
        permission_snapshot=_permissions(),
    )

    assert status["semanticIndexing"] == {
        "enabled": True,
        "model": "status-test-model",
        "dimension": 128,
        "totalOcrChunks": 1,
        "embeddedChunks": 1,
        "lastError": None,
    }


def test_provider_unavailable_job_error_reports_error_state(tmp_path):
    config = _enabled_config(tmp_path)
    initialize_database(config.database_path)
    connection = connect(config.database_path)
    try:
        jobs = OcrJobRepository(connection)
        jobs.create_pending_job(
            job_id="ocr_job_dead",
            job_type="frame_ocr",
            target_id="screen_frame_1",
        )
        jobs.mark_dead(
            "ocr_job_dead",
            error=OcrUnavailableError("provider unavailable"),
        )
    finally:
        connection.close()

    status = build_screen_text_status(
        config,
        permission_snapshot=_permissions(),
    )

    assert status["state"] == "error"
    assert status["reason"] == "provider_unavailable"
    assert status["deadJobCount"] == 1
    assert status["lastError"]["symbol"] == "provider_unavailable"
    assert status["lastError"]["jobId"] == "ocr_job_dead"


def test_stale_dead_job_before_latest_index_does_not_force_error(tmp_path):
    config = _enabled_config(tmp_path)
    initialize_database(config.database_path)
    _seed_indexed_chunk(config.database_path)
    connection = connect(config.database_path)
    try:
        jobs = OcrJobRepository(connection)
        jobs.create_pending_job(
            job_id="ocr_job_old_dead",
            job_type="frame_ocr",
            target_id="screen_frame_old",
            now="2026-06-19T00:00:00.000Z",
        )
        jobs.mark_dead(
            "ocr_job_old_dead",
            error=OcrUnavailableError("old provider unavailable"),
            now="2026-06-19T00:00:01.000Z",
        )
    finally:
        connection.close()

    status = build_screen_text_status(
        config,
        permission_snapshot=_permissions(),
    )

    assert status["state"] == "ready"
    assert status["reason"] is None
    assert status["deadJobCount"] == 1
    assert status["latestIndexedAt"] == INDEXED_AT


def test_legacy_vlm_unavailable_error_maps_to_provider_unavailable(tmp_path):
    config = _enabled_config(tmp_path)
    initialize_database(config.database_path)
    connection = connect(config.database_path)
    try:
        jobs = OcrJobRepository(connection)
        jobs.create_pending_job(
            job_id="ocr_job_legacy_dead",
            job_type="frame_ocr",
            target_id="screen_frame_1",
        )
        jobs.mark_dead(
            "ocr_job_legacy_dead",
            error="VlmOcrUnavailableError: local OpenAI-compatible VLM is unavailable",
        )
    finally:
        connection.close()

    status = build_screen_text_status(
        config,
        permission_snapshot=_permissions(),
    )

    assert status["state"] == "error"
    assert status["reason"] == "provider_unavailable"
    assert status["lastError"]["type"] == "VlmOcrUnavailableError"
    assert status["lastError"]["symbol"] == "provider_unavailable"


def test_rpc_screen_text_status_and_update_settings_persist(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    monkeypatch.setenv("MELONE_OCR_PROVIDER", "mock")
    monkeypatch.setattr(
        "melone_service.screen_text_status.check_permission_status",
        lambda: _permissions(),
    )

    assert dispatch("screenText.status", {})["state"] == "off"

    updated = dispatch("screenText.updateSettings", {"enabled": True})
    assert updated["state"] == "ready"
    assert updated["settings"]["enabled"] is True

    reloaded = dispatch("screenText.status", {})
    assert reloaded["state"] == "ready"
    assert reloaded["settings"]["enabled"] is True


def _config(tmp_path, *, provider: str = "mock"):
    return load_config(
        env={
            "MELONE_HOME": str(tmp_path),
            "MELONE_OCR_PROVIDER": provider,
        }
    )


def _enabled_config(tmp_path, *, provider: str = "mock"):
    update_screen_text_settings(app_settings_path(tmp_path), enabled=True)
    return _config(tmp_path, provider=provider)


def _permissions(*, screen_recording: str = "granted") -> PermissionSnapshot:
    screen_recording_check = StatusCheck(
        screen_recording,
        None if screen_recording == "granted" else "Screen Recording permission is not granted",
    )
    accessibility_check = StatusCheck("granted")
    return PermissionSnapshot(
        permissions={
            "accessibility": accessibility_check,
            "screen_recording": screen_recording_check,
        },
        collectors={
            "active_window": screen_recording_check,
            "current_asset": screen_recording_check,
            "keyboard": accessibility_check,
            "mouse": accessibility_check,
            "screenshot": screen_recording_check,
        },
    )


def _seed_indexed_chunk(database_path):
    connection = connect(database_path)
    try:
        screen_repository = ScreenRepository(connection)
        screen_repository.create_session(
            session_id="screen_session_1",
            source_key="url:https://example.com/docs",
            retrieval_locator="url:https://example.com/docs",
            app_name="Safari",
            bundle_id="com.apple.Safari",
            window_title="Docs",
            url="https://example.com/docs",
            started_at=INDEXED_AT,
            now=INDEXED_AT,
        )
        screen_repository.insert_frame(
            frame_id="screen_frame_1",
            session_id="screen_session_1",
            captured_at=INDEXED_AT,
            image_path="/tmp/screen_frame_1.png",
            sha256=hashlib.sha256(b"screen_frame_1").hexdigest(),
            width=1280,
            height=720,
        )
        OcrChunkRepository(connection).insert_chunk_with_fts(
            chunk_id="ocr_chunk_1",
            session_id="screen_session_1",
            frame_id="screen_frame_1",
            source_key="url:https://example.com/docs",
            retrieval_locator="url:https://example.com/docs",
            text="Searchable screen text",
            text_hash=hashlib.sha256(b"Searchable screen text").hexdigest(),
            provider="mock",
            model="mock-ocr",
            created_at=INDEXED_AT,
        )
        connection.commit()
    finally:
        connection.close()
