import binascii
import hashlib
import struct
import zlib
from pathlib import Path

from melone_service.collectors.screenshot import (
    CapturedScreenshot,
    DenylistSensitiveScreenPolicy,
    ScreenshotCollector,
)
from melone_service.config import ServiceConfig
from melone_service.main import _create_collectors, _poll_collectors
from melone_service.pipeline.normalizer import normalize_event
from melone_service.store.db import connect, initialize_database
from melone_service.store.events import EventRepository
from melone_service.store.screen import ScreenRepository


NOW = "2026-06-09T06:00:00.000Z"
LATER = "2026-06-09T06:00:11.000Z"


def test_fake_png_capture_creates_session_scoped_file_and_frame_row(tmp_path):
    connection = _connection(tmp_path)
    try:
        repository = ScreenRepository(connection)
        session = _create_session(repository)
        png_bytes = _png_bytes(2, 1, text=b"first")
        capture = _FakeCapture(
            [
                CapturedScreenshot(
                    png_bytes=png_bytes,
                    width=2,
                    height=1,
                    perceptual_hash="phash-1",
                )
            ]
        )
        collector = _collector(
            repository,
            tmp_path,
            capture_api=capture,
            frame_ids=["screen_frame_1"],
            timestamps=[NOW],
        )

        frame = collector.capture_latest_frame()

        assert frame is not None
        assert frame.id == "screen_frame_1"
        assert frame.session_id == session.id
        assert frame.captured_at == NOW
        assert frame.sha256 == hashlib.sha256(png_bytes).hexdigest()
        assert frame.width == 2
        assert frame.height == 1
        assert frame.perceptual_hash == "phash-1"

        image_path = Path(frame.image_path)
        assert image_path == (
            tmp_path
            / "screenshots"
            / session.id
            / f"{NOW}-screen_frame_1.png"
        )
        assert image_path.read_bytes() == png_bytes
        assert repository.count_frames() == 1
    finally:
        connection.close()


def test_throttle_prevents_capture_before_minimum_interval(tmp_path):
    connection = _connection(tmp_path)
    try:
        repository = ScreenRepository(connection)
        _create_session(repository)
        clock = _MonotonicClock(100.0)
        capture = _FakeCapture(
            [
                CapturedScreenshot(_png_bytes(1, 1, text=b"first"), 1, 1),
                CapturedScreenshot(_png_bytes(1, 1, text=b"second"), 1, 1),
            ]
        )
        collector = _collector(
            repository,
            tmp_path,
            capture_api=capture,
            monotonic_clock=clock,
            frame_ids=["screen_frame_1", "screen_frame_2"],
            timestamps=[NOW, LATER],
            min_interval_seconds=10,
        )

        collector.poll()
        clock.advance(9.9)
        collector.poll()
        clock.advance(0.2)
        collector.poll()

        assert capture.calls == 2
        assert repository.count_frames() == 2
    finally:
        connection.close()


def test_new_session_bypasses_throttle_for_guaranteed_frame(tmp_path):
    connection = _connection(tmp_path)
    try:
        repository = ScreenRepository(connection)
        _create_session(repository)
        clock = _MonotonicClock(100.0)
        capture = _FakeCapture(
            [
                CapturedScreenshot(_png_bytes(1, 1, text=b"first"), 1, 1),
                CapturedScreenshot(_png_bytes(1, 1, text=b"second"), 1, 1),
            ]
        )
        collector = _collector(
            repository,
            tmp_path,
            capture_api=capture,
            monotonic_clock=clock,
            frame_ids=["screen_frame_1", "screen_frame_2"],
            timestamps=[NOW, LATER],
            min_interval_seconds=10,
        )

        collector.poll()

        # A second session opens well within the throttle window. It still gets a
        # frame because a not-yet-captured session bypasses the global throttle,
        # so every scene gets a keyframe instead of showing "no screenshot".
        repository.create_session(
            session_id="screen_session_2",
            source_key="url:https://example.com/other",
            retrieval_locator="url:https://example.com/other",
            app_name="Google Chrome",
            bundle_id="com.google.Chrome",
            window_title="Other",
            url="https://example.com/other",
            started_at=LATER,
            now=LATER,
        )
        clock.advance(2.0)
        collector.poll()

        assert capture.calls == 2
        assert len(repository.list_session_frames("screen_session_1")) == 1
        assert len(repository.list_session_frames("screen_session_2")) == 1
    finally:
        connection.close()


def test_duplicate_sha256_frames_are_not_stored_twice(tmp_path):
    connection = _connection(tmp_path)
    try:
        repository = ScreenRepository(connection)
        session = _create_session(repository)
        clock = _MonotonicClock(100.0)
        png_bytes = _png_bytes(1, 1, text=b"duplicate")
        capture = _FakeCapture(
            [
                CapturedScreenshot(png_bytes, 1, 1),
                CapturedScreenshot(png_bytes, 1, 1),
            ]
        )
        collector = _collector(
            repository,
            tmp_path,
            capture_api=capture,
            monotonic_clock=clock,
            frame_ids=["screen_frame_1", "screen_frame_2"],
            timestamps=[NOW, LATER],
            min_interval_seconds=10,
        )

        collector.poll()
        clock.advance(11.0)
        collector.poll()

        frame_dir = tmp_path / "screenshots" / session.id
        assert capture.calls == 2
        assert repository.count_frames() == 1
        assert [path.name for path in frame_dir.iterdir()] == [
            f"{NOW}-screen_frame_1.png"
        ]
    finally:
        connection.close()


