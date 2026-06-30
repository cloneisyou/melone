import binascii
import hashlib
import json
import struct
import zlib
from pathlib import Path

from melone_service.store.db import connect, initialize_database
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import (
    IMAGE_RETENTION_DELETED_AFTER_INDEXING,
    IMAGE_RETENTION_RETAINED,
    IMAGE_RETENTION_RETAINED_AFTER_DEAD_JOB,
    IMAGE_RETENTION_RETAINED_FOR_RETRY,
    ScreenRepository,
)
from melone_service.store.ocr_jobs import OcrJobRepository
from melone_service.ocr import MockOcrClient, OcrResult, OcrWorker
from melone_service.ocr.client import OcrRequest


NOW = "2026-06-09T06:00:00.000Z"
LATER = "2026-06-09T06:01:00.000Z"


def test_mock_ocr_text_is_inserted_into_chunks_and_fts(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(connection, tmp_path)
        _create_ocr_job(connection, job_id="ocr_job_ocr", frame_id=frame.id)

        result = _worker(
            connection,
            client=MockOcrClient(default_text="  Searchable   screen text  "),
        ).process_next_due_job(now=LATER)

        chunks = OcrChunkRepository(connection).list_chunks()
        updated_frame = ScreenRepository(connection).get_frame(frame.id)
        fts_row = connection.execute(
            "SELECT chunk_id, source_key, retrieval_locator, text FROM ocr_chunks_fts"
        ).fetchone()
        job = OcrJobRepository(connection).get_job("ocr_job_ocr")

        assert result is not None
        assert result.status == "done"
        assert result.chunks_inserted == 1
        assert len(chunks) == 1
        assert chunks[0].text == "Searchable screen text"
        assert chunks[0].source_key == "url:https://example.com/docs"
        assert chunks[0].retrieval_locator == "url:https://example.com/docs"
        assert fts_row["chunk_id"] == chunks[0].id
        assert fts_row["text"] == "Searchable screen text"
        assert job.status == "done"
        assert not Path(frame.image_path).exists()
        assert updated_frame.image_retention_state == (
            IMAGE_RETENTION_DELETED_AFTER_INDEXING
        )
    finally:
        connection.close()


def test_successful_ocr_preserves_frame_png_when_retention_enabled(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(connection, tmp_path)
        _create_ocr_job(connection, job_id="ocr_job_ocr", frame_id=frame.id)

        result = _worker(
            connection,
            client=MockOcrClient(default_text="debuggable screen text"),
            retain_screenshots=True,
        ).process_next_due_job(now=LATER)

        updated_frame = ScreenRepository(connection).get_frame(frame.id)
        job = OcrJobRepository(connection).get_job("ocr_job_ocr")

        assert result.status == "done"
        assert job.status == "done"
        assert Path(frame.image_path).is_file()
        assert updated_frame.image_retention_state == IMAGE_RETENTION_RETAINED
    finally:
        connection.close()


def test_successful_ocr_keeps_initial_keyframe_png_as_scene_preview(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(connection, tmp_path)
        _create_ocr_job(
            connection,
            job_id="ocr_job_ocr",
            frame_id=frame.id,
            metadata={"reason": "initial_keyframe"},
        )

        result = _worker(
            connection,
            client=MockOcrClient(default_text="first screen of the scene"),
        ).process_next_due_job(now=LATER)

        updated_frame = ScreenRepository(connection).get_frame(frame.id)
        job = OcrJobRepository(connection).get_job("ocr_job_ocr")

        assert result.status == "done"
        assert job.status == "done"
        assert Path(frame.image_path).is_file()
        assert updated_frame.image_retention_state == IMAGE_RETENTION_RETAINED
    finally:
        connection.close()


def test_duplicate_text_hashes_are_skipped(tmp_path):
    connection = _connection(tmp_path)
    try:
        first = _session_and_frame(
            connection,
            tmp_path,
            frame_id="screen_frame_1",
            fill=(0, 0, 0, 255),
        )
        second = _insert_frame(
            ScreenRepository(connection),
            tmp_path,
            frame_id="screen_frame_2",
            session_id="screen_session_1",
            fill=(255, 255, 255, 255),
        )
        _create_ocr_job(connection, job_id="ocr_job_1", frame_id=first.id)
        _create_ocr_job(connection, job_id="ocr_job_2", frame_id=second.id)
        worker = _worker(
            connection,
            client=MockOcrClient(default_text="same OCR text"),
        )

        first_result = worker.process_next_due_job(now=LATER)
        second_result = worker.process_next_due_job(now=LATER)

        repository = OcrChunkRepository(connection)
        second_job = OcrJobRepository(connection).get_job("ocr_job_2")

        assert first_result.chunks_inserted == 1
        assert second_result.duplicate_chunks_skipped == 1
        assert second_job.status == "done"
        assert repository.count_chunks() == 1
        assert repository.count_fts_rows() == 1
    finally:
        connection.close()


def test_empty_ocr_text_completes_without_chunk_creation(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(connection, tmp_path)
        _create_ocr_job(connection, job_id="ocr_job_empty", frame_id=frame.id)

        result = _worker(
            connection,
            client=MockOcrClient(default_text=" \n\t "),
        ).process_next_due_job(now=LATER)

        job = OcrJobRepository(connection).get_job("ocr_job_empty")

        assert result is not None
        assert result.status == "done"
        assert result.empty_text is True
        assert job.status == "done"
        assert OcrChunkRepository(connection).count_chunks() == 0
        assert OcrChunkRepository(connection).count_fts_rows() == 0
    finally:
        connection.close()


def test_crop_jobs_preserve_crop_bbox_metadata_on_chunk(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(
            connection,
            tmp_path,
            png_bytes=_png_bytes(
                10,
                10,
                fill=(0, 0, 0, 255),
                rect=(4, 4, 2, 2, (255, 255, 255, 255)),
            ),
        )
        crop_bbox_json = json.dumps(
            {"x": 4, "y": 4, "width": 2, "height": 2},
            sort_keys=True,
        )
        _create_ocr_job(
            connection,
            job_id="ocr_job_crop",
            job_type="crop_ocr",
            frame_id=frame.id,
            metadata={
                "crop_bbox_json": crop_bbox_json,
                "source_frame_id": frame.id,
            },
        )
        client = CapturingClient("cropped text")

        result = _worker(connection, client=client).process_next_due_job(now=LATER)

        chunk = OcrChunkRepository(connection).list_chunks()[0]
        request = client.requests[0]

        assert result.status == "done"
        assert request.image_path != Path(frame.image_path)
        assert not request.image_path.exists()
        assert request.metadata["crop_bbox"] == {
            "x": 4,
            "y": 4,
            "width": 2,
            "height": 2,
        }
        assert json.loads(chunk.crop_bbox_json) == {
            "x": 4,
            "y": 4,
            "width": 2,
            "height": 2,
        }
    finally:
        connection.close()


def test_failed_fts_upsert_rolls_back_chunk_and_leaves_job_unfinished(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(connection, tmp_path)
        _create_ocr_job(connection, job_id="ocr_job_partial", frame_id=frame.id)
        worker = OcrWorker(
            client=MockOcrClient(default_text="transactional text"),
            job_repository=OcrJobRepository(connection),
            screen_repository=ScreenRepository(connection),
            ocr_repository=FailingFtsRepository(connection),
        )

        try:
            worker.process_next_due_job(now=LATER)
        except RuntimeError as exc:
            assert "simulated FTS failure" in str(exc)
        else:
            raise AssertionError("expected simulated FTS failure")

        job = OcrJobRepository(connection).get_job("ocr_job_partial")
        repository = OcrChunkRepository(connection)

        assert job.status == "retryable_failed"
        assert repository.count_chunks() == 0
        assert repository.count_fts_rows() == 0
    finally:
        connection.close()


def test_malformed_provider_response_is_recorded_as_retryable_failure(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(connection, tmp_path)
        _create_ocr_job(connection, job_id="ocr_job_malformed", frame_id=frame.id)

        result = _worker(
            connection,
            client=MalformedClient(),
        ).process_next_due_job(now=LATER)

        job = OcrJobRepository(connection).get_job("ocr_job_malformed")
        updated_frame = ScreenRepository(connection).get_frame(frame.id)

        assert result.status == "retryable_failed"
        assert "malformed response" in job.last_error
        assert OcrChunkRepository(connection).count_chunks() == 0
        assert Path(frame.image_path).is_file()
        assert updated_frame.image_retention_state == (
            IMAGE_RETENTION_RETAINED_FOR_RETRY
        )
    finally:
        connection.close()


def test_dead_ocr_failures_retain_frame_png_deterministically(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(connection, tmp_path)
        _create_ocr_job(connection, job_id="ocr_job_dead", frame_id=frame.id)

        result = _worker(
            connection,
            client=MalformedClient(),
            max_attempts=1,
        ).process_next_due_job(now=LATER)

        job = OcrJobRepository(connection).get_job("ocr_job_dead")
        updated_frame = ScreenRepository(connection).get_frame(frame.id)

        assert result.status == "dead"
        assert job.status == "dead"
        assert Path(frame.image_path).is_file()
        assert updated_frame.image_retention_state == (
            IMAGE_RETENTION_RETAINED_AFTER_DEAD_JOB
        )
    finally:
        connection.close()


class CapturingClient:
    def __init__(self, text):
        self.text = text
        self.requests = []

    def extract_text(self, request: OcrRequest) -> OcrResult:
        self.requests.append(request)
        return OcrResult(text=self.text, provider="test", model="test-model")


class MalformedClient:
    def extract_text(self, request: OcrRequest):
        return {"text": "not a OcrResult"}


class FailingFtsRepository(OcrChunkRepository):
    def upsert_fts(self, chunk):
        super().upsert_fts(chunk)
        raise RuntimeError("simulated FTS failure")


def _connection(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    return connect(database_path)


def _worker(
    connection,
    *,
    client,
    retain_screenshots=False,
    max_attempts=3,
):
    return OcrWorker(
        client=client,
        job_repository=OcrJobRepository(connection),
        screen_repository=ScreenRepository(connection),
        ocr_repository=OcrChunkRepository(connection),
        retain_screenshots=retain_screenshots,
        max_attempts=max_attempts,
    )


def _session_and_frame(
    connection,
    tmp_path,
    *,
    frame_id="screen_frame_1",
    fill=(0, 0, 0, 255),
    png_bytes=None,
):
    repository = ScreenRepository(connection)
    existing_session = repository.get_session("screen_session_1")
    if existing_session is None:
        repository.create_session(
            session_id="screen_session_1",
            source_key="url:https://example.com/docs",
            retrieval_locator="url:https://example.com/docs",
            app_name="Google Chrome",
            bundle_id="com.google.Chrome",
            window_title="Docs",
            url="https://example.com/docs",
            started_at=NOW,
            now=NOW,
        )
    return _insert_frame(
        repository,
        tmp_path,
        frame_id=frame_id,
        session_id="screen_session_1",
        fill=fill,
        png_bytes=png_bytes,
    )


def _insert_frame(
    repository,
    tmp_path,
    *,
    frame_id,
    session_id,
    fill=(0, 0, 0, 255),
    png_bytes=None,
):
    png_bytes = _png_bytes(4, 4, fill=fill) if png_bytes is None else png_bytes
    image_path = Path(tmp_path) / f"{frame_id}.png"
    image_path.write_bytes(png_bytes)
    frame = repository.insert_frame(
        frame_id=frame_id,
        session_id=session_id,
        captured_at=NOW,
        image_path=str(image_path),
        sha256=hashlib.sha256(png_bytes).hexdigest(),
        width=_png_dimensions(png_bytes)[0],
        height=_png_dimensions(png_bytes)[1],
    )
    assert frame is not None
    return frame


def _create_ocr_job(
    connection,
    *,
    job_id,
    frame_id,
    job_type="frame_ocr",
    metadata=None,
):
    return OcrJobRepository(connection).create_pending_job(
        job_id=job_id,
        job_type=job_type,
        target_id=frame_id,
        session_id="screen_session_1",
        frame_id=frame_id,
        source_key="url:https://example.com/docs",
        retrieval_locator="url:https://example.com/docs",
        next_run_at=NOW,
        metadata=metadata or {},
        now=NOW,
    )


def _png_bytes(width, height, *, fill=(0, 0, 0, 255), rect=None):
    pixels = [fill for _ in range(width * height)]
    if rect is not None:
        x, y, rect_width, rect_height, color = rect
        for row in range(y, y + rect_height):
            for column in range(x, x + rect_width):
                pixels[row * width + column] = color

    rows = []
    for row in range(height):
        start = row * width
        row_pixels = pixels[start : start + width]
        rows.append(b"\x00" + b"".join(bytes(pixel) for pixel in row_pixels))

    chunks = [
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(b"".join(rows))),
        _png_chunk(b"IEND", b""),
    ]
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _png_dimensions(png_bytes):
    return (
        int.from_bytes(png_bytes[16:20], byteorder="big"),
        int.from_bytes(png_bytes[20:24], byteorder="big"),
    )


def _png_chunk(kind, data):
    crc = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)
