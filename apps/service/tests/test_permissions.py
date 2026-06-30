import subprocess

import pytest

from melone_service import permissions
from melone_service.permissions import (
    PERMISSION_STATUS_CHANGED,
    PermissionSnapshot,
    RequiredPermissionsMissingError,
    StatusCheck,
    check_accessibility_permission,
    check_permission_status,
    record_permission_status,
    require_all_permissions,
    run_permission_probe,
)
from melone_service.store.db import connect, initialize_database
from melone_service.store.events import EventRepository


@pytest.fixture(autouse=True)
def _clear_fresh_cache(monkeypatch):
    # The subprocess recheck caches its result in a module global; reset it so
    # tests do not leak state into one another.
    monkeypatch.setattr(permissions, "_fresh_accessibility_cache", None)


def test_accessibility_granted_in_process_skips_subprocess(monkeypatch):
    monkeypatch.setattr(permissions, "_accessibility_in_process", lambda: StatusCheck("granted"))

    def _boom():  # pragma: no cover - must never run
        raise AssertionError("granted in-process must not spawn a probe")

    monkeypatch.setattr(permissions, "_recheck_accessibility_via_subprocess", _boom)
    assert check_accessibility_permission().status == "granted"


def test_accessibility_denied_in_process_rechecks_in_subprocess(monkeypatch):
    monkeypatch.setattr(permissions, "_accessibility_in_process", lambda: StatusCheck("denied"))
    monkeypatch.setattr(
        permissions, "_recheck_accessibility_via_subprocess", lambda: StatusCheck("granted")
    )
    # A stale "denied" from the long-lived daemon is corrected by the fresh probe.
    assert check_accessibility_permission().status == "granted"


def test_accessibility_falls_back_to_in_process_when_probe_fails(monkeypatch):
    monkeypatch.setattr(permissions, "_accessibility_in_process", lambda: StatusCheck("denied"))
    monkeypatch.setattr(permissions, "_recheck_accessibility_via_subprocess", lambda: None)
    assert check_accessibility_permission().status == "denied"


def test_probe_process_does_not_recurse(monkeypatch):
    # When we ARE the spawned probe (env marker set), report the raw value and
    # never spawn another probe.
    monkeypatch.setenv("MELONE_PERMISSION_PROBE", "1")
    monkeypatch.setattr(permissions, "_accessibility_in_process", lambda: StatusCheck("denied"))

    def _boom():  # pragma: no cover - must never run
        raise AssertionError("the probe must not spawn another probe")

    monkeypatch.setattr(permissions, "_recheck_accessibility_via_subprocess", _boom)
    assert check_accessibility_permission().status == "denied"


def test_recheck_parses_probe_json(monkeypatch):
    monkeypatch.setattr(permissions, "_permission_probe_command", lambda: ["probe"])

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout='{"accessibility": "granted"}', stderr="")

    monkeypatch.setattr(permissions.subprocess, "run", fake_run)
    result = permissions._recheck_accessibility_via_subprocess()
    assert result is not None and result.status == "granted"


def test_recheck_returns_none_on_subprocess_error(monkeypatch):
    monkeypatch.setattr(permissions, "_permission_probe_command", lambda: ["probe"])

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(permissions.subprocess, "run", fake_run)
    assert permissions._recheck_accessibility_via_subprocess() is None


def test_run_permission_probe_emits_json(monkeypatch, capsys):
    monkeypatch.setattr(permissions, "_accessibility_in_process", lambda: StatusCheck("granted"))
    assert run_permission_probe() == 0
    assert capsys.readouterr().out.strip() == '{"accessibility": "granted"}'


def test_check_permission_status_marks_non_macos_as_unsupported():
    snapshot = check_permission_status(platform_name="linux")

    assert snapshot.permissions["accessibility"].status == "unsupported"
    assert snapshot.permissions["screen_recording"].status == "unsupported"
    assert snapshot.collectors["current_asset"].status == "unsupported"
    assert snapshot.collectors["keyboard"].status == "unsupported"


def test_check_permission_status_maps_permissions_to_collectors():
    snapshot = check_permission_status(
        platform_name="darwin",
        accessibility_check=lambda: StatusCheck("granted"),
        screen_recording_check=lambda: StatusCheck("denied"),
    )

    assert snapshot.permissions["accessibility"].status == "granted"
    assert snapshot.permissions["screen_recording"].status == "denied"
    assert snapshot.collectors["keyboard"].status == "enabled"
    assert snapshot.collectors["mouse"].status == "enabled"
    assert snapshot.collectors["active_window"].status == "disabled"
    # current_asset은 accessibility + screen_recording 둘 다 필요(여기선 screen_recording denied).
    assert snapshot.collectors["current_asset"].status == "disabled"
    assert snapshot.collectors["screenshot"].status == "disabled"


def test_require_all_permissions_raises_when_any_required_permission_is_missing():
    snapshot = _snapshot(accessibility="denied", screen_recording="granted")

    with pytest.raises(RequiredPermissionsMissingError) as exc_info:
        require_all_permissions(snapshot)

    assert "accessibility" in str(exc_info.value)


def test_record_permission_status_saves_snapshot_metadata(tmp_path):
    repository, connection = _repository(tmp_path)
    try:
        event = record_permission_status(
            repository,
            _snapshot(accessibility="granted", screen_recording="denied"),
        )
        events = repository.list(event_type=PERMISSION_STATUS_CHANGED)
    finally:
        connection.close()

    assert event.type == PERMISSION_STATUS_CHANGED
    assert len(events) == 1
    assert events[0].source == "permission_checker"
    assert events[0].metadata["missing_required_permissions"] == ["screen_recording"]


def _repository(tmp_path):
    database_path = tmp_path / "melone.sqlite"
    initialize_database(database_path)
    connection = connect(database_path)
    return EventRepository(connection), connection


def _snapshot(
    *,
    accessibility: str,
    screen_recording: str,
) -> PermissionSnapshot:
    return PermissionSnapshot(
        permissions={
            "accessibility": StatusCheck(accessibility),
            "screen_recording": StatusCheck(screen_recording),
        },
        collectors={
            "active_window": StatusCheck("enabled"),
            "keyboard": StatusCheck("enabled"),
            "mouse": StatusCheck("enabled"),
            "screenshot": StatusCheck("enabled"),
        },
    )
