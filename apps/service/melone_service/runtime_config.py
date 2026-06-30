from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from .config import (
    DEFAULT_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD,
    DEFAULT_SCREEN_SEARCH_MAX_JOBS_PER_TICK,
    DEFAULT_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS,
    DEFAULT_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD,
    DEFAULT_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS,
    DEFAULT_SCREEN_TEXT_RETAIN_SCREENSHOTS,
    DEFAULT_EMBEDDING_BATCH_SIZE,
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_OCR_ENDPOINT,
    DEFAULT_OCR_MAX_TOKENS,
    DEFAULT_OCR_MODEL,
    DEFAULT_OCR_LANGUAGE_CORRECTION,
    DEFAULT_OCR_PROVIDER,
    DEFAULT_OCR_RECOGNITION_LANGUAGES,
    DEFAULT_OCR_TIMEOUT_SECONDS,
    DEFAULT_SEMANTIC_SEARCH_CANDIDATE_LIMIT,
    DEFAULT_SEMANTIC_SEARCH_ENABLED,
    LEGACY_OCR_ENDPOINT_ENV,
    LEGACY_OCR_MAX_TOKENS_ENV,
    LEGACY_OCR_MODEL_ENV,
    LEGACY_OCR_PROVIDER_ENV,
    LEGACY_OCR_TIMEOUT_SECONDS_ENV,
    MELONE_EMBEDDING_BATCH_SIZE_ENV,
    MELONE_EMBEDDING_DIMENSION_ENV,
    MELONE_EMBEDDING_MODEL_ENV,
    MELONE_HOME_ENV,
    MELONE_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD_ENV,
    MELONE_SCREEN_SEARCH_MAX_JOBS_PER_TICK_ENV,
    MELONE_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS_ENV,
    MELONE_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD_ENV,
    MELONE_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS_ENV,
    MELONE_SCREEN_SEARCH_WORKERS_ENABLED_ENV,
    MELONE_SCREEN_TEXT_RETAIN_SCREENSHOTS_ENV,
    MELONE_SCREENSHOT_COLLECTOR_ENABLED_ENV,
    MELONE_SEMANTIC_SEARCH_CANDIDATE_LIMIT_ENV,
    MELONE_SEMANTIC_SEARCH_ENABLED_ENV,
    MELONE_OCR_ENDPOINT_ENV,
    MELONE_OCR_LANGUAGES_ENV,
    MELONE_OCR_LANGUAGE_CORRECTION_ENV,
    MELONE_OCR_MAX_TOKENS_ENV,
    MELONE_OCR_MODEL_ENV,
    MELONE_OCR_PROVIDER_ENV,
    MELONE_OCR_TIMEOUT_SECONDS_ENV,
)


RuntimeScope = Literal[
    "product",
    "developer",
    "advanced-backend",
    "integration",
    "secret",
]


@dataclass(frozen=True)
class RuntimeParameter:
    name: str
    scope: RuntimeScope
    default: str
    used_by: tuple[str, ...]
    required_for: str
    description: str
    secret: bool = False
    aliases: tuple[str, ...] = ()

    def current_value(self, env: dict[str, str] | None = None) -> str | None:
        env = os.environ if env is None else env
        value = _current_env_value(env, self.name, aliases=self.aliases)
        if value is None or value.strip() == "":
            return None
        return "<set>" if self.secret else value.strip()


