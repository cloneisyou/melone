import argparse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from . import __version__
from .asset.resolvers.agent_sessions import (
    current_terminal_sessions,
    pick_candidate,
)
from .config import ServiceConfig, is_screenshot_collection_enabled, load_config
from .main import (
    ACTIVITY_EVENT_LIMIT,
    ServiceAlreadyRunningError,
    ServiceStartError,
    get_process_state,
    run_service,
    start_service,
    stop_service,
)
from .models import NormalizedEvent, utc_now, utc_timestamp
from .pipeline.activity import (
    ACTIVITY_EVENT_TYPES,
    ActivityState,
    ActivityThresholds,
    classify_activity_state,
)
from .pipeline.context_graph import rank_contexts
from .pipeline.context_rank_cache import (
    DEFAULT_CONTEXT_RANK_CACHE_EVENT_LIMIT,
    ContextRankCacheRefresher,
)
from .pipeline.context_pages import CONTEXT_GRAPH_EVENT_TYPES, RankedContextPage
from .pipeline.screen_search_scheduler import get_last_embedding_indexing_error
from .queries import build_semantic_candidate_provider
from .permissions import (
    PermissionSnapshot,
    RequiredPermissionsMissingError,
    StatusCheck,
    check_permission_status,
    record_permission_status,
)
from .pipeline.normalizer import normalize_event
from .runtime_config import RuntimeScope, configured_runtime_parameters, runtime_parameters
from .screen_text_status import build_screen_text_status
from .search import ScreenSearchResult, ScreenSearchService
from .store.context_rank import ContextRankRepository, ContextRankScore
from .store.db import connect, initialize_database
from .store.embeddings import EmbeddingRepository
from .store.events import DEFAULT_EVENT_LIMIT, EventRepository
from .store.migrations import run_migrations
from .store.ocr import OcrChunkRepository
from .store.search import DEFAULT_SCREEN_SEARCH_LIMIT, ScreenSearchRepository
from .store.ocr_jobs import OcrJobRepository


DURATION_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}

CONTEXT_EVENT_TYPES = (
    "active_app_snapshot",
    "active_app_changed",
    "window_title_changed",
    "current_asset_changed",
)
CONTEXT_RANKING_EVENT_TYPES = (*CONTEXT_GRAPH_EVENT_TYPES, *ACTIVITY_EVENT_TYPES)
CONTEXT_RANKING_EVENT_LIMIT = DEFAULT_EVENT_LIMIT


def _print_not_implemented(command: str) -> int:
    # 아직 구현되지 않은 명령도 CLI 형태를 먼저 검증할 수 있게 둔 임시 응답입니다.
    print(f"{command}: not implemented yet")
    return 0


def _handle_start(args: argparse.Namespace) -> int:
    # foreground 실행과 백그라운드 daemon 실행을 같은 start 명령에서 분기합니다.
    config = load_config()

    if args.foreground:
        try:
            print("starting Melone service in foreground")
            return run_service(config)
        except RequiredPermissionsMissingError as exc:
            print(f"start failed: {exc}")
            return 1
        except ServiceAlreadyRunningError:
            print("status: already running")
            return 1

    try:
        result = start_service(config)
    except RequiredPermissionsMissingError as exc:
        print(f"start failed: {exc}")
        return 1
    except ServiceStartError as exc:
        print(f"start failed: {exc}")
        return 1

    if not result.started:
        print("status: already running")
        if result.pid is not None:
            print(f"pid: {result.pid}")
        return 1

    print("status: started")
    print(f"pid: {result.pid}")
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    # 서비스, DB, 마이그레이션, 권한 상태를 한 번에 점검해 출력합니다.
    config = load_config()
    process_state = get_process_state(config)
    migration_status = initialize_database(config.database_path)
    permission_status = check_permission_status()
    _record_permission_status(config.database_path, permission_status)

    print(f"{config.app_name} service")
    print(f"status: {'running' if process_state.is_running else 'not running'}")
    if process_state.pid is not None:
        print(f"pid: {process_state.pid}")
    if process_state.is_stale:
        print("pid file: stale")
    print(f"data directory: {config.data_dir}")
    print(f"db path: {config.database_path}")
    print("db connection: ok")
    print(
        "migration version: "
        f"{migration_status.current_version}/{migration_status.latest_version}"
    )
    print(f"pending migrations: {len(migration_status.pending_versions)}")
    print(f"logs directory: {config.logs_dir}")
    print(f"screenshots directory: {config.screenshots_dir}")
    print(f"polling interval: {config.polling_interval_seconds:g}s")
    print(f"idle timeout: {config.idle_timeout_seconds}s")
    print(f"activity active window: {config.activity_active_window_seconds}s")
    print(
        "screenshot minimum interval: "
        f"{config.screenshot_min_interval_seconds}s"
    )
    _print_screen_search_status(config)
    print(
        "screenshot collector enabled: "
        f"{_format_bool(is_screenshot_collection_enabled(config))}"
    )
    _print_permission_status(permission_status)
    return 0


