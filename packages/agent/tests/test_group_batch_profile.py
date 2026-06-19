"""Tests for profile tools inside GroupBatchChatDispatcher (Phase 5)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from linling_agent.agent_def import AgentDef
from linling_agent.group_batch import GroupBatchChatDispatcher, GroupBatchConfig
from linling_agent.llm import LLMResponse, Message, ToolCall, ToolSchema
from linling_agent.profile import ProfileStore
from linling_agent.runtime import AgentResult
from linling_core.events import Action, Event, Scope, User
from linling_core.pipeline import ConversationKey, ConversationStore, Session
from linling_core.segments import TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore


class _Inner:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.calls: list[str] = []
        self.recorded_messages: list[list[Message]] = []
        self._agent = type(
            "Agent",
            (),
            {
                "provider": None,
                "agent_def": AgentDef(name="batch-agent", model="mock", system=""),
            },
        )()

    @property
    def agent(self):
        return self._agent

    @property
    def context_max_tokens(self) -> int:
        return 4_000

    async def dispatch(self, event: Event, session: Session) -> AgentResult:
        self.calls.append(event.text)
        return AgentResult(content=self.content)

    async def ensure_history_key(self, session: Session, scope_id: str, sender_id: str) -> None:
        _ = session, scope_id, sender_id

    async def record_messages(
        self, *, session: Session, scope_id: str, sender_id: str, messages: list[Message]
    ) -> None:
        _ = scope_id, sender_id
        self.recorded_messages.append(list(messages))

    async def stop(self) -> None:
        pass


class _AgentInner(_Inner):
    def __init__(self, provider: object, content: str = "") -> None:
        super().__init__(content)
        self._agent = type(
            "Agent",
            (),
            {"provider": provider, "agent_def": AgentDef(name="batch-agent", model="mock", system="")},
        )()


class _ToolProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[Message], list[ToolSchema] | None]] = []

    @property
    def name(self) -> str:
        return "tool-provider"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        self.calls.append((list(messages), tools))
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(
            message=Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="auto_ft", name="finish_turn", arguments='{"summary":"done"}')
                ],
            )
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


def _event(text: str, *, eid: str = "m1", sender: str = "u1") -> Event:
    return Event(
        id=eid,
        platform="test",
        bot_id="bot1",
        scope=Scope(kind="group", id="g1", platform="test"),
        sender=User(id=sender, platform="test", display_name=sender),
        segments=[TextSegment(text=text)],
    )


async def _wait_for(condition: Callable[[], bool]) -> None:
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def _tc(cid: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=json.dumps(args, ensure_ascii=False))


async def test_profile_tools_present_in_schema() -> None:
    from linling_agent.group_batch import _group_batch_tool_schemas

    names = {s.name for s in _group_batch_tool_schemas()}
    assert "read_user_profile" in names
    assert "write_user_profile" in names


async def test_probe_tool_subset_excludes_profile_tools() -> None:
    """The attention probe must only see reply-oriented tools.

    A curiosity ``read_user_profile`` is not a reply intent and would be
    mis-counted as "no action" by ``_assistant_message_has_action``, wrongly
    vetoing the batch. So the probe's tool list excludes profile tools.
    """
    from linling_agent.group_batch import (
        _group_batch_tool_schemas,
        _reply_tool_schemas,
    )

    probe_tools = {s.name for s in _reply_tool_schemas()}
    assert probe_tools == {"read_batch_messages", "reply_to_message", "send_group", "finish_turn"}
    assert "read_user_profile" not in probe_tools
    assert "write_user_profile" not in probe_tools
    # The main loop still gets everything.
    main_tools = {s.name for s in _group_batch_tool_schemas()}
    assert {"read_user_profile", "write_user_profile", "send_group", "finish_turn"} <= main_tools


async def test_read_profile_tool_continues_loop_no_action() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        await ProfileStore(kv).save("u1", "老画像", name="小明")
        provider = _ToolProvider(
            [
                LLMResponse(
                    message=Message(
                        role="assistant",
                        content="",
                        tool_calls=[_tc("c1", "read_user_profile", {"qq": "u1"})],
                    )
                ),
                # After reading, decide to stay silent.
                LLMResponse(message=Message(role="assistant", content="no_reply")),
            ]
        )
        inner = _AgentInner(provider)
        dispatcher = GroupBatchChatDispatcher(
            inner=inner,
            config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
            kv=kv,
        )
        sent: list[Action] = []
        dispatcher.set_action_sink(sent.append)
        store = ConversationStore(rate_per_second=100, burst=100)
        session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

        await dispatcher.run(_event("苏苏，u1 是谁？", eid="m1"), session)

        # Two provider rounds: read tool, then no_reply.
        await _wait_for(lambda: len(provider.calls) >= 2)
        # The read result reached the model as a tool message containing the profile.
        second_round_msgs = provider.calls[1][0]
        assert any(
            m.role == "tool" and "老画像" in m.content for m in second_round_msgs
        )
        # Read tool produced no outbound action.
        assert sent == []
        await dispatcher.stop()


async def test_write_profile_tool_persists() -> None:
    async with SqliteKVStore("bot1", ":memory:") as kv:
        provider = _ToolProvider(
            [
                LLMResponse(
                    message=Message(
                        role="assistant",
                        content="",
                        tool_calls=[
                            _tc(
                                "c1",
                                "write_user_profile",
                                {"qq": "u1", "profile": "喜欢钓鱼", "name": "小明"},
                            )
                        ],
                    )
                ),
                LLMResponse(message=Message(role="assistant", content="no_reply")),
            ]
        )
        inner = _AgentInner(provider)
        dispatcher = GroupBatchChatDispatcher(
            inner=inner,
            config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
            kv=kv,
        )
        sent: list[Action] = []
        dispatcher.set_action_sink(sent.append)
        store = ConversationStore(rate_per_second=100, burst=100)
        session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

        await dispatcher.run(_event("u1 说他喜欢钓鱼", eid="m1"), session)

        await _wait_for(lambda: len(provider.calls) >= 2)
        assert await ProfileStore(kv).load("u1") == "喜欢钓鱼"
        assert await ProfileStore(kv).load_name("u1") == "小明"
        await dispatcher.stop()


async def test_profile_tool_without_kv_returns_error_no_crash() -> None:
    provider = _ToolProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[_tc("c1", "read_user_profile", {"qq": "u1"})],
                )
            ),
            LLMResponse(message=Message(role="assistant", content="no_reply")),
        ]
    )
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
        kv=None,
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("u1 是谁", eid="m1"), session)

    await _wait_for(lambda: len(provider.calls) >= 2)
    tool_msgs = [m for m in provider.calls[1][0] if m.role == "tool"]
    assert tool_msgs
    assert "unavailable" in tool_msgs[-1].content
    assert sent == []
    await dispatcher.stop()
