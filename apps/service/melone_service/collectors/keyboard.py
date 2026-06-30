from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from melone_service.models import NormalizedEvent, utc_now
from melone_service.pipeline.normalizer import normalize_event


KEYBOARD_RAW_EVENT = "keyboard_raw_event"
KEYBOARD_BURST = "keyboard_burst"
CLIPBOARD_SHORTCUT = "clipboard_shortcut"

DEFAULT_BURST_WINDOW_SECONDS = 2.0
DEFAULT_EVENT_TAP_START_TIMEOUT_SECONDS = 1.0
MAX_UNICODE_CHARS = 8

KEY_CODE_C = 8
KEY_CODE_V = 9
ENTER_KEY_CODES = frozenset({36, 76})
BACKSPACE_KEY_CODES = frozenset({51})

CARBON_FRAMEWORK_PATH = "/System/Library/Frameworks/Carbon.framework/Carbon"

HANGUL_BASE_CODEPOINT = 0xAC00
HANGUL_VOWEL_COUNT = 21
HANGUL_TAIL_COUNT = 28

HANGUL_LEADS = (
    "ㄱ",
    "ㄲ",
    "ㄴ",
    "ㄷ",
    "ㄸ",
    "ㄹ",
    "ㅁ",
    "ㅂ",
    "ㅃ",
    "ㅅ",
    "ㅆ",
    "ㅇ",
    "ㅈ",
    "ㅉ",
    "ㅊ",
    "ㅋ",
    "ㅌ",
    "ㅍ",
    "ㅎ",
)
HANGUL_VOWELS = (
    "ㅏ",
    "ㅐ",
    "ㅑ",
    "ㅒ",
    "ㅓ",
    "ㅔ",
    "ㅕ",
    "ㅖ",
    "ㅗ",
    "ㅘ",
    "ㅙ",
    "ㅚ",
    "ㅛ",
    "ㅜ",
    "ㅝ",
    "ㅞ",
    "ㅟ",
    "ㅠ",
    "ㅡ",
    "ㅢ",
    "ㅣ",
)
HANGUL_TAILS = (
    "",
    "ㄱ",
    "ㄲ",
    "ㄳ",
    "ㄴ",
    "ㄵ",
    "ㄶ",
    "ㄷ",
    "ㄹ",
    "ㄺ",
    "ㄻ",
    "ㄼ",
    "ㄽ",
    "ㄾ",
    "ㄿ",
    "ㅀ",
    "ㅁ",
    "ㅂ",
    "ㅄ",
    "ㅅ",
    "ㅆ",
    "ㅇ",
    "ㅈ",
    "ㅊ",
    "ㅋ",
    "ㅌ",
    "ㅍ",
    "ㅎ",
)
COMPOUND_HANGUL_VOWELS = {
    ("ㅗ", "ㅏ"): "ㅘ",
    ("ㅗ", "ㅐ"): "ㅙ",
    ("ㅗ", "ㅣ"): "ㅚ",
    ("ㅜ", "ㅓ"): "ㅝ",
    ("ㅜ", "ㅔ"): "ㅞ",
    ("ㅜ", "ㅣ"): "ㅟ",
    ("ㅡ", "ㅣ"): "ㅢ",
}
COMPOUND_HANGUL_TAILS = {
    ("ㄱ", "ㅅ"): "ㄳ",
    ("ㄴ", "ㅈ"): "ㄵ",
    ("ㄴ", "ㅎ"): "ㄶ",
    ("ㄹ", "ㄱ"): "ㄺ",
    ("ㄹ", "ㅁ"): "ㄻ",
    ("ㄹ", "ㅂ"): "ㄼ",
    ("ㄹ", "ㅅ"): "ㄽ",
    ("ㄹ", "ㅌ"): "ㄾ",
    ("ㄹ", "ㅍ"): "ㄿ",
    ("ㄹ", "ㅎ"): "ㅀ",
    ("ㅂ", "ㅅ"): "ㅄ",
}
HANGUL_LEAD_INDEX = {value: index for index, value in enumerate(HANGUL_LEADS)}
HANGUL_VOWEL_INDEX = {value: index for index, value in enumerate(HANGUL_VOWELS)}
HANGUL_TAIL_INDEX = {value: index for index, value in enumerate(HANGUL_TAILS)}
HANGUL_TAIL_CHARS = frozenset(HANGUL_TAILS[1:])


