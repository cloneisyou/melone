import binascii
import hashlib
import struct
import zlib
from pathlib import Path

import pytest

from melone_service.config import load_config
from melone_service.store.db import connect, initialize_database
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository
from melone_service.store.ocr_jobs import OcrJobRepository
from melone_service.ocr import (
    APPLE_VISION_MODEL,
    APPLE_VISION_PROVIDER,
    AppleVisionOcrClient,
    OcrRequest,
    OcrUnavailableError,
    OcrWorker,
    create_ocr_client,
)
from melone_service.ocr.apple_vision import AppleVisionTextBackend


NOW = "2026-06-09T06:00:00.000Z"
LATER = "2026-06-09T06:01:00.000Z"


def test_factory_uses_injected_apple_vision_backend_for_png_fixture(tmp_path):
    image_path = tmp_path / "fixture.png"
    image_path.write_bytes(_png_bytes(3, 2))
    backend = FakeVisionBackend(["Screen Text", "Settings panel"])
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path / "home"),
            "MELONE_OCR_PROVIDER": "apple_vision",
        }
    )

    client = create_ocr_client(config, apple_vision_backend=backend)
    result = client.extract_text(OcrRequest(image_path=image_path))

    assert isinstance(client, AppleVisionOcrClient)
    assert backend.image_paths == [image_path]
    assert result.text == "Screen Text\nSettings panel"
    assert result.provider == APPLE_VISION_PROVIDER
    assert result.model == APPLE_VISION_MODEL
    assert isinstance(result.latency_ms, int)


def test_factory_passes_configured_recognition_languages(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path / "home"),
            "MELONE_OCR_PROVIDER": "apple_vision",
            "MELONE_OCR_LANGUAGES": "ko-KR,en-US",
        }
    )

    # No injected backend: factory builds the real text backend so we can assert
    # the configured languages reach Apple Vision.
    client = create_ocr_client(config)

    assert isinstance(client, AppleVisionOcrClient)
    assert client.backend.recognition_languages == ("ko-KR", "en-US")


def test_factory_defaults_recognition_languages_to_korean(tmp_path):
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path / "home"),
            "MELONE_OCR_PROVIDER": "apple_vision",
        }
    )

    client = create_ocr_client(config)

    assert client.backend.recognition_languages == ("ko-KR", "en-US")


def test_apple_vision_disables_language_correction_by_default():
    # Language correction corrupts on-screen code/identifiers/paths, so it is off
    # unless explicitly enabled (matches screenpipe's native OCR usage).
    assert AppleVisionTextBackend().uses_language_correction is False
    assert AppleVisionTextBackend(uses_language_correction=True).uses_language_correction is True

    client = AppleVisionOcrClient(uses_language_correction=True)
    assert client.backend.uses_language_correction is True


def test_factory_defaults_language_correction_off_and_reads_env(tmp_path):
    off = create_ocr_client(
        load_config(
            env={
                "MELONE_HOME": str(tmp_path / "off"),
                "MELONE_OCR_PROVIDER": "apple_vision",
            }
        )
    )
    assert off.backend.uses_language_correction is False

    on = create_ocr_client(
        load_config(
            env={
                "MELONE_HOME": str(tmp_path / "on"),
                "MELONE_OCR_PROVIDER": "apple_vision",
                "MELONE_OCR_LANGUAGE_CORRECTION": "true",
            }
        )
    )
    assert on.backend.uses_language_correction is True


def test_factory_apple_vision_reports_unavailable_off_macos(tmp_path):
    image_path = tmp_path / "fixture.png"
    image_path.write_bytes(_png_bytes(1, 1))
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path / "home"),
            "MELONE_OCR_PROVIDER": "apple_vision",
        }
    )
    client = create_ocr_client(config, apple_vision_platform_name="linux")

    with pytest.raises(OcrUnavailableError) as exc_info:
        client.extract_text(OcrRequest(image_path=image_path))

    assert "only available on macOS" in str(exc_info.value)


def test_worker_uses_apple_vision_provider_without_local_vlm_server(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(connection, tmp_path)
        _create_ocr_job(connection, frame_id=frame.id)
        config = load_config(
            env={
                "MELONE_HOME": str(tmp_path / "home"),
                "MELONE_OCR_PROVIDER": "apple_vision",
            }
        )
        client = create_ocr_client(
            config,
            apple_vision_backend=FakeVisionBackend(["Visible", "screen text"]),
        )

        result = OcrWorker(
            client=client,
            job_repository=OcrJobRepository(connection),
            screen_repository=ScreenRepository(connection),
            ocr_repository=OcrChunkRepository(connection),
        ).process_next_due_job(now=LATER)

        chunk = OcrChunkRepository(connection).list_chunks()[0]
        assert result.status == "done"
        assert chunk.text == "Visible screen text"
        assert chunk.provider == APPLE_VISION_PROVIDER
        assert chunk.model == APPLE_VISION_MODEL
    finally:
        connection.close()


class FakeVisionBackend:
    def __init__(self, text):
        self.text = text
        self.image_paths = []

    def extract_text(self, image_path: Path):
        self.image_paths.append(image_path)
        return self.text


def _connection(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    return connect(database_path)


def _session_and_frame(connection, tmp_path):
    repository = ScreenRepository(connection)
    repository.create_session(
        session_id="screen_session_1",
        source_key="url:https://example.com/docs",
        retrieval_locator="url:https://example.com/docs",
        app_name="Safari",
        bundle_id="com.apple.Safari",
        window_title="Docs",
        url="https://example.com/docs",
        started_at=NOW,
        now=NOW,
    )
    png_bytes = _png_bytes(4, 4)
    image_path = tmp_path / "screen_frame_1.png"
    image_path.write_bytes(png_bytes)
    frame = repository.insert_frame(
        frame_id="screen_frame_1",
        session_id="screen_session_1",
        captured_at=NOW,
        image_path=str(image_path),
        sha256=hashlib.sha256(png_bytes).hexdigest(),
        width=4,
        height=4,
    )
    assert frame is not None
    return frame


def _create_ocr_job(connection, *, frame_id):
    return OcrJobRepository(connection).create_pending_job(
        job_id="ocr_job_ocr",
        job_type="frame_ocr",
        target_id=frame_id,
        session_id="screen_session_1",
        frame_id=frame_id,
        source_key="url:https://example.com/docs",
        retrieval_locator="url:https://example.com/docs",
        next_run_at=NOW,
        now=NOW,
    )


def _png_bytes(width, height):
    row = b"\x00" + (b"\x00\x00\x00\x00" * width)
    image_data = row * height
    chunks = [
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(image_data)),
        _png_chunk(b"IEND", b""),
    ]
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _png_chunk(kind, data):
    crc = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)
