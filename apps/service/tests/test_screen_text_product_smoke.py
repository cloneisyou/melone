import hashlib
from pathlib import Path

from PIL import Image

from melone_service.config import (
    is_screen_search_workers_enabled,
    is_screenshot_collection_enabled,
    load_config,
)
from melone_service.permissions import PermissionSnapshot, StatusCheck
from melone_service.screen_text_status import build_screen_text_status
from melone_service.search import ScreenSearchService
from melone_service.settings import app_settings_path, update_screen_text_settings
from melone_service.store.db import connect, initialize_database
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import (
    IMAGE_RETENTION_DELETED_AFTER_INDEXING,
    ScreenRepository,
)
from melone_service.store.search import ScreenSearchRepository
from melone_service.store.ocr_jobs import OcrJobRepository
from melone_service.ocr import MockOcrClient, OcrWorker


NOW = "2026-06-19T01:00:00.000Z"
INDEXED_AT = "2026-06-19T01:01:00.000Z"


def test_screen_text_product_smoke_defaults_opt_in_indexing_and_search(tmp_path):
    env = {
        "MELONE_HOME": str(tmp_path),
        "MELONE_OCR_PROVIDER": "mock",
    }
    fresh_config = load_config(env=env)

    fresh_status = build_screen_text_status(
        fresh_config,
        permission_snapshot=_permissions(),
    )

    assert fresh_status["state"] == "off"
    assert fresh_status["settings"]["enabled"] is False
    assert fresh_status["effectiveEnabled"] is False
    assert is_screenshot_collection_enabled(fresh_config) is False
    assert is_screen_search_workers_enabled(fresh_config) is False

    update_screen_text_settings(app_settings_path(tmp_path), enabled=True)
    enabled_config = load_config(env=env)

    assert is_screenshot_collection_enabled(enabled_config) is True
    assert is_screen_search_workers_enabled(enabled_config) is True

    blocked_status = build_screen_text_status(
        enabled_config,
        permission_snapshot=_permissions(screen_recording="denied"),
    )

    assert blocked_status["state"] == "blocked"
    assert blocked_status["reason"] == "screen_recording_permission_required"
    assert blocked_status["requiredPermissions"] == ["screen_recording"]

    initialize_database(enabled_config.database_path)
    connection = connect(enabled_config.database_path)
    try:
        frame = _seed_screen_frame(connection, tmp_path)
        OcrJobRepository(connection).create_pending_job(
            job_id="ocr_job_screen_text_smoke",
            job_type="frame_ocr",
            target_id=frame.id,
            session_id="screen_session_smoke",
            frame_id=frame.id,
            source_key="url:https://example.com/release-checklist",
            retrieval_locator="url:https://example.com/release-checklist",
            next_run_at=NOW,
            now=NOW,
        )

        indexing_status = build_screen_text_status(
            enabled_config,
            permission_snapshot=_permissions(),
        )

        assert indexing_status["state"] == "indexing"
        assert indexing_status["backlogCount"] == 1

        result = OcrWorker(
            client=MockOcrClient(
                default_text="Quarterly review screen text approval"
            ),
            job_repository=OcrJobRepository(connection),
            screen_repository=ScreenRepository(connection),
            ocr_repository=OcrChunkRepository(connection),
            retain_screenshots=enabled_config.screen_text_retain_screenshots,
        ).process_next_due_job(now=INDEXED_AT)

        updated_frame = ScreenRepository(connection).get_frame(frame.id)
        search_results = ScreenSearchService(
            ScreenSearchRepository(connection),
            preview_length=120,
        ).search("quarterly review")

        assert result is not None
        assert result.status == "done"
        assert result.chunks_inserted == 1
        assert OcrChunkRepository(connection).count_fts_rows() == 1
        assert Path(frame.image_path).exists() is False
        assert updated_frame is not None
        assert (
            updated_frame.image_retention_state
            == IMAGE_RETENTION_DELETED_AFTER_INDEXING
        )
        assert len(search_results) == 1
        assert search_results[0].retrieval_locator == (
            "url:https://example.com/release-checklist"
        )
        assert "Quarterly review screen text approval" in search_results[0].preview
    finally:
        connection.close()

    ready_status = build_screen_text_status(
        enabled_config,
        permission_snapshot=_permissions(),
    )

    assert ready_status["state"] == "ready"
    assert ready_status["latestIndexedAt"] == INDEXED_AT
    assert ready_status["screenshotRetention"] == "delete_after_indexing"


def _seed_screen_frame(connection, tmp_path):
    screen_repository = ScreenRepository(connection)
    session = screen_repository.create_session(
        session_id="screen_session_smoke",
        source_key="url:https://example.com/release-checklist",
        retrieval_locator="url:https://example.com/release-checklist",
        app_name="Safari",
        bundle_id="com.apple.Safari",
        window_title="Release Checklist",
        url="https://example.com/release-checklist",
        started_at=NOW,
        now=NOW,
    )
    image_path = tmp_path / "screenshots" / session.id / "screen_frame_smoke.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 8), color=(255, 255, 255)).save(image_path)
    png_bytes = image_path.read_bytes()
    frame = screen_repository.insert_frame(
        frame_id="screen_frame_smoke",
        session_id=session.id,
        captured_at=NOW,
        image_path=str(image_path),
        sha256=hashlib.sha256(png_bytes).hexdigest(),
        width=16,
        height=8,
    )
    assert frame is not None
    return frame


def _permissions(*, screen_recording: str = "granted") -> PermissionSnapshot:
    screen_recording_check = StatusCheck(
        screen_recording,
        None
        if screen_recording == "granted"
        else "Screen Recording permission is not granted",
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
