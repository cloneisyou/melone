from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from melone_service.collectors.active_window import ActiveWindowSnapshot

from ..model import Asset, AssetPermissionError

# 범용 local_file resolver. 기본적으로 AX기반.

_AX_SUCCESS = 0
_AX_API_DISABLED = -25211  # Accessibility 권한 미허용 => 명시적 실패
_AX_ATTRIBUTE_UNSUPPORTED = -25205  # 이 앱은 문서 개념이 없음 => None
_AX_NO_VALUE = -25212  # 저장 안 된/문서 없는 창 => None

_IMPORT_FAILED = -1


@dataclass(frozen=True)
class AXDocumentResult:
    error: int
    value: str | None 

AXDocumentReader = Callable[[int], AXDocumentResult]


class DocumentURIResolver:
    source = "ax_document"

    def __init__(
        self,
        *,
        ax_document_reader: AXDocumentReader | None = None,
        exclude_bundle_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._read = ax_document_reader or _read_ax_document
        self._exclude = exclude_bundle_ids

    def handles(self, snapshot: ActiveWindowSnapshot) -> bool:
        if snapshot.pid is None:
            return False
        return snapshot.bundle_id not in self._exclude

    def resolve(self, snapshot: ActiveWindowSnapshot) -> Asset | None:
        assert snapshot.pid is not None
        result = self._read(snapshot.pid)

        if result.error == _AX_API_DISABLED:
            raise AssetPermissionError(
                "accessibility",
                source=self.source,
                bundle_id=snapshot.bundle_id,
                detail="AXIsProcessTrusted is false",
            )
        if result.error != _AX_SUCCESS or not result.value:
            return None

        uri = _as_file_uri(result.value)
        if uri is None:
            return None
        return Asset(kind="local_file", uri=uri, source=self.source)


def _as_file_uri(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if text.startswith("file://"):
        return text
    if text.startswith("/"):
        from pathlib import Path

        return Path(text).as_uri()
    return None


def _read_ax_document(pid: int) -> AXDocumentResult:
    # AXUIElementCreateApplication(pid) -> focused/main window -> AXDocument.
    try:
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
        )
    except ImportError:
        return AXDocumentResult(_IMPORT_FAILED, None)

    app = AXUIElementCreateApplication(pid)
    err, window = AXUIElementCopyAttributeValue(app, "AXFocusedWindow", None)
    if err == _AX_API_DISABLED:
        return AXDocumentResult(err, None)
    if err or window is None:
        err, window = AXUIElementCopyAttributeValue(app, "AXMainWindow", None)
    if err or window is None:
        return AXDocumentResult(err or _AX_NO_VALUE, None)

    err, doc = AXUIElementCopyAttributeValue(window, "AXDocument", None)
    return AXDocumentResult(err, None if doc is None else str(doc))
