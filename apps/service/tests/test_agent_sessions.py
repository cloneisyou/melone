import json
import os
import subprocess

import pytest

from melone_service.asset.model import AssetPermissionError
import melone_service.asset.resolvers.agent_sessions as agent_sessions_mod

from melone_service.asset.resolvers.agent import AgentURIResolver
from melone_service.asset.resolvers.agent_sessions import (
    AgentConversation,
    ClaudeDesktopCodeCollector,
    ClaudeDesktopCoworkCollector,
    ClaudeLocalCollector,
    ClaudeWebCollector,
    OpenAILocalCollector,
    OpenAIWebCollector,
    _agent_session_id,
    _cmux_focused_panel,
    _focused_session,
    _session_from_path,
    resolve_sessions,
)
from melone_service.collectors.active_window import ActiveWindowSnapshot


def make_asset_resolver(collectors=None):
    # 옮긴 테스트가 기대하는 "url만 돌려주는 resolver"를 AgentURIResolver로 재현하는 shim.
    resolver = (
        AgentURIResolver(collectors=collectors)
        if collectors is not None
        else AgentURIResolver()
    )

    def resolve(snapshot):
        asset = resolver.resolve(snapshot)
        return asset.uri if asset is not None else None

    return resolve


def _resolve_url(snapshot, collectors):
    # The asset resolver returns just the url/file path stamped onto the window event.
    return make_asset_resolver(collectors)(snapshot)


def _last_selected(kind, ident):
    return json.dumps(
        {
            "lastUpdated": 1.0,
            "conversationID": {kind: {"_0": ident}},
            "localID": ident,
            "modelSlug": "gpt-5-5",
        }
    )


# Synthetic conversation ids: remote is server-synced (lowercase, maps to a URL),
# local is an unsynced ChatGPT chat (uppercase, no URL).
REMOTE_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
LOCAL_ID = "DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD"
CLAUDE_CHAT_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def test_chatgpt_collector_maps_remote_conversation_to_url():
    collector = OpenAIWebCollector(
        defaults_reader=_FakeDefaultsReader(_last_selected("remote", REMOTE_ID))
    )

    assert collector.resolve(_chatgpt()) == AgentConversation(
        conversation_id=REMOTE_ID,
        url=f"https://chatgpt.com/c/{REMOTE_ID}",
        kind="remote",
        connector_name="chatgpt_desktop",
    )


def test_chatgpt_collector_keeps_local_conversation_without_url():
    collector = OpenAIWebCollector(
        defaults_reader=_FakeDefaultsReader(_last_selected("local", LOCAL_ID))
    )

    conversation = collector.resolve(_chatgpt())

    assert conversation is not None
    assert conversation.conversation_id == LOCAL_ID
    assert conversation.kind == "local"
    assert conversation.url is None


def test_chatgpt_collector_ignores_missing_or_malformed_pointer():
    assert OpenAIWebCollector(
        defaults_reader=_FakeDefaultsReader(None)
    ).resolve(_chatgpt()) is None
    assert OpenAIWebCollector(
        defaults_reader=_FakeDefaultsReader("not json")
    ).resolve(_chatgpt()) is None


def test_chatgpt_collector_ignores_non_string_identifier():
    raw = json.dumps({"conversationID": {"remote": {"_0": {"unexpected": 1}}}})

    assert OpenAIWebCollector(
        defaults_reader=_FakeDefaultsReader(raw)
    ).resolve(_chatgpt()) is None


def test_chatgpt_collector_ignores_non_object_json():
    # Valid JSON that isn't an object (null, array) must not raise.
    assert OpenAIWebCollector(
        defaults_reader=_FakeDefaultsReader("null")
    ).resolve(_chatgpt()) is None
    assert OpenAIWebCollector(
        defaults_reader=_FakeDefaultsReader("[1, 2]")
    ).resolve(_chatgpt()) is None


# The focused window exposes several web areas; only the /chat/ one is the conversation.
CLAUDE_TITLE = "PageRank papers"
_CLAUDE_WEB_AREAS = [
    {"url": "file:///Applications/Claude.app/index.html", "title": None},
    {"url": f"https://claude.ai/chat/{CLAUDE_CHAT_ID}", "title": f"{CLAUDE_TITLE} - Claude"},
    {"url": "https://claude.ai/new#dframe-main", "title": "Claude"},
]


