import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from .settings import app_settings_path, load_app_settings


APP_NAME = "Melone"
MELONE_HOME_ENV = "MELONE_HOME"
DEFAULT_POLLING_INTERVAL_SECONDS = 1.0
DEFAULT_IDLE_TIMEOUT_SECONDS = 5 * 60
DEFAULT_ACTIVITY_ACTIVE_WINDOW_SECONDS = 30
DEFAULT_SCREENSHOT_MIN_INTERVAL_SECONDS = 10
DEFAULT_OCR_PROVIDER = "apple_vision"
DEFAULT_OCR_ENDPOINT = "http://127.0.0.1:8000/v1"
DEFAULT_OCR_MODEL = "lightonai/LightOnOCR-2-1B"
DEFAULT_OCR_TIMEOUT_SECONDS = 120.0
DEFAULT_OCR_MAX_TOKENS = 4096
# Apple Vision returns empty/garbled text when the recognition language does not
# match the screen content; default to Korean + English for this product's users.
DEFAULT_OCR_RECOGNITION_LANGUAGES = ("ko-KR", "en-US")
# Off by default: language correction autocorrects toward dictionary words, which
# corrupts the code, identifiers, paths, and URLs common on screen.
DEFAULT_OCR_LANGUAGE_CORRECTION = True
DEFAULT_SCREENSHOT_COLLECTOR_ENABLED = True
DEFAULT_SCREEN_SEARCH_WORKERS_ENABLED = True
DEFAULT_SCREEN_SEARCH_MAX_JOBS_PER_TICK = 2
DEFAULT_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS = 60
DEFAULT_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD = 20
DEFAULT_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD = 100
# Context-rank recompute is expensive (rebuilds the behavior graph over the
# event window). It only feeds search ranking, so per-second freshness is
# unnecessary; recompute at most this often.
DEFAULT_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS = 30.0
DEFAULT_SCREEN_TEXT_RETAIN_SCREENSHOTS = True
DEFAULT_SEMANTIC_SEARCH_ENABLED = True
DEFAULT_EMBEDDING_MODEL = "google/embeddinggemma-300m"
DEFAULT_EMBEDDING_DIMENSION = 256
DEFAULT_EMBEDDING_BATCH_SIZE = 16
DEFAULT_SEMANTIC_SEARCH_CANDIDATE_LIMIT = 50
SUPPORTED_EMBEDDING_DIMENSIONS = (128, 256, 512, 768)

MELONE_OCR_PROVIDER_ENV = "MELONE_OCR_PROVIDER"
MELONE_OCR_ENDPOINT_ENV = "MELONE_OCR_ENDPOINT"
MELONE_OCR_MODEL_ENV = "MELONE_OCR_MODEL"
MELONE_OCR_TIMEOUT_SECONDS_ENV = "MELONE_OCR_TIMEOUT_SECONDS"
MELONE_OCR_MAX_TOKENS_ENV = "MELONE_OCR_MAX_TOKENS"
MELONE_OCR_LANGUAGES_ENV = "MELONE_OCR_LANGUAGES"
MELONE_OCR_LANGUAGE_CORRECTION_ENV = "MELONE_OCR_LANGUAGE_CORRECTION"
LEGACY_OCR_PROVIDER_ENV = "MELONE_VLM_PROVIDER"
LEGACY_OCR_ENDPOINT_ENV = "MELONE_VLM_ENDPOINT"
LEGACY_OCR_MODEL_ENV = "MELONE_VLM_MODEL"
LEGACY_OCR_TIMEOUT_SECONDS_ENV = "MELONE_VLM_TIMEOUT_SECONDS"
LEGACY_OCR_MAX_TOKENS_ENV = "MELONE_VLM_MAX_TOKENS"
MELONE_SCREENSHOT_COLLECTOR_ENABLED_ENV = "MELONE_SCREENSHOT_COLLECTOR_ENABLED"
MELONE_SCREEN_SEARCH_WORKERS_ENABLED_ENV = "MELONE_SCREEN_SEARCH_WORKERS_ENABLED"
MELONE_SCREEN_SEARCH_MAX_JOBS_PER_TICK_ENV = (
    "MELONE_SCREEN_SEARCH_MAX_JOBS_PER_TICK"
)
MELONE_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS_ENV = (
    "MELONE_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS"
)
MELONE_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD_ENV = (
    "MELONE_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD"
)
MELONE_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD_ENV = (
    "MELONE_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD"
)
MELONE_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS_ENV = (
    "MELONE_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS"
)
MELONE_SCREEN_TEXT_RETAIN_SCREENSHOTS_ENV = (
    "MELONE_SCREEN_TEXT_RETAIN_SCREENSHOTS"
)
MELONE_SEMANTIC_SEARCH_ENABLED_ENV = "MELONE_SEMANTIC_SEARCH_ENABLED"
MELONE_EMBEDDING_MODEL_ENV = "MELONE_EMBEDDING_MODEL"
MELONE_EMBEDDING_DIMENSION_ENV = "MELONE_EMBEDDING_DIMENSION"
MELONE_EMBEDDING_BATCH_SIZE_ENV = "MELONE_EMBEDDING_BATCH_SIZE"
MELONE_SEMANTIC_SEARCH_CANDIDATE_LIMIT_ENV = (
    "MELONE_SEMANTIC_SEARCH_CANDIDATE_LIMIT"
)
# Max events fetched per activity-state classification; lives in config so
# both main.py and queries.py can import it.
ACTIVITY_EVENT_LIMIT = 30000


