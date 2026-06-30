from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from melone_service.collectors.active_window import (
    ActiveWindowAPI,
    ActiveWindowSnapshot,
    MacOSActiveWindowAPI,
)

from ..model import AssetPermissionError

# CLI 에이전트(Codex, Claude Code)는 터미널 안에서 돌기 때문에, 
# 이 터미널들이 foreground일 때만 세션 파일을 확인합니다.
TERMINAL_BUNDLE_IDS = frozenset(
    {
        "com.apple.Terminal",
        "com.cmuxterm.app",
    }
)

_CMUX_BUNDLE_ID = "com.cmuxterm.app"
_TERMINAL_BUNDLE_ID = "com.apple.Terminal"
_CODEX_DESKTOP_BUNDLE_ID = "com.openai.codex"

SESSION_FRESHNESS_SECONDS = 180.0
_CLOCK_SKEW_SECONDS = 5.0

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_CLAUDE_CHAT_URL = re.compile(r"https://claude\.ai/chat/(" + _UUID_RE.pattern + r")")

DefaultsReader = Callable[[str, str], str | None]
AXWebAreaReader = Callable[[int], list[dict]]


# url은 랭킹 키(웹은 공개 링크, CLI는 세션 파일 file:// URI). 
# 터미널이 애매하면 url/conversation_id는 None이되 candidates에 fresh 세션을 모두 남깁니다.
@dataclass(frozen=True)
class AgentConversation:
    conversation_id: str | None
    url: str | None = None
    kind: str | None = None
    title: str | None = None
    connector_name: str | None = None
    candidates: list[dict] = field(default_factory=list)

    def identity(self) -> tuple:
        if self.url is not None:
            return (self.conversation_id, self.url)
        # 애매한 순간: candidate URI 집합이 identity
        return (None, None, tuple(sorted(c["url"] for c in self.candidates)))


# bundle_ids는 처리할 foreground 앱, source_type은 대화 저장 위치(web/local)
class AgentConversationCollector:
    name: str = ""
    source_type: str = ""
    bundle_ids: frozenset[str] = frozenset()

    def resolve(self, snapshot: ActiveWindowSnapshot) -> AgentConversation | None:
        raise NotImplementedError
    

# 대화가 웹 URL로 식별되는 에이전트(가능하면 title도 함께).
class WebCollector(AgentConversationCollector):
    source_type = "web"


# 세션 파일에 기록하는 에이전트. 세션의 "URL"은 그 파일의 file:// URI.
class LocalCollector(AgentConversationCollector):
    source_type = "local"
    bundle_ids = TERMINAL_BUNDLE_IDS

    connector_name: str = ""
    pattern: str = ""
    cwd_keys: tuple[str, ...] = ()
    whole_file_json: bool = False 

    def __init__(
        self,
        *,
        root: Path | None = None,
        now: Callable[[], float] | None = None,
        freshness_seconds: float = SESSION_FRESHNESS_SECONDS,
    ) -> None:
        self.root = root if root is not None else self._default_root()
        self.now = now or time.time
        self.freshness_seconds = freshness_seconds
        self._cwd_cache: dict[Path, tuple[float, str | None]] = {}

    def _default_root(self) -> Path:
        raise NotImplementedError

    def resolve(self, snapshot: ActiveWindowSnapshot) -> AgentConversation | None:
        return resolve_sessions(self.fresh_sessions(), snapshot.window_title)

    def fresh_sessions(self) -> list[dict]:
        now = self.now()
        sessions: list[dict] = []
        for path in self.root.glob(self.pattern):
            if _UUID_RE.search(path.name) is None:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            age = now - mtime
            if age > self.freshness_seconds or age < -_CLOCK_SKEW_SECONDS:
                continue
            sessions.append(self._session_dict(path, mtime))
        return sessions

    def find_session(self, session_id: str) -> dict | None:
        for path in self.root.glob(self.pattern):
            if session_id in path.name and _UUID_RE.search(path.name):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    return None
                return self._session_dict(path, mtime)
        return None

    def _session_dict(self, path: Path, mtime: float) -> dict:
        return {
            "connector": self.connector_name,
            "conversation_id": _UUID_RE.search(path.name).group(0),
            "cwd": self._cwd_for(path, mtime),
            "updated_at": mtime,
            "url": path.as_uri(),
        }

    def _cwd_for(self, path: Path, mtime: float) -> str | None:
        cached = self._cwd_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        read = _read_json_cwd if self.whole_file_json else _read_session_cwd
        cwd = read(path, self.cwd_keys)
        self._cwd_cache[path] = (mtime, cwd)
        return cwd


