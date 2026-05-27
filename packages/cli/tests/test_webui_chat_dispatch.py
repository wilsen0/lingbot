"""End-to-end: ``/ws/agents/<name>/stream`` runs DSL handlers first.

Regression coverage for the bug where the WebUI chat tab forwarded
every user message straight to the LLM, bypassing the bot's DSL
classifier. ``我的灵玉`` / ``我的好感`` are real triggers in the
QRDic ruleset; the WebUI must serve them out of the rule file
without ever calling the agent runtime.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from linling_cli.bootstrap import bootstrap_bot
from linling_cli.wire_webui import attach_bot_to_webui
from linling_core.config import BotConfig
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig

if TYPE_CHECKING:
    from linling_cli.bootstrap import RunningBot


class _FakeAgent:
    """Records every ``invoke`` call so a test can assert it never ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.name = "susu"
        self.provider_name = "fake"
        self.model = "fake-1"
        self.content = "[LLM-WAS-CALLED]"

    async def invoke(self, content: str, *, event=None, history=None):  # type: ignore[no-untyped-def]
        self.calls.append(content)

        # Match the ``AgentResult`` shape the dispatcher expects.
        class _R:
            def __init__(self, content: str) -> None:
                self.content = content
                self.tool_calls_made = 0
                self.total_tokens = 0

        return _R(self.content)


def _write(base: Path, rel: str, content: str) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def chat_app(tmp_path: Path) -> Iterator[tuple[TestClient, str, _FakeAgent]]:
    """Boot a real bot with a tiny rule set + fake agent, then mount WebUI."""
    _write(
        tmp_path,
        "rules/main.ling",
        # Two literal triggers that QRDic users actually type.
        "我的灵玉\n灵玉数 999\n\n我的好感\n好感值 88\n",
    )
    bot_yaml = """\
bot_id: chat_test
name: chat_test
storage:
  kv: ":memory:"
rules:
  - "rules/**/*.ling"
agent:
  multi_reply_delay_min_s: 2
  multi_reply_delay_max_s: 2
"""
    cfg_path = _write(tmp_path, "bot.yaml", bot_yaml)

    import asyncio

    cfg = BotConfig.from_yaml(cfg_path)

    # ``asyncio.run`` here would tear down its loop before the
    # WS test client is exercised, breaking the bot's KV connection.
    # We instead reuse a single loop for the whole fixture lifetime.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = loop.run_until_complete(bootstrap_bot(cfg, base_dir=tmp_path))

    # Replace the (default empty) agents with a fake we can assert on.
    fake = _FakeAgent()
    bot.agents = {"susu": fake}

    web_cfg = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
        login_rate_per_minute=10_000,
    )
    app = create_app(web_cfg)
    app.state.runtime.auth.upsert_user("admin", "Test-Pwd-9!aB", role="superadmin")

    attach_bot_to_webui(app, bot)

    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Test-Pwd-9!aB"},
        ).json()["access"]
        try:
            yield client, token, fake
        finally:
            loop.run_until_complete(bot.stop())
            loop.close()