class KeyboardCaptureUnavailable(RuntimeError):
    pass


class KeyboardEventSource(Protocol):
    def start(self) -> None:
        """Start collecting sanitized keyDown samples."""

    def drain(self) -> list[KeyboardSample]:
        """Return samples collected since the previous drain."""


@dataclass(frozen=True)
class KeyboardSample:
    timestamp: float
    key_code: int
    command: bool = False
    secure_input: bool | None = None
    text: str = ""
    captured_at: datetime | None = None
    control: bool = False
    option: bool = False
    shift: bool = False


@dataclass(frozen=True)
class KeyboardBurst:
    start_time: float
    end_time: float
    key_count: int
    enter_count: int
    backspace_count: int
    copy_count: int
    paste_count: int
    secure_input: bool
    text: str
    raw_text: str

    @property
    def special_key_count(self) -> int:
        return (
            self.enter_count
            + self.backspace_count
            + self.copy_count
            + self.paste_count
        )


class KeyboardCollector:
    name = "keyboard"

    def __init__(
        self,
        *,
        event_source: KeyboardEventSource | None = None,
        platform_name: str | None = None,
        burst_window_seconds: float = DEFAULT_BURST_WINDOW_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.platform_name = sys.platform if platform_name is None else platform_name
        self.event_source = event_source or MacOSKeyboardEventSource(
            platform_name=self.platform_name
        )
        self.burst_window_seconds = burst_window_seconds
        self.monotonic = monotonic or time.monotonic
        self._started = False
        self._burst_builder: _BurstBuilder | None = None

    def poll(self) -> list[NormalizedEvent]:
        # Accessibility 권한은 서비스 시작 시점에 강제되므로 여기서 권한 분기는 하지 않는다.
        # event tap 실패는 service의 collector 경계에서 처리한다.
        if self.platform_name != "darwin":
            return []

        if not self._started:
            self.event_source.start()
            self._started = True

        samples = self.event_source.drain()
        ordered_samples = sorted(samples, key=lambda sample: sample.timestamp)
        events = [_keyboard_raw_event(sample) for sample in ordered_samples]
        bursts = self._record_keyboard_bursts(ordered_samples)
        events.extend(_keyboard_burst_event(burst) for burst in bursts)
        events.extend(_clipboard_shortcut_events(ordered_samples))
        return events

    def _record_keyboard_bursts(
        self,
        samples: Sequence[KeyboardSample],
    ) -> list[KeyboardBurst]:
        if self.burst_window_seconds <= 0:
            raise ValueError("burst_window_seconds must be greater than 0")

        bursts: list[KeyboardBurst] = []

        for sample in samples:
            if self._should_start_new_burst(sample):
                bursts.append(self._flush_keyboard_burst())

            if self._burst_builder is None:
                self._burst_builder = _BurstBuilder(sample.timestamp)

            self._burst_builder.add(sample)

        if self._should_flush_keyboard_burst():
            bursts.append(self._flush_keyboard_burst())

        return bursts

    def _should_start_new_burst(self, sample: KeyboardSample) -> bool:
        if self._burst_builder is None:
            return False

        return self._burst_builder.is_idle_before(
            sample,
            self.burst_window_seconds,
        )

    def _should_flush_keyboard_burst(self) -> bool:
        if self._burst_builder is None:
            return False

        return (
            self._burst_builder.ends_at_text_boundary()
            or self._burst_builder.is_idle_at(
                self.monotonic(),
                self.burst_window_seconds,
            )
        )

    def _flush_keyboard_burst(self) -> KeyboardBurst:
        if self._burst_builder is None:
            raise RuntimeError("keyboard burst builder is empty")

        burst = self._burst_builder.build()
        self._burst_builder = None
        return burst


class MacOSKeyboardEventSource:
    def __init__(
        self,
        *,
        platform_name: str | None = None,
        monotonic: Callable[[], float] | None = None,
        secure_input_detector: Callable[[], bool | None] | None = None,
        start_timeout_seconds: float = DEFAULT_EVENT_TAP_START_TIMEOUT_SECONDS,
    ) -> None:
        self.platform_name = sys.platform if platform_name is None else platform_name
        self.monotonic = monotonic or time.monotonic
        self.secure_input_detector = (
            secure_input_detector or SecureInputDetector().is_enabled
        )
        self.start_timeout_seconds = start_timeout_seconds
        self._samples: queue.Queue[KeyboardSample] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._setup_error: KeyboardCaptureUnavailable | None = None
        self._callback: Callable[..., Any] | None = None
        self._started = False

    def start(self) -> None:
        if self.platform_name != "darwin":
            raise KeyboardCaptureUnavailable("keyboard collector is macOS only")
        if self._started:
            return

        self._ready.clear()
        self._setup_error = None
        self._thread = threading.Thread(
            target=self._run_event_tap,
            name="melone-keyboard-event-tap",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(self.start_timeout_seconds):
            raise KeyboardCaptureUnavailable("keyboard event tap did not start")
        if self._setup_error is not None:
            raise self._setup_error

        self._started = True

    def drain(self) -> list[KeyboardSample]:
        samples: list[KeyboardSample] = []
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
                CGEventGetFlags,
                CGEventGetIntegerValueField,
                CGEventKeyboardGetUnicodeString,
                CGEventMaskBit,
                CGEventTapCreate,
                CGEventTapEnable,
                kCFRunLoopCommonModes,
                kCGEventFlagMaskAlternate,
                kCGEventFlagMaskCommand,
                kCGEventFlagMaskControl,
                kCGEventFlagMaskShift,
                kCGEventKeyDown,
                kCGEventTapOptionListenOnly,
                kCGHeadInsertEventTap,
                kCGKeyboardEventKeycode,
                kCGSessionEventTap,
            )
        except ImportError as exc:
            self._setup_error = KeyboardCaptureUnavailable(
                "keyboard collector requires PyObjC Quartz on macOS"
            )
            self._ready.set()
            return

        def callback(
            _proxy: object,
            event_type: int,
            event: object,
            _refcon: object,
        ) -> object:
            if event_type == kCGEventKeyDown:
                key_code = int(
                    CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                )
                flags = int(CGEventGetFlags(event))
                self._samples.put(
                    KeyboardSample(
                        timestamp=self.monotonic(),
                        key_code=key_code,
                        captured_at=utc_now(),
                        command=bool(flags & kCGEventFlagMaskCommand),
                        control=bool(flags & kCGEventFlagMaskControl),
                        option=bool(flags & kCGEventFlagMaskAlternate),
                        shift=bool(flags & kCGEventFlagMaskShift),
                        secure_input=_detect_secure_input(
                            self.secure_input_detector
                        ),
                        text=_event_unicode_text(
                            event, CGEventKeyboardGetUnicodeString
                        ),
                    )
                )
            return event

        self._callback = callback
        event_mask = CGEventMaskBit(kCGEventKeyDown)
        event_tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            event_mask,
            self._callback,
            None,
        )
        if event_tap is None:
            self._setup_error = KeyboardCaptureUnavailable(
                "keyboard event tap could not start; Accessibility permission may be missing"
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


class SecureInputDetector:
    def __init__(
        self,
        *,
        framework_loader: Callable[[str], Any] | None = None,
    ) -> None:
        self.framework_loader = framework_loader or ctypes.CDLL
        self._loaded = False
        self._function: Callable[[], bool] | None = None

    def is_enabled(self) -> bool | None:
        if not self._loaded:
            self._load_function()
        if self._function is None:
            return None

        return bool(self._function())

    def _load_function(self) -> None:
        self._loaded = True
        try:
            framework = self.framework_loader(CARBON_FRAMEWORK_PATH)
            function = getattr(framework, "IsSecureEventInputEnabled")
            function.argtypes = []
            function.restype = ctypes.c_bool
        except (AttributeError, OSError):
            self._function = None
            return

        self._function = function


def aggregate_keyboard_samples(
    samples: Sequence[KeyboardSample],
    *,
    window_seconds: float = DEFAULT_BURST_WINDOW_SECONDS,
) -> list[KeyboardBurst]:
    if window_seconds <= 0:
        raise ValueError("window_seconds must be greater than 0")

    ordered_samples = sorted(samples, key=lambda sample: sample.timestamp)
    bursts: list[KeyboardBurst] = []
    current: _BurstBuilder | None = None

    for sample in ordered_samples:
        if (
            current is None
            or sample.timestamp - current.start_time >= window_seconds
        ):
            if current is not None:
                bursts.append(current.build())
            current = _BurstBuilder(sample.timestamp)

        current.add(sample)

    if current is not None:
        bursts.append(current.build())

    return bursts


def _clipboard_shortcut_events(
    samples: Sequence[KeyboardSample],
) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    for sample in sorted(samples, key=lambda item: item.timestamp):
        action = _clipboard_action(sample)
        if action is None:
            continue

        events.append(
            normalize_event(
                CLIPBOARD_SHORTCUT,
                source="keyboard",
                metadata={
                    "action": action,
                    "copy_count": 1 if action == "copy" else 0,
                    "paste_count": 1 if action == "paste" else 0,
                    "secure_input": sample.secure_input is True,
                },
            )
        )

    return events


def _keyboard_raw_event(sample: KeyboardSample) -> NormalizedEvent:
    return normalize_event(
        KEYBOARD_RAW_EVENT,
        source="keyboard",
        timestamp=sample.captured_at,
        metadata={
            "key_code": sample.key_code,
            "text": sample.text,
            "is_shortcut": _is_shortcut(sample),
            "modifiers": _modifiers(sample),
            "command": sample.command,
            "control": sample.control,
            "option": sample.option,
            "shift": sample.shift,
            "secure_input": sample.secure_input is True,
        },
    )


def _keyboard_burst_event(burst: KeyboardBurst) -> NormalizedEvent:
    return normalize_event(
        KEYBOARD_BURST,
        source="keyboard",
        metadata={
            "key_count": burst.key_count,
            "special_key_count": burst.special_key_count,
            "enter_count": burst.enter_count,
            "backspace_count": burst.backspace_count,
            "copy_count": burst.copy_count,
            "paste_count": burst.paste_count,
            "secure_input": burst.secure_input,
            "duration_ms": int(round((burst.end_time - burst.start_time) * 1000)),
            "text": burst.text,
            "raw_text": burst.raw_text,
        },
    )


@dataclass
class _BurstBuilder:
    start_time: float
    end_time: float | None = None
    key_count: int = 0
    enter_count: int = 0
    backspace_count: int = 0
    copy_count: int = 0
    paste_count: int = 0
    secure_input: bool = False
    raw_text_parts: list[str] = field(default_factory=list)

    @property
    def raw_text(self) -> str:
        return "".join(self.raw_text_parts)

    def add(self, sample: KeyboardSample) -> None:
        self.key_count += 1
        self.end_time = sample.timestamp
        self.secure_input = self.secure_input or sample.secure_input is True

        if sample.key_code in ENTER_KEY_CODES:
            self.enter_count += 1
        if sample.key_code in BACKSPACE_KEY_CODES:
            self.backspace_count += 1

        action = _clipboard_action(sample)
        if action == "copy":
            self.copy_count += 1
        elif action == "paste":
            self.paste_count += 1

        self._record_typed_text(sample)

    def is_idle_before(
        self,
        sample: KeyboardSample,
        idle_seconds: float,
    ) -> bool:
        return (
            self.end_time is not None
            and sample.timestamp - self.end_time >= idle_seconds
        )

    def is_idle_at(self, now: float, idle_seconds: float) -> bool:
        return self.end_time is not None and now - self.end_time >= idle_seconds

    def ends_at_text_boundary(self) -> bool:
        return _ends_at_text_boundary(self.raw_text)

    def build(self) -> KeyboardBurst:
        raw_text = self.raw_text
        return KeyboardBurst(
            start_time=self.start_time,
            end_time=self.start_time if self.end_time is None else self.end_time,
            key_count=self.key_count,
            enter_count=self.enter_count,
            backspace_count=self.backspace_count,
            copy_count=self.copy_count,
            paste_count=self.paste_count,
            secure_input=self.secure_input,
            text=_compose_keyboard_text(raw_text),
            raw_text=raw_text,
        )

    def _record_typed_text(self, sample: KeyboardSample) -> None:
        if _is_shortcut(sample):
            return

        if sample.key_code in BACKSPACE_KEY_CODES:
            _delete_last_character(self.raw_text_parts)
            return

        if sample.key_code in ENTER_KEY_CODES:
            self.raw_text_parts.append(_enter_text(sample))
            return

        if sample.text:
            self.raw_text_parts.append(sample.text)


def _ends_at_text_boundary(text: str) -> bool:
    if not text:
        return False

    last_character = text[-1]
    return last_character.isspace() or _is_text_delimiter(last_character)


def _is_text_delimiter(character: str) -> bool:
    return not character.isalnum() and not _is_hangul_jamo(character)


def _is_hangul_jamo(character: str) -> bool:
    return (
        character in HANGUL_LEAD_INDEX
        or character in HANGUL_VOWEL_INDEX
        or character in HANGUL_TAIL_CHARS
    )


def _compose_keyboard_text(raw_text: str) -> str:
    if not raw_text:
        return ""

    chars = list(raw_text)
    result: list[str] = []
    index = 0

    while index < len(chars):
        lead = chars[index]
        if _can_start_hangul_syllable(chars, index):
            syllable, index = _consume_hangul_syllable(chars, index)
            result.append(syllable)
            continue

        result.append(lead)
        index += 1

    return "".join(result)


def _can_start_hangul_syllable(chars: Sequence[str], index: int) -> bool:
    return (
        index + 1 < len(chars)
        and chars[index] in HANGUL_LEAD_INDEX
        and chars[index + 1] in HANGUL_VOWEL_INDEX
    )


def _consume_hangul_syllable(
    chars: Sequence[str],
    index: int,
) -> tuple[str, int]:
    lead = chars[index]
    vowel = chars[index + 1]
    index += 2

    if index < len(chars):
        compound_vowel = COMPOUND_HANGUL_VOWELS.get((vowel, chars[index]))
        if compound_vowel is not None:
            vowel = compound_vowel
            index += 1

    tail = ""
    if index < len(chars) and chars[index] in HANGUL_TAIL_CHARS:
        tail, index = _consume_hangul_tail(chars, index)

    return _hangul_syllable(lead, vowel, tail), index


def _consume_hangul_tail(chars: Sequence[str], index: int) -> tuple[str, int]:
    tail = chars[index]

    if _is_followed_by_vowel(chars, index):
        return "", index

    next_index = index + 1
    if next_index < len(chars):
        compound_tail = COMPOUND_HANGUL_TAILS.get((tail, chars[next_index]))
        if compound_tail is not None and not _is_followed_by_vowel(
            chars,
            next_index,
        ):
            return compound_tail, next_index + 1

    return tail, index + 1


def _is_followed_by_vowel(chars: Sequence[str], index: int) -> bool:
    return index + 1 < len(chars) and chars[index + 1] in HANGUL_VOWEL_INDEX


def _hangul_syllable(lead: str, vowel: str, tail: str) -> str:
    codepoint = HANGUL_BASE_CODEPOINT + (
        (
            HANGUL_LEAD_INDEX[lead] * HANGUL_VOWEL_COUNT
            + HANGUL_VOWEL_INDEX[vowel]
        )
        * HANGUL_TAIL_COUNT
        + HANGUL_TAIL_INDEX[tail]
    )
    return chr(codepoint)


def _clipboard_action(sample: KeyboardSample) -> str | None:
    if not sample.command:
        return None
    if sample.key_code == KEY_CODE_C:
        return "copy"
    if sample.key_code == KEY_CODE_V:
        return "paste"
    return None


def _is_shortcut(sample: KeyboardSample) -> bool:
    return sample.command or sample.control or (sample.option and not sample.text)


def _modifiers(sample: KeyboardSample) -> list[str]:
    modifiers: list[str] = []
    if sample.command:
        modifiers.append("command")
    if sample.control:
        modifiers.append("control")
    if sample.option:
        modifiers.append("option")
    if sample.shift:
        modifiers.append("shift")
    return modifiers


def _enter_text(sample: KeyboardSample) -> str:
    if sample.text:
        return sample.text.replace("\r", "\n")
    return "\n"


def _delete_last_character(parts: list[str]) -> None:
    while parts:
        text = parts.pop()
        if len(text) > 1:
            parts.append(text[:-1])
        if text:
            return


def _detect_secure_input(detector: Callable[[], bool | None]) -> bool | None:
    try:
        return detector()
    except Exception:
        return None


def _event_unicode_text(
    event: object,
    get_unicode_string: Callable[..., Any],
) -> str:
    try:
        actual_length, chars = get_unicode_string(
            event, MAX_UNICODE_CHARS, None, None
        )
    except Exception:  # pragma: no cover - defensive PyObjC boundary
        return ""

    if not actual_length or not chars:
        return ""
    if isinstance(chars, str):
        return chars[:actual_length]

    return "".join(chr(code) for code in chars[:actual_length])
