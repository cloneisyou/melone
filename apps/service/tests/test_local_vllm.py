import base64
import hashlib
import json
import socket
from urllib.error import URLError

import pytest

from melone_service.config import load_config
from melone_service.store.db import connect, initialize_database
from melone_service.store.ocr import OcrChunkRepository
from melone_service.store.screen import ScreenRepository
from melone_service.store.ocr_jobs import OcrJobRepository
from melone_service.ocr import (
    LOCAL_VLLM_OCR_PROMPT,
    LOCAL_VLLM_PROVIDER,
    PROVIDER_UNAVAILABLE_ERROR_SYMBOL,
    LocalVllmOcrClient,
    OcrRequest,
    OcrTimeoutError,
    OcrUnavailableError,
    OcrWorker,
    create_ocr_client,
    image_path_to_data_url,
)


NOW = "2026-06-09T06:00:00.000Z"
LATER = "2026-06-09T06:01:00.000Z"


def test_image_path_to_data_url_uses_png_mime_and_base64(tmp_path):
    image_path = tmp_path / "frame.png"
    image_bytes = b"not a real png but still request bytes"
    image_path.write_bytes(image_bytes)

    data_url = image_path_to_data_url(image_path)

    assert data_url == (
        "data:image/png;base64,"
        + base64.b64encode(image_bytes).decode("ascii")
    )


def test_local_vllm_request_payload_shape_and_text_parsing(tmp_path):
    image_path = tmp_path / "frame.png"
    image_bytes = b"image bytes"
    image_path.write_bytes(image_bytes)
    opener = FakeOpener(
        FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "Detected text\nSecond line",
                        }
                    }
                ]
            }
        )
    )
    client = LocalVllmOcrClient(
        endpoint="http://127.0.0.1:8000/v1/",
        model="custom-model",
        timeout_seconds=12,
        opener=opener,
    )

    result = client.extract_text(
        OcrRequest(image_path=image_path, request_id="ocr_job_1")
    )

    request = opener.requests[0]
    payload = json.loads(request.data.decode("utf-8"))
    content = payload["messages"][0]["content"]
    image_parts = [part for part in content if part["type"] == "image_url"]

    assert request.full_url == "http://127.0.0.1:8000/v1/chat/completions"
    assert request.get_method() == "POST"
    assert opener.timeouts == [12]
    assert payload["model"] == "custom-model"
    assert payload["messages"][0]["role"] == "user"
    assert content[0] == {"type": "text", "text": LOCAL_VLLM_OCR_PROMPT}
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"] == (
        "data:image/png;base64,"
        + base64.b64encode(image_bytes).decode("ascii")
    )
    assert result.text == "Detected text\nSecond line"
    assert result.provider == LOCAL_VLLM_PROVIDER
    assert result.model == "custom-model"
    assert isinstance(result.latency_ms, int)


def test_local_vllm_readiness_uses_models_endpoint():
    opener = FakeOpener(FakeResponse({"data": [{"id": "custom-model"}]}))
    client = LocalVllmOcrClient(
        endpoint="http://127.0.0.1:8000/v1",
        model="custom-model",
        timeout_seconds=3,
        opener=opener,
    )

    assert client.is_ready() is True
    assert opener.requests[0].full_url == "http://127.0.0.1:8000/v1/models"
    assert opener.requests[0].get_method() == "GET"
    assert opener.requests[0].data is None


def test_timeout_errors_become_ocr_timeout(tmp_path):
    image_path = tmp_path / "frame.png"
    image_path.write_bytes(b"image")
    client = LocalVllmOcrClient(
        endpoint="http://127.0.0.1:8000/v1",
        model="custom-model",
        timeout_seconds=3,
        opener=FakeOpener(socket.timeout("timed out")),
    )

    with pytest.raises(OcrTimeoutError):
        client.extract_text(OcrRequest(image_path=image_path))


def test_5xx_responses_become_unavailable(tmp_path):
    image_path = tmp_path / "frame.png"
    image_path.write_bytes(b"image")
    client = LocalVllmOcrClient(
        endpoint="http://127.0.0.1:8000/v1",
        model="custom-model",
        timeout_seconds=3,
        opener=FakeOpener(FakeResponse({"error": "down"}, status=503)),
    )

    with pytest.raises(OcrUnavailableError):
        client.extract_text(OcrRequest(image_path=image_path))


