from datetime import datetime, timedelta, timezone

import pytest

from melone_service.pipeline.activity import (
    ACTIVE,
    IDLE,
    READING,
    ActivityThresholds,
    classify_activity_state,
)
from melone_service.pipeline.normalizer import normalize_event


NOW = datetime(2026, 6, 9, 6, 0, 0, tzinfo=timezone.utc)
THRESHOLDS = ActivityThresholds(active_window_seconds=30, idle_timeout_seconds=300)


def test_classifies_keyboard_burst_as_active_within_active_window():
    state = classify_activity_state(
        [_event("keyboard_burst", seconds_ago=10, metadata={"key_count": 3})],
        thresholds=THRESHOLDS,
        now=NOW,
    )

    assert state == ACTIVE


def test_classifies_click_or_scroll_as_active_within_active_window():
    click_state = classify_activity_state(
        [
            _event(
                "mouse_activity",
                seconds_ago=10,
                metadata={"click_count": 1, "scroll_count": 0},
            )
        ],
        thresholds=THRESHOLDS,
        now=NOW,
    )
    scroll_state = classify_activity_state(
        [
            _event(
                "mouse_activity",
                seconds_ago=10,
                metadata={"click_count": 0, "scroll_count": 1},
            )
        ],
        thresholds=THRESHOLDS,
        now=NOW,
    )

    assert click_state == ACTIVE
    assert scroll_state == ACTIVE


def test_classifies_recent_activity_outside_active_window_as_reading():
    state = classify_activity_state(
        [_event("keyboard_burst", seconds_ago=90, metadata={"key_count": 5})],
        thresholds=THRESHOLDS,
        now=NOW,
    )

    assert state == READING


def test_classifies_mouse_move_without_click_or_scroll_as_reading():
    state = classify_activity_state(
        [
            _event(
                "mouse_activity",
                seconds_ago=10,
                metadata={
                    "move_count": 12,
                    "click_count": 0,
                    "scroll_count": 0,
                    "drag_count": 0,
                },
            )
        ],
        thresholds=THRESHOLDS,
        now=NOW,
    )

    assert state == READING


def test_classifies_no_recent_activity_as_idle():
    assert classify_activity_state([], thresholds=THRESHOLDS, now=NOW) == IDLE
    assert (
        classify_activity_state(
            [_event("keyboard_burst", seconds_ago=300, metadata={"key_count": 1})],
            thresholds=THRESHOLDS,
            now=NOW,
        )
        == IDLE
    )


def test_activity_thresholds_reject_invalid_values():
    with pytest.raises(ValueError, match="active_window_seconds"):
        ActivityThresholds(active_window_seconds=0)

    with pytest.raises(ValueError, match="idle_timeout_seconds"):
        ActivityThresholds(idle_timeout_seconds=0)


def _event(event_type, *, seconds_ago, metadata):
    return normalize_event(
        event_type,
        timestamp=NOW - timedelta(seconds=seconds_ago),
        source="test",
        metadata=metadata,
    )
