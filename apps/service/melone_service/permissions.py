from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

from .models import NormalizedEvent
from .pipeline.normalizer import normalize_event
from .store.events import EventRepository


PermissionState = Literal["granted", "denied", "unsupported"]
CollectorState = Literal["enabled", "disabled", "unsupported"]

PERMISSION_STATUS_CHANGED = "permission_status_changed"
REQUIRED_PERMISSION_NAMES = ("accessibility", "screen_recording")

# AXIsProcessTrusted caches its verdict for the life of the calling process, so
# the long-lived RPC daemon keeps reporting a stale "denied" after the user grants
# Accessibility. When the in-process check is denied we re-verify in a freshly
# spawned process (it inherits the app's current grant via responsible-process
# attribution and has no cache). These constants drive that probe.
_PERMISSION_PROBE_ENV = "MELONE_PERMISSION_PROBE"
_PERMISSION_PROBE_SUBCOMMAND = "permission-probe"
# Cache the fresh result briefly so a fast status poll cannot spawn a probe every
# tick while a grant is still pending.
_FRESH_RECHECK_TTL_SECONDS = 2.0
_PROBE_TIMEOUT_SECONDS = 10.0
_fresh_accessibility_cache: tuple[float, StatusCheck] | None = None
COLLECTOR_NAMES = (
    "active_window",
    "current_asset",
    "keyboard",
    "mouse",
    "screenshot",
)

APPLICATION_SERVICES_PATH = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)
CORE_GRAPHICS_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"


@dataclass(frozen=True)
class StatusCheck:
    # 단일 권한이나 수집기의 상태와 사용자에게 보여줄 상세 설명을 함께 담습니다.
    status: PermissionState | CollectorState
    detail: str | None = None

    @property
    def is_granted(self) -> bool:
        # 필수 권한 판단에서는 granted만 통과 상태로 봅니다.
        return self.status == "granted"

    def to_metadata(self) -> dict[str, str]:
        # 권한 상태 이벤트에 들어갈 JSON metadata 형태로 변환합니다.
        metadata = {"status": self.status}
        if self.detail:
            metadata["detail"] = self.detail
        return metadata


@dataclass(frozen=True)
class PermissionSnapshot:
    # 현재 권한과 그 권한에 의존하는 수집기 상태를 한 번에 표현합니다.
    permissions: Mapping[str, StatusCheck]
    collectors: Mapping[str, StatusCheck]

    def iter_permissions(self) -> Iterator[tuple[str, StatusCheck]]:
        # CLI 출력과 metadata가 항상 같은 순서로 보이도록 정렬합니다.
        yield from _ordered_items(self.permissions, REQUIRED_PERMISSION_NAMES)

    def iter_collectors(self) -> Iterator[tuple[str, StatusCheck]]:
        # 수집기 상태도 사람이 읽기 쉬운 고정 순서로 순회합니다.
        yield from _ordered_items(self.collectors, COLLECTOR_NAMES)

    def missing_required_permissions(self) -> list[str]:
        # 서비스 시작을 막아야 하는 필수 권한만 골라냅니다.
        missing = []
        for name in REQUIRED_PERMISSION_NAMES:
            check = self.permissions[name]
            if not check.is_granted:
                missing.append(name)
        return missing

    def to_metadata(self) -> dict[str, object]:
        # 권한 점검 결과를 NormalizedEvent metadata에 바로 넣을 수 있게 만듭니다.
        return {
            "permissions": {
                name: check.to_metadata()
                for name, check in self.iter_permissions()
            },
            "collectors": {
                name: check.to_metadata()
                for name, check in self.iter_collectors()
            },
            "missing_required_permissions": self.missing_required_permissions(),
        }


class RequiredPermissionsMissingError(RuntimeError):
    # 서비스 실행 전에 필수 권한이 없을 때 snapshot과 함께 던지는 예외입니다.
    def __init__(self, snapshot: PermissionSnapshot) -> None:
        self.snapshot = snapshot
        missing = ", ".join(snapshot.missing_required_permissions())
        super().__init__(f"required permissions are not granted: {missing}")


