from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from linling_agent.agent_def import AgentDef
from linling_agent.context import ContextBudget
from linling_agent.dispatcher import AgentChatDispatcher
from linling_agent.group_batch import GroupBatchChatDispatcher, GroupBatchConfig
from linling_agent.history import KVHistoryStore
from linling_agent.llm import LLMResponse, Message, ToolCall, ToolSchema
from linling_agent.runtime import AgentResult, AgentRuntime
from linling_core.events import Action, Event, Scope, User
from linling_core.pipeline import ConversationKey, ConversationStore, Session
from linling_core.segments import ReplySegment, TextSegment, at, reply
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry


class _Inner:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[str] = []
        self.events: list[Event] = []
        self.recorded: list[tuple[str, str, str]] = []
        self.stopped = False

    async def dispatch(self, event: Event, session: Session) -> AgentResult:
        self.calls.append(event.text)
        self.events.append(event)
        return AgentResult(content=self.content)

    async def run(self, event: Event, session: Session) -> list[str]:
        result = await self.dispatch(event, session)
        return [result.content]

    async def record_history(
        self,
        *,
        session: Session,
        scope_id: str,
        sender_id: str,
        user_input: str,
        assistant_output: str,
    ) -> None:
        self.recorded.append((sender_id, user_input, assistant_output))

    async def stop(self) -> None:
        self.stopped = True


class _ClearInner(_Inner):
    def __init__(self, content: str) -> None:
        super().__init__(content)
        self.history_clears: list[tuple[str, str]] = []

    async def clear_history(self, scope_id: str, sender_id: str) -> None:
        self.history_clears.append((scope_id, sender_id))


class _AgentInner(_Inner):
    def __init__(self, provider: object, content: str = "") -> None:
        super().__init__(content)
        self._agent = type(
            "Agent",
            (),
            {
                "provider": provider,
                "agent_def": AgentDef(name="batch-agent", model="mock", system=""),
            },
        )()
        self.ensure_calls = 0
        self.ensure_keys: list[tuple[str, str]] = []

    @property
    def agent(self):
        return self._agent

    @property
    def context_max_tokens(self) -> int:
        return 4_000

    async def ensure_history(self, session: Session, event: Event) -> None:
        _ = session, event
        self.ensure_calls += 1

    async def ensure_history_key(self, session: Session, scope_id: str, sender_id: str) -> None:
        _ = session
        self.ensure_keys.append((scope_id, sender_id))


class _ToolProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[Message], list[ToolSchema] | None]] = []

    @property
    def name(self) -> str:
        return "tool-provider"

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        _ = temperature, max_tokens
        self.calls.append((list(messages), tools))
        return self.responses.pop(0)

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


class _FailingProvider:
    @property
    def name(self) -> str:
        return "failing-provider"

    async def chat(self, *args, **kwargs):
        _ = args, kwargs
        raise RuntimeError("tools unsupported")

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


class _SummaryThenNoopProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Message], list[ToolSchema] | None]] = []

    @property
    def name(self) -> str:
        return "summary-tool-provider"

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        _ = temperature, max_tokens
        self.calls.append((list(messages), tools))
        if len(messages) == 1 and messages[0].content.startswith("Summarize"):
            return LLMResponse(message=Message(role="assistant", content="group compressed facts"))
        return LLMResponse(message=Message(role="assistant", content=""))

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


class _BlockingToolProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def name(self) -> str:
        return "blocking-tool-provider"

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        _ = messages, tools, temperature, max_tokens
        self.started.set()
        await self.release.wait()
        return LLMResponse(
            message=Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="send1",
                        name="send_group",
                        arguments=json.dumps({"text": "不该发送"}, ensure_ascii=False),
                    )
                ],
            )
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


class _PromptInner(_Inner):
    def __init__(self, content: str) -> None:
        super().__init__(content)
        self.messages: list[list[Message]] = []

    async def dispatch(self, event: Event, session: Session) -> AgentResult:
        self.events.append(event)
        system = str(event.raw.get("_linling_prompt_system") or "")
        if system:
            self.messages.append(
                [
                    Message(role="system", content=system),
                    Message(role="user", content=event.text),
                ]
            )
        else:
            self.messages.append([Message(role="user", content=event.text)])
        return AgentResult(content=self.content)


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