def test_claude_desktop_collector_extracts_chat_url_and_title():
    collector = ClaudeWebCollector(
        web_area_reader=lambda pid: list(_CLAUDE_WEB_AREAS)
    )

    assert collector.resolve(_claude_desktop()) == AgentConversation(
        conversation_id=CLAUDE_CHAT_ID,
        url=f"https://claude.ai/chat/{CLAUDE_CHAT_ID}",
        kind="remote",
        title=CLAUDE_TITLE,
        connector_name="claude_desktop",
    )


def test_claude_desktop_collector_returns_none_without_open_conversation():
    # Only a "new" chat and unrelated links — no /chat/<id>.
    collector = ClaudeWebCollector(
        web_area_reader=lambda pid: [
            {"url": "https://claude.ai/new", "title": "Claude"},
            {"url": "https://anthropic.com", "title": None},
        ]
    )

    assert collector.resolve(_claude_desktop()) is None


def test_claude_desktop_collector_returns_none_without_pid():
    collector = ClaudeWebCollector(
        web_area_reader=lambda pid: list(_CLAUDE_WEB_AREAS)
    )

    assert collector.resolve(_claude_desktop(pid=None)) is None


def test_resolver_returns_none_for_unsupported_app():
    resolve = make_asset_resolver()
    assert resolve(_chatgpt(bundle_id="com.apple.Safari")) is None
    assert resolve(_chatgpt(bundle_id=None)) is None


def test_resolver_chatgpt_url():
    url = _resolve_url(
        _chatgpt(),
        [
            OpenAIWebCollector(
                defaults_reader=_FakeDefaultsReader(_last_selected("remote", REMOTE_ID))
            )
        ],
    )
    assert url == f"https://chatgpt.com/c/{REMOTE_ID}"


def test_resolver_claude_desktop_chat_url():
    url = _resolve_url(
        _claude_desktop(),
        [ClaudeWebCollector(web_area_reader=lambda pid: list(_CLAUDE_WEB_AREAS))],
    )
    assert url == f"https://claude.ai/chat/{CLAUDE_CHAT_ID}"


class _FakeDefaultsReader:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, bundle_id, key):
        self.calls.append((bundle_id, key))
        return self.result


def _chatgpt(
    *,
    app_name="ChatGPT",
    bundle_id="com.openai.chat",
    window_title="ChatGPT",
):
    return ActiveWindowSnapshot(
        app_name=app_name,
        bundle_id=bundle_id,
        pid=123,
        window_title=window_title,
        window_number=456,
    )


def _claude_desktop(*, pid=4321, window_title="Claude"):
    return ActiveWindowSnapshot(
        app_name="Claude",
        bundle_id="com.anthropic.claudefordesktop",
        pid=pid,
        window_title=window_title,
        window_number=789,
    )




def _vscode(*, window_title="Code"):
    # VSCode is local-only and not a terminal, so it uses the file-scan path.
    return ActiveWindowSnapshot(
        app_name="Code",
        bundle_id="com.microsoft.VSCode",
        pid=7000,
        window_title=window_title,
        window_number=1414,
    )


# Synthetic session ids for the terminal CLI agents.
CODEX_SESSION_ID = "11111111-1111-1111-1111-111111111111"
CLAUDE_CODE_SESSION_ID = "22222222-2222-2222-2222-222222222222"
OTHER_SESSION_ID = "33333333-3333-3333-3333-333333333333"


def _write_codex_session(root, *, session_id, cwd, mtime):
    # Codex rollout: <root>/2026/06/12/rollout-...-<uuid>.jsonl with a session_meta
    # line carrying payload.cwd.
    directory = root / "2026" / "06" / "12"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-06-12T00-00-00-{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))
    return path


def _write_claude_code_session(root, *, session_id, cwd, mtime):
    # Claude Code: <root>/<encoded-cwd-dir>/<uuid>.jsonl with top-level cwd.
    encoded = cwd.replace("/", "-")
    directory = root / encoded
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"cwd": cwd, "type": "user"}) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))
    return path


def _local_collectors(codex_root, claude_root, *, now=lambda: 1000.0, freshness=180):
    # The terminal collectors for both CLI agents, sharing a clock and freshness window.
    return [
        OpenAILocalCollector(root=codex_root, now=now, freshness_seconds=freshness),
        ClaudeLocalCollector(root=claude_root, now=now, freshness_seconds=freshness),
    ]


def _resolve_terminal(collectors, window_title):
    # Arbitrate across every CLI agent's sessions at once (Terminal.app path).
    sessions = [s for c in collectors for s in c.fresh_sessions()]
    return resolve_sessions(sessions, window_title)


