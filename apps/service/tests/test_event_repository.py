from datetime import datetime, timezone

from melone_service.pipeline.normalizer import normalize_event
from melone_service.store.db import connect, initialize_database
from melone_service.store.events import EventRepository


def test_event_repository_inserts_and_lists_events(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    connection = connect(database_path)
    try:
        repository = EventRepository(connection)
        event = normalize_event(
            "active_app_changed",
            event_id="evt_repository_test",
            timestamp=datetime(2026, 6, 9, 6, 0, 0, tzinfo=timezone.utc),
            app={"name": "Cursor", "bundle_id": "com.todesktop.230313mzl4w4u92"},
            window={"title": "melone"},
            source="test",
            metadata={"reason": "unit-test"},
        )

        repository.insert(event)
        events = repository.list()
    finally:
        connection.close()

    assert len(events) == 1
    assert events[0].id == "evt_repository_test"
    assert events[0].timestamp == "2026-06-09T06:00:00.000Z"
    assert events[0].type == "active_app_changed"
    assert events[0].app_name == "Cursor"
    assert events[0].bundle_id == "com.todesktop.230313mzl4w4u92"
    assert events[0].window_title == "melone"
    assert events[0].source == "test"
    assert events[0].metadata == {"reason": "unit-test"}


def test_event_repository_filters_by_since_and_type(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    connection = connect(database_path)
    try:
        repository = EventRepository(connection)
        repository.insert(
            normalize_event(
                "active_app_changed",
                event_id="evt_old",
                timestamp=datetime(2026, 6, 9, 5, 0, 0, tzinfo=timezone.utc),
            )
        )
        repository.insert(
            normalize_event(
                "keyboard_burst",
                event_id="evt_wrong_type",
                timestamp=datetime(2026, 6, 9, 6, 0, 0, tzinfo=timezone.utc),
            )
        )
        repository.insert(
            normalize_event(
                "active_app_changed",
                event_id="evt_match",
                timestamp=datetime(2026, 6, 9, 6, 1, 0, tzinfo=timezone.utc),
            )
        )

        events = repository.list(
            since="2026-06-09T06:00:00.000Z",
            event_type="active_app_changed",
        )
    finally:
        connection.close()

    assert [event.id for event in events] == ["evt_match"]


def test_event_repository_lists_and_finds_latest_by_multiple_types(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    connection = connect(database_path)
    try:
        repository = EventRepository(connection)
        repository.insert(
            normalize_event(
                "active_app_changed",
                event_id="evt_context",
                timestamp=datetime(2026, 6, 9, 6, 0, 0, tzinfo=timezone.utc),
            )
        )
        repository.insert(
            normalize_event(
                "keyboard_burst",
                event_id="evt_activity",
                timestamp=datetime(2026, 6, 9, 6, 1, 0, tzinfo=timezone.utc),
                metadata={"key_count": 2},
            )
        )
        repository.insert(
            normalize_event(
                "permission_status_changed",
                event_id="evt_other",
                timestamp=datetime(2026, 6, 9, 6, 2, 0, tzinfo=timezone.utc),
            )
        )

        events = repository.list_by_types(
            ("active_app_changed", "keyboard_burst"),
            since="2026-06-09T06:00:30.000Z",
        )
        latest = repository.latest_by_types(
            ("active_app_changed", "keyboard_burst")
        )
    finally:
        connection.close()

    assert [event.id for event in events] == ["evt_activity"]
    assert latest is not None
    assert latest.id == "evt_activity"


def test_event_repository_lists_recent_by_types_in_chronological_order(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)

    connection = connect(database_path)
    try:
        repository = EventRepository(connection)
        for index in range(4):
            repository.insert(
                normalize_event(
                    "active_app_snapshot",
                    event_id=f"evt_context_{index}",
                    timestamp=datetime(
                        2026,
                        6,
                        9,
                        6,
                        index,
                        0,
                        tzinfo=timezone.utc,
                    ),
                )
            )

        repository.insert(
            normalize_event(
                "permission_status_changed",
                event_id="evt_other",
                timestamp=datetime(2026, 6, 9, 6, 4, 0, tzinfo=timezone.utc),
            )
        )

        events = repository.list_recent_by_types(
            ("active_app_snapshot",),
            limit=2,
        )
    finally:
        connection.close()

    assert [event.id for event in events] == ["evt_context_2", "evt_context_3"]