def test_worker_uses_local_adapter_when_config_selects_it(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(connection, tmp_path)
        _create_ocr_job(connection, frame_id=frame.id)
        opener = FakeOpener(
            FakeResponse({"choices": [{"message": {"content": "Local OCR text"}}]})
        )
        config = load_config(
            env={
                "MELONE_HOME": str(tmp_path / "home"),
                "MELONE_OCR_PROVIDER": "local_vllm",
                "MELONE_OCR_ENDPOINT": "http://127.0.0.1:8000/v1",
                "MELONE_OCR_MODEL": "lightonai/LightOnOCR-2-1B",
                "MELONE_OCR_TIMEOUT_SECONDS": "5",
            }
        )
        client = create_ocr_client(config, opener=opener)

        result = OcrWorker(
            client=client,
            job_repository=OcrJobRepository(connection),
            screen_repository=ScreenRepository(connection),
            ocr_repository=OcrChunkRepository(connection),
        ).process_next_due_job(now=LATER)

        chunk = OcrChunkRepository(connection).list_chunks()[0]
        assert isinstance(client, LocalVllmOcrClient)
        assert result.status == "done"
        assert chunk.text == "Local OCR text"
        assert chunk.provider == LOCAL_VLLM_PROVIDER
        assert chunk.model == "lightonai/LightOnOCR-2-1B"
    finally:
        connection.close()


def test_factory_accepts_mlx_ocr_provider_alias(tmp_path):
    image_path = tmp_path / "frame.png"
    image_path.write_bytes(b"image bytes")
    opener = FakeOpener(
        FakeResponse({"choices": [{"message": {"content": "MLX OCR text"}}]})
    )
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path / "home"),
            "MELONE_OCR_PROVIDER": "mlx_vlm",
            "MELONE_OCR_ENDPOINT": "http://127.0.0.1:8080/v1",
            "MELONE_OCR_MODEL": "mlx-community/LightOnOCR-2-1B-bf16",
            "MELONE_OCR_TIMEOUT_SECONDS": "5",
            "MELONE_OCR_MAX_TOKENS": "2048",
        }
    )

    client = create_ocr_client(config, opener=opener)
    result = client.extract_text(OcrRequest(image_path=image_path))

    request = opener.requests[0]
    payload = json.loads(request.data.decode("utf-8"))

    assert isinstance(client, LocalVllmOcrClient)
    assert request.full_url == "http://127.0.0.1:8080/v1/chat/completions"
    assert payload["model"] == "mlx-community/LightOnOCR-2-1B-bf16"
    assert payload["max_tokens"] == 2048
    assert result.text == "MLX OCR text"
    assert result.provider == "mlx_vlm"
    assert result.model == "mlx-community/LightOnOCR-2-1B-bf16"


def test_factory_does_not_pass_apple_vision_kwargs_to_local_vlm(tmp_path):
    image_path = tmp_path / "frame.png"
    image_path.write_bytes(b"image bytes")
    opener = FakeOpener(
        FakeResponse({"choices": [{"message": {"content": "Local text"}}]})
    )
    config = load_config(
        env={
            "MELONE_HOME": str(tmp_path / "home"),
            "MELONE_OCR_PROVIDER": "local_vllm",
            "MELONE_OCR_ENDPOINT": "http://127.0.0.1:8000/v1",
            "MELONE_OCR_MODEL": "lightonai/LightOnOCR-2-1B",
        }
    )

    client = create_ocr_client(
        config,
        opener=opener,
        apple_vision_backend=object(),
    )
    result = client.extract_text(OcrRequest(image_path=image_path))

    assert isinstance(client, LocalVllmOcrClient)
    assert result.text == "Local text"


def test_down_local_server_retries_without_immediate_dead_letter(tmp_path):
    connection = _connection(tmp_path)
    try:
        frame = _session_and_frame(connection, tmp_path)
        _create_ocr_job(connection, frame_id=frame.id)
        client = LocalVllmOcrClient(
            endpoint="http://127.0.0.1:8000/v1",
            model="lightonai/LightOnOCR-2-1B",
            timeout_seconds=5,
            opener=FakeOpener(URLError(ConnectionRefusedError("refused"))),
        )

        result = OcrWorker(
            client=client,
            job_repository=OcrJobRepository(connection),
            screen_repository=ScreenRepository(connection),
            ocr_repository=OcrChunkRepository(connection),
        ).process_next_due_job(now=LATER)

        job = OcrJobRepository(connection).get_job("ocr_job_ocr")
        assert result.status == "retryable_failed"
        assert result.provider_unavailable is True
        assert result.error_symbol == PROVIDER_UNAVAILABLE_ERROR_SYMBOL
        assert job.status == "retryable_failed"
        assert job.attempts == 1
        assert "OcrUnavailableError" in job.last_error
        assert OcrChunkRepository(connection).count_chunks() == 0
    finally:
        connection.close()


class FakeResponse:
    def __init__(self, body, *, status=200):
        self.status = status
        self._body = (
            body
            if isinstance(body, bytes)
            else json.dumps(body).encode("utf-8")
        )
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, response_or_error):
        self.response_or_error = response_or_error
        self.requests = []
        self.timeouts = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if isinstance(self.response_or_error, BaseException):
            raise self.response_or_error
        return self.response_or_error


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
        app_name="Google Chrome",
        bundle_id="com.google.Chrome",
        window_title="Docs",
        url="https://example.com/docs",
        started_at=NOW,
        now=NOW,
    )
    png_bytes = b"\x89PNG\r\n\x1a\nfake"
    image_path = tmp_path / "frame.png"
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
