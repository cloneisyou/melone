from __future__ import annotations

import fcntl
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import TextIO

from .asset import build_default_resolver
from .collectors.active_window import ActiveWindowCollector
from .collectors.base import Collector
from .collectors.current_asset import CurrentAssetCollector
from .collectors.keyboard import KeyboardCollector
from .collectors.mouse import MouseCollector
from .collectors.screenshot import ScreenshotCollector
from .config import (
    ACTIVITY_EVENT_LIMIT,
    MELONE_SCREENSHOT_COLLECTOR_ENABLED_ENV,
    MELONE_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD_ENV,
    MELONE_SCREEN_SEARCH_MAX_JOBS_PER_TICK_ENV,
    MELONE_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS_ENV,
    MELONE_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD_ENV,
    MELONE_SCREEN_SEARCH_WORKERS_ENABLED_ENV,
    MELONE_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS_ENV,
    MELONE_SCREEN_TEXT_RETAIN_SCREENSHOTS_ENV,
    MELONE_EMBEDDING_BATCH_SIZE_ENV,
    MELONE_EMBEDDING_DIMENSION_ENV,
    MELONE_EMBEDDING_MODEL_ENV,
    MELONE_SEMANTIC_SEARCH_CANDIDATE_LIMIT_ENV,
    MELONE_SEMANTIC_SEARCH_ENABLED_ENV,
    MELONE_OCR_ENDPOINT_ENV,
    MELONE_OCR_MAX_TOKENS_ENV,
    MELONE_OCR_MODEL_ENV,
    MELONE_OCR_PROVIDER_ENV,
    MELONE_OCR_TIMEOUT_SECONDS_ENV,
    ServiceConfig,
    is_screen_search_workers_enabled,
    is_screenshot_collection_enabled,
    load_config,
)
from .recording import clear_paused, is_paused
from .models import NormalizedEvent, utc_now, utc_timestamp
from .permissions import (
    PermissionSnapshot,
    RequiredPermissionsMissingError,
    check_permission_status,
    record_permission_status,
    require_all_permissions,
)
from .pipeline.activity import (
    ACTIVITY_EVENT_TYPES,
    ACTIVITY_STATE_CHANGED,
    ActivityThresholds,
    activity_state_changed_event,
    activity_state_from_event,
    classify_activity_state,
)
from .pipeline.screen_search_scheduler import (
    capture_policy_for_backlog,
    count_screen_search_backlog,
    run_screen_search_workers_once,
)
from .store.db import connect, initialize_database
from .store.events import EventRepository
from .store.ocr_jobs import OcrJobRepository
from .store.screen import ScreenRepository


logger = logging.getLogger(__name__)
SERVICE_LOG_NAME = "service.log"
START_TIMEOUT_SECONDS = 5.0
STOP_TIMEOUT_SECONDS = 5.0
KILL_TIMEOUT_SECONDS = 2.0 # lock 해제 확인
PERMISSIONS_CHECKED_ENV = "MELONE_PERMISSIONS_CHECKED"


class ServiceAlreadyRunningError(RuntimeError):
    # lock 파일이 잡혀 있어 같은 서비스를 중복 실행할 수 없을 때 사용합니다.
    pass


class ServiceStartError(RuntimeError):
    # 백그라운드 서비스가 제한 시간 안에 정상 기동하지 못했을 때 사용합니다.
    pass


@dataclass(frozen=True)
class ProcessState:
    # PID 파일, lock 파일, 실제 프로세스 상태를 합쳐 서비스 상태를 표현합니다.
    is_running: bool
    pid: int | None
    pid_file_path: Path
    lock_file_path: Path
    is_stale: bool = False


@dataclass(frozen=True)
class StartResult:
    # start 명령이 새 프로세스를 띄웠는지와 대상 pid를 함께 반환합니다.
    started: bool
    pid: int | None


@dataclass(frozen=True)
class StopResult:
    # stop 명령의 성공 여부와 원래 실행 중이었는지를 구분해 CLI 메시지를 만듭니다.
    stopped: bool
    pid: int | None
    was_running: bool


class ProcessLock:
    # 파일 lock으로 한 번에 하나의 Melone 서비스 프로세스만 실행되게 합니다.
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: TextIO | None = None

    def __enter__(self) -> ProcessLock:
        # lock 획득에 실패하면 다른 프로세스가 이미 실행 중인 것으로 간주합니다.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file.close()
            self.file = None
            raise ServiceAlreadyRunningError(
                "Melone is already running"
            ) from exc

        return self

    def __exit__(self, *exc_info: object) -> None:
        # 서비스 루프가 끝나면 lock 파일 핸들을 반드시 정리합니다.
        if self.file is None:
            return

        fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()
        self.file = None