def _handle_stop(args: argparse.Namespace) -> int:
    # 실행 중인 서비스 프로세스에 종료 요청을 보내고 결과를 CLI용 메시지로 바꿉니다.
    config = load_config()
    result = stop_service(config)

    if result.stopped:
        print("status: stopped")
        return 0

    if not result.was_running:
        print("status: not running")
        return 0

    print("status: stop failed")
    print(f"pid: {result.pid}")
    return 1


def _handle_context(args: argparse.Namespace) -> int:
    # 최신 앱/창/URL 컨텍스트와 현재 activity 상태를 간단히 출력합니다.
    config = load_config()
    now = utc_now()
    thresholds = ActivityThresholds(
        active_window_seconds=config.activity_active_window_seconds,
        idle_timeout_seconds=config.idle_timeout_seconds,
    )
    since = utc_timestamp(now - timedelta(seconds=thresholds.idle_timeout_seconds))

    with _open_event_repository() as repository:
        context_event = repository.latest_by_types(CONTEXT_EVENT_TYPES)
        activity_events = repository.list_by_types(
            ACTIVITY_EVENT_TYPES,
            since=since,
            limit=ACTIVITY_EVENT_LIMIT,
        )

    activity_state = classify_activity_state(
        activity_events,
        thresholds=thresholds,
        now=now,
    )
    _print_context(context_event, activity_state)
    return 0


def _handle_contexts(args: argparse.Namespace) -> int:
    # 저장된 context/activity 이벤트를 PageRank ranking으로 압축해 사람이 읽는 표로 보여줍니다.
    with _open_event_repository() as repository:
        events = repository.list_by_types(
            CONTEXT_RANKING_EVENT_TYPES,
            since=args.since,
            limit=CONTEXT_RANKING_EVENT_LIMIT,
        )

    ranked_contexts = rank_contexts(
        events,
        limit=args.limit,
        show_hidden=args.show_hidden,
    )
    _print_ranked_contexts(ranked_contexts)
    return 0


def _handle_context_rank_cache(args: argparse.Namespace) -> int:
    with _open_context_rank_cache_refresher() as refresher:
        result = refresher.refresh(
            since=args.since,
            event_limit=args.event_limit,
        )

    print(f"refreshed context rank cache: {result.upserted_count} row(s)")
    print(f"events considered: {result.event_count}")
    print(f"computed_at: {result.computed_at}")
    print(f"model_version: {result.model_version}")
    _print_context_rank_scores(result.scores)
    return 0


def _handle_timeline(args: argparse.Namespace) -> int:
    # 저장된 이벤트를 시간순 활동 타임라인 형태로 보여줍니다.
    with _open_event_repository() as repository:
        events = repository.list(since=args.since, limit=DEFAULT_EVENT_LIMIT)

    _print_timeline(events)
    return 0


def _handle_events(args: argparse.Namespace) -> int:
    # 개발용 샘플 추가와 필터링된 raw event 조회를 처리합니다.
    with _open_event_repository() as repository:
        if args.events_action == "add-sample":
            event = _sample_event()
            repository.insert(event)
            print(f"added sample event: {event.id}")
            print(f"timestamp: {event.timestamp}")
            return 0

        events = repository.list(
            since=args.since,
            event_type=args.event_type,
            limit=DEFAULT_EVENT_LIMIT,
        )

    _print_events(events)
    return 0


def _handle_sessions(args: argparse.Namespace) -> int:
    # 향후 이벤트를 묶어 만든 작업 세션 조회 명령이 들어올 자리입니다.
    return _print_not_implemented(args.command)


