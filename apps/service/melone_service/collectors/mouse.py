from __future__ import annotations

import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from melone_service.models import NormalizedEvent
from melone_service.pipeline.normalizer import normalize_event


MOUSE_ACTIVITY = "mouse_activity"

DEFAULT_ACTIVITY_WINDOW_SECONDS = 2.0
DEFAULT_EVENT_TAP_START_TIMEOUT_SECONDS = 1.0

MouseSampleKind = Literal["move", "click", "scroll", "drag"]


class MouseCaptureUnavailable(RuntimeError):
    pass


class MouseEventSource(Protocol):
    def start(self) -> None:
        """Start collecting sanitized mouse samples."""

    def drain(self) -> list[MouseSample]:
        """Return samples collected since the previous drain."""


@dataclass(frozen=True)
class MousePosition:
    x: float
    y: float

    def metadata(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True)
class MouseSample:
    timestamp: float
    kind: MouseSampleKind
    position: MousePosition | None = None
    display_id: int | None = None


@dataclass(frozen=True)
class MouseActivity:
    start_time: float
    end_time: float
    window_seconds: float
    click_count: int
    scroll_count: int
    drag_count: int
    move_count: int
    last_position: MousePosition | None
    active_display_id: int | None

    @property
    def drag_active(self) -> bool:
        return self.drag_count > 0

    @property
    def move_density(self) -> float:
        return self.move_count / self.window_seconds


