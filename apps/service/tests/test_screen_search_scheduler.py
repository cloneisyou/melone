import binascii
import struct
import threading
import zlib
from datetime import datetime, timezone
from pathlib import Path

from melone_service.cli import main
from melone_service.collectors.screenshot import CapturedScreenshot, ScreenshotCollector
from melone_service.config import ServiceConfig, load_config
from melone_service.embeddings import (
    EmbeddingModelInfo,
    EmbeddingUnavailableError,
    FakeEmbeddingModel,
)
from melone_service.pipeline.normalizer import normalize_event
from melone_service.pipeline.screen_search_scheduler import (
    capture_policy_for_backlog,
    get_last_embedding_indexing_error,
    run_screen_search_workers_once,
)
from melone_service.settings import AppSettings, ScreenTextSettings, save_app_settings
from melone_service.store.context_rank import ContextRankRepository, ContextRankScore
from melone_service.store.db import connect, initialize_database
from melone_service.store.embeddings import EmbeddingRepository
from melone_service.store.events import EventRepository
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository
from melone_service.store.ocr_jobs import OcrJobRepository
from melone_service.store.search import ScreenSearchRepository
from melone_service.ocr import (
    PROVIDER_UNAVAILABLE_ERROR_SYMBOL,
    OcrUnavailableError,
)


NOW = datetime(2026, 6, 9, 6, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 6, 9, 6, 1, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-06-09T06:00:00.000Z"


def test_workers_disabled_does_not_create_screen_sessions(tmp_path):
    config = _config(tmp_path)
    initialize_database(config.database_path)
    _insert_context_event(config.database_path, NOW, url="https://example.com/docs")

    result = run_screen_search_workers_once(config)

    connection = connect(config.database_path)
    try:
        assert result.jobs_processed == 0
        assert ScreenRepository(connection).count_sessions() == 0
        assert ContextRankRepository(connection).count_scores() == 0
    finally:
        connection.close()


def test_persisted_off_setting_blocks_loaded_screen_search_workers(tmp_path):
    save_app_settings(
        tmp_path / "settings.json",
        AppSettings(screen_text=ScreenTextSettings(enabled=False)),
    )
    config = load_config(env={"MELONE_HOME": str(tmp_path)})
    initialize_database(config.database_path)
    _insert_context_event(config.database_path, NOW, url="https://example.com/docs")

    result = run_screen_search_workers_once(config)

    connection = connect(config.database_path)
    try:
        assert result.jobs_processed == 0
        assert ScreenRepository(connection).count_sessions() == 0
    finally:
        connection.close()


def test_workers_stop_cleanly_when_stop_event_is_set(tmp_path):
    config = _config(tmp_path, workers_enabled=True)
    initialize_database(config.database_path)
    _insert_context_event(config.database_path, NOW, url="https://example.com/docs")
    stop_event = threading.Event()
    stop_event.set()

    result = run_screen_search_workers_once(config, stop_event=stop_event)

    connection = connect(config.database_path)
    try:
        assert result.jobs_processed == 0
        assert ScreenRepository(connection).count_sessions() == 0
    finally:
        connection.close()


def test_backlog_thresholds_change_capture_policy(tmp_path):
    config = _config(
        tmp_path,
        high_backlog_threshold=3,
        very_high_backlog_threshold=5,
    )

    normal = capture_policy_for_backlog(config, backlog_count=2)
    high = capture_policy_for_backlog(config, backlog_count=3)
    very_high = capture_policy_for_backlog(config, backlog_count=5)

    assert normal.level == "normal"
    assert normal.min_interval_seconds == 10
    assert normal.transition_frame_only is False
    assert high.level == "high"
    assert high.min_interval_seconds == 20
    assert high.transition_frame_only is False
    assert very_high.level == "very_high"
    assert very_high.min_interval_seconds == 40
    assert very_high.transition_frame_only is True


def test_transition_frame_only_policy_skips_repeated_session_capture(tmp_path):
    config = _config(tmp_path)
    initialize_database(config.database_path)
    connection = connect(config.database_path)
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
            started_at=NOW_ISO,
            now=NOW_ISO,
        )
        capture = _FakeCapture(
            [
                CapturedScreenshot(_png_bytes(1, 1, text=b"first"), 1, 1),
                CapturedScreenshot(_png_bytes(1, 1, text=b"second"), 1, 1),
            ]
        )
        collector = ScreenshotCollector(
            screen_repository=screen_repository,
            screenshots_dir=tmp_path / "screenshots",
            min_interval_seconds=0,
            capture_api=capture,
            platform_name="darwin",
            frame_id_factory=_FrameIds(),
            timestamp_factory=lambda: NOW_ISO,
        )
        collector.set_capture_policy(
            min_interval_seconds=0,
            transition_frame_only=True,
        )

        collector.poll()
        collector.poll()

        assert capture.calls == 1
        assert screen_repository.count_frames() == 1
    finally:
        connection.close()


