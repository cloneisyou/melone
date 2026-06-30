import binascii
import hashlib
import json
import struct
import zlib
from pathlib import Path

from melone_service.pipeline.screen_finalize import (
    ScreenFinalizePolicy,
    SessionFinalizer,
)
from melone_service.store.db import connect, initialize_database
from melone_service.store.screen import ScreenRepository
from melone_service.store.ocr_jobs import OcrJobRepository


NOW = "2026-06-09T06:00:00.000Z"
LATER = "2026-06-09T06:01:00.000Z"
FINALIZED_AT = "2026-06-09T06:02:00.000Z"


def test_exact_duplicate_sha_frames_do_not_create_duplicate_ocr_jobs(tmp_path):
    connection = _connection(tmp_path)
    try:
        screen_repository = ScreenRepository(connection)
        session = _closed_session(screen_repository)
        png_bytes = _png_bytes(4, 4)

        first = _insert_frame(
            screen_repository,
            tmp_path,
            session_id=session.id,
            frame_id="screen_frame_1",
            png_bytes=png_bytes,
        )
        duplicate = screen_repository.insert_frame(
            frame_id="screen_frame_2",
            session_id=session.id,
            captured_at=LATER,
            image_path=str(tmp_path / "duplicate.png"),
            sha256=first.sha256,
            width=4,
            height=4,
        )
        _create_finalize_job(connection, session.id)

        result = _finalizer(connection).finalize_next_due_job(now=FINALIZED_AT)

        assert duplicate is None
        assert result is not None
        assert result.frame_ocr_jobs_created == 1
        assert _count_ocr_jobs(connection) == 1
        assert _jobs(connection, job_type="frame_ocr")[0]["frame_id"] == first.id
    finally:
        connection.close()


def test_near_duplicate_perceptual_hashes_are_deduped(tmp_path):
    connection = _connection(tmp_path)
    try:
        screen_repository = ScreenRepository(connection)
        session = _closed_session(screen_repository)
        _insert_frame(
            screen_repository,
            tmp_path,
            session_id=session.id,
            frame_id="screen_frame_1",
            png_bytes=_png_bytes(4, 4, fill=(0, 0, 0, 255)),
            perceptual_hash="0000000000000000",
        )
        near_duplicate = _insert_frame(
            screen_repository,
            tmp_path,
            session_id=session.id,
            frame_id="screen_frame_2",
            png_bytes=_png_bytes(4, 4, fill=(1, 1, 1, 255)),
            perceptual_hash="0000000000000001",
            captured_at=LATER,
        )
        _create_finalize_job(connection, session.id)

        result = _finalizer(connection).finalize_next_due_job(now=FINALIZED_AT)

        assert result is not None
        assert result.near_duplicates_skipped == 1
        assert _count_ocr_jobs(connection) == 1
        assert screen_repository.get_frame(near_duplicate.id).status == "skipped"
    finally:
        connection.close()


def test_frames_with_meaningful_change_become_keyframe_ocr_jobs(tmp_path):
    connection = _connection(tmp_path)
    try:
        screen_repository = ScreenRepository(connection)
        session = _closed_session(screen_repository)
        _insert_frame(
            screen_repository,
            tmp_path,
            session_id=session.id,
            frame_id="screen_frame_1",
            png_bytes=_png_bytes(4, 4, fill=(0, 0, 0, 255)),
            perceptual_hash="0000",
        )
        changed = _insert_frame(
            screen_repository,
            tmp_path,
            session_id=session.id,
            frame_id="screen_frame_2",
            png_bytes=_png_bytes(4, 4, fill=(255, 255, 255, 255)),
            perceptual_hash="ffff",
            captured_at=LATER,
        )
        _create_finalize_job(connection, session.id)

        result = _finalizer(connection).finalize_next_due_job(now=FINALIZED_AT)

        assert result is not None
        assert result.frame_ocr_jobs_created == 2
        assert _count_ocr_jobs(connection) == 2
        assert screen_repository.get_frame(changed.id).diff_score == 1.0
        assert [job["type"] for job in _ocr_jobs(connection)] == [
            "frame_ocr",
            "frame_ocr",
        ]
    finally:
        connection.close()


