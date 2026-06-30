"""Newline-delimited JSON-RPC 2.0 stdio loop for the Electron shell.

One line = one message (UTF-8, \\n-terminated); stdout is protocol-only —
logs and diagnostics must go to stderr. Requests without an id
(notifications) also always get a response — the only client is our own
Electron shell, which reads one reply per line it writes.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
import traceback
from typing import TextIO

from melone_service import __version__
from melone_service.rpc.errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    RpcError,
)
from melone_service.rpc.methods import dispatch

JSONRPC_VERSION = "2.0"

# error.data shown to users; raw exception details go to stderr only.
INTERNAL_ERROR_DATA = "요청 처리 중 내부 오류가 발생했습니다"


def handle_line(line: str) -> str | None:
    """Process one input line into one response JSON line (None for blank)."""
    if not line.strip():
        return None

    try:
        request = json.loads(line)
    except json.JSONDecodeError as error:
        # The request is unreadable, so respond with id null per JSON-RPC.
        return _dump(
            _error_response(
                None, RpcError(PARSE_ERROR, "PARSE_ERROR", f"JSON 파싱 실패: {error}")
            )
        )

    response = handle_request(request)
    try:
        return _dump(response)
    except (TypeError, ValueError):
        # A handler returned something json.dumps cannot encode; convert it to
        # an INTERNAL_ERROR response so the daemon survives the handler bug.
        traceback.print_exc(file=sys.stderr)
        return _dump(
            _error_response(
                response.get("id"),
                RpcError(INTERNAL_ERROR, "INTERNAL_ERROR", INTERNAL_ERROR_DATA),
            )
        )


def handle_request(request: object) -> dict[str, object]:
    """Dispatch a parsed request object and always return a response dict.

    The jsonrpc version field is not validated — the only client is our own
    Electron shell, so strict validation buys nothing.
    """
    request_id = request.get("id") if isinstance(request, dict) else None
    if not isinstance(request, dict) or not isinstance(request.get("method"), str):
        return _error_response(
            request_id,
            RpcError(INVALID_REQUEST, "INVALID_REQUEST", "요청은 method 문자열을 가진 객체여야 합니다"),
        )

    try:
        result = dispatch(request["method"], request.get("params"))
    except RpcError as error:
        return _error_response(request_id, error)
    except Exception:  # noqa: BLE001
        # A single handler bug must not kill the long-lived daemon; convert it
        # to a generic response and log the traceback to stderr only.
        traceback.print_exc(file=sys.stderr)
        return _error_response(
            request_id,
            RpcError(INTERNAL_ERROR, "INTERNAL_ERROR", INTERNAL_ERROR_DATA),
        )
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _stop_collector_on_exit() -> None:
    """Stop the collector this daemon started so it cannot outlive the daemon
    while holding the SQLite WAL lock.

    macOS-only: there is no collector on other platforms, and ``main`` cannot
    even be imported off darwin (it needs ``fcntl``). Best-effort — a failure
    here must never mask the real shutdown.
    """
    if sys.platform != "darwin":
        return
    try:
        from melone_service import main

        main.stop_service()
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Main loop: read requests from stdin, write responses to stdout.

    On exit — stdin EOF (Electron gone) or SIGTERM/SIGINT — the collector the
    daemon spawned is stopped in the ``finally`` so it does not orphan and keep
    the database locked across an app quit/update/reinstall.
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout

    try:
        while True:
            line = stdin.readline()
            if line == "":
                break

            response = handle_line(line)
            if response is None:
                continue
            stdout.write(response + "\n")
            stdout.flush()
    except (KeyboardInterrupt, SystemExit):
        # A signal unwound the loop; fall through to collector cleanup.
        pass
    finally:
        _stop_collector_on_exit()


def _install_shutdown_signals() -> None:
    # Electron's child.kill() sends SIGTERM; without a handler the daemon dies
    # abruptly and skips serve()'s collector cleanup. Raise SystemExit so the
    # loop unwinds into its finally. Main-thread only (signals can't be set off it).
    if threading.current_thread() is not threading.main_thread():
        return

    def _request_exit(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _request_exit)
    except (ValueError, OSError):
        pass


def main() -> int:
    # Pin real stdio to UTF-8 with \n endings so the Windows console default
    # (cp949) and \r\n translation cannot break the protocol.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    _install_shutdown_signals()
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    print(
        f"melone rpc daemon v{__version__} (python {python_version}) ready",
        file=sys.stderr,
        flush=True,
    )
    serve()
    return 0


def _error_response(request_id: object, error: RpcError) -> dict[str, object]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error.to_payload()}


def _dump(response: dict[str, object]) -> str:
    # json.dumps without indent emits no newlines, preserving one line = one message.
    return json.dumps(response, ensure_ascii=False)