async def test_group_batch_silences_empty_actions() -> None:
    inner = _Inner('{"actions":[]}')
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    actions = await dispatcher.run(_event("普通闲聊"), session)

    assert actions == []
    await _wait_for(lambda: len(inner.calls) == 1)
    assert len(inner.calls) == 1
    assert inner.recorded == []
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_reply_to_message_action_and_history() -> None:
    content = json.dumps(
        {
            "actions": [
                {
                    "type": "reply_to_message",
                    "message_id": "m1",
                    "text": "可以，这条我回。",
                }
            ]
        },
        ensure_ascii=False,
    )
    inner = _Inner(content)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    actions = await dispatcher.run(_event("苏苏在吗？", eid="m1"), session)

    assert actions == []
    await _wait_for(lambda: len(sent) == 1)
    action = sent[0]
    assert action.kind == "reply"
    assert isinstance(action.segments[0], ReplySegment)
    assert action.segments[0].message_id == "m1"
    assert action.segments[1].text == "可以，这条我回。"
    assert len(inner.recorded) == 1
    assert inner.recorded[0][0] == ""
    assert "苏苏在吗" in inner.recorded[0][1]
    assert "可以" in inner.recorded[0][2]
    assert inner.events[0].raw["_linling_skip_history"] is True
    assert inner.events[0].raw["_linling_disable_tools"] is True
    await dispatcher.stop()


async def test_group_batch_toolcall_reply_action_and_history() -> None:
    provider = _ToolProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="tc1",
                            name="reply_to_message",
                            arguments=json.dumps(
                                {"message_id": "m1", "text": "可以，这条我回。"},
                                ensure_ascii=False,
                            ),
                        )
                    ],
                )
            )
        ]
    )
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("苏苏在吗？", eid="m1"), session)

    await _wait_for(lambda: len(sent) == 1)
    assert provider.calls[0][1]
    assert "完整内容" not in provider.calls[0][0][-1].content
    assert sent[0].kind == "reply"
    assert sent[0].segments[0].message_id == "m1"
    assert inner.recorded
    assert "苏苏在吗" in inner.recorded[0][1]
    assert "可以，这条我回" in inner.recorded[0][2]
    assert inner.ensure_calls == 0
    assert inner.ensure_keys == [("g1", "")]
    await dispatcher.stop()


async def test_group_batch_toolcall_can_read_batch_before_replying() -> None:
    provider = _ToolProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="read1",
                            name="read_batch_messages",
                            arguments=json.dumps({"message_ids": ["m1"]}),
                        )
                    ],
                )
            ),
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="send1",
                            name="reply_to_message",
                            arguments=json.dumps(
                                {"message_id": "m1", "text": "看到了。"},
                                ensure_ascii=False,
                            ),
                        )
                    ],
                )
            ),
        ]
    )
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("需要看完整内容", eid="m1"), session)

    await _wait_for(lambda: len(sent) == 1)
    second_prompt = provider.calls[1][0]
    tool_messages = [m for m in second_prompt if m.role == "tool"]
    assert tool_messages
    assert "需要看完整内容" in tool_messages[0].content
    await dispatcher.stop()


async def test_group_batch_toolcall_send_failure_does_not_record_history() -> None:
    provider = _ToolProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="tc1",
                            name="send_group",
                            arguments=json.dumps({"text": "不该记住"}, ensure_ascii=False),
                        )
                    ],
                )
            )
        ]
    )
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )

    async def _fail(action: Action) -> None:
        _ = action
        raise RuntimeError("send failed")

    dispatcher.set_action_sink(_fail)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("苏苏在吗？", eid="m1"), session)

    await _wait_for(lambda: len(provider.calls) == 1)
    assert inner.recorded == []
    await dispatcher.stop()


async def test_group_batch_toolcall_summarizes_shared_group_history_without_recording_noop() -> None:
    kv = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with kv:
        history = KVHistoryStore(kv, max_turns=20)
        provider = _SummaryThenNoopProvider()
        agent_def = AgentDef(name="batch-agent", model="mock", system="")
        agent = AgentRuntime(
            agent_def=agent_def,
            provider=provider,
            tool_registry=registry,
            kv=kv,
            bot_id="bot1",
        )
        inner = AgentChatDispatcher(
            agent=agent,
            history_store=history,
            context_budget=ContextBudget(
                max_tokens=2_000,
                summary_trigger_tokens=300,
                summary_keep_recent_turns=1,
                summary_max_tokens=50,
            ),
        )
        conversations = ConversationStore(rate_per_second=100, burst=100, history_turns=20)
        dispatcher = GroupBatchChatDispatcher(
            inner=inner,
            config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
            conversations=conversations,
            bot_id="bot1",
        )
        sent: list[Action] = []
        dispatcher.set_action_sink(sent.append)
        group_session = await conversations.get_or_create(
            ConversationKey("bot1", "g1", "")
        )
        for i in range(6):
            group_session.history.append(Message(role="user", content=f"old user {i} " + "很长" * 20))
            group_session.history.append(Message(role="assistant", content=f"old assistant {i}"))

        await dispatcher.run(_event("普通闲聊", eid="m1"), group_session)

        await _wait_for(lambda: len(provider.calls) >= 2)
        assert sent == []
        assert await history.load_summary("g1", "") == "group compressed facts"
        actual_prompt = provider.calls[-1][0]
        assert provider.calls[-1][1]
        assert any("<conversation_summary>" in message.content for message in actual_prompt)
        assert not any(message.content == "普通闲聊" for message in group_session.history)
        loaded = await history.load("g1", "")
        assert all("普通闲聊" not in message.content for message in loaded)
        await dispatcher.stop()


