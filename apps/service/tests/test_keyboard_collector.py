import pytest

from melone_service.collectors.keyboard import (
    BACKSPACE_KEY_CODES,
    CLIPBOARD_SHORTCUT,
    ENTER_KEY_CODES,
    KEY_CODE_C,
    KEY_CODE_V,
    KEYBOARD_BURST,
    KEYBOARD_RAW_EVENT,
    KeyboardCaptureUnavailable,
    KeyboardCollector,
    KeyboardSample,
    aggregate_keyboard_samples,
)


def test_aggregate_keyboard_samples_counts_short_bursts_and_special_keys():
    enter_key = next(iter(ENTER_KEY_CODES))
    backspace_key = next(iter(BACKSPACE_KEY_CODES))
    bursts = aggregate_keyboard_samples(
        [
            KeyboardSample(timestamp=0.0, key_code=0, text="h"),
            KeyboardSample(timestamp=0.1, key_code=0, text="i"),
            KeyboardSample(timestamp=0.2, key_code=enter_key),
            KeyboardSample(timestamp=0.3, key_code=0, text="x"),
            KeyboardSample(timestamp=0.4, key_code=backspace_key),
            KeyboardSample(
                timestamp=0.5,
                key_code=KEY_CODE_C,
                command=True,
                text="c",
            ),
            KeyboardSample(timestamp=0.5, key_code=KEY_CODE_V, command=True),
            KeyboardSample(timestamp=1.4, key_code=0, secure_input=True, text="x"),
        ],
        window_seconds=1.0,
    )

    assert len(bursts) == 2
    assert bursts[0].key_count == 7
    assert bursts[0].enter_count == 1
    assert bursts[0].backspace_count == 1
    assert bursts[0].copy_count == 1
    assert bursts[0].paste_count == 1
    assert bursts[0].special_key_count == 4
    assert bursts[0].secure_input is False
    assert bursts[0].text == "hi\n"
    assert bursts[0].raw_text == "hi\n"
    assert bursts[1].key_count == 1
    assert bursts[1].secure_input is True
    assert bursts[1].text == "x"
    assert bursts[1].raw_text == "x"


def test_aggregate_keyboard_samples_composes_korean_jamo_text():
    bursts = aggregate_keyboard_samples(
        [
            KeyboardSample(timestamp=0.0, key_code=5, text="ㅎ"),
            KeyboardSample(timestamp=0.1, key_code=40, text="ㅏ"),
            KeyboardSample(timestamp=0.2, key_code=1, text="ㄴ"),
            KeyboardSample(timestamp=0.3, key_code=15, text="ㄱ"),
            KeyboardSample(timestamp=0.4, key_code=46, text="ㅡ"),
            KeyboardSample(timestamp=0.5, key_code=3, text="ㄹ"),
        ],
        window_seconds=1.0,
    )

    assert len(bursts) == 1
    assert bursts[0].text == "한글"
    assert bursts[0].raw_text == "ㅎㅏㄴㄱㅡㄹ"


def test_aggregate_keyboard_samples_composes_mixed_text():
    bursts = aggregate_keyboard_samples(
        [
            KeyboardSample(timestamp=0.0, key_code=0, text="a"),
            KeyboardSample(timestamp=0.1, key_code=11, text="b"),
            KeyboardSample(timestamp=0.2, key_code=8, text="c"),
            KeyboardSample(timestamp=0.3, key_code=5, text="ㅎ"),
            KeyboardSample(timestamp=0.4, key_code=40, text="ㅏ"),
            KeyboardSample(timestamp=0.5, key_code=1, text="ㄴ"),
            KeyboardSample(timestamp=0.6, key_code=15, text="ㄱ"),
            KeyboardSample(timestamp=0.7, key_code=46, text="ㅡ"),
            KeyboardSample(timestamp=0.8, key_code=3, text="ㄹ"),
            KeyboardSample(timestamp=0.9, key_code=2, text="d"),
            KeyboardSample(timestamp=1.0, key_code=14, text="e"),
            KeyboardSample(timestamp=1.1, key_code=3, text="f"),
        ],
        window_seconds=2.0,
    )

    assert len(bursts) == 1
    assert bursts[0].text == "abc한글def"
    assert bursts[0].raw_text == "abcㅎㅏㄴㄱㅡㄹdef"


def test_aggregate_keyboard_samples_handles_hangul_backspace():
    backspace_key = next(iter(BACKSPACE_KEY_CODES))
    bursts = aggregate_keyboard_samples(
        [
            KeyboardSample(timestamp=0.0, key_code=5, text="ㅎ"),
            KeyboardSample(timestamp=0.1, key_code=40, text="ㅏ"),
            KeyboardSample(timestamp=0.2, key_code=1, text="ㄴ"),
            KeyboardSample(timestamp=0.3, key_code=backspace_key, text="\b"),
        ],
        window_seconds=1.0,
    )

    assert len(bursts) == 1
    assert bursts[0].text == "하"
    assert bursts[0].raw_text == "ㅎㅏ"


def test_keyboard_collector_emits_aggregate_metadata_with_raw_text():
    source = _FakeKeyboardEventSource(
        [
            KeyboardSample(timestamp=0.0, key_code=0, text="a"),
            KeyboardSample(timestamp=0.1, key_code=KEY_CODE_C, command=True, text="c"),
            KeyboardSample(timestamp=0.2, key_code=KEY_CODE_V, command=True),
        ]
    )
    collector = KeyboardCollector(
        event_source=source,
        platform_name="darwin",
        burst_window_seconds=1.0,
    )

    events = collector.poll()
    burst = next(event for event in events if event.type == KEYBOARD_BURST)

    assert source.start_calls == 1
    assert burst.source == "keyboard"
    assert burst.metadata == {
        "key_count": 3,
        "special_key_count": 2,
        "enter_count": 0,
        "backspace_count": 0,
        "copy_count": 1,
        "paste_count": 1,
        "secure_input": False,
        "duration_ms": 200,
        "text": "a",
        "raw_text": "a",
    }


def test_keyboard_collector_keeps_hangul_composition_across_polls():
    current_time = 0.5
    collector = KeyboardCollector(
        event_source=_FakeKeyboardEventSource(
            [
                [
                    KeyboardSample(timestamp=0.0, key_code=5, text="ㄱ"),
                    KeyboardSample(timestamp=0.1, key_code=40, text="ㅣ"),
                    KeyboardSample(timestamp=0.2, key_code=1, text="ㄹ"),
                ],
                [
                    KeyboardSample(timestamp=0.3, key_code=31, text="ㅗ"),
                    KeyboardSample(timestamp=0.4, key_code=5, text="ㄱ"),
                    KeyboardSample(timestamp=0.5, key_code=49, text=" "),
                ],
            ]
        ),
        platform_name="darwin",
        burst_window_seconds=1.0,
        monotonic=lambda: current_time,
    )

    first_events = collector.poll()
    second_events = collector.poll()
    first_bursts = [
        event for event in first_events if event.type == KEYBOARD_BURST
    ]
    second_bursts = [
        event for event in second_events if event.type == KEYBOARD_BURST
    ]

    assert first_bursts == []
    assert len(second_bursts) == 1
    assert second_bursts[0].metadata["text"] == "기록 "
    assert second_bursts[0].metadata["raw_text"] == "ㄱㅣㄹㅗㄱ "


def test_keyboard_collector_keeps_mixed_text_across_polls():
    current_time = 0.9
    collector = KeyboardCollector(
        event_source=_FakeKeyboardEventSource(
            [
                [
                    KeyboardSample(timestamp=0.0, key_code=0, text="a"),
                    KeyboardSample(timestamp=0.1, key_code=11, text="b"),
                    KeyboardSample(timestamp=0.2, key_code=8, text="c"),
                ],
                [
                    KeyboardSample(timestamp=0.3, key_code=5, text="ㅎ"),
                    KeyboardSample(timestamp=0.4, key_code=40, text="ㅏ"),
                    KeyboardSample(timestamp=0.5, key_code=1, text="ㄴ"),
                    KeyboardSample(timestamp=0.6, key_code=15, text="ㄱ"),
                    KeyboardSample(timestamp=0.7, key_code=46, text="ㅡ"),
                    KeyboardSample(timestamp=0.8, key_code=3, text="ㄹ"),
                    KeyboardSample(timestamp=0.9, key_code=49, text=" "),
                ],
            ]
        ),
        platform_name="darwin",
        burst_window_seconds=1.0,
        monotonic=lambda: current_time,
    )

    first_events = collector.poll()
    second_events = collector.poll()
    first_bursts = [
        event for event in first_events if event.type == KEYBOARD_BURST
    ]
    second_bursts = [
        event for event in second_events if event.type == KEYBOARD_BURST
    ]

    assert first_bursts == []
    assert len(second_bursts) == 1
    assert second_bursts[0].metadata["text"] == "abc한글 "
    assert second_bursts[0].metadata["raw_text"] == "abcㅎㅏㄴㄱㅡㄹ "