def run_service(
    config: ServiceConfig | None = None,
    *,
    stop_event: threading.Event | None = None,
    permission_checker: Callable[[], PermissionSnapshot] | None = None,
) -> int:
    # 실제 수집 루프를 실행하는 진입점으로 foreground와 daemon 프로세스가 공유합니다.
    config = load_config() if config is None else config
    stop_event = threading.Event() if stop_event is None else stop_event

    initialize_database(config.database_path)
    if os.environ.get(PERMISSIONS_CHECKED_ENV) != "1":
        _record_and_require_permissions(config, permission_checker)

    with ProcessLock(config.lock_file_path):
        pid = os.getpid()
        _write_pid_file(config.pid_file_path, pid)
        try:
            with _shutdown_signals(stop_event):
                _run_collector_loop(stop_event, config)
        finally:
            _remove_pid_file(config.pid_file_path, expected_pid=pid)

    return 0


def _service_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "service"]
    return [sys.executable, "-m", "melone_service.main"]


def start_service(
    config: ServiceConfig | None = None,
    *,
    timeout_seconds: float = START_TIMEOUT_SECONDS,
    permission_checker: Callable[[], PermissionSnapshot] | None = None,
) -> StartResult:
    # 권한과 DB 상태를 확인한 뒤 별도 Python 프로세스로 서비스를 기동합니다.
    config = load_config() if config is None else config
    initialize_database(config.database_path)

    state = get_process_state(config)
    if state.is_running:
        return StartResult(started=False, pid=state.pid)
    if state.is_stale:
        _remove_pid_file(config.pid_file_path)

    _record_and_require_permissions(config, permission_checker)

    log_path = config.logs_dir / SERVICE_LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)

    command = _service_command()
    env = os.environ.copy()
    _populate_service_env(env, config)
    env[PERMISSIONS_CHECKED_ENV] = "1"

    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = get_process_state(config)
        if state.is_running:
            return StartResult(started=True, pid=state.pid or process.pid)

        if process.poll() is not None:
            raise ServiceStartError(
                f"Melone service exited with code {process.returncode}"
            )

        time.sleep(0.05)

    raise ServiceStartError("Melone service did not start before timeout")


