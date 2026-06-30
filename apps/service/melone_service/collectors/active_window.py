from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from melone_service.models import NormalizedEvent
from melone_service.pipeline.normalizer import normalize_event


ACTIVE_APP_SNAPSHOT = "active_app_snapshot"
ACTIVE_APP_CHANGED = "active_app_changed"
WINDOW_TITLE_CHANGED = "window_title_changed"

WINDOW_OWNER_PID_KEY = "kCGWindowOwnerPID"
WINDOW_OWNER_NAME_KEY = "kCGWindowOwnerName"
WINDOW_LAYER_KEY = "kCGWindowLayer"
WINDOW_NAME_KEY = "kCGWindowName"
WINDOW_NUMBER_KEY = "kCGWindowNumber"


class ActiveWindowAPI(Protocol):
    # PyObjC를 직접 쓰는 부분을 감싸 collector 로직은 mock으로 테스트합니다.
    def get_snapshot(self) -> ActiveWindowSnapshot | None:
        """Return the current foreground app and window title."""


@dataclass(frozen=True)
class ActiveWindowSnapshot:
    app_name: str | None
    bundle_id: str | None
    pid: int | None
    window_title: str | None = None
    window_number: int | None = None
    window_owner_name: str | None = None

    @property
    def app_identity(self) -> tuple[str | None, int | None, str | None]:
        return (self.bundle_id, self.pid, self.app_name)

    def app_context(self) -> dict[str, object | None]:
        return {
            "name": self.app_name,
            "bundle_id": self.bundle_id,
            "pid": self.pid,
        }

    def window_context(self) -> dict[str, object | None]:
        return {"title": self.window_title}

    def metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if self.window_number is not None:
            metadata["window_number"] = self.window_number
        if self.window_owner_name:
            metadata["window_owner_name"] = self.window_owner_name
        return metadata