def _handle_agent_sessions(args: argparse.Namespace) -> int:
    # Live view of the terminal agent sessions active right now: best-guess first (marked
    # *), then the other candidates, with each project's URL.
    conversation = current_terminal_sessions()
    if conversation is None or not conversation.candidates:
        print("No active agent sessions.")
        return 0

    candidates = conversation.candidates
    if args.cwd:
        match = pick_candidate(candidates, cwd=args.cwd)
        if match is None:
            print(f"No unambiguous match for {args.cwd}.")
            return 0
        candidates = [match]

    _print_agent_sessions(candidates, best_id=conversation.conversation_id)
    return 0


def _handle_search(args: argparse.Namespace) -> int:
    query = " ".join(args.query)
    with _open_screen_search_service() as service:
        results = service.search(query, limit=args.limit)

    _print_screen_search_results(results)
    return 0


def _handle_config(args: argparse.Namespace) -> int:
    if args.config_action == "list":
        _print_runtime_config_list(scope=args.scope)
        return 0

    _print_config_doctor(include_desktop=args.desktop)
    return 0


def build_parser() -> argparse.ArgumentParser:
    # 모든 CLI 서브커맨드와 옵션을 한곳에서 등록해 테스트와 확장을 쉽게 합니다.
    parser = argparse.ArgumentParser(
        prog="melone",
        description="Inspect and manage Melone",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    commands = {
        "start": ("Start the local service.", _handle_start),
        "status": ("Show local service status.", _handle_status),
        "stop": ("Stop the local service.", _handle_stop),
        "context": ("Show the latest captured context.", _handle_context),
        "contexts": ("Show ranked context history.", _handle_contexts),
        "context-rank-cache": (
            "Refresh cached context rank scores.",
            _handle_context_rank_cache,
        ),
        "timeline": ("Inspect the captured event timeline.", _handle_timeline),
        "events": ("Inspect captured normalized events.", _handle_events),
        "sessions": ("Inspect derived activity sessions.", _handle_sessions),
        "agent-sessions": (
            "Show recorded agent conversation session candidates.",
            _handle_agent_sessions,
        ),
        "search": ("Search indexed screen OCR chunks.", _handle_search),
        "config": ("Inspect runtime configuration.", _handle_config),
    }

    for name, (help_text, handler) in commands.items():
        subparser = subparsers.add_parser(name, help=help_text, description=help_text)
        if name == "start":
            subparser.add_argument(
                "--foreground",
                action="store_true",
                help="Run the service in the current process.",
            )
        if name == "events":
            subparser.add_argument(
                "events_action",
                nargs="?",
                choices=("add-sample",),
                help="Add a development sample event.",
            )
            subparser.add_argument(
                "--since",
                type=_parse_since,
                help="Only show events newer than a duration such as 30m, 2h, or 1d.",
            )
            subparser.add_argument(
                "--type",
                dest="event_type",
                help="Only show events with this normalized event type.",
            )
        if name == "timeline":
            subparser.add_argument(
                "--since",
                type=_parse_since,
                help="Only show events newer than a duration such as 30m, 2h, or 1d.",
            )
        if name == "contexts":
            subparser.add_argument(
                "--since",
                type=_parse_since,
                help="Only rank context events newer than a duration such as 2h.",
            )
            subparser.add_argument(
                "--limit",
                type=_parse_positive_int,
                help="Maximum number of ranked contexts to show.",
            )
            subparser.add_argument(
                "--show-hidden",
                action="store_true",
                help="Include hidden bridge contexts such as browser new tabs.",
            )
        if name == "context-rank-cache":
            subparser.add_argument(
                "cache_action",
                nargs="?",
                choices=("refresh",),
                default="refresh",
                help="Refresh the context rank score cache.",
            )
            subparser.add_argument(
                "--since",
                type=_parse_since,
                help="Only cache context events newer than a duration such as 2h.",
            )
            subparser.add_argument(
                "--event-limit",
                type=_parse_positive_int,
                default=DEFAULT_CONTEXT_RANK_CACHE_EVENT_LIMIT,
                help="Maximum number of recent context/activity events to rank.",
            )
        if name == "agent-sessions":
            subparser.add_argument(
                "--cwd",
                help="Show only the candidate whose working directory matches this path.",
            )
        if name == "search":
            subparser.add_argument(
                "query",
                nargs="+",
                help="Search query for indexed screen OCR text.",
            )
            subparser.add_argument(
                "--limit",
                type=_parse_positive_int,
                default=DEFAULT_SCREEN_SEARCH_LIMIT,
                help="Maximum number of OCR chunk candidates to inspect.",
            )
        if name == "config":
            subparser.add_argument(
                "config_action",
                nargs="?",
                choices=("doctor", "list"),
                default="doctor",
                help="Run a config doctor check or list known runtime parameters.",
            )
            subparser.add_argument(
                "--desktop",
                action="store_true",
                help="Include desktop-only integration checks.",
            )
            subparser.add_argument(
                "--scope",
                choices=(
                    "product",
                    "developer",
                    "advanced-backend",
                    "integration",
                    "secret",
                ),
                help="Only list parameters in this scope.",
            )
        subparser.set_defaults(handler=handler)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # 테스트에서는 argv를 주입하고 실제 CLI에서는 sys.argv를 argparse가 읽습니다.
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


@contextmanager
def _open_event_repository() -> Iterator[EventRepository]:
    # CLI 조회 전에 DB 연결과 마이그레이션을 보장하고 사용 후 연결을 닫습니다.
    config = load_config()
    connection = connect(config.database_path)
    try:
        run_migrations(connection)
        yield EventRepository(connection)
    finally:
        connection.close()


@contextmanager
def _open_screen_search_service() -> Iterator[ScreenSearchService]:
    config = load_config()
    connection = connect(config.database_path)
    try:
        run_migrations(connection)
        yield ScreenSearchService(
            ScreenSearchRepository(connection),
            ContextRankRepository(connection),
            semantic_candidate_provider=build_semantic_candidate_provider(
                connection,
                config,
            ),
        )
    finally:
        connection.close()


@contextmanager
def _open_context_rank_cache_refresher() -> Iterator[ContextRankCacheRefresher]:
    config = load_config()
    connection = connect(config.database_path)
    try:
        run_migrations(connection)
        yield ContextRankCacheRefresher(
            EventRepository(connection),
            ContextRankRepository(connection),
        )
    finally:
        connection.close()


def _parse_since(value: str) -> str:
    # 30m, 2h 같은 상대 시간을 이벤트 timestamp 필터용 UTC 문자열로 바꿉니다.
    text = value.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("--since cannot be blank")

    unit = text[-1]
    if unit not in DURATION_SECONDS:
        raise argparse.ArgumentTypeError(
            "--since must use one of these units: s, m, h, d"
        )

    amount_text = text[:-1]
    try:
        amount = float(amount_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--since must start with a number") from exc

    if amount <= 0:
        raise argparse.ArgumentTypeError("--since must be greater than zero")

    since = utc_now() - timedelta(seconds=amount * DURATION_SECONDS[unit])
    return utc_timestamp(since)


def _parse_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--limit must be an integer") from exc

    if parsed <= 0:
        raise argparse.ArgumentTypeError("--limit must be greater than zero")
    return parsed


def _sample_event() -> NormalizedEvent:
    # 저장소와 출력 흐름을 로컬에서 확인할 수 있는 개발용 이벤트입니다.
    return normalize_event(
        "active_app_changed",
        app={
            "name": "Sample App",
            "bundle_id": "com.example.SampleApp",
            "pid": 12345,
        },
        window={"title": "Sample Window"},
        url="https://example.com/work?q=private#section",
        source="sample",
        metadata={"sample": True},
    )


def _print_events(events: list[NormalizedEvent]) -> None:
    # raw event 조회 결과를 컬럼 폭이 고정된 간단한 표로 출력합니다.
    if not events:
        print("No events found.")
        return

    print(
        f"{'timestamp':<24} {'type':<24} {'app':<20} "
        f"{'window':<28} {'url'}"
    )
    for event in events:
        print(
            f"{event.timestamp:<24} "
            f"{_clip(event.type, 24):<24} "
            f"{_clip(event.app_name, 20):<20} "
            f"{_clip(event.window_title, 28):<28} "
            f"{event.url or '-'}"
        )


def _print_timeline(events: list[NormalizedEvent]) -> None:
    # 앱, 창, URL을 하나의 context 문자열로 묶어 시간 흐름을 보여줍니다.
    if not events:
        print("No events found.")
        return

    print(f"{'time':<24} {'event':<24} context")
    for event in events:
        context = " | ".join(
            value
            for value in (event.app_name, event.window_title, event.url)
            if value
        )
        print(f"{event.timestamp:<24} {_clip(event.type, 24):<24} {context or '-'}")


def _print_context(
    context_event: NormalizedEvent | None,
    activity_state: ActivityState,
) -> None:
    print("Melone context")
    print(f"app: {_context_value(context_event.app_name if context_event else None)}")
    print(
        "window: "
        f"{_context_value(context_event.window_title if context_event else None)}"
    )
    print(f"url: {_context_value(context_event.url if context_event else None)}")
    print(f"activity: {activity_state}")


def _print_ranked_contexts(ranked_contexts: Sequence[RankedContextPage]) -> None:
    if not ranked_contexts:
        print("No contexts found.")
        return

    print(f"{'score':<8} {'visits':<6} {'kind':<10} {'context':<60} url")
    for ranked_context in ranked_contexts:
        page = ranked_context.page
        print(
            f"{ranked_context.score:<8.5f} "
            f"{ranked_context.visits:<6} "
            f"{page.kind:<10} "
            f"{_clip(page.label, 60):<60} "
            f"{page.url or '-'}"
        )


def _print_context_rank_scores(scores: Sequence[ContextRankScore]) -> None:
    if not scores:
        print("No context rank scores cached.")
        return

    print(f"{'score':<8} {'visits':<6} {'source_key':<60} locators")
    for score in scores:
        print(
            f"{score.score:<8.5f} "
            f"{score.visits:<6} "
            f"{_clip(score.source_key, 60):<60} "
            f"{', '.join(score.retrieval_locators) or '-'}"
        )


def _print_agent_sessions(
    candidates: Sequence[dict], *, best_id: str | None = None
) -> None:
    # Print the live agent session candidates as a simple fixed-width table, marking the
    # best-guess (active) one with '*' and showing each project's URL.
    if not candidates:
        print("No agent sessions found.")
        return

    print(f"{'':<1} {'connector':<12} {'conversation_id':<38} {'url'}")
    for candidate in candidates:
        marker = "*" if candidate.get("conversation_id") == best_id else " "
        location = candidate.get("url") or candidate.get("cwd") or "-"
        print(
            f"{marker:<1} "
            f"{_clip(candidate.get('connector'), 12):<12} "
            f"{_clip(candidate.get('conversation_id'), 38):<38} "
            f"{location}"
        )


def _print_screen_search_results(results: Sequence[ScreenSearchResult]) -> None:
    if not results:
        print("No screen results found.")
        return

    print(
        f"{'score':<8} {'bm25':<8} {'emb':<8} {'rank':<8} "
        f"{'chunks':<6} {'context':<60} preview"
    )
    for result in results:
        context = result.retrieval_locator or f"session:{result.session_id}"
        print(
            f"{result.final_score:<8.5f} "
            f"{result.bm25_relevance:<8.5f} "
            f"{result.embedding_relevance:<8.5f} "
            f"{result.context_rank:<8.5f} "
            f"{len(result.chunks):<6} "
            f"{_clip(context, 60):<60} "
            f"{result.preview}"
        )


def _print_runtime_config_list(*, scope: RuntimeScope | None) -> None:
    parameters = runtime_parameters(scope=scope)
    if not parameters:
        print("No runtime parameters found.")
        return

    name_width = max(len("name"), *(len(parameter.name) for parameter in parameters))
    name_width += 2
    print(f"{'name':<{name_width}} {'scope':<18} {'default':<38} required for")
    for parameter in parameters:
        print(
            f"{parameter.name:<{name_width}} "
            f"{parameter.scope:<18} "
            f"{_clip(parameter.default, 38):<38} "
            f"{parameter.required_for}"
        )


def _print_config_doctor(*, include_desktop: bool) -> None:
    config = load_config()
    screen_text = build_screen_text_status(config)
    configured = configured_runtime_parameters()

    print("Melone config doctor")
    print("normal desktop launch: no env required")
    print(f"data directory: {config.data_dir}")
    print(f"settings path: {config.settings_path}")
    print(f"logs directory: {config.logs_dir}")
    print(f"Screen Text Search: {screen_text['state']}")
    print(f"  product setting: {_format_bool(bool(screen_text['enabled']))}")
    print(f"  effective enabled: {_format_bool(bool(screen_text['effectiveEnabled']))}")
    if screen_text.get("reason"):
        print(f"  reason: {screen_text['reason']}")
    print(f"OCR provider: {config.ocr_provider}")
    if _uses_advanced_ocr_provider(config.ocr_provider):
        print(f"  endpoint: {config.ocr_endpoint}")
        print(f"  model: {config.ocr_model}")

    print("configured env overrides:")
    visible = [
        parameter
        for parameter in configured
        if include_desktop or "desktop" not in parameter.used_by
    ]
    if not visible:
        print("  none")
    for parameter in visible:
        print(f"  {parameter.name}: {parameter.current_value()}")

    if include_desktop:
        _print_desktop_config_doctor()


def _print_desktop_config_doctor() -> None:
    import os

    google_id = os.environ.get("MELONE_GOOGLE_CLIENT_ID", "").strip()
    google_secret = os.environ.get("MELONE_GOOGLE_CLIENT_SECRET", "").strip()
    python_override = os.environ.get("MELONE_PYTHON", "").strip()
    fake_update = os.environ.get("MELONE_FAKE_UPDATE", "").strip()

    print("desktop integrations:")
    if google_id and google_secret:
        print("  Google sign-in: configured")
    elif google_id or google_secret:
        print("  Google sign-in: incomplete credentials")
    else:
        print("  Google sign-in: disabled")

    print(f"  Python override: {python_override or 'auto'}")
    print(f"  fake update flow: {'enabled' if fake_update == '1' else 'off'}")


def _uses_advanced_ocr_provider(provider: str) -> bool:
    normalized = provider.strip().lower().replace("-", "_")
    return normalized not in {"apple_vision", "macos_vision", "vision", "mock"}


def _context_value(value: object | None) -> str:
    return "-" if value is None or str(value).strip() == "" else str(value)


def _print_permission_status(snapshot: PermissionSnapshot) -> None:
    # 권한과 수집기 상태를 사람이 읽기 쉬운 두 섹션으로 출력합니다.
    print("permissions:")
    for name, check in snapshot.iter_permissions():
        print(f"  {name}: {_format_status_check(check)}")

    print("collectors:")
    for name, check in snapshot.iter_collectors():
        print(f"  {name}: {_format_status_check(check)}")


def _print_screen_search_status(config: ServiceConfig) -> None:
    connection = connect(config.database_path)
    try:
        ocr_jobs = OcrJobRepository(connection)
        context_rank = ContextRankRepository(connection)
        print("screen search workers:")
        print(f"  pending OCR jobs: {ocr_jobs.count_jobs(status='pending')}")
        print(f"  running OCR jobs: {ocr_jobs.count_jobs(status='running')}")
        print(f"  dead OCR jobs: {ocr_jobs.count_jobs(status='dead')}")
        print(
            "  latest context rank computed_at: "
            f"{context_rank.latest_computed_at() or '-'}"
        )
        if config.semantic_search_enabled:
            chunks = OcrChunkRepository(connection)
            embeddings = EmbeddingRepository(connection)
            total_chunks = chunks.count_chunks()
            embedded_chunks = embeddings.count_current_chunk_embeddings(
                model=config.embedding_model,
                dimension=config.embedding_dimension,
            )
            print("  semantic search: enabled")
            print(
                "  embedding model: "
                f"{config.embedding_model} ({config.embedding_dimension}d)"
            )
            print(
                "  embedding cache coverage: "
                f"{_format_chunk_coverage(embedded_chunks, total_chunks)}"
            )
            last_error = get_last_embedding_indexing_error(
                database_path=config.database_path,
                model=config.embedding_model,
                dimension=config.embedding_dimension,
            )
            if last_error is not None:
                print(
                    "  latest embedding error: "
                    f"{last_error['type']}: {last_error['message']}"
                )
    finally:
        connection.close()


def _format_status_check(check: StatusCheck) -> str:
    # 상태 상세 설명이 있을 때만 괄호로 붙여 CLI 출력이 길어지지 않게 합니다.
    if check.detail:
        return f"{check.status} ({check.detail})"
    return check.status


def _format_bool(value: bool) -> str:
    return "yes" if value else "no"


def _format_chunk_coverage(embedded_chunks: int, total_chunks: int) -> str:
    if total_chunks <= 0:
        return f"{embedded_chunks}/{total_chunks} chunks (n/a)"
    return f"{embedded_chunks}/{total_chunks} chunks ({embedded_chunks / total_chunks:.1%})"


def _record_permission_status(
    database_path: Path,
    snapshot: PermissionSnapshot,
) -> None:
    # status 명령을 실행할 때마다 현재 권한 상태를 이벤트로 남깁니다.
    connection = connect(database_path)
    try:
        record_permission_status(EventRepository(connection), snapshot)
    finally:
        connection.close()


def _clip(value: object | None, width: int) -> str:
    # 긴 앱 이름이나 창 제목이 표 형태의 CLI 출력을 깨지 않도록 줄입니다.
    text = "-" if value is None else str(value)
    if len(text) <= width:
        return text

    return text[: width - 3] + "..."