async def test_group_batch_falls_back_to_json_when_toolcall_unavailable() -> None:
    content = json.dumps(
        {
            "actions": [
                {
                    "type": "reply_to_message",
                    "message_id": "m1",
                    "text": "fallback ok",
                }
            ]
        }
    )
    inner = _AgentInner(_FailingProvider(), content=content)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("苏苏在吗？", eid="m1"), session)

    await _wait_for(lambda: len(sent) == 1)
    assert sent[0].segments[1].text == "fallback ok"
    assert inner.calls
    assert inner.recorded
    await dispatcher.stop()


async def test_group_batch_drops_uninteresting_when_required() -> None:
    inner = _Inner('{"actions":[{"type":"send_group","text":"不该发"}]}')
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0,
            require_attention=True,
            max_hold_s=0.05,
        ),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    actions = await dispatcher.run(_event("哈哈哈哈"), session)

    assert actions == []
    await asyncio.sleep(0.1)
    assert inner.calls == []
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_keeps_idle_context_until_attention() -> None:
    content = json.dumps(
        {
            "actions": [
                {
                    "type": "reply_to_message",
                    "message_id": "m2",
                    "text": "在。",
                }
            ]
        },
        ensure_ascii=False,
    )
    inner = _Inner(content)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0,
            require_attention=True,
            max_hold_s=1.0,
            bot_names=("苏苏",),
        ),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("前情闲聊", eid="m1"), session)
    await asyncio.sleep(0.05)
    assert inner.calls == []

    await dispatcher.run(_event("苏苏在吗？", eid="m2"), session)

    await _wait_for(lambda: len(sent) == 1)
    assert "前情闲聊" in inner.calls[0]
    assert "苏苏在吗" in inner.calls[0]
    await dispatcher.stop()


async def test_group_batch_system_prompt_is_separate_from_candidate_messages() -> None:
    inner = _PromptInner('{"actions":[]}')
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )
    async def _ignore(action: Action) -> None:
        return None

    dispatcher.set_action_sink(_ignore)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("普通闲聊"), session)

    await _wait_for(lambda: len(inner.messages) == 1)
    assert inner.messages[0][0].role == "system"
    assert "只输出严格 JSON" in inner.messages[0][0].content
    assert "候选消息" in inner.messages[0][1].content
    assert "只输出严格 JSON" not in inner.messages[0][1].content
    await dispatcher.stop()


async def test_group_batch_does_not_record_history_when_sink_missing() -> None:
    inner = _Inner('{"actions":[{"type":"send_group","text":"不该记住"}]}')
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("苏苏在吗？"), session)

    await _wait_for(lambda: len(inner.calls) == 1)
    assert inner.recorded == []
    await dispatcher.stop()


async def test_group_batch_treats_reply_segment_without_source_as_attention() -> None:
    inner = _Inner('{"actions":[]}')
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0,
            require_attention=True,
            max_hold_s=1.0,
        ),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))
    event = _event("接着说", eid="m1")
    event.segments.insert(0, reply("quoted"))

    await dispatcher.run(event, session)

    await _wait_for(lambda: len(inner.calls) == 1)
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_recognizes_raw_self_id_mentions() -> None:
    inner = _Inner('{"actions":[]}')
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0,
            require_attention=True,
            max_hold_s=1.0,
        ),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))
    event = _event("在吗", eid="m1")
    event.segments.insert(0, at("qq-self"))
    event.raw["self_id"] = "qq-self"

    await dispatcher.run(event, session)

    await _wait_for(lambda: len(inner.calls) == 1)
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_clear_history_cancels_pending_batch() -> None:
    inner = _ClearInner('{"actions":[{"type":"send_group","text":"不该发"}]}')
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0.2,
            require_attention=False,
            max_hold_s=1.0,
        ),
        bot_id="configured-bot",
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("configured-bot", "g1", "u1"))
    event = _event("稍后不该触发", eid="m1")
    event.bot_id = "platform-self"

    await dispatcher.run(event, session)
    await dispatcher.clear_history("g1", "u1")
    await asyncio.sleep(0.25)

    assert inner.calls == []
    assert sent == []
    assert inner.history_clears == [("g1", "u1"), ("g1", "")]
    await dispatcher.stop()


async def test_group_batch_clear_history_marks_inflight_tool_batch_stale() -> None:
    provider = _BlockingToolProvider()
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
        bot_id="bot1",
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("苏苏在吗？", eid="m1"), session)
    await asyncio.wait_for(provider.started.wait(), timeout=1.0)
    await dispatcher.clear_history("g1", "u1")
    provider.release.set()
    await asyncio.sleep(0.05)

    assert sent == []
    assert inner.recorded == []
    await dispatcher.stop()
