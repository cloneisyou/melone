from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from melone_service.config import (
    DEFAULT_ACTIVITY_ACTIVE_WINDOW_SECONDS,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
)
from melone_service.models import NormalizedEvent, utc_now
from melone_service.pipeline.normalizer import normalize_event


ActivityState = Literal["active", "reading", "idle"]

ACTIVE: ActivityState = "active"
READING: ActivityState = "reading"
IDLE: ActivityState = "idle"

ACTIVITY_STATE_CHANGED = "activity_state_changed"

KEYBOARD_ACTIVITY_TYPES = frozenset({"keyboard_burst", "clipboard_shortcut"})
MOUSE_ACTIVITY_TYPES = frozenset({"mouse_activity"})
ACTIVITY_EVENT_TYPES = tuple(sorted(KEYBOARD_ACTIVITY_TYPES | MOUSE_ACTIVITY_TYPES))
ACTIVITY_STATES = frozenset({ACTIVE, READING, IDLE})


@dataclass(frozen=True)
class ActivityThresholds:
    active_window_seconds: int = DEFAULT_ACTIVITY_ACTIVE_WINDOW_SECONDS
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.active_window_seconds <= 0:
            raise ValueError("active_window_seconds must be greater than 0")
        if self.idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be greater than 0")


def classify_activity_state(
    events: Sequence[NormalizedEvent],
    *,
    thresholds: ActivityThresholds | None = None,
    now: datetime | None = None,
) -> ActivityState:
    thresholds = thresholds or ActivityThresholds()
    reference_time = utc_now() if now is None else _utc_datetime(now)
    latest_activity_at: datetime | None = None
    latest_active_at: datetime | None = None

    for event in events:
        if not _is_activity_event(event):
            continue

        timestamp = _parse_event_timestamp(event.timestamp)
        latest_activity_at = _latest(latest_activity_at, timestamp)
        if _is_active_event(event):
            latest_active_at = _latest(latest_active_at, timestamp)

    if latest_activity_at is None:
        return IDLE

    idle_after = timedelta(seconds=thresholds.idle_timeout_seconds)
    if reference_time - latest_activity_at >= idle_after:
        return IDLE

    active_after = timedelta(seconds=thresholds.active_window_seconds)
    if (
        latest_active_at is not None
        and reference_time - latest_active_at <= active_after
    ):
        return ACTIVE

    return READING


def activity_state_changed_event(
    state: ActivityState,
    *,
    previous_state: ActivityState | None = None,
    thresholds: ActivityThresholds | None = None,
    timestamp: datetime | None = None,
) -> NormalizedEvent:
    if state not in ACTIVITY_STATES:
        raise ValueError("state must be active, reading, or idle")

    metadata: dict[str, object] = {"state": state}
    if previous_state is not None:
        metadata["previous_state"] = previous_state
    if thresholds is not None:
        metadata["active_window_seconds"] = thresholds.active_window_seconds
        metadata["idle_timeout_seconds"] = thresholds.idle_timeout_seconds

    return normalize_event(
        ACTIVITY_STATE_CHANGED,
        source="activity",
        metadata=metadata,
        timestamp=timestamp,
    )


def activity_state_from_event(event: NormalizedEvent | None) -> ActivityState | None:
    if event is None or event.type != ACTIVITY_STATE_CHANGED:
        return None

    state = event.metadata.get("state")
    if state in ACTIVITY_STATES:
        return state  # type: ignore[return-value]
    return None


def _is_activity_event(event: NormalizedEvent) -> bool:
    if event.type in KEYBOARD_ACTIVITY_TYPES:
        return _keyboard_count(event) > 0
    if event.type in MOUSE_ACTIVITY_TYPES:
        return _mouse_activity_count(event) > 0
    return False


def _is_active_event(event: NormalizedEvent) -> bool:
    if event.type in KEYBOARD_ACTIVITY_TYPES:
        return _keyboard_count(event) > 0
    if event.type in MOUSE_ACTIVITY_TYPES:
        return _metadata_number(event, "click_count") > 0 or _metadata_number(
            event,
            "scroll_count",
        ) > 0
    return False


def _keyboard_count(event: NormalizedEvent) -> float:
    if event.type == "clipboard_shortcut":
        return 1
    return _metadata_number(event, "key_count")


def _mouse_activity_count(event: NormalizedEvent) -> float:
    return sum(
        _metadata_number(event, key)
        for key in ("click_count", "scroll_count", "drag_count", "move_count")
    )


def _metadata_number(event: NormalizedEvent, key: str) -> float:
    value = event.metadata.get(key, 0)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)

    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _parse_event_timestamp(value: str) -> datetime:
    return _utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _latest(current: datetime | None, candidate: datetime) -> datetime:
    if current is None or candidate > current:
        return candidate
    return current