# ChatGPT Desktop
# : 마지막 선택 대화를 UserDefaults에 기록. remote(서버 동기화)만 공개 URL.
class OpenAIWebCollector(WebCollector):
    name = "chatgpt_desktop"
    bundle_ids = frozenset({"com.openai.chat"})

    def __init__(self, *, defaults_reader: DefaultsReader | None = None) -> None:
        self._read_default = defaults_reader or _read_default

    def resolve(self, snapshot: ActiveWindowSnapshot) -> AgentConversation | None:
        raw = self._read_default("com.openai.chat", "lastSelectedConversation")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return _missing(f"chatgpt: lastSelectedConversation이 JSON이 아님 ({raw!r})")

        identifier = data.get("conversationID") if isinstance(data, dict) else None
        if not isinstance(identifier, dict) or not identifier:
            return _missing(f"chatgpt: conversationID 형식이 예상과 다름 ({data!r})")

        # conversationID = {"remote"|"local"|...: {"_0": "<id>"}}. id는 _0에서만 읽습니다
        # (localID 폴백은 local을 remote로 오인해 가짜 공개 URL을 만들 수 있음).
        kind = next(iter(identifier))
        inner = identifier[kind]
        ident = inner.get("_0") if isinstance(inner, dict) else None

        if not isinstance(ident, str) or not ident:
            return _missing(f"chatgpt: conversationID에서 _0 id를 못 찾음 ({identifier!r})")
        url = f"https://chatgpt.com/c/{ident.lower()}" if kind == "remote" else None
        return AgentConversation(
            conversation_id=ident, url=url, kind=kind, connector_name=self.name
        )


# Claude Desktop(Chat)
# : AXManualAccessibility를 켜면 AXWebArea가 claude.ai/chat/<id>와 title을 노출합니다.
class ClaudeWebCollector(WebCollector):
    name = "claude_desktop"
    bundle_ids = frozenset({"com.anthropic.claudefordesktop"})

    def __init__(self, *, web_area_reader: AXWebAreaReader | None = None) -> None:
        self._read_web_areas = web_area_reader or _read_ax_web_areas

    def resolve(self, snapshot: ActiveWindowSnapshot) -> AgentConversation | None:
        if snapshot.pid is None:
            return None
        # 윈도우의 여러 web area(셸, 첨부 등) 중 대화 URL을 가진 것만 선택.
        for area in self._read_web_areas(snapshot.pid):
            match = _CLAUDE_CHAT_URL.match(area.get("url") or "")
            if match:
                return AgentConversation(
                    conversation_id=match.group(1),
                    url=area["url"],
                    kind="remote",
                    title=_clean_claude_title(area.get("title")),
                    connector_name=self.name,
                )
        return None


# Codex: CLI(터미널) + Desktop + VSCode 확장. 모두 ~/.codex/sessions에 기록.
class OpenAILocalCollector(LocalCollector):
    name = "codex"
    connector_name = "codex_cli"
    bundle_ids = TERMINAL_BUNDLE_IDS | {"com.openai.codex", "com.microsoft.VSCode"}
    pattern = "**/rollout-*.jsonl"
    cwd_keys = ("payload", "cwd")

    def _default_root(self) -> Path:
        return Path.home() / ".codex" / "sessions"


