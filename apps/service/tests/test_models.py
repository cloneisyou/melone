from datetime import datetime, timedelta, timezone

from melone_service.models import create_event_id, utc_timestamp


def test_create_event_id_uses_event_prefix():
    event_id = create_event_id()

    assert event_id.startswith("evt_")
    assert len(event_id) > len("evt_")


def test_utc_timestamp_formats_datetime_as_utc_milliseconds():
    timestamp = datetime(
        2026,
        6,
        9,
        15,
        0,
        0,
        123456,
        tzinfo=timezone(timedelta(hours=9)),
    )

    assert utc_timestamp(timestamp) == "2026-06-09T06:00:00.123Z"


def test_utc_timestamp_treats_naive_datetime_as_utc():
    timestamp = datetime(2026, 6, 9, 6, 0, 0, 123456)

    assert utc_timestamp(timestamp) == "2026-06-09T06:00:00.123Z"