def stop_service(
    config: ServiceConfig | None = None,
    *,
    timeout_seconds: float = STOP_TIMEOUT_SECONDS,
) -> StopResult:
    # PID 파일에 기록된 프로세스에 SIGTERM을 보내고 종료 완료를 기다립니다.
    config = load_config() if config is None else config
    state = get_process_state(config)

    if not state.is_running:
        if state.is_stale:
            _remove_pid_file(config.pid_file_path, expected_pid=state.pid)
        return StopResult(stopped=False, pid=None, was_running=False)

    if state.pid is None:
        return StopResult(stopped=False, pid=None, was_running=True)

    try:
        os.kill(state.pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pid_file(config.pid_file_path, expected_pid=state.pid)
        return StopResult(stopped=False, pid=state.pid, was_running=False)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = get_process_state(config)
        if not state.is_running:
            if state.is_stale:
                _remove_pid_file(config.pid_file_path, expected_pid=state.pid)
            return StopResult(stopped=True, pid=state.pid, was_running=True)

        time.sleep(0.05)

    return StopResult(stopped=False, pid=state.pid, was_running=True)


def kill_service(
    config: ServiceConfig | None = None,
    *,
    timeout_seconds: float = KILL_TIMEOUT_SECONDS,
) -> StopResult:
    config = load_config() if config is None else config
    state = get_process_state(config)

    if not state.is_running:
        if state.is_stale:
            _remove_pid_file(config.pid_file_path, expected_pid=state.pid)
        return StopResult(stopped=False, pid=None, was_running=False)

    if state.pid is None:
        return StopResult(stopped=False, pid=None, was_running=True)

    try:
        os.kill(state.pid, signal.SIGKILL)
    except ProcessLookupError:
        _remove_pid_file(config.pid_file_path, expected_pid=state.pid)
        return StopResult(stopped=False, pid=state.pid, was_running=False)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = get_process_state(config)
        if not state.is_running:
            if state.is_stale:
                _remove_pid_file(config.pid_file_path, expected_pid=state.pid)
            return StopResult(stopped=True, pid=state.pid, was_running=True)

        time.sleep(0.02)

    return StopResult(stopped=False, pid=state.pid, was_running=True)


def get_process_state(config: ServiceConfig | None = None) -> ProcessState:
    # lock, pid 파일, 프로세스 생존 여부를 조합해 실행/비정상 종료 상태를 판단합니다.
    config = load_config() if config is None else config

    has_pid_file = config.pid_file_path.exists()
    pid = _read_pid_file(config.pid_file_path)
    lock_held = _is_lock_held(config.lock_file_path)
    process_running = pid is not None and _is_process_running(pid)
    is_running = lock_held and (pid is None or process_running)
    is_stale = has_pid_file and not is_running

    return ProcessState(
        is_running=is_running,
        pid=pid,
        pid_file_path=config.pid_file_path,
        lock_file_path=config.lock_file_path,
        is_stale=is_stale,
    )


def _run_collector_loop(
    stop_event: threading.Event,
    config: ServiceConfig,
) -> None:
    # 각 tick에서 collector가 만든 표준 이벤트를 DB에 저장합니다.
    connection = connect(config.database_path)
    try:
        repository = EventRepository(connection)
        collectors = _create_collectors(repository, config)
        # 'running' 상태에서 종료되었으면 해당 OCR job 다시 queue
        reclaimed = OcrJobRepository(connection).reclaim_running_jobs()
        if reclaimed:
            logger.info("reclaimed %d interrupted OCR job(s) for retry", reclaimed)

        clear_paused(config.pause_flag_path)
        _apply_screenshot_capture_policy(collectors, config)
        _poll_collectors(collectors, repository)
        _record_activity_state(repository, config)
        run_screen_search_workers_once(config, stop_event=stop_event, logger=logger)

        while not stop_event.wait(config.polling_interval_seconds):
            if is_paused(config.pause_flag_path):
                continue
            _apply_screenshot_capture_policy(collectors, config)
            _poll_collectors(collectors, repository)
            _record_activity_state(repository, config)
            run_screen_search_workers_once(config, stop_event=stop_event, logger=logger)
    finally:
        connection.close()


def _create_collectors(
    repository: EventRepository,
    config: ServiceConfig,
) -> list[Collector]:
    collectors: list[Collector] = [
        ActiveWindowCollector(),
        CurrentAssetCollector(resolver=build_default_resolver()),
        KeyboardCollector(),
        MouseCollector(),
    ]
    if is_screenshot_collection_enabled(config):
        screen_repository = ScreenRepository(repository.connection)
        collectors.append(
            ScreenshotCollector(
                screen_repository=screen_repository,
                screenshots_dir=config.screenshots_dir,
                min_interval_seconds=config.screenshot_min_interval_seconds,
            )
        )
    return collectors


def _apply_screenshot_capture_policy(
    collectors: Sequence[Collector],
    config: ServiceConfig,
) -> None:
    if not is_screenshot_collection_enabled(config):
        return

    backlog_count = (
        count_screen_search_backlog(config.database_path)
        if is_screen_search_workers_enabled(config)
        else 0
    )
    policy = capture_policy_for_backlog(config, backlog_count=backlog_count)
    for collector in collectors:
        if isinstance(collector, ScreenshotCollector):
            collector.set_capture_policy(
                min_interval_seconds=policy.min_interval_seconds,
                transition_frame_only=policy.transition_frame_only,
            )


def _poll_collectors(
    collectors: Sequence[Collector],
    repository: EventRepository,
) -> None:
    for collector in collectors:
        try:
            events = collector.poll()
        except Exception as exc:
            print(f"collector {collector.name} failed: {exc}", file=sys.stderr)
            continue

        for event in events:
            repository.insert(event)


def _record_activity_state(
    repository: EventRepository,
    config: ServiceConfig,
    *,
    now: datetime | None = None,
) -> NormalizedEvent | None:
    reference_time = utc_now() if now is None else now
    thresholds = ActivityThresholds(
        active_window_seconds=config.activity_active_window_seconds,
        idle_timeout_seconds=config.idle_timeout_seconds,
    )
    since = utc_timestamp(
        reference_time - timedelta(seconds=thresholds.idle_timeout_seconds)
    )
    events = repository.list_by_types(
        ACTIVITY_EVENT_TYPES,
        since=since,
        limit=ACTIVITY_EVENT_LIMIT,
    )
    state = classify_activity_state(
        events,
        thresholds=thresholds,
        now=reference_time,
    )
    previous_state = activity_state_from_event(
        repository.latest(event_type=ACTIVITY_STATE_CHANGED)
    )
    if state == previous_state:
        return None

    event = activity_state_changed_event(
        state,
        previous_state=previous_state,
        thresholds=thresholds,
        timestamp=reference_time,
    )
    repository.insert(event)
    return event


def _record_and_require_permissions(
    config: ServiceConfig,
    permission_checker: Callable[[], PermissionSnapshot] | None,
) -> None:
    # 시작 시점의 권한 상태를 DB에 기록하고 필수 권한이 없으면 실행을 막습니다.
    checker = (
        check_permission_status
        if permission_checker is None
        else permission_checker
    )
    snapshot = checker()
    connection = connect(config.database_path)
    try:
        record_permission_status(EventRepository(connection), snapshot)
    finally:
        connection.close()

    require_all_permissions(snapshot)


def _populate_service_env(env: dict[str, str], config: ServiceConfig) -> None:
    env["MELONE_HOME"] = str(config.data_dir)
    env[MELONE_OCR_PROVIDER_ENV] = config.ocr_provider
    env[MELONE_OCR_ENDPOINT_ENV] = config.ocr_endpoint
    env[MELONE_OCR_MODEL_ENV] = config.ocr_model
    env[MELONE_OCR_TIMEOUT_SECONDS_ENV] = str(config.ocr_timeout_seconds)
    env[MELONE_OCR_MAX_TOKENS_ENV] = str(config.ocr_max_tokens)
    _populate_development_override_env(
        env,
        MELONE_SCREENSHOT_COLLECTOR_ENABLED_ENV,
        config.screenshot_collector_development_override,
    )
    _populate_development_override_env(
        env,
        MELONE_SCREEN_SEARCH_WORKERS_ENABLED_ENV,
        config.screen_search_workers_development_override,
    )
    env[MELONE_SCREEN_SEARCH_MAX_JOBS_PER_TICK_ENV] = str(
        config.screen_search_max_jobs_per_tick
    )
    env[MELONE_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS_ENV] = str(
        config.screen_search_retry_backoff_seconds
    )
    env[MELONE_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD_ENV] = str(
        config.screen_search_high_backlog_threshold
    )
    env[MELONE_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD_ENV] = str(
        config.screen_search_very_high_backlog_threshold
    )
    env[MELONE_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS_ENV] = str(
        config.context_rank_refresh_min_interval_seconds
    )
    env[MELONE_SCREEN_TEXT_RETAIN_SCREENSHOTS_ENV] = _env_bool_text(
        config.screen_text_retain_screenshots
    )
    env[MELONE_SEMANTIC_SEARCH_ENABLED_ENV] = _env_bool_text(
        config.semantic_search_enabled
    )
    env[MELONE_EMBEDDING_MODEL_ENV] = config.embedding_model
    env[MELONE_EMBEDDING_DIMENSION_ENV] = str(config.embedding_dimension)
    env[MELONE_EMBEDDING_BATCH_SIZE_ENV] = str(config.embedding_batch_size)
    env[MELONE_SEMANTIC_SEARCH_CANDIDATE_LIMIT_ENV] = str(
        config.semantic_search_candidate_limit
    )


def _populate_development_override_env(
    env: dict[str, str],
    name: str,
    value: bool | None,
) -> None:
    if value is None:
        env.pop(name, None)
        return
    env[name] = _env_bool_text(value)


def _env_bool_text(value: bool) -> str:
    return "1" if value else "0"


@contextmanager
def _shutdown_signals(stop_event: threading.Event):
    # SIGTERM/SIGINT를 stop_event로 바꿔 서비스 루프가 정리 경로를 타게 합니다.
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    handled_signals = (signal.SIGTERM, signal.SIGINT)
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in handled_signals
    }

    def request_shutdown(_signum: int, _frame: FrameType | None) -> None:
        stop_event.set()

    for signum in handled_signals:
        signal.signal(signum, request_shutdown)

    try:
        yield
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)