@dataclass(frozen=True)
class ServiceConfig:
    # 서비스가 런타임에 사용하는 경로와 수집 주기를 한곳에 모은 설정 객체입니다.
    app_name: str
    data_dir: Path
    database_path: Path
    pid_file_path: Path
    lock_file_path: Path
    pause_flag_path: Path
    logs_dir: Path
    screenshots_dir: Path
    settings_path: Path | None = None
    polling_interval_seconds: float = DEFAULT_POLLING_INTERVAL_SECONDS
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS
    activity_active_window_seconds: int = DEFAULT_ACTIVITY_ACTIVE_WINDOW_SECONDS
    screenshot_min_interval_seconds: int = DEFAULT_SCREENSHOT_MIN_INTERVAL_SECONDS
    ocr_provider: str = DEFAULT_OCR_PROVIDER
    ocr_endpoint: str = DEFAULT_OCR_ENDPOINT
    ocr_model: str = DEFAULT_OCR_MODEL
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS
    ocr_max_tokens: int = DEFAULT_OCR_MAX_TOKENS
    ocr_recognition_languages: tuple[str, ...] = DEFAULT_OCR_RECOGNITION_LANGUAGES
    ocr_language_correction: bool = DEFAULT_OCR_LANGUAGE_CORRECTION
    screen_text_search_enabled: bool = False
    screenshot_collector_enabled: bool = DEFAULT_SCREENSHOT_COLLECTOR_ENABLED
    screen_search_workers_enabled: bool = DEFAULT_SCREEN_SEARCH_WORKERS_ENABLED
    screenshot_collector_development_override: bool | None = None
    screen_search_workers_development_override: bool | None = None
    screenshot_collector_development_override_enabled: bool = False
    screen_search_workers_development_override_enabled: bool = False
    screen_search_max_jobs_per_tick: int = DEFAULT_SCREEN_SEARCH_MAX_JOBS_PER_TICK
    screen_search_retry_backoff_seconds: int = (
        DEFAULT_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS
    )
    screen_search_high_backlog_threshold: int = (
        DEFAULT_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD
    )
    screen_search_very_high_backlog_threshold: int = (
        DEFAULT_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD
    )
    context_rank_refresh_min_interval_seconds: float = (
        DEFAULT_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS
    )
    screen_text_retain_screenshots: bool = DEFAULT_SCREEN_TEXT_RETAIN_SCREENSHOTS
    semantic_search_enabled: bool = DEFAULT_SEMANTIC_SEARCH_ENABLED
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    semantic_search_candidate_limit: int = (
        DEFAULT_SEMANTIC_SEARCH_CANDIDATE_LIMIT
    )


def load_env_file(dotenv_path: str | Path | None = None) -> bool:
    # 로컬 개발용 .env를 읽되 이미 설정된 환경 변수는 덮어쓰지 않습니다.
    env_path = str(dotenv_path) if dotenv_path is not None else find_dotenv(usecwd=True)
    if not env_path:
        return False

    return load_dotenv(dotenv_path=env_path, override=False)