def test_non_darwin_platform_skips_without_capture_or_rows(tmp_path):
    connection = _connection(tmp_path)
    try:
        repository = ScreenRepository(connection)
        _create_session(repository)
        capture = _FakeCapture([CapturedScreenshot(_png_bytes(1, 1), 1, 1)])
        collector = _collector(
            repository,
            tmp_path,
            capture_api=capture,
            platform_name="linux",
        )

        assert collector.poll() == []
        assert capture.calls == 0
        assert repository.count_frames() == 0
    finally:
        connection.close()


def test_sensitive_screen_policy_skips_before_capture(tmp_path):
    connection = _connection(tmp_path)
    try:
        repository = ScreenRepository(connection)
        _create_session(
            repository,
            app_name="Password Manager",
            bundle_id="com.example.passwords",
        )
        capture = _FakeCapture([CapturedScreenshot(_png_bytes(1, 1), 1, 1)])
        collector = _collector(
            repository,
            tmp_path,
            capture_api=capture,
            sensitive_policy=DenylistSensitiveScreenPolicy(
                app_names=("password manager",),
                bundle_ids=("com.example.passwords",),
            ),
        )

        collector.poll()

        assert capture.calls == 0
        assert repository.count_frames() == 0
    finally:
        connection.close()


def test_capture_failure_does_not_stop_later_collectors(tmp_path):
    connection = _connection(tmp_path)
    try:
        screen_repository = ScreenRepository(connection)
        event_repository = EventRepository(connection)
        _create_session(screen_repository)
        screenshot = _collector(
            screen_repository,
            tmp_path,
            capture_api=_FailingCapture(),
        )
        event_collector = _EventCollector()

        _poll_collectors([screenshot, event_collector], event_repository)

        assert screen_repository.count_frames() == 0
        assert event_repository.latest(event_type="test_event") is not None
    finally:
        connection.close()


def test_disabled_screen_text_config_omits_screenshot_collector(tmp_path):
    connection = _connection(tmp_path)
    try:
        event_repository = EventRepository(connection)
        config = _service_config(tmp_path, screen_text_enabled=False)

        collectors = _create_collectors(event_repository, config)

        assert not any(
            isinstance(collector, ScreenshotCollector) for collector in collectors
        )
    finally:
        connection.close()


def _connection(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    return connect(database_path)


def _create_session(
    repository,
    *,
    app_name="Google Chrome",
    bundle_id="com.google.Chrome",
):
    return repository.create_session(
        session_id="screen_session_1",
        source_key="url:https://example.com/docs",
        retrieval_locator="url:https://example.com/docs",
        app_name=app_name,
        bundle_id=bundle_id,
        window_title="Docs",
        url="https://example.com/docs",
        started_at=NOW,
        now=NOW,
    )


def _collector(
    repository,
    tmp_path,
    *,
    capture_api,
    platform_name="darwin",
    sensitive_policy=None,
    monotonic_clock=None,
    frame_ids=None,
    timestamps=None,
    min_interval_seconds=10,
):
    frame_ids = list(frame_ids or ["screen_frame_1"])
    timestamps = list(timestamps or [NOW])
    return ScreenshotCollector(
        screen_repository=repository,
        screenshots_dir=tmp_path / "screenshots",
        min_interval_seconds=min_interval_seconds,
        capture_api=capture_api,
        sensitive_policy=sensitive_policy,
        platform_name=platform_name,
        monotonic_clock=monotonic_clock or _MonotonicClock(0.0),
        frame_id_factory=lambda: frame_ids.pop(0),
        timestamp_factory=lambda: timestamps.pop(0),
    )


def _service_config(tmp_path, *, screen_text_enabled):
    return ServiceConfig(
        app_name="Melone",
        data_dir=tmp_path,
        database_path=tmp_path / "melone.sqlite",
        pid_file_path=tmp_path / "melone.pid",
        lock_file_path=tmp_path / "melone.lock",
        pause_flag_path=tmp_path / "melone.paused",
        logs_dir=tmp_path / "logs",
        screenshots_dir=tmp_path / "screenshots",
        settings_path=tmp_path / "settings.json",
        screenshot_collector_enabled=screen_text_enabled,
        screen_text_search_enabled=screen_text_enabled,
    )


class _FakeCapture:
    def __init__(self, captures):
        self.captures = list(captures)
        self.calls = 0

    def capture_png(self):
        self.calls += 1
        if len(self.captures) == 1:
            return self.captures[0]
        return self.captures.pop(0)


class _FailingCapture:
    def capture_png(self):
        raise RuntimeError("screen recording permission denied")


class _EventCollector:
    name = "event"

    def poll(self):
        return [normalize_event("test_event", source="test")]


class _MonotonicClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _png_bytes(width=1, height=1, *, text=b""):
    row = b"\x00" + (b"\x00\x00\x00\x00" * width)
    image_data = row * height
    chunks = [
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(image_data)),
    ]
    if text:
        chunks.append(_png_chunk(b"tEXt", text))
    chunks.append(_png_chunk(b"IEND", b""))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _png_chunk(kind, data):
    crc = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)
