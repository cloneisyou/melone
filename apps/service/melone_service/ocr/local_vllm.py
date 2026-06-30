from __future__ import annotations

import base64
import json
import mimetypes
import socket
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .client import OcrRequest, OcrResult
from .errors import OcrError, OcrTimeoutError, OcrUnavailableError


LOCAL_VLLM_PROVIDER = "local_vllm"
_LOCAL_OPENAI_COMPATIBLE_LABEL = "local OpenAI-compatible VLM"
LOCAL_VLLM_OCR_PROMPT = (
    "Extract all readable text from this image. Return only the text you can "
    "see, preserving line breaks when useful. Do not describe the image."
)
DEFAULT_LOCAL_VLLM_MAX_TOKENS = 4096

_CHAT_COMPLETIONS_PATH = "/chat/completions"
_DEFAULT_READINESS_PATH = "/models"
_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_TIMEOUT_ERRORS = (TimeoutError, socket.timeout)


UrlOpener = Callable[..., Any]


class LocalVllmOcrClient:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        timeout_seconds: float,
        readiness_path: str = _DEFAULT_READINESS_PATH,
        opener: UrlOpener | None = None,
        max_tokens: int = DEFAULT_LOCAL_VLLM_MAX_TOKENS,
        provider: str = LOCAL_VLLM_PROVIDER,
    ) -> None:
        if not endpoint.strip():
            raise ValueError("endpoint must not be empty")
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be greater than zero")
        if not provider.strip():
            raise ValueError("provider must not be empty")

        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.readiness_path = readiness_path
        self.max_tokens = max_tokens
        self.provider = provider
        self._opener = urlopen if opener is None else opener

    def extract_text(self, request: OcrRequest) -> OcrResult:
        started = time.monotonic()
        payload = self._ocr_payload(image_path_to_data_url(request.image_path))
        response_json = self._request_json(
            "POST",
            _CHAT_COMPLETIONS_PATH,
            payload=payload,
            request_id=request.request_id,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        return OcrResult(
            text=_parse_chat_completion_text(response_json),
            provider=self.provider,
            model=self.model,
            latency_ms=latency_ms,
        )

    def check_readiness(self) -> None:
        self._request("GET", self.readiness_path, payload=None, request_id=None)

    def is_ready(self) -> bool:
        try:
            self.check_readiness()
        except OcrError:
            return False
        return True

    def _ocr_payload(self, data_url: str) -> dict[str, object]:
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": LOCAL_VLLM_OCR_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "stream": False,
        }

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, object] | None,
        request_id: str | None,
    ) -> object:
        body = self._request(method, path, payload=payload, request_id=request_id)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OcrError(
                f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} returned invalid JSON"
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, object] | None,
        request_id: str | None,
    ) -> bytes:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if request_id:
            headers["X-Request-ID"] = request_id

        http_request = Request(
            self._url(path),
            data=data,
            headers=headers,
            method=method,
        )

        try:
            response = self._opener(http_request, timeout=self.timeout_seconds)
            try:
                _raise_for_status(_response_status(response))
                response_body = response.read()
                return (
                    response_body
                    if isinstance(response_body, bytes)
                    else str(response_body).encode("utf-8")
                )
            finally:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
        except HTTPError as exc:
            _raise_for_status(exc.code)
            raise OcrError(
                f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} request failed with HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, _TIMEOUT_ERRORS):
                raise OcrTimeoutError(
                    f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} OCR request timed out"
                ) from exc
            raise OcrUnavailableError(
                f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} is unavailable: {reason}"
            ) from exc
        except _TIMEOUT_ERRORS as exc:
            raise OcrTimeoutError(
                f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} OCR request timed out"
            ) from exc
        except ConnectionError as exc:
            raise OcrUnavailableError(
                f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} is unavailable: {exc}"
            ) from exc
        except OcrError:
            raise

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.endpoint}/{path.lstrip('/')}"


def image_path_to_data_url(image_path: Path) -> str:
    mime_type = _mime_type_for_path(image_path)
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _mime_type_for_path(image_path: Path) -> str:
    suffix = image_path.suffix.lower()
    if suffix in _IMAGE_MIME_TYPES:
        return _IMAGE_MIME_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(str(image_path))
    return guessed or "application/octet-stream"


def _response_status(response: object) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        if getcode is not None:
            status = getcode()
    return int(status or 200)


def _raise_for_status(status: int) -> None:
    if status >= 500:
        raise OcrUnavailableError(
            f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} returned HTTP {status}"
        )
    if status >= 400:
        raise OcrError(
            f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} request failed with HTTP {status}"
        )


def _parse_chat_completion_text(response_json: object) -> str:
    if not isinstance(response_json, Mapping):
        raise OcrError(
            f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} response must be a JSON object"
        )

    choices = response_json.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)):
        raise OcrError(f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} response missing choices")
    if not choices:
        raise OcrError(
            f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} response contained no choices"
        )

    first_choice = choices[0]
    if not isinstance(first_choice, Mapping):
        raise OcrError(
            f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} choice must be a JSON object"
        )

    message = first_choice.get("message")
    if isinstance(message, Mapping):
        return _text_from_content(message.get("content"))

    return _text_from_content(first_choice.get("text"))


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        if parts:
            return "\n".join(parts)
    raise OcrError(f"{_LOCAL_OPENAI_COMPATIBLE_LABEL} response missing text content")
