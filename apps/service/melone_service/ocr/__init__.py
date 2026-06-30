from .client import OcrClient, OcrRequest, OcrResult
from .errors import OcrError, OcrTimeoutError, OcrUnavailableError
from .factory import create_ocr_client
from .local_vllm import (
    LOCAL_VLLM_OCR_PROMPT,
    LOCAL_VLLM_PROVIDER,
    LocalVllmOcrClient,
    image_path_to_data_url,
)
from .apple_vision import (
    APPLE_VISION_MODEL,
    APPLE_VISION_PROVIDER,
    AppleVisionOcrClient,
    AppleVisionTextBackend,
)
from .mock import MockOcrClient
from .worker import (
    OcrJobProcessingResult,
    OcrJobValidationError,
    PROVIDER_UNAVAILABLE_ERROR_SYMBOL,
    OcrWorker,
    hash_ocr_text,
    normalize_ocr_text,
)

__all__ = [
    "MockOcrClient",
    "OcrJobProcessingResult",
    "OcrJobValidationError",
    "PROVIDER_UNAVAILABLE_ERROR_SYMBOL",
    "OcrClient",
    "OcrError",
    "OcrRequest",
    "OcrResult",
    "OcrWorker",
    "OcrTimeoutError",
    "OcrUnavailableError",
    "LOCAL_VLLM_OCR_PROMPT",
    "LOCAL_VLLM_PROVIDER",
    "APPLE_VISION_MODEL",
    "APPLE_VISION_PROVIDER",
    "LocalVllmOcrClient",
    "AppleVisionOcrClient",
    "AppleVisionTextBackend",
    "create_ocr_client",
    "hash_ocr_text",
    "image_path_to_data_url",
    "normalize_ocr_text",
]