def check_permission_status(
    *,
    platform_name: str | None = None,
    accessibility_check: Callable[[], StatusCheck] | None = None,
    screen_recording_check: Callable[[], StatusCheck] | None = None,
) -> PermissionSnapshot:
    # macOS 권한을 확인하고 각 수집기가 실제로 켜질 수 있는지 계산합니다.
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name != "darwin":
        unsupported = StatusCheck("unsupported", "macOS only")
        return PermissionSnapshot(
            permissions={
                name: unsupported for name in REQUIRED_PERMISSION_NAMES
            },
            collectors={name: unsupported for name in COLLECTOR_NAMES},
        )

    accessibility = (
        accessibility_check or check_accessibility_permission
    )()
    screen_recording = (
        screen_recording_check or check_screen_recording_permission
    )()

    return PermissionSnapshot(
        permissions={
            "accessibility": accessibility,
            "screen_recording": screen_recording,
        },
        collectors={
            "active_window": _collector_status(screen_recording),
            # 전면 앱 감지는 screen_recording에, 문서/Claude 데스크탑 AX는 accessibility에
            # 의존한다. 둘 중 하나라도 없으면 일부가 동작하지 않으므로 보수적으로
            # 둘 다 granted일 때만 enabled로 본다.
            "current_asset": _collector_status(
                _require_both(accessibility, screen_recording)
            ),
            "keyboard": _collector_status(accessibility),
            "mouse": _collector_status(accessibility),
            "screenshot": _collector_status(screen_recording),
        },
    )


def require_all_permissions(snapshot: PermissionSnapshot) -> None:
    # 필수 권한이 없으면 호출자가 서비스 시작을 중단할 수 있게 예외를 던집니다.
    if snapshot.missing_required_permissions():
        raise RequiredPermissionsMissingError(snapshot)


def _accessibility_in_process() -> StatusCheck:
    # 현재 프로세스 기준 Accessibility 권한(캐시될 수 있음)을 확인합니다.
    return _call_boolean_framework_function(
        APPLICATION_SERVICES_PATH,
        "AXIsProcessTrusted",
        "Accessibility permission is not granted",
    )


def check_accessibility_permission() -> StatusCheck:
    # 키보드/마우스/활성 앱 감지에 필요한 Accessibility 권한을 확인합니다.
    in_process = _accessibility_in_process()
    if in_process.is_granted:
        return in_process
    # 우리가 바로 그 fresh probe 프로세스라면 재귀를 막고 raw 값을 그대로 보고합니다.
    if os.environ.get(_PERMISSION_PROBE_ENV) == "1":
        return in_process
    # in-process가 denied면 캐시된 stale 값일 수 있으니 새 프로세스에서 다시 확인합니다.
    fresh = _recheck_accessibility_via_subprocess()
    return fresh if fresh is not None else in_process


def _recheck_accessibility_via_subprocess() -> StatusCheck | None:
    # 새 프로세스를 띄워 AXIsProcessTrusted 캐시를 우회합니다. 실패하면 None을 돌려
    # 호출자가 in-process 값으로 폴백하게 합니다.
    global _fresh_accessibility_cache
    now = time.monotonic()
    cached = _fresh_accessibility_cache
    if cached is not None and now - cached[0] < _FRESH_RECHECK_TTL_SECONDS:
        return cached[1]

    command = _permission_probe_command()
    if command is None:
        return None

    env = os.environ.copy()
    env[_PERMISSION_PROBE_ENV] = "1"
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    status = _parse_probe_status(completed.stdout)
    if status is None:
        return None
    result = (
        StatusCheck("granted")
        if status == "granted"
        else StatusCheck("denied", "Accessibility permission is not granted")
    )
    _fresh_accessibility_cache = (now, result)
    return result


def _parse_probe_status(stdout: str) -> str | None:
    # probe가 출력한 JSON에서 accessibility 상태 문자열만 안전하게 뽑아냅니다.
    try:
        payload = json.loads(stdout.strip() or "{}")
    except ValueError:
        return None
    status = payload.get("accessibility") if isinstance(payload, dict) else None
    return status if isinstance(status, str) else None