class MouseCollector:
    name = "mouse"

    def __init__(
        self,
        *,
        event_source: MouseEventSource | None = None,
        platform_name: str | None = None,
        activity_window_seconds: float = DEFAULT_ACTIVITY_WINDOW_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if activity_window_seconds <= 0:
            raise ValueError("activity_window_seconds must be greater than 0")

        self.platform_name = sys.platform if platform_name is None else platform_name
        self.event_source = event_source or MacOSMouseEventSource(
            platform_name=self.platform_name
        )
        self.activity_window_seconds = activity_window_seconds
        self.monotonic = monotonic or time.monotonic
        self._started = False
        self._disabled = False
        self.disable_reason: str | None = None
        self._builder: _MouseActivityBuilder | None = None

    @property
    def is_disabled(self) -> bool:
        return self._disabled

    def poll(self) -> list[NormalizedEvent]:
        if self.platform_name != "darwin" or self._disabled:
            return []

        if not self._started:
            self._start_source()
        if self._disabled:
            return []

        try:
            samples = self.event_source.drain()
        except Exception as exc:  # pragma: no cover - defensive collector boundary
            self._disable(str(exc))
            return []

        activities = self._aggregate_for_poll(samples, now=self.monotonic())
        return [_mouse_activity_event(activity) for activity in activities]

    def _aggregate_for_poll(
        self,
        samples: Sequence[MouseSample],
        *,
        now: float,
    ) -> list[MouseActivity]:
        activities: list[MouseActivity] = []
        for sample in sorted(samples, key=lambda item: item.timestamp):
            if self._builder is None:
                self._builder = _MouseActivityBuilder(
                    sample.timestamp,
                    self.activity_window_seconds,
                )

            if (
                sample.timestamp - self._builder.start_time
                >= self.activity_window_seconds
            ):
                activities.append(self._builder.build())
                self._builder = _MouseActivityBuilder(
                    sample.timestamp,
                    self.activity_window_seconds,
                )

            self._builder.add(sample)

        if (
            self._builder is not None
            and now - self._builder.start_time >= self.activity_window_seconds
        ):
            activities.append(self._builder.build())
            self._builder = None

        return activities

    def _start_source(self) -> None:
        try:
            self.event_source.start()
        except MouseCaptureUnavailable as exc:
            self._disable(str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive collector boundary
            self._disable(str(exc))
            return

        self._started = True

    def _disable(self, reason: str | None = None) -> None:
        self._disabled = True
        self.disable_reason = reason


class MacOSMouseEventSource:
    def __init__(
        self,
        *,
        platform_name: str | None = None,
        monotonic: Callable[[], float] | None = None,
        display_detector: Callable[[MousePosition], int | None] | None = None,
        start_timeout_seconds: float = DEFAULT_EVENT_TAP_START_TIMEOUT_SECONDS,
    ) -> None:
        self.platform_name = sys.platform if platform_name is None else platform_name
        self.monotonic = monotonic or time.monotonic
        self.display_detector = display_detector or MacOSDisplayDetector(
            platform_name=self.platform_name
        ).display_id_for_position
        self.start_timeout_seconds = start_timeout_seconds
        self._samples: queue.Queue[MouseSample] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._setup_error: MouseCaptureUnavailable | None = None
        self._callback: Callable[..., Any] | None = None
        self._started = False

    def start(self) -> None:
        if self.platform_name != "darwin":
            raise MouseCaptureUnavailable("mouse collector is macOS only")
        if self._started:
            return

        self._ready.clear()
        self._setup_error = None
        self._thread = threading.Thread(
            target=self._run_event_tap,
            name="melone-mouse-event-tap",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(self.start_timeout_seconds):
            raise MouseCaptureUnavailable("mouse event tap did not start")
        if self._setup_error is not None:
            raise self._setup_error

        self._started = True

    def drain(self) -> list[MouseSample]:
        samples: list[MouseSample] = []
        while True:
            try:
                samples.append(self._samples.get_nowait())
            except queue.Empty:
                return samples

    def _run_event_tap(self) -> None:
        try:
            from Quartz import (
                CFRunLoopAddSource,
                CFRunLoopGetCurrent,
                CFRunLoopRun,
                CFMachPortCreateRunLoopSource,
                CGEventGetLocation,
                CGEventMaskBit,
                CGEventTapCreate,
                CGEventTapEnable,
                kCFRunLoopCommonModes,
                kCGEventLeftMouseDown,
                kCGEventLeftMouseDragged,
                kCGEventMouseMoved,
                kCGEventOtherMouseDown,
                kCGEventOtherMouseDragged,
                kCGEventRightMouseDown,
                kCGEventRightMouseDragged,
                kCGEventScrollWheel,
                kCGEventTapOptionListenOnly,
                kCGHeadInsertEventTap,
                kCGSessionEventTap,
            )
        except ImportError as exc:
            self._setup_error = MouseCaptureUnavailable(
                "mouse collector requires PyObjC Quartz on macOS"
            )
            self._ready.set()
            return

        click_events = {
            kCGEventLeftMouseDown,
            kCGEventRightMouseDown,
            kCGEventOtherMouseDown,
        }
        drag_events = {
            kCGEventLeftMouseDragged,
            kCGEventRightMouseDragged,
            kCGEventOtherMouseDragged,
        }

        def callback(
            _proxy: object,
            event_type: int,
            event: object,
            _refcon: object,
        ) -> object:
            kind = _mouse_sample_kind(
                event_type,
                click_events=click_events,
                drag_events=drag_events,
                move_event=kCGEventMouseMoved,
                scroll_event=kCGEventScrollWheel,
            )
            if kind is None:
                return event

            position = _position_from_location(CGEventGetLocation(event))
            display_id = _detect_display_id(self.display_detector, position)
            self._samples.put(
                MouseSample(
                    timestamp=self.monotonic(),
                    kind=kind,
                    position=position,
                    display_id=display_id,
                )
            )
            return event

        self._callback = callback
        event_mask = _event_mask(
            [
                kCGEventMouseMoved,
                kCGEventLeftMouseDown,
                kCGEventRightMouseDown,
                kCGEventOtherMouseDown,
                kCGEventScrollWheel,
                kCGEventLeftMouseDragged,
                kCGEventRightMouseDragged,
                kCGEventOtherMouseDragged,
            ],
            mask_bit=CGEventMaskBit,
        )
        event_tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            event_mask,
            self._callback,
            None,
        )
        if event_tap is None:
            self._setup_error = MouseCaptureUnavailable(
                "mouse event tap could not start; Accessibility permission may be missing"
            )
            self._ready.set()
            return

        run_loop_source = CFMachPortCreateRunLoopSource(None, event_tap, 0)
        CFRunLoopAddSource(
            CFRunLoopGetCurrent(),
            run_loop_source,
            kCFRunLoopCommonModes,
        )
        CGEventTapEnable(event_tap, True)
        self._ready.set()
        CFRunLoopRun()


class MacOSDisplayDetector:
    def __init__(self, *, platform_name: str | None = None) -> None:
        self.platform_name = sys.platform if platform_name is None else platform_name

    def display_id_for_position(self, position: MousePosition) -> int | None:
        if self.platform_name != "darwin":
            return None

        try:
            from Quartz import CGGetDisplaysWithPoint, CGPointMake

            error, display_ids, display_count = CGGetDisplaysWithPoint(
                CGPointMake(position.x, position.y),
                1,
                None,
                None,
            )
        except Exception:
            return None

        if error != 0 or display_count < 1 or not display_ids:
            return None
        return int(display_ids[0])


def aggregate_mouse_samples(
    samples: Sequence[MouseSample],
    *,
    window_seconds: float = DEFAULT_ACTIVITY_WINDOW_SECONDS,
) -> list[MouseActivity]:
    if window_seconds <= 0:
        raise ValueError("window_seconds must be greater than 0")

    activities: list[MouseActivity] = []
    current: _MouseActivityBuilder | None = None
    for sample in sorted(samples, key=lambda item: item.timestamp):
        if (
            current is None
            or sample.timestamp - current.start_time >= window_seconds
        ):
            if current is not None:
                activities.append(current.build())
            current = _MouseActivityBuilder(sample.timestamp, window_seconds)

        current.add(sample)

    if current is not None:
        activities.append(current.build())

    return activities


def _mouse_activity_event(activity: MouseActivity) -> NormalizedEvent:
    metadata: dict[str, object] = {
        "click_count": activity.click_count,
        "scroll_count": activity.scroll_count,
        "drag_count": activity.drag_count,
        "drag_active": activity.drag_active,
        "move_count": activity.move_count,
        "move_density": round(activity.move_density, 3),
        "duration_ms": int(round((activity.end_time - activity.start_time) * 1000)),
    }
    if activity.last_position is not None:
        metadata["last_position"] = activity.last_position.metadata()
    if activity.active_display_id is not None:
        metadata["active_display_id"] = activity.active_display_id

    return normalize_event(
        MOUSE_ACTIVITY,
        window={"display_id": activity.active_display_id},
        source="mouse",
        metadata=metadata,
    )


@dataclass
class _MouseActivityBuilder:
    start_time: float
    window_seconds: float
    end_time: float | None = None
    click_count: int = 0
    scroll_count: int = 0
    drag_count: int = 0
    move_count: int = 0
    last_position: MousePosition | None = None
    active_display_id: int | None = None

    def add(self, sample: MouseSample) -> None:
        self.end_time = sample.timestamp

        if sample.kind == "click":
            self.click_count += 1
        elif sample.kind == "scroll":
            self.scroll_count += 1
        elif sample.kind == "drag":
            self.drag_count += 1

        if sample.kind in {"move", "drag"}:
            self.move_count += 1

        if sample.position is not None:
            self.last_position = sample.position
        if sample.display_id is not None:
            self.active_display_id = sample.display_id

    def build(self) -> MouseActivity:
        return MouseActivity(
            start_time=self.start_time,
            end_time=self.start_time if self.end_time is None else self.end_time,
            window_seconds=self.window_seconds,
            click_count=self.click_count,
            scroll_count=self.scroll_count,
            drag_count=self.drag_count,
            move_count=self.move_count,
            last_position=self.last_position,
            active_display_id=self.active_display_id,
        )


def _mouse_sample_kind(
    event_type: int,
    *,
    click_events: set[int],
    drag_events: set[int],
    move_event: int,
    scroll_event: int,
) -> MouseSampleKind | None:
    if event_type in click_events:
        return "click"
    if event_type == scroll_event:
        return "scroll"
    if event_type in drag_events:
        return "drag"
    if event_type == move_event:
        return "move"
    return None


def _event_mask(
    event_types: Sequence[int],
    *,
    mask_bit: Callable[[int], int],
) -> int:
    mask = 0
    for event_type in event_types:
        mask |= int(mask_bit(event_type))
    return mask


def _position_from_location(location: Any) -> MousePosition:
    return MousePosition(x=float(location.x), y=float(location.y))


def _detect_display_id(
    detector: Callable[[MousePosition], int | None],
    position: MousePosition,
) -> int | None:
    try:
        return detector(position)
    except Exception:
        return None