def test_dsl_trigger_short_circuits_llm_via_rest_chat(chat_app) -> None:
    client, token, fake = chat_app
    r = client.post(
        "/api/agents/susu/chat",
        json={"input": "我的灵玉"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "灵玉数 999" in body["content"]
    assert fake.calls == [], f"LLM was called for a DSL trigger: {fake.calls!r}"


def test_second_dsl_trigger_also_short_circuits(chat_app) -> None:
    client, token, fake = chat_app
    r = client.post(
        "/api/agents/susu/chat",
        json={"input": "我的好感"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert "好感值 88" in r.json()["content"]
    assert fake.calls == []


def test_non_dsl_input_falls_back_to_llm(chat_app) -> None:
    client, token, fake = chat_app
    r = client.post(
        "/api/agents/susu/chat",
        json={"input": "你好啊"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "[LLM-WAS-CALLED]"
    assert fake.calls == ["你好啊"]


def test_webui_agent_actions_are_private_chat_segments_with_delay(chat_app) -> None:
    client, token, fake = chat_app
    fake.content = (
        '{"actions":[{"type":"send","text":"第一句"},'
        '{"type":"send","text":"第二句"},'
        '{"type":"send","text":"第三句"}]}'
    )

    r = client.post(
        "/api/agents/susu/chat",
        json={"input": "发3条"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "第一句\n第二句\n第三句"
    assert [segment["text"] for segment in body["segments"]] == [
        "第一句",
        "第二句",
        "第三句",
    ]
    assert [segment["delay_before_s"] for segment in body["segments"]] == [0.0, 2.0, 2.0]


def test_ws_stream_dsl_trigger_short_circuits_llm(chat_app) -> None:
    client, token, fake = chat_app
    with client.websocket_connect(f"/ws/agents/susu/stream?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["t"] == "hello"

        ws.send_json({"t": "input", "content": "我的灵玉"})

        delta = ws.receive_json()
        assert delta["t"] == "delta"
        assert "灵玉数 999" in delta["text"]

        done = ws.receive_json()
        assert done["t"] == "done"
        assert done.get("source") == "dsl"

    assert fake.calls == [], f"LLM was called for a DSL trigger: {fake.calls!r}"


def test_ws_stream_falls_back_to_llm_for_chat(chat_app) -> None:
    client, token, fake = chat_app
    with client.websocket_connect(f"/ws/agents/susu/stream?token={token}") as ws:
        ws.receive_json()  # hello

        ws.send_json({"t": "input", "content": "请告诉我天气"})

        delta = ws.receive_json()
        assert delta["t"] == "delta"
        assert delta["text"] == "[LLM-WAS-CALLED]"

        done = ws.receive_json()
        assert done["t"] == "done"
        assert done.get("source") == "agent"

    assert fake.calls == ["请告诉我天气"]


@pytest.fixture
def image_chat_app(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    """Bot whose rule emits a mixed text + image bubble.

    Verifies the ``±img=...±`` syntax flows through:
    DSL parse → VM emit → Action.segments → web dispatcher's
    ``_collect_web_segments`` → ChatResponse.segments / WS done frame.
    """
    _write(
        tmp_path,
        "rules/main.ling",
        # Trigger emits a text line, a remote image, and a `@pic:`
        # shorthand. Both image shapes need to round-trip through the
        # web dispatcher into a URL the browser can fetch.
        "我的灵玉\n"
        "0\n"
        "±img=https://example.com/badge.png±\n"
        "±img=@pic:思思±\n",
    )
    bot_yaml = """\
bot_id: chat_test_img
name: chat_test_img
storage:
  kv: ":memory:"
rules:
  - "rules/**/*.ling"
"""
    cfg_path = _write(tmp_path, "bot.yaml", bot_yaml)

    # Lay down a fake bundled asset so the asset-root finder picks it up.
    asset_dir = tmp_path / "assets" / "picture"
    asset_dir.mkdir(parents=True)
    (asset_dir / "思思.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    import asyncio

    cfg = BotConfig.from_yaml(cfg_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = loop.run_until_complete(bootstrap_bot(cfg, base_dir=tmp_path))
    bot.agents = {"susu": _FakeAgent()}

    web_cfg = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
        login_rate_per_minute=10_000,
    )
    app = create_app(web_cfg)
    app.state.runtime.auth.upsert_user("admin", "Test-Pwd-9!aB", role="superadmin")
    attach_bot_to_webui(app, bot)

    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Test-Pwd-9!aB"},
        ).json()["access"]
        try:
            yield client, token
        finally:
            loop.run_until_complete(bot.stop())
            loop.close()


def test_image_segment_returns_remote_and_rewritten_local_urls(image_chat_app) -> None:
    client, token = image_chat_app
    r = client.post(
        "/api/agents/susu/chat",
        json={"input": "我的灵玉"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "dsl"
    segments = body["segments"]
    kinds = [s["kind"] for s in segments]
    # Order matters: text first, then two images (remote-via-proxy +
    # ``@pic:`` shorthand).
    assert kinds == ["text", "image", "image"]
    assert "0" in segments[0]["text"]
    # Remote URLs are routed through ``/api/files/proxy`` so the
    # browser only ever fetches from same-origin (CSP friendly).
    assert segments[1]["url"].startswith("/api/files/proxy?url=")
    assert "example.com" in segments[1]["url"]
    # ``@pic:思思`` (migrator output) is rewritten with default .jpg.
    assert segments[2]["url"] == "/api/files/assets/picture/思思.jpg"


def test_asset_endpoint_serves_image(image_chat_app) -> None:
    client, token = image_chat_app
    r = client.get(
        "/api/files/assets/picture/思思.jpg",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8\xff\xe0")


def test_asset_endpoint_rejects_unknown_extension(image_chat_app, tmp_path) -> None:
    client, token = image_chat_app
    # Drop a non-image file inside the asset root and verify it 404s
    # — the endpoint only serves whitelisted extensions.
    fake = tmp_path / "assets" / "picture" / "secret.txt"
    fake.write_text("nope", encoding="utf-8")
    r = client.get(
        "/api/files/assets/picture/secret.txt",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_asset_endpoint_404s_missing_file(image_chat_app) -> None:
    client, token = image_chat_app
    r = client.get(
        "/api/files/assets/picture/does-not-exist.png",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_proxy_endpoint_streams_remote_image(image_chat_app, monkeypatch) -> None:
    """``/api/files/proxy`` fetches remote images through httpx and streams
    them back to the browser so the strict CSP can still render them."""
    import httpx
    from linling_webui.routers import files as files_router

    client, _token = image_chat_app

    fake_body = b"\x89PNG\r\n\x1a\nfake-png-bytes"

    class _FakeResp:
        def __init__(self) -> None:
            self.status_code = 200
            self.headers = {"Content-Type": "image/png"}
            self.content = fake_body

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            return None

        async def get(self, url: str) -> _FakeResp:
            assert url == "https://example.com/badge.png"
            return _FakeResp()

    monkeypatch.setattr(files_router.httpx, "AsyncClient", _FakeClient)

    r = client.get("/api/files/proxy?url=https%3A%2F%2Fexample.com%2Fbadge.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == fake_body
    # Don't reach back into httpx; just sanity-check the cache header
    # got applied.
    assert "max-age" in r.headers["cache-control"]
    _ = httpx  # keep import alive for the monkeypatch attribute


def test_proxy_endpoint_rejects_non_image_content_type(image_chat_app, monkeypatch) -> None:
    from linling_webui.routers import files as files_router

    client, _token = image_chat_app

    class _FakeResp:
        def __init__(self) -> None:
            self.status_code = 200
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
            self.content = b"<html>nope</html>"

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            return None

        async def get(self, url: str) -> _FakeResp:
            return _FakeResp()

    monkeypatch.setattr(files_router.httpx, "AsyncClient", _FakeClient)

    r = client.get("/api/files/proxy?url=https%3A%2F%2Fexample.com%2Fnotanimage")
    assert r.status_code == 502


def test_proxy_endpoint_rejects_non_http_scheme(image_chat_app) -> None:
    client, _token = image_chat_app
    r = client.get("/api/files/proxy?url=file%3A%2F%2F%2Fetc%2Fpasswd")
    assert r.status_code == 400



@pytest.fixture
def main_group_chat_app(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    """Verify the WebUI scope bridge against a group-gated handler.

    The DSL handler ``main_test`` early-returns unless ``%群号%`` matches
    a specific group ID. WebUI requests with no ``scope_id`` land in a
    DM scope (``%群号%==0``); explicit ``scope_id=754800438`` reaches
    the in-group branch. The fixture name is historical — predates the
    removal of the bot-wide ``main_group`` config.
    """
    _write(
        tmp_path,
        "rules/main.ling",
        # Trigger only fires inside the named group.
        "main_test\n如果:%群号%==754800438\n群号正确 %群号%\n返回\n如果尾\n群号错了 %群号%\n",
    )
    bot_yaml = """\
bot_id: chat_test_main
name: chat_test_main
storage:
  kv: ":memory:"
rules:
  - "rules/**/*.ling"
"""
    cfg_path = _write(tmp_path, "bot.yaml", bot_yaml)

    import asyncio

    cfg = BotConfig.from_yaml(cfg_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = loop.run_until_complete(bootstrap_bot(cfg, base_dir=tmp_path))
    bot.agents = {"susu": _FakeAgent()}

    web_cfg = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
        login_rate_per_minute=10_000,
    )
    app = create_app(web_cfg)
    app.state.runtime.auth.upsert_user("admin", "Test-Pwd-9!aB", role="superadmin")
    attach_bot_to_webui(app, bot)

    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Test-Pwd-9!aB"},
        ).json()["access"]
        try:
            yield client, token
        finally:
            loop.run_until_complete(bot.stop())
            loop.close()


def test_webui_default_scope_is_dm_with_group_id_zero(main_group_chat_app) -> None:
    """A WebUI chat with no ``scope_id`` runs as a private chat (``%群号%==0``).

    Most dicpro.txt rules either skip non-group messages outright
    (``如果:%群号%!=<g> 返回``) or branch on a private-chat shape
    (``如果:%群号%==0``). Either way the WebUI's synthesised DM
    scope behaves the same as a real QQ-side private message —
    rules don't have to know the request came from a browser.
    """
    client, token = main_group_chat_app
    r = client.post(
        "/api/agents/susu/chat",
        json={"input": "main_test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "dsl"
    # The handler's else-branch ran with %群号% set to "0" — that's
    # the QRSpeed convention for a private chat.
    assert "群号错了 0" in body["content"]


def test_webui_explicit_scope_id_can_target_main_group(main_group_chat_app) -> None:
    """An explicit ``scope_id=<group>`` in the request body still works.

    Operators who want to verify a group-only handler branch can pass
    the target group id directly. This is the manual escape hatch for
    rules that *only* run inside a specific group.
    """
    client, token = main_group_chat_app
    r = client.post(
        "/api/agents/susu/chat",
        json={"input": "main_test", "scope_id": "754800438"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "群号正确 754800438" in body["content"]


def test_webui_explicit_scope_id_overrides_default(main_group_chat_app) -> None:
    """An explicit ``scope_id`` overrides the per-account default."""
    client, token = main_group_chat_app
    r = client.post(
        "/api/agents/susu/chat",
        json={"input": "main_test", "scope_id": "999"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "群号错了 999" in body["content"]


def test_webui_ws_input_accepts_scope_id_frame(main_group_chat_app) -> None:
    """The WS ``input`` frame can also carry a ``scope_id`` override."""
    client, token = main_group_chat_app
    with client.websocket_connect(f"/ws/agents/susu/stream?token={token}") as ws:
        ws.receive_json()  # hello
        ws.send_json({"t": "input", "content": "main_test", "scope_id": "12345"})
        delta = ws.receive_json()
        assert delta["t"] == "delta"
        assert "群号错了 12345" in delta["text"]
        ws.receive_json()  # done


# ---------------------------------------------------------------------------
# Regression: ``$调用 0 handler$`` must emit inline on the WebUI surface.
# ---------------------------------------------------------------------------


@pytest.fixture
def inline_call_chat_app(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    """Bot whose rule uses ``$调用 0 X$`` to delegate the visible reply.

    QRSpeed's ``$调用 0 handler$`` is fire-and-forget on QQ — the
    inner handler emits one tick later as a separate bubble. On the
    WebUI's single-bubble surface that scheduler hop made the rule
    silent (the scheduled task fired *after* the WebUI dispatch
    returned). The fix promotes ``ms == 0`` to a synchronous inline
    emit through ``ctx.extras["_inline_emit"]`` so the inner
    handler's segments land on the calling rule's output buffer.
    """
    _write(
        tmp_path,
        "rules/main.ling",
        # Outer trigger calls an [内部] handler with $调用 0$. The
        # inner emits a bare text line; without the inline-emit
        # path the WebUI sees nothing.
        "echo_test\n$调用 0 echo$\n\n[内部]echo\n苏苏在\n",
    )
    bot_yaml = """\
bot_id: chat_test_inline
name: chat_test_inline
storage:
  kv: ":memory:"
rules:
  - "rules/**/*.ling"
"""
    cfg_path = _write(tmp_path, "bot.yaml", bot_yaml)

    import asyncio

    cfg = BotConfig.from_yaml(cfg_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = loop.run_until_complete(bootstrap_bot(cfg, base_dir=tmp_path))
    bot.agents = {"susu": _FakeAgent()}

    web_cfg = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
        login_rate_per_minute=10_000,
    )
    app = create_app(web_cfg)
    app.state.runtime.auth.upsert_user("admin", "Test-Pwd-9!aB", role="superadmin")
    attach_bot_to_webui(app, bot)

    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Test-Pwd-9!aB"},
        ).json()["access"]
        try:
            yield client, token
        finally:
            loop.run_until_complete(bot.stop())
            loop.close()


def test_zero_delay_invoke_emits_inline_on_webui(inline_call_chat_app) -> None:
    """``$调用 0 handler$`` synchronously emits the inner handler's text.

    Without the inline-emit path this rule was silent on the WebUI:
    the outer handler returned before the scheduler fired the inner,
    and the inner's reply landed on the bot's adapter sink (= the
    OneBot adapter, which has no idea what a WebUI is). The fix
    makes the inner handler's segments appear on the same WebUI
    bubble as the outer.
    """
    client, token = inline_call_chat_app
    r = client.post(
        "/api/agents/susu/chat",
        json={"input": "echo_test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "dsl"
    assert "苏苏在" in body["content"]


# ---------------------------------------------------------------------------
# Regression: WebUI chat must thread short-term + persistent history through
# every turn. Without this, the LLM sees a fresh prompt every time and can't
# answer "what did I just say?" — the bug the operator hit on the screenshot
# (LLM responding with "我没有保存对话历史").
# ---------------------------------------------------------------------------


class _RecordingProvider:
    """Echo provider that captures every prompt it ever saw.

    Counts how many ``user`` turns the dispatcher prepended (history
    rehydration) so a test can assert "the second turn carried the
    first turn's user + assistant messages".
    """

    def __init__(self) -> None:
        # Snapshot of every prompt the provider received, in order.
        # Each entry is the full ``messages`` list the dispatcher
        # built for that call — including any system prompt, the
        # rehydrated history, and the fresh user turn.
        self.prompts: list[list] = []  # type: ignore[type-arg]

    @property
    def name(self) -> str:
        return "recording"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):  # type: ignore[no-untyped-def]
        from linling_agent.llm import LLMResponse, Message, TokenUsage

        # ``list(...)`` defends against the caller mutating its
        # outbound buffer between turns (the runtime appends tool
        # results in place when tool calling is exercised).
        self.prompts.append(list(messages))
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"), ""
        )
        user_count = sum(1 for m in messages if m.role == "user")
        return LLMResponse(
            message=Message(
                role="assistant",
                content=f"[heard turn {user_count}: {last_user}]",
            ),
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def chat_stream(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.fixture
def history_chat_app(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str, _RecordingProvider, RunningBot]]:
    """Bot whose chat dispatcher uses a real :class:`AgentChatDispatcher`.

    We bootstrap a bot without ``agent.default_agent`` (so the network
    isn't touched) and then *replace* the static fallback dispatcher
    with a real history-aware :class:`AgentChatDispatcher` wired to a
    recording fake provider. That gives us identical wiring to a
    production deployment without needing API keys.

    Yields ``(client, token, provider, bot)`` — ``bot`` is exposed so
    tests can simulate a process restart by clearing its
    :class:`ConversationStore`.
    """
    _write(tmp_path, "rules/main.ling", "ping\npong\n")  # any rule, won't match
    bot_yaml = """\
bot_id: chat_test_history
name: chat_test_history
storage:
  kv: ":memory:"
rules:
  - "rules/**/*.ling"
"""
    cfg_path = _write(tmp_path, "bot.yaml", bot_yaml)

    import asyncio

    from linling_agent.agent_def import AgentDef
    from linling_agent.bridge import AgentRegistry
    from linling_agent.dispatcher import AgentChatDispatcher
    from linling_agent.history import KVHistoryStore
    from linling_agent.runtime import AgentRuntime
    from linling_core.tools import registry as tool_registry

    cfg = BotConfig.from_yaml(cfg_path)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = loop.run_until_complete(bootstrap_bot(cfg, base_dir=tmp_path))

    # Build a real :class:`AgentChatDispatcher` against the bot's
    # already-open KV. Sharing the KV is what makes the persistent
    # ``KVHistoryStore`` survive a "process restart" simulation.
    provider = _RecordingProvider()
    agent_def = AgentDef(name="susu", system="", tools=[], temperature=0.0)
    runtime = AgentRuntime(
        agent_def=agent_def,
        provider=provider,  # type: ignore[arg-type]
        tool_registry=tool_registry,
        kv=bot.kv,
        bot_id=bot.config.bot_id,
        metrics=bot.metrics,
    )
    history_store = KVHistoryStore(bot.kv, max_turns=8)
    bot.chat_dispatcher = AgentChatDispatcher(agent=runtime, history_store=history_store)
    bot.agents = {"susu": runtime}

    # Mirror the registry wiring ``bootstrap_bot`` does when an agent
    # is configured, so the WebUI's ``/api/agents`` discovery sees us.
    agent_registry = AgentRegistry()
    agent_registry.register("susu", runtime)
    bot.router.command_dispatcher.update_extras(agent_registry=agent_registry)

    web_cfg = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
        login_rate_per_minute=10_000,
    )
    app = create_app(web_cfg)
    app.state.runtime.auth.upsert_user("admin", "Test-Pwd-9!aB", role="superadmin")

    attach_bot_to_webui(app, bot)

    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Test-Pwd-9!aB"},
        ).json()["access"]
        try:
            yield client, token, provider, bot
        finally:
            loop.run_until_complete(bot.stop())
            loop.close()


def test_webui_chat_remembers_previous_turn(history_chat_app) -> None:
    """Two consecutive ``/api/agents/*/chat`` posts must share history.

    The second turn's prompt should contain *both* user messages — that's
    what lets the LLM answer "what did I just say?" instead of seeing a
    fresh context. Before the fix, the WebUI fallback called
    ``runtime.invoke`` directly and discarded ``session.history``.
    """
    client, token, provider, _bot = history_chat_app

    r1 = client.post(
        "/api/agents/susu/chat",
        json={"input": "我喜欢抹茶拿铁"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["source"] == "agent"
    assert "我喜欢抹茶拿铁" in body1["content"]

    r2 = client.post(
        "/api/agents/susu/chat",
        json={"input": "我刚才说了什么?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    # Echo provider tags the reply with how many user turns it saw —
    # 2 means the dispatcher correctly prepended turn 1.
    assert "turn 2" in body2["content"], body2["content"]

    # Direct prompt inspection: the second prompt must include the
    # first user message *and* its assistant reply, in order.
    assert len(provider.prompts) == 2
    second = provider.prompts[1]
    roles = [m.role for m in second]
    contents = [m.content for m in second]
    # Order matters: rehydrated user → assistant → fresh user.
    assert roles[-3:] == ["user", "assistant", "user"], roles
    assert contents[-3] == "我喜欢抹茶拿铁"
    assert contents[-1] == "我刚才说了什么?"


def test_webui_chat_history_survives_process_restart(history_chat_app) -> None:
    """KVHistoryStore must rehydrate when the in-memory deque was lost.

    Simulates a restart by clearing the bot's :class:`ConversationStore`
    in between turns. The persistent history layer should refill the
    new session's deque from KV before the next dispatch.
    """
    client, token, provider, bot = history_chat_app
    import asyncio

    # Turn 1 — populates both the in-memory deque and the KV row.
    r1 = client.post(
        "/api/agents/susu/chat",
        json={"input": "我喜欢抹茶拿铁"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200

    # Simulate a restart: drop every cached :class:`Session`. The
    # WebUI's next dispatch will allocate a fresh one, find an empty
    # deque, and (correctly) ask :class:`KVHistoryStore` to rehydrate.
    async def _drop_all_sessions() -> None:
        async with bot.conversations._guard:
            bot.conversations._sessions.clear()

    asyncio.get_event_loop().run_until_complete(_drop_all_sessions())

    # Turn 2 — must still see the prior turn via the KV-backed
    # rehydration even though the in-memory session was wiped.
    r2 = client.post(
        "/api/agents/susu/chat",
        json={"input": "我刚才说了什么?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    assert "turn 2" in r2.json()["content"]

    second = provider.prompts[1]
    contents = [m.content for m in second]
    assert "我喜欢抹茶拿铁" in contents, (
        f"history not rehydrated from KV after session drop: {contents!r}"
    )


def test_webui_chat_history_isolated_per_user(history_chat_app) -> None:
    """Two WebUI accounts must not see each other's chat history.

    The DM scope is shared (``id="0"``) but ``ConversationKey`` also
    pins on ``sender_id``, which is the WebUI account name. Different
    operators therefore keep separate history rows in the KV store.
    """
    client, token, provider, _bot = history_chat_app

    # First account talks once.
    r1 = client.post(
        "/api/agents/susu/chat",
        json={"input": "I am alice"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200

    # Register a second account, log in, and have it speak.
    app = client.app
    app.state.runtime.auth.upsert_user("bob", "Test-Pwd-9!aB", role="superadmin")
    bob_token = client.post(
        "/api/auth/login",
        json={"username": "bob", "password": "Test-Pwd-9!aB"},
    ).json()["access"]

    r2 = client.post(
        "/api/agents/susu/chat",
        json={"input": "I am bob"},
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert r2.status_code == 200
    # Bob's first turn must show user_count == 1 (no cross-talk).
    assert "turn 1" in r2.json()["content"]

    # Bob's second turn sees turn 2 (his own history).
    r3 = client.post(
        "/api/agents/susu/chat",
        json={"input": "remember me?"},
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert r3.status_code == 200
    assert "turn 2" in r3.json()["content"]
    # Bob's prompt must NOT contain Alice's input.
    bob_second_prompt = provider.prompts[-1]
    assert all("alice" not in m.content.lower() for m in bob_second_prompt)