def test_status_outputs_ocr_counts_and_context_rank_status(
    capsys,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("MELONE_HOME", str(tmp_path))
    monkeypatch.setenv("MELONE_SCREENSHOT_COLLECTOR_ENABLED", "true")
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    connection = connect(database_path)
    try:
        jobs = OcrJobRepository(connection)
        jobs.create_pending_job(
            job_id="ocr_job_pending",
            job_type="frame_ocr",
            target_id="a",
        )
        jobs.create_pending_job(
            job_id="ocr_job_running",
            job_type="frame_ocr",
            target_id="b",
        )
        jobs.lock_due_job(job_types=("frame_ocr",))
        jobs.create_pending_job(
            job_id="ocr_job_dead",
            job_type="frame_ocr",
            target_id="c",
        )
        jobs.mark_dead("ocr_job_dead", error="terminal")
        ContextRankRepository(connection).upsert_scores(
            [
                ContextRankScore(
                    source_key="url:https://example.com/docs",
                    score=1.0,
                    visits=1,
                    retrieval_locators=("url:https://example.com/docs",),
                    computed_at=NOW_ISO,
                    model_version="test",
                )
            ]
        )
    finally:
        connection.close()

    exit_code = main(["status"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "pending OCR jobs: 1" in output
    assert "running OCR jobs: 1" in output
    assert "dead OCR jobs: 1" in output
    assert f"latest context rank computed_at: {NOW_ISO}" in output
    assert "screenshot collector enabled: yes" in output


def test_enabled_pipeline_flows_from_capture_to_indexing(tmp_path):
    config = _config(
        tmp_path,
        screenshot_enabled=True,
        workers_enabled=True,
        max_jobs_per_tick=4,
    )
    initialize_database(config.database_path)
    _insert_context_event(config.database_path, NOW, url="https://example.com/docs")

    first_tick = run_screen_search_workers_once(config)
    _capture_frame(config.database_path, tmp_path)
    _insert_context_event(config.database_path, LATER, url="https://example.com/blog")
    second_tick = run_screen_search_workers_once(config)

    connection = connect(config.database_path)
    try:
        chunks = OcrChunkRepository(connection).list_chunks()
        jobs = OcrJobRepository(connection)

        assert first_tick.context_session_updated is True
        assert second_tick.finalize_jobs_processed == 1
        assert second_tick.ocr_jobs_processed == 1
        assert second_tick.indexed_chunks == 1
        assert jobs.count_jobs(status="done") == 2
        assert len(chunks) == 1
        assert chunks[0].text == "mock OCR text"
        assert chunks[0].retrieval_locator == "url:https://example.com/docs"
    finally:
        connection.close()


def test_worker_tick_indexes_ocr_embeddings_with_fake_model(tmp_path):
    config = _config(
        tmp_path,
        workers_enabled=True,
        max_jobs_per_tick=1,
        semantic_enabled=True,
        embedding_model="test-embedding-model",
        embedding_dimension=2,
        embedding_batch_size=4,
    )
    initialize_database(config.database_path)
    _insert_screen_frame_and_ocr_job(config.database_path, tmp_path)
    embedding_model = FakeEmbeddingModel(
        model="test-embedding-model",
        dimension=2,
        document_vectors={"mock OCR text": [0.6, 0.8]},
    )

    result = run_screen_search_workers_once(
        config,
        embedding_model=embedding_model,
    )

    connection = connect(config.database_path)
    try:
        chunks = OcrChunkRepository(connection).list_chunks()
        repository = EmbeddingRepository(connection)
        stored = repository.get_chunk_embedding(
            chunk_id=chunks[0].id,
            model="test-embedding-model",
            dimension=2,
        )

        assert result.ocr_jobs_processed == 1
        assert result.indexed_chunks == 1
        assert result.embedding_indexed_chunks == 1
        assert result.embedding_skipped_chunks == 0
        assert result.embedding_error_count == 0
        assert result.semantic_total_ocr_chunks == 1
        assert result.semantic_embedded_chunks == 1
        assert embedding_model.query_calls == []
        assert embedding_model.document_calls == ["mock OCR text"]
        assert stored is not None
    finally:
        connection.close()


def test_worker_tick_keeps_bm25_usable_when_embedding_model_fails(tmp_path):
    config = _config(
        tmp_path,
        workers_enabled=True,
        max_jobs_per_tick=1,
        semantic_enabled=True,
        embedding_model="unavailable-test-model",
        embedding_dimension=2,
        embedding_batch_size=4,
    )
    initialize_database(config.database_path)
    _insert_screen_frame_and_ocr_job(config.database_path, tmp_path)

    result = run_screen_search_workers_once(
        config,
        embedding_model=_UnavailableEmbeddingModel(),
    )

    connection = connect(config.database_path)
    try:
        job = OcrJobRepository(connection).get_job("ocr_job_unavailable")
        chunks = OcrChunkRepository(connection).list_chunks()
        bm25_matches = ScreenSearchRepository(connection).search_chunks("mock OCR")

        assert result.ocr_jobs_processed == 1
        assert result.indexed_chunks == 1
        assert result.embedding_indexed_chunks == 0
        assert result.embedding_error_count == 1
        assert result.semantic_total_ocr_chunks == 1
        assert result.semantic_embedded_chunks == 0
        assert result.last_embedding_error_type == EmbeddingUnavailableError.__name__
        assert "embedding unavailable for test" in result.last_embedding_error
        assert job.status == "done"
        assert len(chunks) == 1
        assert [candidate.chunk_id for candidate in bm25_matches] == [chunks[0].id]
    finally:
        connection.close()


def test_embedding_status_error_is_sanitized(tmp_path):
    config = _config(
        tmp_path,
        workers_enabled=True,
        max_jobs_per_tick=1,
        semantic_enabled=True,
        embedding_model="unavailable-test-model",
        embedding_dimension=2,
        embedding_batch_size=4,
    )
    initialize_database(config.database_path)
    _insert_screen_frame_and_ocr_job(config.database_path, tmp_path)

    run_screen_search_workers_once(
        config,
        embedding_model=_NoisyUnavailableEmbeddingModel(),
    )

    error = get_last_embedding_indexing_error(
        database_path=config.database_path,
        model="unavailable-test-model",
        dimension=2,
    )

    assert error == {
        "type": EmbeddingUnavailableError.__name__,
        "message": "model access denied; accept Gemma license",
    }


def test_worker_tick_surfaces_provider_unavailable_for_status(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path, workers_enabled=True, max_jobs_per_tick=1)
    initialize_database(config.database_path)
    _insert_screen_frame_and_ocr_job(config.database_path, tmp_path)

    monkeypatch.setattr(
        "melone_service.pipeline.screen_search_scheduler.create_ocr_client",
        lambda _config: UnavailableOcrClient(),
    )

    result = run_screen_search_workers_once(config)

    connection = connect(config.database_path)
    try:
        job = OcrJobRepository(connection).get_job("ocr_job_unavailable")
        assert result.ocr_jobs_processed == 1
        assert result.provider_unavailable is True
        assert result.last_ocr_error_type == OcrUnavailableError.__name__
        assert result.last_ocr_error_symbol == PROVIDER_UNAVAILABLE_ERROR_SYMBOL
        assert job.status == "retryable_failed"
        assert job.attempts == 1
    finally:
        connection.close()


def _config(
    data_dir: Path,
    *,
    ocr_provider: str = "mock",
    screenshot_enabled: bool = False,
    workers_enabled: bool = False,
    max_jobs_per_tick: int = 2,
    high_backlog_threshold: int = 20,
    very_high_backlog_threshold: int = 100,
    semantic_enabled: bool = False,
    embedding_model: str = "google/embeddinggemma-300m",
    embedding_dimension: int = 256,
    embedding_batch_size: int = 16,
) -> ServiceConfig:
    return ServiceConfig(
        app_name="Melone",
        data_dir=data_dir,
        database_path=data_dir / "melone.sqlite",
        pid_file_path=data_dir / "melone.pid",
        lock_file_path=data_dir / "melone.lock",
        pause_flag_path=data_dir / "melone.paused",
        logs_dir=data_dir / "logs",
        screenshots_dir=data_dir / "screenshots",
        polling_interval_seconds=0.05,
        idle_timeout_seconds=300,
        ocr_provider=ocr_provider,
        screenshot_min_interval_seconds=10,
        screenshot_collector_enabled=screenshot_enabled,
        screen_search_workers_enabled=workers_enabled,
        screen_search_max_jobs_per_tick=max_jobs_per_tick,
        screen_search_high_backlog_threshold=high_backlog_threshold,
        screen_search_very_high_backlog_threshold=very_high_backlog_threshold,
        semantic_search_enabled=semantic_enabled,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        embedding_batch_size=embedding_batch_size,
    )


class UnavailableOcrClient:
    def extract_text(self, request):
        raise OcrUnavailableError("provider unavailable for test")


class _UnavailableEmbeddingModel:
    @property
    def info(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(
            provider="fake",
            model="unavailable-test-model",
            dimension=2,
        )

    def encode_query(self, query: str):
        raise AssertionError("query path should not be used by the indexer")

    def encode_document(self, text: str):
        raise EmbeddingUnavailableError("embedding unavailable for test")


class _NoisyUnavailableEmbeddingModel(_UnavailableEmbeddingModel):
    def encode_document(self, text: str):
        raise EmbeddingUnavailableError(
            "model access denied; accept Gemma license\n"
            "Traceback (most recent call last):\n"
            '  File "provider.py", line 1, in load\n'
            + ("x" * 500)
        )


def _insert_context_event(database_path: Path, timestamp: datetime, *, url: str) -> None:
    connection = connect(database_path)
    try:
        EventRepository(connection).insert(
            normalize_event(
                "current_asset_changed",
                app={
                    "name": "Safari",
                    "bundle_id": "com.apple.Safari",
                    "pid": 123,
                },
                window={"title": "Docs"},
                url=url,
                source="test",
                timestamp=timestamp,
            )
        )
    finally:
        connection.close()


def _insert_screen_frame_and_ocr_job(database_path: Path, tmp_path: Path) -> None:
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
            started_at=NOW_ISO,
            now=NOW_ISO,
        )
        png_bytes = _png_bytes(2, 2, text=b"ocr")
        image_path = tmp_path / "screen_frame_1.png"
        image_path.write_bytes(png_bytes)
        frame = screen_repository.insert_frame(
            frame_id="screen_frame_1",
            session_id="screen_session_1",
            captured_at=NOW_ISO,
            image_path=str(image_path),
            sha256="screen-frame-sha",
            width=2,
            height=2,
        )
        assert frame is not None
        OcrJobRepository(connection).create_pending_job(
            job_id="ocr_job_unavailable",
            job_type="frame_ocr",
            target_id=frame.id,
            session_id="screen_session_1",
            frame_id=frame.id,
            source_key="url:https://example.com/docs",
            retrieval_locator="url:https://example.com/docs",
            next_run_at=NOW_ISO,
            now=NOW_ISO,
        )
    finally:
        connection.close()


def _capture_frame(database_path: Path, tmp_path: Path) -> None:
    connection = connect(database_path)
    try:
        collector = ScreenshotCollector(
            screen_repository=ScreenRepository(connection),
            screenshots_dir=tmp_path / "screenshots",
            min_interval_seconds=0,
            capture_api=_FakeCapture(
                [CapturedScreenshot(_png_bytes(2, 2, text=b"captured"), 2, 2)]
            ),
            platform_name="darwin",
            frame_id_factory=lambda: "screen_frame_1",
            timestamp_factory=lambda: NOW_ISO,
        )
        frame = collector.capture_latest_frame()
        assert frame is not None
    finally:
        connection.close()


class _FakeCapture:
    def __init__(self, captures):
        self.captures = list(captures)
        self.calls = 0

    def capture_png(self):
        self.calls += 1
        if len(self.captures) == 1:
            return self.captures[0]
        return self.captures.pop(0)


class _FrameIds:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return f"screen_frame_{self.count}"


def _png_bytes(width=1, height=1, *, text=b""):
    row = b"\x00" + (b"\x00\x00\x00\x00" * width)
    image_data = row * height
    chunks = [
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
        _png_chunk(b"tEXt", text),
        _png_chunk(b"IDAT", zlib.compress(image_data)),
        _png_chunk(b"IEND", b""),
    ]
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _png_chunk(kind, data):
    crc = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)