RUNTIME_PARAMETERS: tuple[RuntimeParameter, ...] = (
    RuntimeParameter(
        name=MELONE_HOME_ENV,
        scope="developer",
        default="~/Library/Application Support/Melone",
        used_by=("service", "desktop"),
        required_for="local dev isolation only",
        description="Overrides the app data directory for DB, logs, settings, and screenshots.",
    ),
    RuntimeParameter(
        name="MELONE_PYTHON",
        scope="developer",
        default="desktop resolver: dev venv, then PATH python",
        used_by=("desktop",),
        required_for="custom Python runtime only",
        description="Forces the desktop shell to launch the service with a specific Python.",
    ),
    RuntimeParameter(
        name=MELONE_SCREENSHOT_COLLECTOR_ENABLED_ENV,
        scope="developer",
        default="unset; follows persistent Screen Text Search setting",
        used_by=("service",),
        required_for="development override only",
        description="Overrides screenshot capture on/off without changing the product setting.",
    ),
    RuntimeParameter(
        name=MELONE_SCREEN_SEARCH_WORKERS_ENABLED_ENV,
        scope="developer",
        default="unset; follows persistent Screen Text Search setting",
        used_by=("service",),
        required_for="development override only",
        description="Overrides Screen Text Search indexing workers on/off.",
    ),
    RuntimeParameter(
        name=MELONE_SCREEN_TEXT_RETAIN_SCREENSHOTS_ENV,
        scope="developer",
        default=str(DEFAULT_SCREEN_TEXT_RETAIN_SCREENSHOTS).lower(),
        used_by=("service",),
        required_for="debugging retained screenshots only",
        description="Keeps original PNG screenshots after successful indexing when true.",
    ),
    RuntimeParameter(
        name=MELONE_SEMANTIC_SEARCH_ENABLED_ENV,
        scope="developer",
        default=str(DEFAULT_SEMANTIC_SEARCH_ENABLED).lower(),
        used_by=("service",),
        required_for="semantic search rollout only",
        description="Enables semantic OCR search when the optional semantic extra is installed.",
    ),
    RuntimeParameter(
        name=MELONE_SCREEN_SEARCH_MAX_JOBS_PER_TICK_ENV,
        scope="developer",
        default=str(DEFAULT_SCREEN_SEARCH_MAX_JOBS_PER_TICK),
        used_by=("service",),
        required_for="indexing throughput tuning",
        description="Maximum finalize/OCR jobs processed in one service tick.",
    ),
    RuntimeParameter(
        name=MELONE_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS_ENV,
        scope="developer",
        default=str(DEFAULT_SCREEN_SEARCH_RETRY_BACKOFF_SECONDS),
        used_by=("service",),
        required_for="retry tuning",
        description="Fixed delay before retrying retryable OCR jobs.",
    ),
    RuntimeParameter(
        name=MELONE_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD_ENV,
        scope="developer",
        default=str(DEFAULT_SCREEN_SEARCH_HIGH_BACKLOG_THRESHOLD),
        used_by=("service",),
        required_for="capture backpressure tuning",
        description="Backlog size where screenshot capture interval doubles.",
    ),
    RuntimeParameter(
        name=MELONE_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD_ENV,
        scope="developer",
        default=str(DEFAULT_SCREEN_SEARCH_VERY_HIGH_BACKLOG_THRESHOLD),
        used_by=("service",),
        required_for="capture backpressure tuning",
        description="Backlog size where transition-frame-only capture starts.",
    ),
    RuntimeParameter(
        name=MELONE_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS_ENV,
        scope="developer",
        default=str(DEFAULT_CONTEXT_RANK_REFRESH_MIN_INTERVAL_SECONDS),
        used_by=("service",),
        required_for="context-rank refresh cadence tuning",
        description=(
            "Minimum seconds between context-rank cache recomputes. Refresh is "
            "skipped entirely when no new relevant event has arrived since the "
            "last compute."
        ),
    ),
    RuntimeParameter(
        name=MELONE_OCR_PROVIDER_ENV,
        scope="advanced-backend",
        default=DEFAULT_OCR_PROVIDER,
        used_by=("service",),
        required_for="advanced OCR backend selection",
        description="OCR provider. Product default is Apple Vision; local VLM providers are advanced/dev paths.",
        aliases=(LEGACY_OCR_PROVIDER_ENV,),
    ),
    RuntimeParameter(
        name=MELONE_OCR_LANGUAGES_ENV,
        scope="advanced-backend",
        default=",".join(DEFAULT_OCR_RECOGNITION_LANGUAGES),
        used_by=("service",),
        required_for="Apple Vision recognition accuracy",
        description=(
            "Comma-separated recognition languages for the default Apple Vision "
            "backend (e.g. 'ko-KR,en-US'). Wrong/empty languages yield empty or "
            "garbled text. Set 'auto' to let Vision auto-detect."
        ),
    ),
    RuntimeParameter(
        name=MELONE_OCR_LANGUAGE_CORRECTION_ENV,
        scope="advanced-backend",
        default=str(DEFAULT_OCR_LANGUAGE_CORRECTION).lower(),
        used_by=("service",),
        required_for="Apple Vision recognition tuning",
        description=(
            "Enables Apple Vision language correction. Off by default because it "
            "autocorrects code, identifiers, paths, and URLs on screen. Set true "
            "for prose-heavy content."
        ),
    ),
    RuntimeParameter(
        name=MELONE_OCR_ENDPOINT_ENV,
        scope="advanced-backend",
        default=DEFAULT_OCR_ENDPOINT,
        used_by=("service",),
        required_for="local OpenAI-compatible VLM only",
        description="Base URL for local OpenAI-compatible advanced OCR backends.",
        aliases=(LEGACY_OCR_ENDPOINT_ENV,),
    ),
    RuntimeParameter(
        name=MELONE_OCR_MODEL_ENV,
        scope="advanced-backend",
        default=DEFAULT_OCR_MODEL,
        used_by=("service",),
        required_for="local OpenAI-compatible VLM only",
        description="Model name sent to local advanced OCR backends.",
        aliases=(LEGACY_OCR_MODEL_ENV,),
    ),
    RuntimeParameter(
        name=MELONE_OCR_TIMEOUT_SECONDS_ENV,
        scope="advanced-backend",
        default=str(int(DEFAULT_OCR_TIMEOUT_SECONDS)),
        used_by=("service",),
        required_for="provider timeout tuning",
        description="OCR provider request timeout in seconds.",
        aliases=(LEGACY_OCR_TIMEOUT_SECONDS_ENV,),
    ),
    RuntimeParameter(
        name=MELONE_OCR_MAX_TOKENS_ENV,
        scope="advanced-backend",
        default=str(DEFAULT_OCR_MAX_TOKENS),
        used_by=("service",),
        required_for="local OpenAI-compatible VLM only",
        description="Max tokens sent to local advanced OCR backends.",
        aliases=(LEGACY_OCR_MAX_TOKENS_ENV,),
    ),
    RuntimeParameter(
        name=MELONE_EMBEDDING_MODEL_ENV,
        scope="advanced-backend",
        default=DEFAULT_EMBEDDING_MODEL,
        used_by=("service",),
        required_for="semantic search model selection",
        description="Embedding model identifier used by semantic OCR search.",
    ),
    RuntimeParameter(
        name=MELONE_EMBEDDING_DIMENSION_ENV,
        scope="advanced-backend",
        default=str(DEFAULT_EMBEDDING_DIMENSION),
        used_by=("service",),
        required_for="semantic search embedding size tuning",
        description="Embedding dimension for semantic OCR search; supported values are 128, 256, 512, and 768.",
    ),
    RuntimeParameter(
        name=MELONE_EMBEDDING_BATCH_SIZE_ENV,
        scope="advanced-backend",
        default=str(DEFAULT_EMBEDDING_BATCH_SIZE),
        used_by=("service",),
        required_for="semantic indexing throughput tuning",
        description="Maximum OCR chunks encoded in one semantic indexing batch.",
    ),
    RuntimeParameter(
        name=MELONE_SEMANTIC_SEARCH_CANDIDATE_LIMIT_ENV,
        scope="advanced-backend",
        default=str(DEFAULT_SEMANTIC_SEARCH_CANDIDATE_LIMIT),
        used_by=("service",),
        required_for="semantic search candidate tuning",
        description="Maximum semantic OCR candidates considered for a search query.",
    ),
    RuntimeParameter(
        name="MELONE_GOOGLE_CLIENT_ID",
        scope="secret",
        default="unset; Google sign-in disabled",
        used_by=("desktop",),
        required_for="Google sign-in",
        description="Desktop OAuth client ID for Google sign-in.",
        secret=True,
    ),
    RuntimeParameter(
        name="MELONE_GOOGLE_CLIENT_SECRET",
        scope="secret",
        default="unset; Google sign-in disabled",
        used_by=("desktop",),
        required_for="Google sign-in",
        description="Desktop OAuth client secret for Google sign-in.",
        secret=True,
    ),
    RuntimeParameter(
        name="MELONE_FAKE_UPDATE",
        scope="integration",
        default="unset",
        used_by=("desktop",),
        required_for="desktop update UI simulation",
        description="Set to 1 in development to exercise the update banner flow.",
    ),
)


def runtime_parameters(
    *,
    scope: RuntimeScope | None = None,
) -> tuple[RuntimeParameter, ...]:
    if scope is None:
        return RUNTIME_PARAMETERS
    return tuple(parameter for parameter in RUNTIME_PARAMETERS if parameter.scope == scope)


def configured_runtime_parameters(
    env: dict[str, str] | None = None,
) -> tuple[RuntimeParameter, ...]:
    env = os.environ if env is None else env
    return tuple(
        parameter
        for parameter in RUNTIME_PARAMETERS
        if parameter.current_value(env) is not None
    )


def _current_env_value(
    env: dict[str, str],
    name: str,
    *,
    aliases: tuple[str, ...],
) -> str | None:
    value = env.get(name)
    if value is not None and value.strip():
        return value
    for alias in aliases:
        alias_value = env.get(alias)
        if alias_value is not None and alias_value.strip():
            return alias_value
    return value
