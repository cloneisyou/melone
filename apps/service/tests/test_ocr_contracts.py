from pathlib import Path

from melone_service.ocr import MockOcrClient, OcrClient, OcrRequest


def test_mock_ocr_client_returns_deterministic_text():
    client = MockOcrClient(
        default_text="fallback screen text",
        text_by_image_name={"frame.png": "deterministic frame text"},
    )
    request = OcrRequest(
        image_path=Path("/tmp/screens/session-1/frame.png"),
        request_id="ocr_job_1",
    )

    first_result = client.extract_text(request)
    second_result = client.extract_text(request)

    assert isinstance(client, OcrClient)
    assert first_result == second_result
    assert first_result.text == "deterministic frame text"
    assert first_result.provider == "mock"
    assert first_result.model == "mock-ocr"
    assert first_result.latency_ms == 0
