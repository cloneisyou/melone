from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def create_event_id() -> str:
    # 이벤트 저장소에서 충돌 없이 추적할 수 있는 고유 ID를 만듭니다.
    return f"evt_{uuid4().hex}"


def utc_now() -> datetime:
    # 서비스 전역에서 같은 기준의 UTC 시간을 쓰기 위한 작은 래퍼입니다.
    return datetime.now(timezone.utc)


def utc_timestamp(value: datetime | None = None) -> str:
    # DB 정렬과 CLI 출력이 안정적이도록 모든 시간을 UTC ISO 문자열로 맞춥니다.
    timestamp = utc_now() if value is None else value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)

    return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class AppContext:
    # 이벤트가 발생한 앱 정보를 담는 최소 컨텍스트입니다.
    name: str | None = None
    bundle_id: str | None = None
    pid: int | None = None


@dataclass(frozen=True)
class WindowContext:
    # 이벤트가 발생한 창의 식별 가능한 정보를 담습니다.
    title: str | None = None
    display_id: int | None = None


@dataclass(frozen=True)
class NormalizedEvent:
    # 수집기, 파이프라인, 저장소가 공통으로 주고받는 표준 이벤트 모델입니다.
    type: str
    id: str = field(default_factory=create_event_id)
    timestamp: str = field(default_factory=utc_timestamp)
    app: AppContext = field(default_factory=AppContext)
    window: WindowContext = field(default_factory=WindowContext)
    url: str | None = None
    source: str = "macos"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def app_name(self) -> str | None:
        return self.app.name

    @property
    def bundle_id(self) -> str | None:
        return self.app.bundle_id

    @property
    def pid(self) -> int | None:
        return self.app.pid

    @property
    def window_title(self) -> str | None:
        return self.window.title