# Claude Code CLI: CLI(터미널) + VSCode 확장. ~/.claude/projects에 기록.
# (Claude Desktop의 Code/Cowork는 별도 디렉터리라 ClaudeDesktop*Collector가 담당)
class ClaudeLocalCollector(LocalCollector):
    name = "claude_code"
    connector_name = "claude_code"
    bundle_ids = TERMINAL_BUNDLE_IDS | {"com.microsoft.VSCode"}
    pattern = "*/*.jsonl"
    cwd_keys = ("cwd",)

    def _default_root(self) -> Path:
        return Path.home() / ".claude" / "projects"


def _claude_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Claude"


# Claude Desktop - Code: 프로젝트를 여는 Code 모드. 앱 전용 디렉터리에 단일 JSON으로
# 기록되며 cwd는 실제 프로젝트 경로입니다.
class ClaudeDesktopCodeCollector(LocalCollector):
    name = "claude_desktop_code"
    connector_name = "claude_desktop_code"
    bundle_ids = frozenset({"com.anthropic.claudefordesktop"})
    pattern = "*/*/local_*.json"
    cwd_keys = ("cwd",)
    whole_file_json = True

    def _default_root(self) -> Path:
        return _claude_support_dir() / "claude-code-sessions"


# Claude Desktop - Cowork: 로컬 에이전트(Cowork) 모드. 같은 형식이지만 cwd는 샌드박스 경로.
class ClaudeDesktopCoworkCollector(LocalCollector):
    name = "claude_desktop_cowork"
    connector_name = "claude_desktop_cowork"
    bundle_ids = frozenset({"com.anthropic.claudefordesktop"})
    pattern = "*/*/local_*.json"
    cwd_keys = ("cwd",)
    whole_file_json = True

    def _default_root(self) -> Path:
        return _claude_support_dir() / "local-agent-mode-sessions"


def resolve_sessions(
    sessions: list[dict], window_title: str | None
) -> AgentConversation | None:
    # 로컬 전용 앱(터미널/IDE)의 세션 판정.
    if not sessions:
        return None
    ordered = _ordered_sessions(sessions)
    if window_title and _looks_like_path(window_title):
        # 셸 프롬프트(창 title이 cwd 경로): 그 cwd의 세션만 본다. 일치가 없으면 이 창은
        # 다른 프로젝트라 무관하므로 아무것도 방출하지 않음(다른 창의 세션 오인 방지).
        target = _normalize_path(window_title)
        matched = [s for s in ordered if s["cwd"] and _normalize_path(s["cwd"]) == target]
        if not matched:
            return None
        return _session_conversation(matched[0] if len(matched) == 1 else None, matched)
    # title이 경로가 아니면(에이전트 실행 중 등): 1개면 고정, 여러 개면 후보 유지.
    return _session_conversation(ordered[0] if len(ordered) == 1 else None, ordered)


def _ordered_sessions(sessions: list[dict]) -> list[dict]:
    # 최신순, 결정적 tie-break.
    return sorted(
        sessions,
        key=lambda s: (-s["updated_at"], s["connector"], s["conversation_id"]),
    )


def _session_conversation(active: dict | None, ordered: list[dict]) -> AgentConversation:
    if active is None:
        return AgentConversation(
            conversation_id=None, url=None, kind="session", candidates=ordered
        )
    front = [active, *(s for s in ordered if s is not active)]
    return AgentConversation(
        conversation_id=active["conversation_id"],
        url=active["url"],
        kind="session",
        connector_name=active["connector"],
        candidates=front,
    )


def _looks_like_path(title: str) -> bool:
    # 터미널 창 title이 cwd 경로처럼 보이는지(셸 프롬프트) 판단.
    stripped = title.strip()
    return stripped.startswith("/") or stripped.startswith("~")


def default_collectors() -> list[AgentConversationCollector]:
    return [
        OpenAIWebCollector(),
        ClaudeWebCollector(),
        OpenAILocalCollector(),
        ClaudeLocalCollector(),
        ClaudeDesktopCodeCollector(),
        ClaudeDesktopCoworkCollector(),
    ]