def test_crop_ocr_jobs_preserve_crop_metadata(tmp_path):
    connection = _connection(tmp_path)
    try:
        screen_repository = ScreenRepository(connection)
        session = _closed_session(screen_repository)
        _insert_frame(
            screen_repository,
            tmp_path,
            session_id=session.id,
            frame_id="screen_frame_1",
            png_bytes=_png_bytes(10, 10, fill=(0, 0, 0, 255)),
        )
        crop_frame = _insert_frame(
            screen_repository,
            tmp_path,
            session_id=session.id,
            frame_id="screen_frame_2",
            png_bytes=_png_bytes(
                10,
                10,
                fill=(0, 0, 0, 255),
                rect=(4, 4, 2, 2, (255, 255, 255, 255)),
            ),
            captured_at=LATER,
        )
        _create_finalize_job(connection, session.id)

        result = _finalizer(
            connection,
            policy=ScreenFinalizePolicy(
                keyframe_diff_threshold=0.20,
                crop_diff_threshold=0.02,
            ),
        ).finalize_next_due_job(now=FINALIZED_AT)

        crop_jobs = _jobs(connection, job_type="crop_ocr")
        metadata = json.loads(crop_jobs[0]["metadata_json"])

        assert result is not None
        assert result.frame_ocr_jobs_created == 1
        assert result.crop_ocr_jobs_created == 1
        assert len(crop_jobs) == 1
        assert crop_jobs[0]["target_id"] == crop_frame.id
        assert crop_jobs[0]["frame_id"] == crop_frame.id
        assert metadata["source_frame_id"] == crop_frame.id
        assert metadata["compared_frame_id"] == "screen_frame_1"
        assert metadata["diff_score"] == 0.04
        assert json.loads(metadata["crop_bbox_json"]) == {
            "x": 4,
            "y": 4,
            "width": 2,
            "height": 2,
        }
    finally:
        connection.close()


def test_empty_sessions_finalize_without_creating_ocr_jobs(tmp_path):
    connection = _connection(tmp_path)
    try:
        screen_repository = ScreenRepository(connection)
        session = _closed_session(screen_repository)
        _create_finalize_job(connection, session.id)

        result = _finalizer(connection).finalize_next_due_job(now=FINALIZED_AT)
        stored_session = screen_repository.get_session(session.id)
        finalize_job = _jobs(connection, job_type="session_finalize")[0]

        assert result is not None
        assert result.total_frames == 0
        assert result.frame_ocr_jobs_created == 0
        assert result.crop_ocr_jobs_created == 0
        assert _count_ocr_jobs(connection) == 0
        assert stored_session.status == "finalized"
        assert finalize_job["status"] == "done"
    finally:
        connection.close()


def _connection(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    return connect(database_path)


def _closed_session(repository):
    session = repository.create_session(
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
    closed_session = repository.close_session(session.id, ended_at=LATER)
    assert closed_session is not None
    return closed_session


def _insert_frame(
    repository,
    tmp_path,
    *,
    session_id,
    frame_id,
    png_bytes,
    perceptual_hash=None,
    captured_at=NOW,
):
    image_path = Path(tmp_path) / f"{frame_id}.png"
    image_path.write_bytes(png_bytes)
    frame = repository.insert_frame(
        frame_id=frame_id,
        session_id=session_id,
        captured_at=captured_at,
        image_path=str(image_path),
        sha256=hashlib.sha256(png_bytes).hexdigest(),
        perceptual_hash=perceptual_hash,
        width=_png_dimensions(png_bytes)[0],
        height=_png_dimensions(png_bytes)[1],
    )
    assert frame is not None
    return frame


def _create_finalize_job(connection, session_id):
    OcrJobRepository(connection).create_pending_job(
        job_id="ocr_job_finalize",
        job_type="session_finalize",
        target_id=session_id,
        session_id=session_id,
        source_key="url:https://example.com/docs",
        retrieval_locator="url:https://example.com/docs",
        next_run_at=LATER,
        now=LATER,
    )


def _finalizer(connection, *, policy=None):
    return SessionFinalizer(
        screen_repository=ScreenRepository(connection),
        job_repository=OcrJobRepository(connection),
        policy=policy,
    )


def _jobs(connection, *, job_type):
    return connection.execute(
        """
        SELECT *
        FROM ocr_jobs
        WHERE type = ?
        ORDER BY created_at, id
        """,
        (job_type,),
    ).fetchall()


def _ocr_jobs(connection):
    return connection.execute(
        """
        SELECT *
        FROM ocr_jobs
        WHERE type IN ('frame_ocr', 'crop_ocr')
        ORDER BY priority DESC, created_at ASC, id ASC
        """
    ).fetchall()


def _count_ocr_jobs(connection):
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM ocr_jobs
        WHERE type IN ('frame_ocr', 'crop_ocr')
        """
    ).fetchone()
    return int(row[0])


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