def resolve_app_data_dir(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    # MELONE_HOME이 있으면 우선 사용하고, 없으면 macOS Application Support를 씁니다.
    env = os.environ if env is None else env
    override = env.get(MELONE_HOME_ENV)
    if override:
        return Path(override).expanduser()

    base_home = Path.home() if home is None else home
    return base_home / "Library" / "Application Support" / APP_NAME


def ensure_runtime_paths(config: ServiceConfig) -> None:
    # DB, 로그, 스크린샷 저장 전에 필요한 디렉터리를 미리 만듭니다.
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    config.screenshots_dir.mkdir(parents=True, exist_ok=True)


def load_config(env: Mapping[str, str] | None = None) -> ServiceConfig:
    # 환경 변수를 반영해 ServiceConfig를 만들고 즉시 사용할 수 있게 경로를 보장합니다.
    if env is None:
        load_env_file()
        env = os.environ

    data_dir = resolve_app_data_dir(env=env)
    settings_path = app_settings_path(data_dir)
    app_settings = load_app_settings(settings_path)
    screen_text_enabled = app_settings.screen_text.enabled
    screenshot_collector_env = _env_bool_override(
        env,
        MELONE_SCREENSHOT_COLLECTOR_ENABLED_ENV,
    )
    screen_search_workers_env = _env_bool_override(
        env,
        MELONE_SCREEN_SEARCH_WORKERS_ENABLED_ENV,
    )
    config = ServiceConfig(
        app_name=APP_NAME,
        data_dir=data_dir,
        database_path=data_dir / "melone.sqlite",
        pid_file_path=data_dir / "melone.pid",
        lock_file_path=data_dir / "melone.lock",
        pause_flag_path=data_dir / "melone.paused",
        logs_dir=data_dir / "logs",
        screenshots_dir=data_dir / "screenshots",
        settings_path=settings_path,
        ocr_provider=_env_text(
            env,
            MELONE_OCR_PROVIDER_ENV,
            DEFAULT_OCR_PROVIDER,
            legacy_names=(LEGACY_OCR_PROVIDER_ENV,),
        ),
        ocr_endpoint=_env_text(
            env,
            MELONE_OCR_ENDPOINT_ENV,
            DEFAULT_OCR_ENDPOINT,
            legacy_names=(LEGACY_OCR_ENDPOINT_ENV,),
        ),
        ocr_model=_env_text(
            env,
            MELONE_OCR_MODEL_ENV,
            DEFAULT_OCR_MODEL,
            legacy_names=(LEGACY_OCR_MODEL_ENV,),
        ),
        ocr_timeout_seconds=_env_float(
            env,
            MELONE_OCR_TIMEOUT_SECONDS_ENV,
            DEFAULT_OCR_TIMEOUT_SECONDS,
            legacy_names=(LEGACY_OCR_TIMEOUT_SECONDS_ENV,),
        ),
        ocr_max_tokens=_env_int(
            env,
            MELONE_OCR_MAX_TOKENS_ENV,
            DEFAULT_OCR_MAX_TOKENS,
            minimum=1,
            legacy_names=(LEGACY_OCR_MAX_TOKENS_ENV,),
        ),
        ocr_recognition_languages=_env_list(
            env,
            MELONE_OCR_LANGUAGES_ENV,
            DEFAULT_OCR_RECOGNITION_LANGUAGES,
        ),
        ocr_language_correction=_env_bool(
            env,
            MELONE_OCR_LANGUAGE_CORRECTION_ENV,
            DEFAULT_OCR_LANGUAGE_CORRECTION,
        ),
        screen_text_search_enabled=screen_text_enabled,
        screenshot_collector_enabled=(
            screenshot_collector_env
            if screenshot_collector_env is not None
            else screen_text_enabled
        ),
        screen_search_workers_enabled=(
            screen_search_workers_env
            if screen_search_workers_env is not None
            else screen_text_enabled
        ),
        screenshot_collector_development_override=screenshot_collector_env,
        screen_search_workers_development_override=screen_search_workers_env,
        screenshot_collector_development_override_enabled=(
            screenshot_collector_env is True
        ),
        screen_search_workers_development_override_enabled=(
            screen_search_workers_env is True
        ),
        screen_search_max_jobs_per_tick=_env_int(
            env,
            MELONE_SCREEN_SEARCH_MAX_JOBS_PER_TICK_ENV,
            DEFAULT_SCREEN_SEARCH_MAX_JOBS_PER_TICK,
            minimum=1,
        ),
        screen_search_retry_backoff_seconds=_env_int(
            env,
            MELONE_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS_ENV,
            DEFAULT_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS,
            minimum=1,
        ),
        screen_search_high_backlog_threshold=_env_int(
            env,
            MELONE_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD_ENV,
            DEFAULT_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD,
            minimum=1,
        ),
        screen_search_very_high_backlog_threshold=_env_int(
            env,
            MELONE_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD_ENV,
            DEFAULT_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD,
            minimum=1,
        ),
        context_rank_refresh_min_interval_seconds=_env_float(
            env,
            MELONE_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS_ENV,
            DEFAULT_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS,
        ),
        screen_text_retain_screenshots=_env_bool(
            env,
            MELONE_SCREEN_TEXT_RETAIN_SCREENSHOTS_ENV,
            DEFAULT_SCREEN_TEXT_RETAIN_SCREENSHOTS,
        ),
        semantic_search_enabled=_env_bool(
            env,
            MELONE_SEMANTIC_SEARCH_ENABLED_ENV,
            DEFAULT_SEMANTIC_SEARCH_ENABLED,
        ),
        embedding_model=_env_text(
            env,
            MELONE_EMBEDDING_MODEL_ENV,
            DEFAULT_EMBEDDING_MODEL,
        ),
        embedding_dimension=_env_int_choice(
            env,
            MELONE_EMBEDDING_DIMENSION_ENV,
            DEFAULT_EMBEDDING_DIMENSION,
            choices=SUPPORTED_EMBEDDING_DIMENSIONS,
        ),
        embedding_batch_size=_env_int(
            env,
            MELONE_EMBEDDING_BATCH_SIZE_ENV,
            DEFAULT_EMBEDDING_BATCH_SIZE,
            minimum=1,
        ),
        semantic_search_candidate_limit=_env_int(
            env,
            MELONE_SEMANTIC_SEARCH_CANDIDATE_LIMIT_ENV,
            DEFAULT_SEMANTIC_SEARCH_CANDIDATE_LIMIT,
            minimum=1,
        ),
    )
    if (
        config.screen_search_very_high_backlog_threshold
        < config.screen_search_high_backlog_threshold
    ):
        raise ValueError(
            f"{MELONE_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD_ENV} "
            f"must be greater than or equal to "
            f"{MELONE_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD_ENV}"
        )
    ensure_runtime_paths(config)
    return config


def is_screenshot_collection_enabled(config: ServiceConfig) -> bool:
    return _dynamic_screen_text_feature_enabled(
        base_enabled=config.screenshot_collector_enabled,
        development_override=config.screenshot_collector_development_override,
    )


def is_screen_search_workers_enabled(config: ServiceConfig) -> bool:
    return _dynamic_screen_text_feature_enabled(
        base_enabled=config.screen_search_workers_enabled,
        development_override=config.screen_search_workers_development_override,
    )


def _env_text(
    env: Mapping[str, str],
    name: str,
    default: str,
    *,
    legacy_names: tuple[str, ...] = (),
) -> str:
    value = _env_value(env, name, legacy_names=legacy_names)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_float(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    legacy_names: tuple[str, ...] = (),
) -> float:
    value = _env_value(env, name, legacy_names=legacy_names)
    if value is None or not value.strip():
        return default

    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc

    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _env_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    legacy_names: tuple[str, ...] = (),
) -> int:
    value = _env_value(env, name, legacy_names=legacy_names)
    if value is None or not value.strip():
        return default

    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc

    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return parsed


