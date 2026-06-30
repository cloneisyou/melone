from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# "지금 보고 있는" 대상을 하나의 URI로 통일. local_file이 web_url보다 우선.
AssetKind = Literal["local_file", "web_url"]


def kind_for_uri(uri: str) -> AssetKind:
    return "local_file" if uri.startswith("file://") else "web_url"


@dataclass(frozen=True)
class Asset:
    kind: AssetKind
    uri: str
    source: str
    title: str | None = None
    confidence: float = 1.0
    candidates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.uri:
            raise ValueError("Asset.uri must be non-empty")

    def identity(self) -> tuple[AssetKind, str]:
        return self.kind, self.uri


class AssetPermissionError(RuntimeError):
    def __init__(
        self,
        permission: str,  # "accessibility" | "automation"
        *,
        source: str,
        bundle_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.permission = permission
        self.source = source
        self.bundle_id = bundle_id
        self.detail = detail
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"{source}: {permission} permission blocked{suffix}")
