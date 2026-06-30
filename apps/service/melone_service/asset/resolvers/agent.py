from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from melone_service.collectors.active_window import ActiveWindowSnapshot

from ..model import Asset
from .agent_sessions import (
    AgentConversation,
    AgentConversationCollector,
    _resolve,
    default_collectors,
)

# CLI는 세션 .jsonl(local_file), 데스크톱 AI 앱은 채팅 URL(web_url).

_WEB_SCHEMES = ("http://", "https://")
# resolve()는 매 poll마다 ps/lsof를 띄우므로, foreground 창 상태가 같으면 잠깐 캐시한다.
# poll 간격(~1s)보다 길어야 실제로 spawn을 건너뛴다.
_DEFAULT_TTL_SECONDS = 2.0


class AgentURIResolver:
    source = "agent"

    def __init__(
        self,
        *,
        collectors: Sequence[AgentConversationCollector] | None = None,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._collectors = (
            list(collectors) if collectors is not None else default_collectors()
        )
        self._ttl_seconds = ttl_seconds
        self._monotonic = monotonic or time.monotonic
        self._cache: tuple[tuple, Asset | None, float] | None = None

    def handles(self, snapshot: ActiveWindowSnapshot) -> bool:
        bundle_id = snapshot.bundle_id
        if not bundle_id:
            return False
        return any(bundle_id in c.bundle_ids for c in self._collectors)

    def resolve(self, snapshot: ActiveWindowSnapshot) -> Asset | None:
        now = self._monotonic()
        key = (snapshot.bundle_id, snapshot.pid, snapshot.window_title)
        if self._cache is not None and self._cache[0] == key and now < self._cache[2]:
            return self._cache[1]

        matching = [c for c in self._collectors if snapshot.bundle_id in c.bundle_ids]
        asset = _to_asset(_resolve(snapshot, matching))
        self._cache = (key, asset, now + self._ttl_seconds)
        return asset


def _to_asset(conversation: AgentConversation | None) -> Asset | None:
    # url이 확정된 경우에만 Asset.
    if conversation is None or not conversation.url:
        return None
    url = conversation.url
    kind = "web_url" if url.startswith(_WEB_SCHEMES) else "local_file"
    return Asset(
        kind=kind,
        uri=url,
        source=conversation.connector_name or "agent",
        title=conversation.title,
        candidates=tuple(c["url"] for c in conversation.candidates),
    )
