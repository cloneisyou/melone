import pytest

from melone_service.collectors.mouse import (
    MOUSE_ACTIVITY,
    MouseCaptureUnavailable,
    MouseCollector,
    MousePosition,
    MouseSample,
    aggregate_mouse_samples,
)


def test_aggregate_mouse_samples_counts_activity_and_last_metadata():
    activities = aggregate_mouse_samples(
        [
            MouseSample(
                timestamp=0.0,
                kind="move",
                position=MousePosition(10, 20),
                display_id=1,
            ),
            MouseSample(timestamp=0.2, kind="click", position=MousePosition(11, 21)),
            MouseSample(timestamp=0.3, kind="scroll", position=MousePosition(12, 22)),
            MouseSample(
                timestamp=0.4,
                kind="drag",
                position=MousePosition(30, 40),
                display_id=2,
            ),
            MouseSample(timestamp=1.2, kind="move", position=MousePosition(50, 60)),
        ],
        window_seconds=1.0,
    )

    assert len(activities) == 2
    assert activities[0].click_count == 1
    assert activities[0].scroll_count == 1
    assert activities[0].drag_count == 1
    assert activities[0].drag_active is True
    assert activities[0].move_count == 2
    assert activities[0].move_density == 2.0
    assert activities[0].last_position == MousePosition(30, 40)
    assert activities[0].active_display_id == 2
    assert activities[1].click_count == 0
    assert activities[1].move_count == 1
    assert activities[1].last_position == MousePosition(50, 60)


def test_mouse_collector_emits_aggregate_metadata_at_bounded_interval():
    source = _FakeMouseEventSource(
        [
            MouseSample(
                timestamp=0.0,
                kind="move",
                position=MousePosition(10, 20),
                display_id=7,
            ),
            MouseSample(timestamp=0.1, kind="click", position=MousePosition(11, 21)),
            MouseSample(timestamp=0.2, kind="scroll", position=MousePosition(12, 22)),
            MouseSample(
                timestamp=0.3,
                kind="drag",
                position=MousePosition(13, 23),
                display_id=8,
            ),
        ]
    )
    collector = MouseCollector(
        event_source=source,
        platform_name="darwin",
        activity_window_seconds=1.0,
        monotonic=_FakeClock([0.4, 1.2]),
    )

    assert collector.poll() == []
    events = collector.poll()

    assert source.start_calls == 1
    assert [event.type for event in events] == [MOUSE_ACTIVITY]
    event = events[0]
    assert event.source == "mouse"
    assert event.window.display_id == 8
    assert event.metadata == {
        "click_count": 1,
        "scroll_count": 1,
        "drag_count": 1,
        "drag_active": True,
        "move_count": 2,
        "move_density": 2.0,
        "duration_ms": 300,
        "last_position": {"x": 13, "y": 23},
        "active_display_id": 8,
    }


def test_mouse_collector_emits_closed_windows_without_raw_stream_rows():
    source = _FakeMouseEventSource(
        [
            MouseSample(timestamp=0.0, kind="move"),
            MouseSample(timestamp=0.1, kind="move"),
            MouseSample(timestamp=1.1, kind="click"),
            MouseSample(timestamp=1.2, kind="scroll"),
            MouseSample(timestamp=2.2, kind="drag"),
        ]
    )
    collector = MouseCollector(
        event_source=source,
        platform_name="darwin",
        activity_window_seconds=1.0,
        monotonic=lambda: 3.3,
    )

    events = collector.poll()

    assert len(events) == 3
    assert [event.metadata["move_count"] for event in events] == [2, 0, 1]
    assert [event.metadata["click_count"] for event in events] == [0, 1, 0]
    assert [event.metadata["scroll_count"] for event in events] == [0, 1, 0]
    assert [event.metadata["drag_count"] for event in events] == [0, 0, 1]


def test_mouse_collector_is_noop_off_macos():
    source = _FakeMouseEventSource([MouseSample(timestamp=0.0, kind="click")])
    collector = MouseCollector(event_source=source, platform_name="linux")

    assert collector.poll() == []
    assert source.start_calls == 0
    assert collector.is_disabled is False


def test_mouse_collector_disables_when_event_tap_is_unavailable():
    source = _FakeMouseEventSource(
        [MouseSample(timestamp=0.0, kind="click")],
        start_error=MouseCaptureUnavailable("Accessibility permission is not granted"),
    )
    collector = MouseCollector(event_source=source, platform_name="darwin")

    assert collector.poll() == []
    assert collector.is_disabled is True
    assert "Accessibility" in (collector.disable_reason or "")

    assert collector.poll() == []
    assert source.start_calls == 1


def test_aggregate_mouse_samples_rejects_invalid_window():
    with pytest.raises(ValueError, match="window_seconds"):
        aggregate_mouse_samples(
            [MouseSample(timestamp=0.0, kind="click")],
            window_seconds=0,
        )


class _FakeMouseEventSource:
    def __init__(self, samples, *, start_error=None):
        self.samples = list(samples)
        self.start_error = start_error
        self.start_calls = 0

    def start(self):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error

    def drain(self):
        samples = self.samples
        self.samples = []
        return samples


class _FakeClock:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)