def test_agent_session_id_extracts_from_command_line():
    assert (
        _agent_session_id(f"/x/claude --session-id {CLAUDE_CODE_SESSION_ID} --settings {{}}")
        == CLAUDE_CODE_SESSION_ID
    )
    assert _agent_session_id(f"/x/claude --resume {OTHER_SESSION_ID}") == OTHER_SESSION_ID
    assert _agent_session_id(f"/x/codex resume {CODEX_SESSION_ID}") == CODEX_SESSION_ID
    assert _agent_session_id("/x/codex") is None


def test_find_session_ignores_freshness(tmp_path):
    claude_root = tmp_path / "claude"
    # 오래된(stale) 세션: fresh_sessions엔 안 잡혀도 find_session은 잡아야 한다.
    _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/b", mtime=100
    )
    collector = ClaudeLocalCollector(
        root=claude_root, now=lambda: 1000.0, freshness_seconds=180
    )

    assert collector.fresh_sessions() == []
    found = collector.find_session(CLAUDE_CODE_SESSION_ID)
    assert found is not None
    assert found["conversation_id"] == CLAUDE_CODE_SESSION_ID


def test_focused_session_picks_running_id_over_freshest(tmp_path):
    claude_root = tmp_path / "claude"
    # 같은 cwd에 두 세션. 포커스 pane이 도는 건 오래된 쪽(stale)이고 다른 하나가 freshest.
    _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/proj", mtime=100
    )
    _write_claude_code_session(
        claude_root, session_id=OTHER_SESSION_ID, cwd="/proj", mtime=990
    )
    collectors = _local_collectors(tmp_path / "codex", claude_root)

    # freshest(OTHER)가 아니라 실제 도는 세션 id를 정확히 고른다(freshness 무시).
    session = _focused_session(collectors, "claude_code", CLAUDE_CODE_SESSION_ID)
    assert session["conversation_id"] == CLAUDE_CODE_SESSION_ID


def test_focused_session_none_without_session_id(tmp_path):
    claude_root = tmp_path / "claude"
    _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/proj", mtime=990
    )
    collectors = _local_collectors(tmp_path / "codex", claude_root)

    # session id가 없으면 추측하지 않는다(엉뚱한 세션 attribution 방지).
    assert _focused_session(collectors, "claude_code", None) is None


def test_session_from_path_builds_conversation_from_open_file():
    # codex처럼 lsof로 집은 열린 rollout 경로 -> AgentConversation.
    path = (
        "/Users/me/.codex/sessions/2026/06/13/"
        "rollout-2026-06-13T14-58-58-019ebf8f-e2c7-7ac0-b2b9-5fa174334c79.jsonl"
    )
    conv = _session_from_path("codex_cli", path)
    assert conv.connector_name == "codex_cli"
    assert conv.conversation_id == "019ebf8f-e2c7-7ac0-b2b9-5fa174334c79"
    assert conv.kind == "session"
    assert conv.url.startswith("file://") and conv.url.endswith(".jsonl")


def test_terminal_focused_tty_raises_on_automation_denial(monkeypatch):
    # Terminal 자동화 권한이 막히면 조용히 None이 아니라 AssetPermissionError로 드러난다.
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="execution error: Not authorized to send Apple events to Terminal. (-1743)",
        )

    monkeypatch.setattr(agent_sessions_mod.subprocess, "run", fake_run)
    with pytest.raises(AssetPermissionError) as exc_info:
        agent_sessions_mod._terminal_focused_tty("melone-mvp — claude")
    assert exc_info.value.permission == "automation"


def test_terminal_focused_tty_matches_window_by_title(monkeypatch):
    # 다중 Terminal 창에서 'front window'가 아니라 OS window_title과 맞는 창의 tty를 고른다.
    dump = (
        "/dev/ttys011\tmelone-mvp — melone-mvp — codex ◂ node — 214×65\n"
        "/dev/ttys020\tmelone-mvp — ✳ Review branch activity — claude --resume x — 214×65\n"
        "/dev/ttys004\tmelone-mvp — user@host — -zsh — 214×65\n"
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=dump, stderr="")

    monkeypatch.setattr(agent_sessions_mod.subprocess, "run", fake_run)
    # 스피너 글리프와 프로세스 꼬리가 달라도 dir+작업으로 claude 창을 정확히 고른다.
    title = "melone-mvp — ⠂ Review branch activity — caffeinate ◂ claude — 214×65"
    assert agent_sessions_mod._terminal_focused_tty(title) == "ttys020"
    # 어느 창과도 안 맞고 창이 여러 개면 추측하지 않는다.
    assert agent_sessions_mod._terminal_focused_tty("Other — unrelated") is None