def _permission_probe_command() -> list[str] | None:
    # collector를 띄우는 _service_command과 같은 규칙입니다. frozen 바이너리는
    # subcommand로(rpc/__main__.py: "permission-probe"), 개발 인터프리터는 -m
    # 디스패처로 같은 진입점을 새 프로세스에서 다시 부릅니다.
    if not sys.executable:
        return None
    if getattr(sys, "frozen", False):
        return [sys.executable, _PERMISSION_PROBE_SUBCOMMAND]
    return [sys.executable, "-m", "melone_service.rpc", _PERMISSION_PROBE_SUBCOMMAND]


def run_permission_probe() -> int:
    # 새로 띄워진 프로세스에서 Accessibility 권한만 즉시 확인해 JSON으로 출력합니다.
    # (오래 떠 있는 데몬의 stale 캐시를 우회하기 위한 일회용 진입점)
    status = _accessibility_in_process().status
    print(json.dumps({"accessibility": status}))
    return 0


def check_screen_recording_permission() -> StatusCheck:
    # 화면 정보와 스크린샷 수집에 필요한 Screen Recording 권한을 확인합니다.
    return _call_boolean_framework_function(
        CORE_GRAPHICS_PATH,
        "CGPreflightScreenCaptureAccess",
        "Screen Recording permission is not granted",
    )


def record_permission_status(
    repository: EventRepository,
    snapshot: PermissionSnapshot,
) -> NormalizedEvent:
    # 권한 점검 결과를 이벤트 타임라인에 남겨 이후 상태 변화를 추적합니다.
    event = create_permission_status_event(snapshot)
    repository.insert(event)
    return event


def create_permission_status_event(snapshot: PermissionSnapshot) -> NormalizedEvent:
    # 권한 snapshot을 표준 permission_status_changed 이벤트로 정규화합니다.
    return normalize_event(
        PERMISSION_STATUS_CHANGED,
        source="permission_checker",
        metadata=snapshot.to_metadata(),
    )


def _collector_status(permission: StatusCheck) -> StatusCheck:
    # 수집기는 의존 권한이 granted일 때만 enabled로 표시합니다.
    if permission.status == "granted":
        return StatusCheck("enabled")
    if permission.status == "unsupported":
        return StatusCheck("unsupported", permission.detail)
    return StatusCheck("disabled", permission.detail)


def _require_both(first: StatusCheck, second: StatusCheck) -> StatusCheck:
    # 두 권한을 모두 요구하는 수집기를 위해 하나의 상태로 합칩니다.
    if first.status == "unsupported" or second.status == "unsupported":
        return StatusCheck("unsupported", first.detail or second.detail)
    if first.is_granted and second.is_granted:
        return StatusCheck("granted")
    denied = first if not first.is_granted else second
    return StatusCheck("denied", denied.detail)


def _call_boolean_framework_function(
    framework_path: str,
    symbol_name: str,
    denied_detail: str,
) -> StatusCheck:
    # macOS 프레임워크의 bool 반환 함수를 호출해 권한 상태로 변환합니다.
    try:
        framework = ctypes.CDLL(framework_path)
        function = getattr(framework, symbol_name)
        function.argtypes = []
        function.restype = ctypes.c_bool
    except (AttributeError, OSError):
        return StatusCheck("denied", f"{symbol_name} check failed")

    is_granted = function()
    if is_granted:
        return StatusCheck("granted")
    return StatusCheck("denied", denied_detail)


def _ordered_items(
    values: Mapping[str, StatusCheck],
    preferred_order: tuple[str, ...],
) -> Iterator[tuple[str, StatusCheck]]:
    # 알려진 항목을 먼저 내보내고, 새 항목은 이름순으로 뒤에 붙입니다.
    yielded = set()
    for name in preferred_order:
        if name in values:
            yielded.add(name)
            yield name, values[name]

    for name in sorted(values):
        if name not in yielded:
            yield name, values[name]