def _is_lock_held(path: Path) -> bool:
    # non-blocking flock 시도로 다른 프로세스가 lock을 잡고 있는지 확인합니다.
    if not path.exists():
        return False

    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return False


def _is_process_running(pid: int) -> bool:
    # signal 0은 프로세스를 죽이지 않고 존재 여부와 접근 가능성을 확인합니다.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


def _read_pid_file(path: Path) -> int | None:
    # 비어 있거나 깨진 PID 파일은 stale 판단을 위해 None으로 취급합니다.
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None

    if not text:
        return None

    try:
        return int(text)
    except ValueError:
        return None


def _write_pid_file(path: Path, pid: int) -> None:
    # 임시 파일을 원자적으로 교체해 읽는 쪽이 부분적으로 쓴 PID를 보지 않게 합니다.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(f"{pid}\n", encoding="utf-8")
    temporary_path.replace(path)


def _remove_pid_file(path: Path, expected_pid: int | None = None) -> None:
    # expected_pid가 다르면 다른 프로세스의 PID 파일일 수 있어 삭제하지 않습니다.
    if expected_pid is not None and _read_pid_file(path) != expected_pid:
        return

    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main() -> int:
    # python -m melone_service.main 실행 시 서비스 프로세스용 종료 코드를 반환합니다.
    try:
        return run_service()
    except RequiredPermissionsMissingError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ServiceAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