def test_keyboard_collector_flushes_pending_hangul_after_idle_timeout():
    current_time = 0.2
    collector = KeyboardCollector(
        event_source=_FakeKeyboardEventSource(
            [
                [
                    KeyboardSample(timestamp=0.0, key_code=5, text="ㅎ"),
                    KeyboardSample(timestamp=0.1, key_code=40, text="ㅏ"),
                    KeyboardSample(timestamp=0.2, key_code=1, text="ㄴ"),
                ],
                [],
            ]
        ),
        platform_name="darwin",
        burst_window_seconds=1.0,
        monotonic=lambda: current_time,
    )

    first_events = collector.poll()
    current_time = 1.3
    second_events = collector.poll()
    first_bursts = [
        event for event in first_events if event.type == KEYBOARD_BURST
    ]
    second_bursts = [
        event for event in second_events if event.type == KEYBOARD_BURST
    ]

    assert first_bursts == []
    assert len(second_bursts) == 1
    assert second_bursts[0].metadata["text"] == "한"
    assert second_bursts[0].metadata["raw_text"] == "ㅎㅏㄴ"


def test_keyboard_collector_emits_raw_events_for_each_sample():
    collector = KeyboardCollector(
        event_source=_FakeKeyboardEventSource(
            [
                KeyboardSample(timestamp=0.0, key_code=0, text="a", shift=True),
                KeyboardSample(
                    timestamp=0.1,
                    key_code=KEY_CODE_C,
                    command=True,
                    text="c",
                ),
            ]
        ),
        platform_name="darwin",
    )

    raw_events = [
        event
        for event in collector.poll()
        if event.type == KEYBOARD_RAW_EVENT
    ]

    assert len(raw_events) == 2
    assert raw_events[0].source == "keyboard"
    assert raw_events[0].metadata == {
        "key_code": 0,
        "text": "a",
        "is_shortcut": False,
        "modifiers": ["shift"],
        "command": False,
        "control": False,
        "option": False,
        "shift": True,
        "secure_input": False,
    }
    assert raw_events[1].metadata["key_code"] == KEY_CODE_C
    assert raw_events[1].metadata["text"] == "c"
    assert raw_events[1].metadata["is_shortcut"] is True
    assert raw_events[1].metadata["modifiers"] == ["command"]


def test_keyboard_collector_emits_copy_and_paste_shortcut_events():
    collector = KeyboardCollector(
        event_source=_FakeKeyboardEventSource(
            [
                KeyboardSample(timestamp=0.0, key_code=KEY_CODE_C, command=True),
                KeyboardSample(timestamp=0.1, key_code=KEY_CODE_V, command=True),
            ]
        ),
        platform_name="darwin",
    )

    shortcuts = [
        event
        for event in collector.poll()
        if event.type == CLIPBOARD_SHORTCUT
    ]

    assert [event.metadata["action"] for event in shortcuts] == ["copy", "paste"]
    assert shortcuts[0].metadata["copy_count"] == 1
    assert shortcuts[0].metadata["paste_count"] == 0
    assert shortcuts[1].metadata["copy_count"] == 0
    assert shortcuts[1].metadata["paste_count"] == 1
    assert all("clipboard" not in event.metadata for event in shortcuts)


def test_keyboard_collector_is_noop_off_macos():
    source = _FakeKeyboardEventSource([KeyboardSample(timestamp=0.0, key_code=0)])
    collector = KeyboardCollector(event_source=source, platform_name="linux")

    assert collector.poll() == []
    assert source.start_calls == 0


def test_keyboard_collector_propagates_event_tap_failure():
    # 권한 분기를 제거했으므로 event tap 실패는 service의 collector 경계에서 처리되도록 전파한다.
    source = _FakeKeyboardEventSource(
        [KeyboardSample(timestamp=0.0, key_code=0)],
        start_error=KeyboardCaptureUnavailable("keyboard event tap did not start"),
    )
    collector = KeyboardCollector(event_source=source, platform_name="darwin")

    with pytest.raises(KeyboardCaptureUnavailable):
        collector.poll()
    assert source.start_calls == 1


def test_aggregate_keyboard_samples_rejects_invalid_window():
    with pytest.raises(ValueError, match="window_seconds"):
        aggregate_keyboard_samples(
            [KeyboardSample(timestamp=0.0, key_code=0)],
            window_seconds=0,
        )


class _FakeKeyboardEventSource:
    def __init__(self, samples, *, start_error=None):
        if samples and isinstance(samples[0], list):
            self.batches = [list(batch) for batch in samples]
        else:
            self.batches = [list(samples)]
        self.start_error = start_error
        self.start_calls = 0

    def start(self):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error

    def drain(self):
        if not self.batches:
            return []

        return self.batches.pop(0)