def _env_int_choice(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    choices: tuple[int, ...],
) -> int:
    parsed = _env_int(env, name, default, minimum=1)
    if parsed not in choices:
        allowed = ", ".join(str(choice) for choice in choices[:-1])
        raise ValueError(f"{name} must be one of {allowed}, or {choices[-1]}")
    return parsed


def _env_list(
    env: Mapping[str, str],
    name: str,
    default: tuple[str, ...],
    *,
    legacy_names: tuple[str, ...] = (),
) -> tuple[str, ...]:
    # Comma-separated list (e.g. "ko-KR,en-US"). Blank/unset falls back to the
    # default; a value of "auto" clears the list so the backend auto-detects.
    value = _env_value(env, name, legacy_names=legacy_names)
    if value is None or not value.strip():
        return default
    if value.strip().casefold() == "auto":
        return ()
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    # A separators-only value (e.g. ",") parses to nothing; keep the documented
    # default rather than silently switching to auto-detect.
    return items or default


def _env_value(
    env: Mapping[str, str],
    name: str,
    *,
    legacy_names: tuple[str, ...] = (),
) -> str | None:
    value = env.get(name)
    if value is not None and value.strip():
        return value
    for legacy_name in legacy_names:
        legacy_value = env.get(legacy_name)
        if legacy_value is not None and legacy_value.strip():
            return legacy_value
    return value


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None or not value.strip():
        return default

    return _parse_env_bool(name, value)


def _env_bool_override(env: Mapping[str, str], name: str) -> bool | None:
    value = env.get(name)
    if value is None or not value.strip():
        return None
    return _parse_env_bool(name, value)


def _parse_env_bool(name: str, value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _dynamic_screen_text_feature_enabled(
    *,
    base_enabled: bool,
    development_override: bool | None,
) -> bool:
    if development_override is not None:
        return development_override
    return base_enabled