def test_terminal_lets_go_when_ambiguous(tmp_path):
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    # Two sessions in DIFFERENT projects, and the title isn't a path -> ambiguous.
    codex_path = _write_codex_session(
        codex_root, session_id=CODEX_SESSION_ID, cwd="/a", mtime=950
    )
    claude_path = _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/b", mtime=990
    )

    conversation = _resolve_terminal(
        _local_collectors(codex_root, claude_root), "zsh"
    )

    # No confident active conversation (don't guess), but the sessions are still listed.
    assert conversation is not None
    assert conversation.url is None
    assert conversation.conversation_id is None
    assert {c["url"] for c in conversation.candidates} == {
        codex_path.as_uri(),
        claude_path.as_uri(),
    }


def test_terminal_single_session_uses_session_url(tmp_path):
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    claude_path = _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/b", mtime=990
    )

    # Only one fresh session -> unambiguous, its session file URI (title irrelevant).
    conversation = _resolve_terminal(
        _local_collectors(codex_root, claude_root), "zsh"
    )

    assert conversation.conversation_id == CLAUDE_CODE_SESSION_ID
    assert conversation.url == claude_path.as_uri()
    assert conversation.connector_name == "claude_code"


def test_terminal_resolves_session_when_title_matches_one(tmp_path):
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    codex_path = _write_codex_session(
        codex_root, session_id=CODEX_SESSION_ID, cwd="/a", mtime=950
    )
    _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/b", mtime=990
    )

    # Title /a uniquely matches the codex session -> its session file URI.
    conversation = _resolve_terminal(
        _local_collectors(codex_root, claude_root), "/a"
    )

    assert conversation.conversation_id == CODEX_SESSION_ID
    assert conversation.url == codex_path.as_uri()
    assert conversation.connector_name == "codex_cli"
    assert conversation.candidates[0]["conversation_id"] == CODEX_SESSION_ID


def test_terminal_several_in_cwd_lets_go(tmp_path):
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    # Two claude sessions in the SAME cwd /b; the title matches that cwd but can't pin one.
    _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/b", mtime=990
    )
    _write_claude_code_session(
        claude_root, session_id=OTHER_SESSION_ID, cwd="/b", mtime=980
    )

    conversation = _resolve_terminal(
        _local_collectors(codex_root, claude_root), "/b"
    )

    # Can't pin a single session file -> let go, but both are still listed.
    assert conversation.conversation_id is None
    assert conversation.url is None
    assert len(conversation.candidates) == 2


def test_terminal_skips_stale_sessions(tmp_path):
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    _write_codex_session(codex_root, session_id=CODEX_SESSION_ID, cwd="/a", mtime=100)

    assert _resolve_terminal(_local_collectors(codex_root, claude_root), "zsh") is None


def test_terminal_ignores_session_from_another_project(tmp_path):
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    # One fresh claude session in /a, but the focused terminal is a shell prompt in /b
    # (title shows the cwd). The session belongs to another window -> attribute nothing.
    _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/a", mtime=990
    )

    assert _resolve_terminal(_local_collectors(codex_root, claude_root), "/b") is None


def test_terminal_reads_cwd_from_each_source_format(tmp_path):
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    _write_codex_session(
        codex_root, session_id=CODEX_SESSION_ID, cwd="/proj/codex", mtime=970
    )
    _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/proj/claude", mtime=980
    )

    conversation = _resolve_terminal(
        _local_collectors(codex_root, claude_root), "zsh"
    )

    by_connector = {c["connector"]: c for c in conversation.candidates}
    # codex cwd comes from payload.cwd; claude cwd from top-level cwd.
    assert by_connector["codex_cli"]["cwd"] == "/proj/codex"
    assert by_connector["claude_code"]["cwd"] == "/proj/claude"


def test_resolver_filescan_single_session_url(tmp_path):
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    claude_path = _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/b", mtime=990
    )

    # VSCode (file-scan path), one fresh session -> its url.
    url = _resolve_url(_vscode(), _local_collectors(codex_root, claude_root))

    assert url == claude_path.as_uri()


