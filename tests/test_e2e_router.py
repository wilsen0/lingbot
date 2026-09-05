"""End-to-end router smoke test.

This ties together the real classifier, conversation store, DSL VM, and
a mock LLM so we can prove the full dispatch chain actually works:

```
Adapter → EventBus.publish(event)
    → Router.handle (subscriber)
    → MessageClassifier → Intent
    → DSL VM (command) or AgentRuntime (chat)
    → ActionSink → collected
```

Highlighted behaviours exercised:

* ``/我的灵玉`` (prefixed) hits the DSL and returns the QRDic balance.
* ``/充值`` writes through the KV store.
* A bare ``你好`` with no matching handler goes to the (mocked) agent.
* Two concurrent chat messages from distinct users are processed in
  parallel; two from the *same* user are serialised.

We deliberately do not load any .ling files — the DSL script lives
inline so the test is fast and self-contained.
"""

from __future__ import annotations

import asyncio
import json

import linling_core.tools_builtin  # noqa: F401 — register DSL built-ins
import linling_tools_stdlib  # noqa: F401 — registers send_reply
import pytest
from linling_agent.agent_def import AgentDef
from linling_agent.dispatcher import AgentChatDispatcher
from linling_agent.llm import LLMResponse, Message, TokenUsage, ToolCall
from linling_agent.runtime import AgentRuntime
from linling_core.bus import EventBus
from linling_core.classifier import MessageClassifier
from linling_core.events import Action, Event, Scope, User
from linling_core.pipeline import ConversationStore
from linling_core.router import Router, RouterConfig
from linling_core.segments import TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry
from linling_dsl.dispatcher import DslCommandDispatcher
from linling_dsl.parser import parse

# ---------------------------------------------------------------------------
# Inline QRDic-style ruleset
# ---------------------------------------------------------------------------


RULES = """\
我的灵玉
玉:$读 啊/灵玉系/灵玉 %QQ% 0$
你有 %玉% 灵玉

充值
玉:$读 啊/灵玉系/灵玉 %QQ% 0$
$写 啊/灵玉系/灵玉 %QQ% [%玉%+288]$
完成
"""


# ---------------------------------------------------------------------------
# Mock LLM provider
# ---------------------------------------------------------------------------


class EchoProvider:
    """Mock provider that replies with the last user message's content verbatim."""

    @property
    def name(self) -> str:
        return "echo"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        # Find the last user message.
        user_text = ""
        for m in reversed(messages):
            if m.role == "user":
                user_text = m.content
                break
        return LLMResponse(
            message=Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="s1",
                        name="send_reply",
                        arguments=json.dumps({"text": f"echo:{user_text}"}),
                    ),
                    ToolCall(
                        id="f1",
                        name="finish_turn",
                        arguments='{"summary":"echoed"}',
                    ),
                ],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _event(text: str, *, sender: str = "u1", group: str = "g1", eid: str | None = None) -> Event:
    return Event(
        id=eid or f"e-{sender}-{text}",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id=group, platform="test"),
        sender=User(id=sender, platform="test", display_name=sender),
        segments=[TextSegment(text=text)],
    )


async def _build_stack():
    """Spin up the full stack the way the real bot runner would.

    Returns ``(router, actions, kv)``. The caller owns ``kv`` and should
    ``await kv.close()`` when done; the e2e tests below use pytest
    fixtures via a helper.
    """
    kv = SqliteKVStore(bot_id="linling", db_path=":memory:")
    await kv._ensure()
    script = parse(RULES, strict=False)
    classifier = MessageClassifier(script)

    # DSL dispatcher.
    cmd = DslCommandDispatcher(registry=registry, kv=kv, bot_id="linling")

    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    # Agent dispatcher backed by the echo provider.
    agent_def = AgentDef(name="default", model="mock", system="", tools=["send_reply"])
    agent = AgentRuntime(
        agent_def=agent_def,
        provider=EchoProvider(),
        tool_registry=registry,
        kv=kv,
        bot_id="linling",
        action_sink=sink,
    )
    chat = AgentChatDispatcher(agent=agent)
    chat.set_action_sink(sink)

    router = Router(
        classifier=classifier,
        commands=cmd,
        chats=chat,
        sink=sink,
        conversations=ConversationStore(rate_per_second=100, burst=100),
        config=RouterConfig(max_concurrent_events=32, enqueue_timeout_s=1.0),
    )
    return router, actions, kv


@pytest.fixture
async def stack():
    """Pytest fixture that yields (router, actions, kv) and closes ``kv``."""
    router, actions, kv = await _build_stack()
    try:
        yield router, actions, kv
    finally:
        await kv.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_prefixed_command_reads_kv(stack):
    router, actions, kv = stack
    # Seed balance for u1.
    await kv.write("啊/灵玉系", "灵玉", "u1", "500")

    await router.handle(_event("/我的灵玉"))

    assert actions
    text = actions[-1].segments[0].text
    assert "500" in text


async def test_command_without_prefix_still_works(stack):
    router, actions, kv = stack
    await kv.write("啊/灵玉系", "灵玉", "u1", "42")

    await router.handle(_event("我的灵玉"))
    assert "42" in actions[-1].segments[0].text


async def test_command_writes_to_kv(stack):
    router, _, kv = stack

    await router.handle(_event("/充值", eid="a"))
    await router.handle(_event("/充值", eid="b"))

    assert await kv.read("啊/灵玉系", "灵玉", "u1") == "576"


async def test_chat_goes_to_agent_via_router(stack):
    router, actions, _ = stack
    await router.handle(_event("hello there"))

    assert actions
    assert actions[-1].segments[0].text == "echo:hello there"


async def test_unknown_prefix_does_not_reach_agent(stack):
    router, actions, _ = stack
    await router.handle(_event("/does-not-exist"))

    text = actions[-1].segments[0].text
    assert text.startswith("Unknown command")
    # The agent must not have been invoked for an unknown slash command.
    assert "echo:" not in text


async def test_concurrent_different_senders_run_in_parallel(stack):
    router, actions, kv = stack
    # Prime both balances.
    await kv.write("啊/灵玉系", "灵玉", "u1", "11")
    await kv.write("啊/灵玉系", "灵玉", "u2", "22")

    await asyncio.gather(
        router.handle(_event("我的灵玉", sender="u1", eid="1")),
        router.handle(_event("我的灵玉", sender="u2", eid="2")),
    )

    texts = [a.segments[0].text for a in actions]
    assert any("11" in t for t in texts)
    assert any("22" in t for t in texts)


async def test_concurrent_same_sender_serialises(stack):
    """充值 from the same user twice concurrently must both succeed with no lost update."""
    router, _, kv = stack
    await kv.write("啊/灵玉系", "灵玉", "u1", "0")

    # Kick off 5 concurrent top-ups. Without the session lock, the
    # read-modify-write race could clobber intermediate states; with it,
    # the final balance must be exactly 5 * 288 = 1440.
    await asyncio.gather(*(router.handle(_event("/充值", eid=f"x-{i}")) for i in range(5)))

    assert await kv.read("啊/灵玉系", "灵玉", "u1") == "1440"


async def test_event_bus_integration(stack):
    """Wire the router as a bus subscriber and publish through the bus."""
    router, actions, kv = stack
    await kv.write("啊/灵玉系", "灵玉", "u1", "7")

    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    await bus.publish(_event("/我的灵玉"))
    # Duplicate publish — the router should drop it.
    await bus.publish(_event("/我的灵玉", eid="e-u1-/我的灵玉"))

    # Only one reply emitted even though we published twice.
    assert len(actions) == 1
    assert "7" in actions[0].segments[0].text