class ActiveWindowCollector:
    name = "active_window"

    def __init__(
        self,
        *,
        api: ActiveWindowAPI | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.platform_name = sys.platform if platform_name is None else platform_name
        self.api = api or MacOSActiveWindowAPI(platform_name=self.platform_name)
        self._last_snapshot: ActiveWindowSnapshot | None = None

    def poll(self) -> list[NormalizedEvent]:
        if self.platform_name != "darwin":
            return []

        snapshot = self.api.get_snapshot()
        if snapshot is None:
            return []

        events = self._build_events(snapshot)
        self._last_snapshot = snapshot
        return events

    def _build_events(
        self,
        snapshot: ActiveWindowSnapshot,
    ) -> list[NormalizedEvent]:
        previous = self._last_snapshot
        if previous is None:
            return [_snapshot_event(snapshot, reason="initial")]

        app_changed = snapshot.app_identity != previous.app_identity
        title_changed = snapshot.window_title != previous.window_title
        if not app_changed and not title_changed:
            return []

        events = [
            _snapshot_event(
                snapshot,
                reason="changed",
                previous=previous,
                app_changed=app_changed,
                title_changed=title_changed,
            )
        ]
        if app_changed:
            events.append(_active_app_changed_event(snapshot, previous))
        if title_changed:
            events.append(_window_title_changed_event(snapshot, previous))
        return events


class MacOSActiveWindowAPI:
    def __init__(
        self,
        *,
        platform_name: str | None = None,
        running_application_resolver: Callable[[int], Any | None] | None = None,
        window_info_reader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.platform_name = sys.platform if platform_name is None else platform_name
        self.running_application_resolver = (
            running_application_resolver or _running_application_for_pid
        )
        self.window_info_reader = window_info_reader or _read_window_info

    def get_snapshot(self) -> ActiveWindowSnapshot | None:
        if self.platform_name != "darwin":
            return None

        windows = self.window_info_reader()
        frontmost_window = _frontmost_layer_zero_window(windows)
        if frontmost_window is None:
            return None

        pid = _optional_int(_window_value(frontmost_window, WINDOW_OWNER_PID_KEY))
        if pid is None:
            return None

        app = self.running_application_resolver(pid)
        if app is None:
            return None

        window = _frontmost_window_for_pid(windows, pid)

        return ActiveWindowSnapshot(
            app_name=_optional_string(_call(app, "localizedName")),
            bundle_id=_optional_string(_call(app, "bundleIdentifier")),
            pid=pid,
            window_title=_optional_string(_window_value(window, WINDOW_NAME_KEY)),
            window_number=_optional_int(_window_value(window, WINDOW_NUMBER_KEY)),
            window_owner_name=_optional_string(
                _window_value(window, WINDOW_OWNER_NAME_KEY)
            ),
        )


def _snapshot_event(
    snapshot: ActiveWindowSnapshot,
    *,
    reason: str,
    previous: ActiveWindowSnapshot | None = None,
    app_changed: bool = False,
    title_changed: bool = False,
) -> NormalizedEvent:
    metadata = snapshot.metadata()
    metadata["reason"] = reason
    metadata["app_changed"] = app_changed
    metadata["window_title_changed"] = title_changed
    if previous is not None:
        metadata["previous_app"] = previous.app_context()
        metadata["previous_window_title"] = previous.window_title

    return _event(ACTIVE_APP_SNAPSHOT, snapshot, metadata=metadata)


def _active_app_changed_event(
    snapshot: ActiveWindowSnapshot,
    previous: ActiveWindowSnapshot,
) -> NormalizedEvent:
    metadata = snapshot.metadata()
    metadata["previous_app"] = previous.app_context()
    metadata["previous_window_title"] = previous.window_title
    return _event(ACTIVE_APP_CHANGED, snapshot, metadata=metadata)


def _window_title_changed_event(
    snapshot: ActiveWindowSnapshot,
    previous: ActiveWindowSnapshot,
) -> NormalizedEvent:
    metadata = snapshot.metadata()
    metadata["previous_window_title"] = previous.window_title
    return _event(WINDOW_TITLE_CHANGED, snapshot, metadata=metadata)


def _event(
    event_type: str,
    snapshot: ActiveWindowSnapshot,
    *,
    metadata: Mapping[str, object],
) -> NormalizedEvent:
    return normalize_event(
        event_type,
        app=snapshot.app_context(),
        window=snapshot.window_context(),
        source="active_window",
        metadata=metadata,
    )


def _frontmost_window_for_pid(
    windows: Sequence[Mapping[str, Any]],
    pid: int | None,
) -> Mapping[str, Any] | None:
    if pid is None:
        return None

    owned_windows = [
        window
        for window in windows
        if _optional_int(_window_value(window, WINDOW_OWNER_PID_KEY)) == pid
        and _optional_int(_window_value(window, WINDOW_LAYER_KEY)) == 0
    ]
    if not owned_windows:
        return None

    titled_window = next(
        (
            window
            for window in owned_windows
            if _optional_string(_window_value(window, WINDOW_NAME_KEY))
        ),
        None,
    )
    return titled_window or owned_windows[0]


def _frontmost_layer_zero_window(
    windows: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    return next(
        (
            window
            for window in windows
            if _optional_int(_window_value(window, WINDOW_LAYER_KEY)) == 0
            and _optional_int(_window_value(window, WINDOW_OWNER_PID_KEY)) is not None
        ),
        None,
    )


def _pyobjc_missing_error(module_name: str, exc: ImportError) -> RuntimeError:
    return RuntimeError(
        f"active_window collector requires PyObjC ({module_name}) on macOS; "
        f"install it with 'pip install pyobjc-framework-Cocoa "
        f"pyobjc-framework-Quartz' "
        f"(original error: {exc})"
    )


def _running_application_for_pid(pid: int) -> Any | None:
    try:
        from AppKit import NSRunningApplication
    except ImportError as exc:
        raise _pyobjc_missing_error("AppKit", exc) from exc

    return NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)


def _read_window_info() -> Sequence[Mapping[str, Any]]:
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )
    except ImportError as exc:
        raise _pyobjc_missing_error("Quartz", exc) from exc

    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    return list(CGWindowListCopyWindowInfo(options, kCGNullWindowID) or [])


def _call(value: Any, method_name: str) -> Any:
    method = getattr(value, method_name, None)
    if method is None:
        return None
    if callable(method):
        return method()
    return method


def _window_value(window: Mapping[str, Any] | None, key: str) -> Any:
    if window is None:
        return None
    return window.get(key)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None
