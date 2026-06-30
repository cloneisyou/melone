from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Protocol

from melone_service.collectors.active_window import ActiveWindowSnapshot

from .model import Asset, AssetPermissionError


class URIResolver(Protocol):
    source: str

    def handles(self, snapshot: ActiveWindowSnapshot) -> bool:
        """이 resolver가 담당하는 foreground 앱인지."""

    def resolve(self, snapshot: ActiveWindowSnapshot) -> Asset | None:
        """Asset 반환. 담당 앱이지만 에셋이 없으면 None. 권한이 막히면 AssetPermissionError."""


class ChainResolver:
    # specificity 순서로 두고 첫 non-None이 이김. 권한 실패는 출력 후 다음 resolver로.
    def __init__(
        self,
        resolvers: Sequence[URIResolver],
        *,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._resolvers = tuple(resolvers)
        self._log = log or _stderr_log
        self._blocked: set[str] = set()

    def resolve(self, snapshot: ActiveWindowSnapshot) -> Asset | None:
        for resolver in self._resolvers:
            if not resolver.handles(snapshot):
                continue
            try:
                asset = resolver.resolve(snapshot)
            except AssetPermissionError as exc:
                if resolver.source not in self._blocked:
                    self._blocked.add(resolver.source)
                    self._log(f"asset resolver {resolver.source} blocked: {exc}")
                continue
            self._blocked.discard(resolver.source)
            if asset is not None:
                return asset
        return None


def _stderr_log(message: str) -> None:
    print(message, file=sys.stderr)
