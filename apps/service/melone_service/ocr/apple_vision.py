from __future__ import annotations

import platform
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .client import OcrRequest, OcrResult
from .errors import OcrError, OcrUnavailableError


APPLE_VISION_PROVIDER = "apple_vision"
APPLE_VISION_MODEL = "apple_vision_text_recognition"


class AppleVisionBackend(Protocol):
    def extract_text(self, image_path: Path) -> str | Sequence[str]:
        """Return text detected in one local image file."""


class AppleVisionOcrClient:
    def __init__(
        self,
        *,
        backend: AppleVisionBackend | None = None,
        platform_name: str | None = None,
        recognition_languages: Sequence[str] | None = None,
        uses_language_correction: bool = False,
    ) -> None:
        self.backend = (
            backend
            if backend is not None
            else AppleVisionTextBackend(
                platform_name=platform_name,
                recognition_languages=recognition_languages,
                uses_language_correction=uses_language_correction,
            )
        )

    def extract_text(self, request: OcrRequest) -> OcrResult:
        started = time.monotonic()
        try:
            raw_text = self.backend.extract_text(request.image_path)
        except OcrError:
            raise
        except Exception as exc:
            raise OcrUnavailableError(f"Apple Vision OCR failed: {exc}") from exc

        return OcrResult(
            text=_coerce_text(raw_text),
            provider=APPLE_VISION_PROVIDER,
            model=APPLE_VISION_MODEL,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


class AppleVisionTextBackend:
    def __init__(
        self,
        *,
        platform_name: str | None = None,
        recognition_languages: Sequence[str] | None = None,
        uses_language_correction: bool = False,
    ) -> None:
        self.platform_name = (
            platform.system().casefold()
            if platform_name is None
            else platform_name.casefold()
        )
        self.recognition_languages = tuple(recognition_languages or ())
        # Off by default: language correction autocorrects toward dictionary
        # words, which corrupts the code, identifiers, paths, and URLs common on
        # screen. screenpipe disables it for the same reason.
        self.uses_language_correction = uses_language_correction

    def extract_text(self, image_path: Path) -> str:
        if self.platform_name != "darwin":
            raise OcrUnavailableError(
                "Apple Vision OCR is only available on macOS"
            )
        if not image_path.is_file():
            raise OcrError(f"OCR image path is not a file: {image_path}")

        api = _load_vision_api()
        request = api.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(api.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(self.uses_language_correction)
        if self.recognition_languages:
            request.setRecognitionLanguages_(list(self.recognition_languages))

        image_url = api.NSURL.fileURLWithPath_(str(image_path))
        handler = api.VNImageRequestHandler.alloc().initWithURL_options_(
            image_url,
            {},
        )
        _perform_requests(handler, [request])
        return "\n".join(_recognized_lines(request))


@dataclass(frozen=True)
class _VisionApi:
    NSURL: object
    VNImageRequestHandler: object
    VNRecognizeTextRequest: object
    VNRequestTextRecognitionLevelAccurate: int


def _load_vision_api() -> _VisionApi:
    try:
        from Foundation import NSURL
        from Vision import (
            VNImageRequestHandler,
            VNRecognizeTextRequest,
            VNRequestTextRecognitionLevelAccurate,
        )
    except ImportError as exc:
        raise OcrUnavailableError(
            "Apple Vision OCR requires PyObjC Vision support"
        ) from exc

    return _VisionApi(
        NSURL=NSURL,
        VNImageRequestHandler=VNImageRequestHandler,
        VNRecognizeTextRequest=VNRecognizeTextRequest,
        VNRequestTextRecognitionLevelAccurate=VNRequestTextRecognitionLevelAccurate,
    )


def _perform_requests(handler: object, requests: Sequence[object]) -> None:
    performed = handler.performRequests_error_(list(requests), None)
    error = None
    success = performed
    if isinstance(performed, tuple):
        success = performed[0] if performed else False
        error = performed[1] if len(performed) > 1 else None

    if not success:
        raise OcrUnavailableError(
            f"Apple Vision OCR failed: {_format_vision_error(error)}"
        )


def _recognized_lines(request: object) -> list[str]:
    lines: list[str] = []
    for observation in request.results() or ():
        candidates = observation.topCandidates_(1) or ()
        if not candidates:
            continue
        text = candidates[0].string()
        if text is not None and str(text).strip():
            lines.append(str(text).strip())
    return lines


def _coerce_text(value: str | Sequence[str]) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        parts: list[str] = []
        for part in value:
            if not isinstance(part, str):
                raise OcrError("Apple Vision OCR backend returned malformed text")
            if part.strip():
                parts.append(part.strip())
        return "\n".join(parts)
    raise OcrError("Apple Vision OCR backend returned malformed text")


def _format_vision_error(error: object | None) -> str:
    if error is None:
        return "unknown Vision error"
    localized_description = getattr(error, "localizedDescription", None)
    if callable(localized_description):
        description = localized_description()
        if description:
            return str(description)
    return str(error)