def test_resolver_filescan_ambiguous_has_no_url(tmp_path):
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    _write_codex_session(codex_root, session_id=CODEX_SESSION_ID, cwd="/a", mtime=950)
    _write_claude_code_session(
        claude_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/b", mtime=990
    )

    # Two projects, title isn't a path -> can't pin one -> no url stamped on the event.
    url = _resolve_url(
        _vscode(window_title="Code"), _local_collectors(codex_root, claude_root)
    )

    assert url is None


def _write_desktop_session(root, *, session_id, cwd, mtime, title="t"):
    # Claude Desktop Code/Cowork record: <root>/<workspace>/<group>/local_<uuid>.json,
    # a single JSON object with a top-level cwd.
    directory = root / "ws" / "grp"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"local_{session_id}.json"
    # Pretty-printed (multi-line) like the real Cowork records, so the whole-file reader
    # is exercised rather than the JSONL line reader.
    path.write_text(
        json.dumps(
            {"sessionId": f"local_{session_id}", "cwd": cwd, "title": title}, indent=2
        ),
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))
    return path


def _desktop_collectors(code_root, cowork_root, *, now=lambda: 1000.0, freshness=180):
    return [
        ClaudeDesktopCodeCollector(
            root=code_root, now=now, freshness_seconds=freshness
        ),
        ClaudeDesktopCoworkCollector(
            root=cowork_root, now=now, freshness_seconds=freshness
        ),
    ]


def _resolve_claude_desktop(code_root, cowork_root, snapshot, *, web_areas=()):
    # web_areas=() simulates a non-chat view (Code/Cowork); pass _CLAUDE_WEB_AREAS for chat.
    collectors = [
        ClaudeWebCollector(web_area_reader=lambda pid: list(web_areas)),
        *_desktop_collectors(code_root, cowork_root),
    ]
    return _resolve_url(snapshot, collectors)


def test_desktop_collector_reads_cwd_from_whole_file_json(tmp_path):
    code_root = tmp_path / "code"
    _write_desktop_session(
        code_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/proj", mtime=990
    )

    sessions = ClaudeDesktopCodeCollector(
        root=code_root, now=lambda: 1000.0
    ).fresh_sessions()

    # Whole-file (multi-line) JSON: the top-level cwd must be extracted, not lost.
    assert len(sessions) == 1
    assert sessions[0]["cwd"] == "/proj"


def test_resolver_claude_desktop_uses_code_session(tmp_path):
    code_root = tmp_path / "code"
    cowork_root = tmp_path / "cowork"
    code_path = _write_desktop_session(
        code_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/proj", mtime=990
    )

    url = _resolve_claude_desktop(code_root, cowork_root, _claude_desktop())

    assert url == code_path.as_uri()


def test_resolver_claude_desktop_uses_cowork_session(tmp_path):
    code_root = tmp_path / "code"
    cowork_root = tmp_path / "cowork"
    cowork_path = _write_desktop_session(
        cowork_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/sandbox", mtime=990
    )

    url = _resolve_claude_desktop(code_root, cowork_root, _claude_desktop())

    assert url == cowork_path.as_uri()


def test_resolver_claude_desktop_prefers_most_recent_session(tmp_path):
    code_root = tmp_path / "code"
    cowork_root = tmp_path / "cowork"
    # Both a Code and a Cowork session are fresh; the more recently active one wins.
    code_path = _write_desktop_session(
        code_root, session_id=CODEX_SESSION_ID, cwd="/proj", mtime=995
    )
    _write_desktop_session(
        cowork_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/sandbox", mtime=980
    )

    url = _resolve_claude_desktop(code_root, cowork_root, _claude_desktop())

    assert url == code_path.as_uri()


def test_resolver_claude_desktop_prefers_chat_url_over_local_session(tmp_path):
    code_root = tmp_path / "code"
    cowork_root = tmp_path / "cowork"
    # A Code session is fresh, but the chat view is up (AX exposes /chat/) -> URL wins.
    _write_desktop_session(
        code_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/proj", mtime=990
    )

    url = _resolve_claude_desktop(
        code_root, cowork_root, _claude_desktop(), web_areas=_CLAUDE_WEB_AREAS
    )

    assert url == f"https://claude.ai/chat/{CLAUDE_CHAT_ID}"


def test_resolver_claude_desktop_falls_back_to_web_without_local_session(tmp_path):
    code_root = tmp_path / "code"
    cowork_root = tmp_path / "cowork"  # no Code/Cowork sessions

    url = _resolve_claude_desktop(
        code_root, cowork_root, _claude_desktop(), web_areas=_CLAUDE_WEB_AREAS
    )

    assert url == f"https://claude.ai/chat/{CLAUDE_CHAT_ID}"


