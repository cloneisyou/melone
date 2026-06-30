from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from melone_service.models import (
    AppContext,
    NormalizedEvent,
    WindowContext,
    create_event_id,
    utc_timestamp,
)


def normalize_event(
    event_type: str,
    *,
    app: AppContext | Mapping[str, Any] | None = None,
    window: WindowContext | Mapping[str, Any] | None = None,
    url: str | None = None,
    source: str = "macos",
    metadata: Mapping[str, Any] | None = None,
    timestamp: datetime | None = None,
    event_id: str | None = None,
) -> NormalizedEvent:
    # 수집기별 입력 형식을 공통 이벤트 모델로 바꾸고 필수 문자열을 검증합니다.
    event_type = event_type.strip()
    source = source.strip()
    if not event_type:
        raise ValueError("event_type is required")
    if not source:
        raise ValueError("source is required")

    return NormalizedEvent(
        id=event_id or create_event_id(),
        timestamp=utc_timestamp(timestamp),
        type=event_type,
        app=_coerce_app_context(app),
        window=_coerce_window_context(window),
        url=_optional_string(url),
        source=source,
        metadata=dict(metadata or {}),
    )


def _coerce_app_context(value: AppContext | Mapping[str, Any] | None) -> AppContext:
    # dict와 AppContext를 모두 허용해 수집기 쪽 호출 코드를 단순하게 둡니다.
    if value is None:
        return AppContext()
    if isinstance(value, AppContext):
        return value

    return AppContext(
        name=_optional_string(value.get("name")),
        bundle_id=_optional_string(value.get("bundle_id")),
        pid=_optional_int(value.get("pid")),
    )


def _coerce_window_context(
    value: WindowContext | Mapping[str, Any] | None,
) -> WindowContext:
    # 창 정보가 없거나 dict로 들어와도 저장소에는 WindowContext 형태로 넘깁니다.
    if value is None:
        return WindowContext()
    if isinstance(value, WindowContext):
        return value

    return WindowContext(
        title=_optional_string(value.get("title")),
        display_id=_optional_int(value.get("display_id")),
    )


def _optional_string(value: Any) -> str | None:
    # 비어 있는 값은 None으로 통일해 DB와 출력에서 같은 의미로 다룹니다.
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    # pid나 display_id처럼 선택적인 숫자 필드를 문자열 입력까지 허용합니다.
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    return int(text)
