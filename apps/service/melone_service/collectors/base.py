from __future__ import annotations

from typing import Protocol

from melone_service.models import NormalizedEvent


class Collector(Protocol):
    # 서비스 루프가 collector 구현을 동일하게 호출하기 위한 최소 인터페이스입니다.
    name: str

    def poll(self) -> list[NormalizedEvent]:
        """Return normalized events captured since the previous poll."""
