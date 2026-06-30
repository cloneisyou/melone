import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from melone_service.config import ServiceConfig
from melone_service.main import (
    _create_collectors,
    _record_activity_state,
    get_process_state,
    kill_service,
    start_service,
    stop_service,
)
from melone_service.permissions import (
    PermissionSnapshot,
    RequiredPermissionsMissingError,
    StatusCheck,
)
from melone_service.pipeline.activity import ACTIVITY_STATE_CHANGED
from melone_service.pipeline.normalizer import normalize_event
from melone_service.store.db import connect, initialize_database
from melone_service.store.events import EventRepository


def test_start_service_spawns_background_process_and_stop_service_stops_it(tmp_path):
    config = _test_config(tmp_path)

    result = start_service(config, permission_checker=_granted_permissions)
    try:
        assert result.started is True
        assert result.pid is not None

        state = get_process_state(config)
        assert state.is_running is True
        assert state.pid == result.pid
        assert config.pid_file_path.is_file()
        assert config.lock_file_path.is_file()
        assert config.database_path.is_file()
    finally:
        stop_result = stop_service(config)

    assert stop_result.stopped is True
    assert _eventually_not_running(config)
    assert not config.pid_file_path.exists()


def test_start_service_blocks_duplicate_process(tmp_path):
    config = _test_config(tmp_path)

    first_result = start_service(config, permission_checker=_granted_permissions)
    try:
        second_result = start_service(config, permission_checker=_granted_permissions)
    finally:
        stop_service(config)

    assert first_result.started is True
    assert second_result.started is False
    assert second_result.pid == first_result.pid


def test_stop_service_when_not_running_is_noop(tmp_path):
    config = _test_config(tmp_path)

    result = stop_service(config)

    assert result.stopped is False
    assert result.pid is None
    assert result.was_running is False


def test_kill_service_sigkills_running_process_and_frees_locks(tmp_path):
    config = _test_config(tmp_path)

    result = start_service(config, permission_checker=_granted_permissions)
    assert result.started is True

    kill_result = kill_service(config)

    # kill_service only returns once the process is gone AND its lock is
    # released, so the state is immediately not-running (no extra wait needed)
    # and the next launch can't race a dying collector for the SQLite lock.
    assert kill_result.stopped is True
    assert kill_result.was_running is True
    assert get_process_state(config).is_running is False
    assert not config.pid_file_path.exists()


def test_kill_service_when_not_running_is_noop(tmp_path):
    config = _test_config(tmp_path)

    result = kill_service(config)

    assert result.stopped is False
    assert result.pid is None
    assert result.was_running is False


def test_service_registers_runtime_collectors(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    connection = connect(database_path)
    try:
        collectors = _create_collectors(
            EventRepository(connection),
            _test_config(tmp_path),
        )
    finally:
        connection.close()

    assert [collector.name for collector in collectors] == [
        "active_window",
        "current_asset",
        "keyboard",
        "mouse",
    ]


def test_service_registers_screenshot_collector_when_enabled(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    connection = connect(database_path)
    try:
        collectors = _create_collectors(
            EventRepository(connection),
            _test_config(tmp_path, screenshot_collector_enabled=True),
        )
    finally:
        connection.close()

    assert [collector.name for collector in collectors] == [
        "active_window",
        "current_asset",
        "keyboard",
        "mouse",
        "screenshot",
    ]


def test_record_activity_state_only_inserts_changed_states(tmp_path):
    config = _test_config(tmp_path)
    initialize_database(config.database_path)
    now = datetime(2026, 6, 9, 6, 0, 0, tzinfo=timezone.utc)

    connection = connect(config.database_path)
    try:
        repository = EventRepository(connection)
        repository.insert(
            normalize_event(
                "keyboard_burst",
                timestamp=now - timedelta(seconds=5),
                source="test",
                metadata={"key_count": 3},
            )
        )

        first = _record_activity_state(repository, config, now=now)
        second = _record_activity_state(
            repository,
            config,
            now=now + timedelta(seconds=5),
        )
        third = _record_activity_state(
            repository,
            config,
            now=now + timedelta(seconds=31),
        )

        events = repository.list(event_type=ACTIVITY_STATE_CHANGED)
    finally:
        connection.close()

    assert first is not None
    assert second is None
    assert third is not None
    assert [event.metadata["state"] for event in events] == ["active", "reading"]


def test_start_service_requires_permissions_before_spawning_process(tmp_path):
    config = _test_config(tmp_path)

    with pytest.raises(RequiredPermissionsMissingError) as exc_info:
        start_service(config, permission_checker=_missing_permissions)

    assert "accessibility" in str(exc_info.value)
    assert not get_process_state(config).is_running
    assert not config.pid_file_path.exists()


def test_get_process_state_marks_stale_pid_file(tmp_path):
    config = _test_config(tmp_path)
    config.pid_file_path.write_text("999999999\n", encoding="utf-8")

    state = get_process_state(config)

    assert state.is_running is False
    assert state.is_stale is True
    assert state.pid == 999999999


def _test_config(
    data_dir: Path,
    *,
    screenshot_collector_enabled: bool = False,
) -> ServiceConfig:
    return ServiceConfig(
        app_name="Melone",
        data_dir=data_dir,
        database_path=data_dir / "melone.sqlite",
        pid_file_path=data_dir / "melone.pid",
        lock_file_path=data_dir / "melone.lock",
        pause_flag_path=data_dir / "melone.paused",
        logs_dir=data_dir / "logs",
        screenshots_dir=data_dir / "screenshots",
        polling_interval_seconds=0.05,
        idle_timeout_seconds=300,
        screenshot_min_interval_seconds=10,
        screenshot_collector_enabled=screenshot_collector_enabled,
    )


def _granted_permissions() -> PermissionSnapshot:
    return PermissionSnapshot(
        permissions={
            "accessibility": StatusCheck("granted"),
            "screen_recording": StatusCheck("granted"),
        },
        collectors={
            "active_window": StatusCheck("enabled"),
            "keyboard": StatusCheck("enabled"),
            "mouse": StatusCheck("enabled"),
            "screenshot": StatusCheck("enabled"),
        },
    )


def _missing_permissions() -> PermissionSnapshot:
    return PermissionSnapshot(
        permissions={
            "accessibility": StatusCheck("denied"),
            "screen_recording": StatusCheck("granted"),
        },
        collectors={
            "active_window": StatusCheck("enabled"),
            "keyboard": StatusCheck("disabled"),
            "mouse": StatusCheck("disabled"),
            "screenshot": StatusCheck("enabled"),
        },
    )


def _eventually_not_running(config: ServiceConfig) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not get_process_state(config).is_running:
            return True
        time.sleep(0.05)
    return False
