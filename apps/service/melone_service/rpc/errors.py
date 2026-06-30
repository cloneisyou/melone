"""JSON-RPC error vocabulary shared by the stdio loop and method handlers."""

from __future__ import annotations

# JSON-RPC standard codes plus app-specific codes (docs/prod/desktop-plan.md error contract).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_SUPPORTED_ON_PLATFORM = -32001
CONFIG_PARSE_ERROR = -32003
SERVICE_ERROR = -32004


class RpcError(Exception):
    """Exception serialized into a JSON-RPC error response.

    `message` is a machine-readable symbol (e.g. NOT_SUPPORTED_ON_PLATFORM);
    the human-readable text goes in `data` (docs/prod/desktop-plan.md error contract).
    """

    def __init__(self, code: int, message: str, data: str | None = None) -> None:
        super().__init__(message if data is None else f"{message}: {data}")
        self.code = code
        self.message = message
        self.data = data

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload
