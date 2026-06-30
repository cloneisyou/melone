from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class OcrRequest:
    image_path: Path
    request_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    provider: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@runtime_checkable
class OcrClient(Protocol):
    def extract_text(self, request: OcrRequest) -> OcrResult:
        """Extract readable text from one prepared image."""
