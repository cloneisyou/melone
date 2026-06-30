from datetime import datetime, timezone

import pytest

from melone_service.models import AppContext, WindowContext
from melone_service.pipeline.normalizer import normalize_event


def test_normalize_event_keeps_raw_url():
    event = normalize_event(
        "browser_url_changed",
        url="https://example.com/search?q=secret#section",
    )

    assert event.url == "https://example.com/search?q=secret#section"


def test_normalize_event_preserves_complex_query_and_fragment():
    raw_url = (
        "https://example.com/callback?"
        "redirect=https%3A%2F%2Fother.test%2Fpath%3Fx%3D1"
        "&token=a+b%2Fc#section/one?debug=true"
    )

    event = normalize_event("browser_url_changed", url=raw_url)

    assert event.url == raw_url


def test_normalize_event_trims_url_outer_whitespace():
    event = normalize_event(
        "browser_url_changed",
        url="  example.com/docs?q=secret  ",
    )

    assert event.url == "example.com/docs?q=secret"


def test_normalize_event_treats_blank_url_as_missing():
    assert normalize_event("browser_url_changed").url is None
    assert normalize_event("browser_url_changed", url="  ").url is None


def test_normalize_event_builds_common_event_schema():
    event = normalize_event(
        "browser_url_changed",
        event_id="evt_test",
        timestamp=datetime(2026, 6, 9, 6, 0, 0, 123456, tzinfo=timezone.utc),
        app={"name": "Safari", "bundle_id": "com.apple.Safari", "pid": "42"},
        window={"title": "Search Results", "display_id": "1"},
        url="https://example.com/search?q=secret#section",
        source="macos",
        metadata={"tab_id": "abc"},
    )

    assert event.id == "evt_test"
    assert event.timestamp == "2026-06-09T06:00:00.123Z"
    assert event.type == "browser_url_changed"
    assert event.app.name == "Safari"
    assert event.app.bundle_id == "com.apple.Safari"
    assert event.app.pid == 42
    assert event.window.title == "Search Results"
    assert event.window.display_id == 1
    assert event.url == "https://example.com/search?q=secret#section"
    assert event.source == "macos"
    assert event.metadata == {"tab_id": "abc"}


def test_normalize_event_treats_blank_optional_ids_as_missing():
    event = normalize_event(
        "active_app_changed",
        app={"pid": " "},
        window={"display_id": ""},
    )

    assert event.pid is None
    assert event.window.display_id is None


def test_normalize_event_accepts_context_models():
    event = normalize_event(
        "active_app_changed",
        app=AppContext(name="Cursor", bundle_id="com.todesktop.230313mzl4w4u92"),
        window=WindowContext(title="melone"),
    )

    assert event.app_name == "Cursor"
    assert event.bundle_id == "com.todesktop.230313mzl4w4u92"
    assert event.window_title == "melone"
    assert event.url is None
    assert event.metadata == {}


def test_normalize_event_copies_metadata():
    metadata = {"count": 1}

    event = normalize_event("keyboard_burst", metadata=metadata)
    metadata["count"] = 2

    assert event.metadata == {"count": 1}


def test_normalize_event_requires_type_and_source():
    with pytest.raises(ValueError, match="event_type"):
        normalize_event(" ")

    with pytest.raises(ValueError, match="source"):
        normalize_event("active_app_changed", source=" ")
