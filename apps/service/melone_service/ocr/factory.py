from __future__ import annotations

from typing import Any

from melone_service.config import ServiceConfig

from .client import OcrClient
from .local_vllm import LocalVllmOcrClient
from .apple_vision import APPLE_VISION_PROVIDER, AppleVisionOcrClient
from .mock import MockOcrClient


_APPLE_VISION_PROVIDERS = {
    APPLE_VISION_PROVIDER,
    "macos_vision",
    "macos-vision",
    "apple-vision",
    "vision",
}
_APPLE_VISION_KWARGS = {
    "apple_vision_backend",
    "apple_vision_platform_name",
    "apple_vision_recognition_languages",
    "apple_vision_uses_language_correction",
}
_LOCAL_OPENAI_COMPATIBLE_PROVIDERS = {
    "local_vllm",
    "local-vllm",
    "vllm",
    "local_mlx",
    "local-mlx",
    "mlx",
    "mlx_vlm",
    "mlx-vlm",
    "local_openai",
    "local-openai",
    "openai_compatible",
    "openai-compatible",
}


def create_ocr_client(
    config: ServiceConfig,
    **provider_kwargs: Any,
) -> OcrClient:
    provider = config.ocr_provider.strip().lower()
    if provider == "mock":
        return MockOcrClient()
    if provider in _APPLE_VISION_PROVIDERS:
        # Fall back to config so every caller (not just those passing kwargs)
        # gets the configured languages/correction. An empty tuple ("auto")
        # flows through to the backend as auto-detect.
        recognition_languages = provider_kwargs.get(
            "apple_vision_recognition_languages"
        )
        if recognition_languages is None:
            recognition_languages = config.ocr_recognition_languages
        uses_language_correction = provider_kwargs.get(
            "apple_vision_uses_language_correction"
        )
        if uses_language_correction is None:
            uses_language_correction = config.ocr_language_correction
        return AppleVisionOcrClient(
            backend=provider_kwargs.get("apple_vision_backend"),
            platform_name=provider_kwargs.get("apple_vision_platform_name"),
            recognition_languages=recognition_languages,
            uses_language_correction=uses_language_correction,
        )
    if provider in _LOCAL_OPENAI_COMPATIBLE_PROVIDERS:
        local_vllm_kwargs = {
            key: value
            for key, value in provider_kwargs.items()
            if key not in _APPLE_VISION_KWARGS
        }
        local_vllm_kwargs.setdefault("provider", provider)
        local_vllm_kwargs.setdefault("max_tokens", config.ocr_max_tokens)
        return LocalVllmOcrClient(
            endpoint=config.ocr_endpoint,
            model=config.ocr_model,
            timeout_seconds=config.ocr_timeout_seconds,
            **local_vllm_kwargs,
        )
    raise ValueError(f"unsupported OCR provider: {config.ocr_provider}")