def _cmux_window(panels, *, focused, selected_index=0):
    return {
        "tabManager": {
            "selectedWorkspaceIndex": selected_index,
            "workspaces": [{"focusedPanelId": focused, "panels": panels}],
        }
    }


def test_cmux_focused_panel_single_pane_is_returned_regardless_of_title():
    windows = [
        _cmux_window([{"id": "a", "ttyName": "ttys001", "title": "left"}], focused="a")
    ]
    # 한 pane뿐이면 모호하지 않으니 title과 무관하게 그것.
    assert _cmux_focused_panel(windows, "whatever")["ttyName"] == "ttys001"


def test_cmux_focused_panel_uses_live_title_over_stale_focused_id():
    # 세션 파일의 focusedPanelId는 pane 전환을 늦게 반영 -> 살아있는 OS 창 title을 우선한다.
    windows = [
        _cmux_window(
            [
                {
                    "id": "agent",
                    "ttyName": "ttys006",
                    "title": "✳ File system timestamp attributes",
                },
                {"id": "shell", "ttyName": "ttys005", "title": "~/clone_corp/melone-mvp"},
            ],
            focused="agent",  # stale: 세션 파일은 아직 에이전트 pane을 가리킴
        )
    ]
    # 사용자는 셸 pane으로 전환 -> OS 창 title이 셸 -> 셸 tty를 골라야 한다.
    assert _cmux_focused_panel(windows, "~/clone_corp/melone-mvp")["ttyName"] == "ttys005"


def test_cmux_focused_panel_picks_window_matching_title_despite_spinner():
    windows = [
        _cmux_window(
            [{"id": "a", "ttyName": "ttys001", "title": "⠂ Build the widget"}],
            focused="a",
        ),
        _cmux_window(
            [{"id": "b", "ttyName": "ttys009", "title": "✳ Ship the release"}],
            focused="b",
        ),
    ]
    # The OS title carries a different spinner glyph but the same text -> second window.
    assert (
        _cmux_focused_panel(windows, "⠐ Ship the release")["ttyName"] == "ttys009"
    )


def test_cmux_focused_panel_returns_none_when_title_matches_nothing():
    # 여러 pane인데 살아있는 title이 아무 panel과도 안 맞으면(세션 파일 stale), 추측하지
    # 않고 None. focusedPanelId도 같은 stale 스냅샷이라 셸 pane을 에이전트로 오인하게 된다.
    windows = [
        _cmux_window(
            [
                {"id": "a", "ttyName": "ttys001", "title": "one"},
                {"id": "b", "ttyName": "ttys002", "title": "two"},
            ],
            focused="b",
        ),
    ]
    assert _cmux_focused_panel(windows, "unrelated") is None
    assert _cmux_focused_panel([], "x") is None


def test_cmux_focused_panel_none_when_title_is_ambiguous():
    # 같은 제목 pane이 둘 이상이면 어느 쪽이 포커스인지 못 가리므로 None.
    windows = [
        _cmux_window(
            [
                {"id": "a", "ttyName": "ttys001", "title": "~/proj"},
                {"id": "b", "ttyName": "ttys002", "title": "~/proj"},
            ],
            focused="a",
        ),
    ]
    assert _cmux_focused_panel(windows, "~/proj") is None


def test_resolver_claude_desktop_ignores_cli_sessions(tmp_path):
    code_root = tmp_path / "code"
    cowork_root = tmp_path / "cowork"
    cli_root = tmp_path / "cli"
    # A fresh CLI session exists in ~/.claude/projects, but Claude Desktop reads only its
    # own Code/Cowork dirs (the CLI collector's bundle excludes it), so it stays on chat.
    _write_claude_code_session(
        cli_root, session_id=CLAUDE_CODE_SESSION_ID, cwd="/proj", mtime=990
    )

    collectors = [
        ClaudeWebCollector(web_area_reader=lambda pid: list(_CLAUDE_WEB_AREAS)),
        ClaudeLocalCollector(root=cli_root, now=lambda: 1000.0),
        *_desktop_collectors(code_root, cowork_root),
    ]

    url = _resolve_url(_claude_desktop(), collectors)

    assert url == f"https://claude.ai/chat/{CLAUDE_CHAT_ID}"