def current_terminal_sessions(
    *,
    active_window_api: ActiveWindowAPI | None = None,
    collectors: Sequence[AgentConversationCollector] | None = None,
) -> AgentConversation | None:
    # `melone agent-sessions` CLI가 쓰는, 지금 활성인 터미널 세션들의 실시간 스냅샷.
    api = active_window_api or MacOSActiveWindowAPI()
    snapshot = api.get_snapshot()
    if snapshot is None:
        return None
    locals_ = [
        c for c in (collectors or default_collectors()) if isinstance(c, LocalCollector)
    ]
    sessions = [s for c in locals_ for s in c.fresh_sessions()]
    return resolve_sessions(sessions, snapshot.window_title)


def pick_candidate(candidates: list[dict], *, cwd: str) -> dict | None:
    # cwd가 정확히 일치하는 단 하나의 candidate. 0개/2개 이상이면 None(잘못 고르지 않음).
    target = _normalize_path(cwd)
    matches = [
        c for c in candidates if c.get("cwd") and _normalize_path(c["cwd"]) == target
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve(
    snapshot: ActiveWindowSnapshot, matching: Sequence[AgentConversationCollector]
) -> AgentConversation | None:
    # web 대체가 있는 앱(Claude Desktop): 채팅 뷰가 떠 있으면(AX에 /chat/) URL을 우선하고,
    # 아니면(Code/Cowork 뷰) 가장 최근 로컬 세션을 씁니다. 로컬 전용 앱(터미널/IDE)은
    # 여러 CLI 세션을 합쳐 판정합니다.
    web = next((c for c in matching if isinstance(c, WebCollector)), None)
    locals_ = [c for c in matching if isinstance(c, LocalCollector)]
    if web is not None:
        conversation = web.resolve(snapshot)
        if conversation is not None:
            return conversation
        sessions = [s for c in locals_ for s in c.fresh_sessions()]
        if sessions:
            ordered = _ordered_sessions(sessions)
            return _session_conversation(ordered[0], ordered)
        return None
    if locals_:
        # 터미널은 포커스된 탭/pane에서 실제로 돌고 있는 에이전트 프로세스로 정확히
        # 매핑(추측 제거). 그 외 로컬 앱(Codex Desktop/VSCode)은 파일 스캔으로 처리.
        if snapshot.bundle_id == _CMUX_BUNDLE_ID:
            return _resolve_focused_tty(
                locals_, _cmux_focused_tty(snapshot.window_title)
            )
        if snapshot.bundle_id == _TERMINAL_BUNDLE_ID:
            return _resolve_focused_tty(
                locals_, _terminal_focused_tty(snapshot.window_title)
            )
        # Codex Desktop은 백엔드가 활성 rollout을 열어두므로 lsof로 집는다.
        if snapshot.bundle_id == _CODEX_DESKTOP_BUNDLE_ID:
            return _open_rollout_session(_codex_app_pids())
        sessions = [s for c in locals_ for s in c.fresh_sessions()]
        return resolve_sessions(sessions, snapshot.window_title)
    return None


_CONNECTOR_BY_PROCESS = {"claude": "claude_code", "codex": "codex_cli"}


def _resolve_focused_tty(
    collectors: Sequence[AgentConversationCollector], tty: str | None
) -> AgentConversation | None:
    # 포커스된 탭/pane에서 도는 에이전트의 세션만 채택. 에이전트가 없으면(셸) None.
    agent = _agent_on_tty(tty)
    if agent is None:
        return None
    connector, pid, command = agent
    # codex처럼 세션 파일을 열어두는 에이전트: lsof로 활성 세션을 정확히(유휴여도, freshness 무시).
    path = _open_session_file(pid)
    if path is not None:
        return _session_from_path(connector, path)
    # claude처럼 파일을 닫아두는 에이전트: 명령줄 session id로 정확히. 못 찾으면 추측하지 않고 None.
    session = _focused_session(collectors, connector, _agent_session_id(command))
    return _session_conversation(session, [session]) if session is not None else None


def _focused_session(
    collectors: Sequence[AgentConversationCollector],
    connector: str,
    session_id: str | None,
) -> dict | None:
    if not session_id:
        return None
    for collector in collectors:
        if isinstance(collector, LocalCollector) and collector.connector_name == connector:
            found = collector.find_session(session_id)
            if found is not None:
                return found
    return None


def _agent_on_tty(tty: str | None) -> tuple[str, str, str] | None:
    # tty의 "foreground" claude/codex 프로세스 -> (connector, pid, command).
    if tty is None:
        return None
    for line in _run(["ps", "-t", tty, "-o", "pid=,pgid=,tpgid=,command="]).splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, pgid, tpgid, command = parts
        if pgid != tpgid:
            continue
        base = command.split()[0].rsplit("/", 1)[-1]
        connector = _CONNECTOR_BY_PROCESS.get(base)
        if connector is not None:
            return (connector, pid, command)
    return None


def _open_session_file(pid: str) -> str | None:
    # 프로세스가 열어둔 에이전트 세션 파일(.jsonl). codex는 활성 rollout을 열어둔다.
    for line in _run(["lsof", "-p", pid, "-Fn"]).splitlines():
        if line.startswith("n"):
            path = line[1:]
            if path.endswith(".jsonl") and (
                ".codex/sessions" in path or ".claude/projects" in path
            ):
                return path
    return None


def _session_from_path(connector: str, path: str) -> AgentConversation:
    match = _UUID_RE.search(path)
    return AgentConversation(
        conversation_id=match.group(0) if match else None,
        url=Path(path).as_uri(),
        kind="session",
        connector_name=connector,
    )


def _codex_app_pids() -> list[str]:
    pids = []
    for line in _run(["ps", "-axo", "pid=,command="]).splitlines():
        pid, _, command = line.strip().partition(" ")
        first = command.split()[0] if command else ""
        if "Codex.app/" in command and first.rsplit("/", 1)[-1] == "codex":
            pids.append(pid)
    return pids


def _open_rollout_session(pids: list[str]) -> AgentConversation | None:
    paths = {p for pid in pids if (p := _open_session_file(pid)) is not None}
    if len(paths) != 1:
        return None
    return _session_from_path("codex_cli", next(iter(paths)))


def _agent_session_id(command: str) -> str | None:
    flagged = re.search(
        r"--(?:session-id|resume)[=\s]+(" + _UUID_RE.pattern + r")", command
    )
    if flagged:
        return flagged.group(1)
    found = _UUID_RE.search(command)
    return found.group(0) if found else None


_TERMINAL_WINDOWS_SCRIPT = (
    'tell application "Terminal"\n'
    "set out to \"\"\n"
    "repeat with w in windows\n"
    "  try\n"
    '    set out to out & (tty of selected tab of w) & character id 9 & (name of w) & linefeed\n'
    "  end try\n"
    "end repeat\n"
    "return out\n"
    "end tell"
)


def _terminal_focused_tty(window_title: str | None) -> str | None:
    # Terminal.app은 상태 파일이 없어 AppleScript로 창 목록을 얻는다(자동화 권한 필요).
    # "front window"는 다중 창에서 사용자가 보는 창과 어긋날 수 있으므로, OS가 준
    # window_title과 제목이 맞는 창의 선택 탭 tty를 고른다.
    try:
        completed = subprocess.run(
            ["osascript", "-e", _TERMINAL_WINDOWS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout).strip()
        if _is_apple_events_denied(error):
            # 자동화 권한 미허용을 조용히 None으로 덮지 않고 명시적으로 알린다(셸이 아니라
            # 권한이 문제임을 chain 로그로 드러낸다).
            raise AssetPermissionError(
                "automation",
                source="agent",
                bundle_id=_TERMINAL_BUNDLE_ID,
                detail=error or "Terminal automation denied",
            )
        return None

    windows = []  # (tty, name)
    for line in completed.stdout.splitlines():
        dev, sep, name = line.partition("\t")
        if sep and dev.startswith("/dev/tty"):
            windows.append((dev.rsplit("/", 1)[-1], name))
    if not windows:
        return None

    target = _terminal_title_key(window_title or "")
    if target:
        matched = [tty for tty, name in windows if _terminal_title_key(name) == target]
        if len(matched) == 1:
            return matched[0]
    # 제목으로 못 가리면(또는 모호하면) 창이 하나뿐일 때만 그것, 아니면 추측하지 않는다.
    return windows[0][0] if len(windows) == 1 else None


def _terminal_title_key(title: str) -> str:
    # Terminal 창 제목 "<dir> — <글리프><작업> — <프로세스> — <크기>"에서 안정적인 앞 두
    # 조각(dir + 작업)만 남긴다. 프로세스/크기 꼬리는 매 순간 바뀌고, 스피너 글리프는
    # 애니메이션이라 제거한다.
    head = " — ".join(title.split(" — ")[:2])
    no_glyph = re.sub(r"[✳-✹⠀-⣿]", "", head)
    return re.sub(r"\s+", " ", no_glyph).strip()


def _is_apple_events_denied(error: str) -> bool:
    text = error.lower()
    return (
        "-1743" in text
        or "not authorized to send apple events" in text
        or "not allowed to send apple events" in text
    )


def _cmux_focused_tty(window_title: str | None) -> str | None:
    # cmux CLI는 소켓 인증 때문에 백그라운드 데몬(launchd로 reparent됨)에서 동작하지 않아,
    # cmux가 디스크에 남기는 세션 상태 파일을 직접 읽습니다.
    try:
        data = json.loads(_cmux_session_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    panel = _cmux_focused_panel(data.get("windows") or [], window_title)
    return panel.get("ttyName") if panel else None


def _cmux_focused_panel(windows: list, window_title: str | None) -> dict | None:
    # 포커스된 pane은 살아있는 OS 창 title(글리프 제외)과 제목이 맞는 panel로 고른다.
    # cmux 세션 파일의 focusedPanelId/제목은 같은 스냅샷이라 함께 지연되므로, 못 맞추면
    # focusedPanelId로 폴백해봐야 똑같이 stale하다 -> 추측하지 않고 None(틀린 attribution 방지).
    panels = [p for w in windows for p in _cmux_active_panels(w)]
    if len(panels) <= 1:
        return panels[0] if panels else None
    target = _strip_status_glyph(window_title or "")
    if not target:
        return None
    # 제목이 정확히 하나 일치할 때만 채택. 같은 제목 pane이 둘 이상이면 못 가리므로 None.
    matched = [p for p in panels if _strip_status_glyph(p.get("title", "")) == target]
    return matched[0] if len(matched) == 1 else None


def _cmux_active_panels(window: dict) -> list[dict]:
    return _cmux_active_workspace(window).get("panels", [])


def _cmux_active_workspace(window: dict) -> dict:
    tab = window.get("tabManager", {})
    workspaces = tab.get("workspaces", [])
    index = tab.get("selectedWorkspaceIndex", 0)
    return workspaces[index] if 0 <= index < len(workspaces) else {}


def _strip_status_glyph(title: str) -> str:
    # 앞에 붙는 스피너/상태 글리프(✳, ⠂ 등)와 공백을 떼어 안정적인 본문만 남긴다.
    return re.sub(r"^[^\w/~.]+", "", title).strip()


def _cmux_session_file() -> Path:
    support = Path.home() / "Library" / "Application Support" / "cmux"
    return support / "session-com.cmuxterm.app.json"


def _run(cmd: list[str]) -> str:
    # ps/lsof가 멈추거나(타임아웃) 없을 때 poll 루프가 죽지 않도록 빈 문자열로 흘려보낸다.
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2.0).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _clean_claude_title(raw: object | None) -> str | None:
    # claude.ai 문서 title "<대화 이름> - Claude"에서 suffix 제거. 빈 값/"Claude"면 None.
    if not raw:
        return None
    title = str(raw).removesuffix(" - Claude").strip()
    return title or None


def _read_ax_web_areas(
    pid: int, *, max_nodes: int = 4000, max_depth: int = 50
) -> list[dict]:
    try:
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXUIElementSetAttributeValue,
        )
    except ImportError:
        _missing("ApplicationServices import 실패 (pyobjc 미설치?)")
        return []

    def attr(element: object, name: str) -> object | None:
        if element is None:  # None을 넘기면 PyObjC가 예외를 던져 순회가 깨진다.
            return None
        err, value = AXUIElementCopyAttributeValue(element, name, None)
        return None if err else value

    app = AXUIElementCreateApplication(pid)
    AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    # 윈도우 노출이 깜빡여서 focused -> main -> 첫 윈도우 순으로 시도.
    window = attr(app, "AXFocusedWindow") or attr(app, "AXMainWindow")
    if window is None:
        windows = attr(app, "AXWindows")
        window = windows[0] if windows else None
    if window is None:
        return []

    areas: list[dict] = []
    stack = [(window, 0)]
    visited = 0
    while stack and visited < max_nodes:
        element, depth = stack.pop()
        visited += 1
        if attr(element, "AXRole") == "AXWebArea":
            url = attr(element, "AXURL")
            if url is not None:
                text = str(url)
                title = attr(element, "AXTitle")
                areas.append(
                    {"url": text, "title": None if title is None else str(title)}
                )
                if _CLAUDE_CHAT_URL.match(text):
                    return areas
        if depth < max_depth:
            children = attr(element, "AXChildren")
            if children:
                stack.extend((child, depth + 1) for child in children)
    return areas


def _read_session_cwd(
    path: Path, keys: tuple[str, ...], *, max_lines: int = 64
) -> str | None:
    # 세션 파일 앞쪽 줄에서 keys 위치의 cwd를 찾습니다. 깨진/부분 줄과 읽기 에러는 무시.
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(max_lines):
                line = handle.readline()
                if not line:
                    break
                try:
                    record: object = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for key in keys:
                    record = record.get(key) if isinstance(record, dict) else None
                if isinstance(record, str) and record:
                    return record
    except OSError as exc:
        return _missing(f"세션 파일 읽기 실패 ({path}): {exc}")
    return _missing(f"세션 파일에서 cwd{keys}를 못 찾음 ({path})")


def _read_json_cwd(path: Path, keys: tuple[str, ...]) -> str | None:
    # Code/Cowork 세션은 파일 전체가 하나의 JSON 객체(여러 줄일 수 있음)라 통째로 파싱.
    try:
        with path.open(encoding="utf-8") as handle:
            record: object = json.load(handle)
    except OSError as exc:
        return _missing(f"세션 파일 읽기 실패 ({path}): {exc}")
    except json.JSONDecodeError as exc:
        return _missing(f"세션 파일 JSON 파싱 실패 ({path}): {exc}")
    for key in keys:
        record = record.get(key) if isinstance(record, dict) else None
    if isinstance(record, str) and record:
        return record
    return _missing(f"세션 파일에서 cwd{keys}를 못 찾음 ({path})")


def _read_default(bundle_id: str, key: str) -> str | None:
    try:
        completed = subprocess.run(
            ["/usr/bin/defaults", "read", bundle_id, key],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _missing(f"defaults read 실패 ({bundle_id} {key}): {exc}")
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _normalize_path(value: str) -> str:
    return os.path.normpath(os.path.expanduser(value.strip()))


def _missing(reason: str) -> None:
    # 필요한 데이터가 형식/환경 문제로 없을 때, 조용히 넘기지 않고 stderr로 알립니다.
    print(f"agent_conversation: {reason}", file=sys.stderr)
