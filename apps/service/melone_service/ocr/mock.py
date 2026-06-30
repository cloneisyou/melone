from __future__ import annotations

from collections.abc import Mapping

from .client import OcrRequest, OcrResult


class MockOcrClient:
    def __init__(
        self,
        *,
        default_text: str = "mock OCR text",
        text_by_image_name: Mapping[str, str] | None = None,
    ) -> None:
        self.default_text = default_text
        self.text_by_image_name = dict(text_by_image_name or {})

    def extract_text(self, request: OcrRequest) -> OcrResult:
        text = self.text_by_image_name.get(
            request.image_path.name,
            self.default_text,
        )
        return OcrResult(
            text=text,
            provider="mock",
            model="mock-ocr",
            latency_ms=0,
        )
