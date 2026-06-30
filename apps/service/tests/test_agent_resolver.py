from melone_service.asset.resolvers.agent import AgentURIResolver, _to_asset
from melone_service.asset.resolvers.agent_sessions import AgentConversation
from melone_service.collectors.active_window import ActiveWindowSnapshot


def _snapshot(bundle_id):
    return ActiveWindowSnapshot(
        app_name="App", bundle_id=bundle_id, pid=1, window_title=None
    )


class _FakeCollector:
    bundle_ids = frozenset({"com.cmuxterm.app"})


def test_resolve_caches_within_ttl(monkeypatch):
    import melone_service.asset.resolvers.agent as agent_mod

    calls = []

    def fake_resolve(snapshot, matching):
        calls.append(snapshot.window_title)
        return AgentConversation(
            conversation_id="s",
            url="file:///x.jsonl",
            kind="session",
            connector_name="codex_cli",
        )

    monkeypatch.setattr(agent_mod, "_resolve", fake_resolve)
    clock = iter([0.0, 0.5, 3.0])  # t=0 compute, t=0.5 cached, t=3.0 expired
    resolver = AgentURIResolver(
        collectors=[_FakeCollector()], ttl_seconds=2.0, monotonic=lambda: next(clock)
    )
    snap = _snapshot("com.cmuxterm.app")

    a1 = resolver.resolve(snap)
    a2 = resolver.resolve(snap)
    a3 = resolver.resolve(snap)

    assert a1.uri == a2.uri == a3.uri == "file:///x.jsonl"
    assert len(calls) == 2  # ps/lsof 경로는 캐시 만료 후에만 다시 실행


def test_handles_known_agent_bundles_only():
    resolver = AgentURIResolver()
    assert resolver.handles(_snapshot("com.openai.chat")) is True  # ChatGPT desktop
    assert resolver.handles(_snapshot("com.cmuxterm.app")) is True  # terminal (CLI agents)
    assert resolver.handles(_snapshot("com.apple.finder")) is False
    assert resolver.handles(_snapshot(None)) is False


def test_resolve_non_agent_app_returns_none():
    assert AgentURIResolver().resolve(_snapshot("com.apple.finder")) is None


def test_to_asset_maps_local_session_to_local_file():
    asset = _to_asset(
        AgentConversation(
            conversation_id="s",
            url="file:///x/s.jsonl",
            kind="session",
            connector_name="codex_cli",
        )
    )
    assert asset.kind == "local_file"
    assert asset.uri == "file:///x/s.jsonl"
    assert asset.source == "codex_cli"


def test_to_asset_maps_web_chat_to_web_url():
    asset = _to_asset(
        AgentConversation(
            conversation_id="c",
            url="https://chatgpt.com/c/x",
            kind="remote",
            connector_name="chatgpt_desktop",
        )
    )
    assert asset.kind == "web_url"
    assert asset.source == "chatgpt_desktop"


def test_to_asset_keeps_candidates():
    asset = _to_asset(
        AgentConversation(
            conversation_id="s",
            url="file:///a",
            kind="session",
            connector_name="claude_code",
            candidates=[{"url": "file:///a"}, {"url": "file:///b"}],
        )
    )
    assert asset.candidates == ("file:///a", "file:///b")


def test_to_asset_ambiguous_or_missing_returns_none():
    ambiguous = AgentConversation(
        conversation_id=None,
        url=None,
        candidates=[{"url": "file:///a"}, {"url": "file:///b"}],
    )
    assert _to_asset(ambiguous) is None
    assert _to_asset(None) is None
