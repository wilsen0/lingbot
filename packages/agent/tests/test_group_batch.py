from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime

from linling_agent.agent_def import AgentDef
from linling_agent.context import ContextBudget
from linling_agent.dispatcher import AgentChatDispatcher
from linling_agent.group_batch import GroupBatchChatDispatcher, GroupBatchConfig
from linling_agent.history import KVHistoryStore
from linling_agent.llm import ContentPart, LLMResponse, Message, ToolCall, ToolSchema
from linling_agent.runtime import AgentResult, AgentRuntime
from linling_agent.segments_text import llm_visible_text
from linling_core.events import Action, Event, Scope, User
from linling_core.pipeline import ConversationKey, ConversationStore, Session
from linling_core.segments import (
    FaceSegment,
    ImageSegment,
    Segment,
    TextSegment,
    at,
    reply,
)
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry


class _Inner:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[str] = []
        self.events: list[Event] = []
        self.recorded: list[tuple[str, str, str]] = []
        self.stopped = False
        self._default_provider: _ToolProvider | None = None

    @property
    def agent(self):
        if self._default_provider is None:
            self._default_provider = _ToolProvider([])
        return type(
            "Agent",
            (),
            {
                "provider": self._default_provider,
                "agent_def": AgentDef(name="inner-agent", model="mock", system=""),
            },
        )()

    @property
    def context_max_tokens(self) -> int:
        return 4_000

    async def ensure_history(self, session: Session, event: Event) -> None:
        pass

    async def ensure_history_key(self, session: Session, scope_id: str, sender_id: str) -> None:
        pass

    async def record_messages(
        self,
        *,
        session: Session,
        scope_id: str,
        sender_id: str,
        messages: list[Message],
    ) -> None:
        user_text = "\n".join(m.content for m in messages if m.role == "user")
        asst_parts = [m.content for m in messages if m.role == "assistant" and m.content]
        for m in messages:
            if m.role == "tool" and m.content:
                try:
                    payload = json.loads(m.content)
                    if isinstance(payload, dict):
                        for key in ("reply", "sent_text"):
                            if key in payload:
                                asst_parts.append(str(payload[key]))
                except (json.JSONDecodeError, TypeError):
                    pass
        self.recorded.append((sender_id, user_text, "\n".join(asst_parts)))

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


class _HistoryAgentInner(_AgentInner):
    def __init__(self, provider: object, content: str = "") -> None:
        super().__init__(provider, content=content)
        self.recorded_messages: list[list[Message]] = []

    async def record_history(
        self,
        *,
        session: Session,
        scope_id: str,
        sender_id: str,
        user_input: str,
        assistant_output: str,
    ) -> None:
        await self.record_messages(
            session=session,
            scope_id=scope_id,
            sender_id=sender_id,
            messages=[
                Message(role="user", content=user_input),
                Message(role="assistant", content=assistant_output),
            ],
        )

    async def record_messages(
        self,
        *,
        session: Session,
        scope_id: str,
        sender_id: str,
        messages: list[Message],
    ) -> None:
        _ = scope_id, sender_id
        self.recorded_messages.append(list(messages))
        self.recorded.append(
            (
                sender_id,
                "\n".join(message.content for message in messages if message.role == "user"),
                "\n".join(message.content for message in messages if message.role == "assistant"),
            )
        )
        session.history.extend(messages)


class _ToolProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
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
        if self.responses:
            return self.responses.pop(0)
        # Repeat a finish_turn response when exhausted so the loop terminates
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


class _ProbeWithProvider:
    def __init__(self, provider: _ToolProvider, *, model: str = "probe-model") -> None:
        self.provider = provider
        self.model = model
        self.aclose_count = 0

    async def aclose(self) -> None:
        self.aclose_count += 1


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


class _FailingSummaryHistoryStore(KVHistoryStore):
    async def save_summary(self, scope_id: str, sender_id: str, summary: str) -> None:
        _ = scope_id, sender_id, summary
        raise RuntimeError("summary store unavailable")


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


def _event(
    text: str,
    *,
    eid: str = "m1",
    sender: str = "u1",
    name: str | None = None,
    at: datetime | None = None,
    segments: list[Segment] | None = None,
) -> Event:
    data = {
        "id": eid,
        "platform": "test",
        "bot_id": "bot1",
        "scope": Scope(kind="group", id="g1", platform="test"),
        "sender": User(id=sender, platform="test", display_name=name or sender),
        "segments": [TextSegment(text=text)] if segments is None else segments,
    }
    if at is not None:
        data["time"] = at
    return Event(**data)


async def _wait_for(condition: Callable[[], bool]) -> None:
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def test_llm_visible_text_marks_non_text_segments() -> None:
    # Pure non-text → a marker, never an empty string.
    assert llm_visible_text([ImageSegment(url="https://x/a.png")]) == "[图片]"
    # FaceSegment covers QQ basic face + mface/bface 商城表情包.
    assert llm_visible_text([FaceSegment(face_id="1")]) == "[表情]"
    # Mixed text + image keeps both sides.
    assert (
        llm_visible_text([TextSegment(text="看这个"), ImageSegment(url="https://x/a.png")])
        == "看这个[图片]"
    )
    # At/Reply are modeled elsewhere (mentions_me / at_targets / reply_to_me),
    # never inlined into the LLM-visible text.
    assert llm_visible_text([at("u2"), TextSegment(text="你好")]) == "你好"
    # Plain text passes through, stripped.
    assert llm_visible_text([TextSegment(text="  hi  ")]) == "hi"


def test_to_buffered_marks_non_text_message() -> None:
    dispatcher = GroupBatchChatDispatcher(
        inner=_Inner(""),
        config=GroupBatchConfig(enabled=True),
    )
    # A pure image used to collapse to text="" — it must now carry a marker.
    msg = dispatcher._to_buffered(
        _event("", eid="m1", segments=[ImageSegment(url="https://x/a.png")])
    )
    assert msg.text == "[图片]"


async def test_group_batch_non_text_message_appears_as_marker_in_prompt() -> None:
    provider = _ToolProvider([LLMResponse(message=Message(role="assistant", content="no_reply"))])
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0.02, require_attention=False),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(
        _event(
            "",
            eid="m1",
            sender="u1",
            name="小明",
            segments=[ImageSegment(url="https://x/a.png")],
        ),
        session,
    )

    await _wait_for(lambda: len(provider.calls) >= 1)
    system_prompt = provider.calls[0][0][0].content
    user_prompt = provider.calls[0][0][-1].content
    # The non-text message reaches the LLM as a marker, not empty text.
    # With natural language format: "小明说：[图片]"
    assert "[图片]" in user_prompt
    assert "小明" in user_prompt
    # And the system prompt teaches the model what the markers mean.
    assert "[图片]" in system_prompt
    assert "非文字内容" in system_prompt
    assert sent == []
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
            ),
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="ft1",
                            name="finish_turn",
                            arguments=json.dumps({"summary": "回复了苏苏在吗"}, ensure_ascii=False),
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

    await dispatcher.run(_event("苏苏在吗？", eid="m1"), session)

    await _wait_for(lambda: len(sent) == 1)
    assert provider.calls[0][1]
    assert "完整内容" not in provider.calls[0][0][-1].content
    assert sent[0].kind == "reply"
    assert sent[0].segments[0].message_id == "m1"
    assert inner.recorded
    assert "苏苏在吗" in inner.recorded[0][1]
    assert '"sender_id": "u1"' in inner.recorded[0][1]
    assert inner.recorded[0][2] == "可以，这条我回。"
    assert inner.ensure_calls == 0
    assert inner.ensure_keys == [("g1", "")]
    await dispatcher.stop()


async def test_group_batch_tool_prompt_orders_by_send_time_and_includes_identity() -> None:
    provider = _ToolProvider([LLMResponse(message=Message(role="assistant", content="no_reply"))])
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0.02, require_attention=False),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))
    earlier = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    later = datetime(2026, 5, 26, 12, 0, 1, tzinfo=UTC)

    await dispatcher.run(
        _event("后发先到", eid="m2", sender="u2", name="同名", at=later),
        session,
    )
    await dispatcher.run(
        _event("先发后到", eid="m1", sender="u1", name="同名", at=earlier),
        session,
    )

    await _wait_for(lambda: len(provider.calls) >= 1)
    prompt = provider.calls[0][0][-1].content
    assert "Candidate messages" in prompt
    # With natural language format, check ordering by text content
    # Earlier message "先发后到" should appear before "后发先到"
    idx_first = prompt.find("先发后到")
    idx_second = prompt.find("后发先到")
    assert idx_first >= 0 and idx_second >= 0
    assert idx_first < idx_second, "messages should be ordered by send time"
    # Both senders appear as "同名"
    assert prompt.count("同名") >= 2
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_tool_prompt_marks_member_at_as_target_not_me() -> None:
    provider = _ToolProvider([LLMResponse(message=Message(role="assistant", content="no_reply"))])
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0.05, require_attention=False),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    first = _event(" 你看这个", eid="m1", sender="u1", name="小明")
    first.bot_id = "linling"
    first.raw["self_id"] = "bot-qq"
    first.segments.insert(0, at("u2"))
    second = _event("我看到了", eid="m2", sender="u2", name="小红")
    second.bot_id = "linling"
    second.raw["self_id"] = "bot-qq"

    await dispatcher.run(first, session)
    await dispatcher.run(second, session)

    await _wait_for(lambda: len(provider.calls) >= 1)
    prompt = provider.calls[0][0][-1].content
    # Natural language format: "小明@小红说：你看这个" shows direction explicitly
    assert "小明@小红说：你看这个" in prompt
    assert "小红说：我看到了" in prompt
    # The @ direction is embedded in the line, not as separate JSON fields
    assert "at_targets" not in prompt
    assert "mentions_me" not in prompt
    assert "@u2" not in prompt
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_llbot_self_id_at_is_mentions_me_not_at_target() -> None:
    provider = _ToolProvider([LLMResponse(message=Message(role="assistant", content="no_reply"))])
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0,
            require_attention=True,
            bot_names=(),
        ),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    event = _event(" 在吗", eid="m1", sender="u1", name="小明")
    event.bot_id = "linling"
    event.raw["self_id"] = 1707476110
    event.segments.insert(0, at("1707476110"))

    await dispatcher.run(event, session)

    await _wait_for(lambda: len(provider.calls) >= 1)
    prompt = provider.calls[0][0][-1].content
    # Natural language format: @bot renders as "小明@你说：在吗"
    assert "小明@你说：在吗" in prompt
    # Raw user id should not appear in prompt
    assert "1707476110" not in prompt
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_at_all_is_attention_but_not_mentions_me() -> None:
    provider = _ToolProvider([LLMResponse(message=Message(role="assistant", content="no_reply"))])
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0,
            require_attention=True,
            bot_names=(),
        ),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    event = _event("开会了", eid="m1", sender="u1", name="小明")
    event.bot_id = "linling"
    event.segments.insert(0, at("all"))

    await dispatcher.run(event, session)

    await _wait_for(lambda: len(provider.calls) >= 1)
    prompt = provider.calls[0][0][-1].content
    # Natural language format: @all renders as "小明@全体说：开会了"
    assert "小明@全体说：开会了" in prompt
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_member_at_question_passes_at_targets_to_llm() -> None:
    provider = _ToolProvider([LLMResponse(message=Message(role="assistant", content="no_reply"))])
    inner = _AgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0,
            require_attention=True,
            bot_names=("苏苏",),
        ),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("linling", "g1", "u1"))
    event = _event(" 你怎么看？", eid="m1", sender="u1", name="小明")
    event.bot_id = "linling"
    event.raw["self_id"] = "bot-qq"
    event.segments.insert(0, at("u2"))
    second = _event("我看到了", eid="m2", sender="u2", name="小红")

    await dispatcher.run(event, session)
    await dispatcher.run(second, session)

    await _wait_for(lambda: len(provider.calls) >= 1)
    prompt = provider.calls[0][0][-1].content
    # Natural language format: @other user renders as "小明@小红说：你怎么看？"
    assert "小明@小红说：你怎么看？" in prompt
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_history_records_reply_target_for_next_prompt() -> None:
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
                        ),
                        ToolCall(
                            id="ft1",
                            name="finish_turn",
                            arguments=json.dumps({"summary": "回复了苏苏在吗"}, ensure_ascii=False),
                        ),
                    ],
                )
            ),
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="ft2",
                            name="finish_turn",
                            arguments=json.dumps({"summary": "不需要回复"}, ensure_ascii=False),
                        )
                    ],
                )
            ),
        ]
    )
    inner = _HistoryAgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("苏苏在吗？", eid="m1", sender="u1", name="小明"), session)

    await _wait_for(lambda: len(sent) == 1 and len(inner.recorded_messages) == 1)
    recorded = inner.recorded_messages[0]
    assert [message.role for message in recorded] == ["user", "assistant", "tool"]
    assert "苏苏在吗" in recorded[0].content
    assert recorded[1].tool_calls
    assert recorded[1].tool_calls[0].name == "reply_to_message"
    tool_result = json.loads(recorded[2].content)
    assert tool_result["ok"] is True
    assert tool_result["action"] == "reply_to_message"
    assert tool_result["replied_to"] == "小明"
    assert tool_result["reply"] == "可以，这条我回。"
    assert all(message.content != "回复好了" for message in recorded)
    await dispatcher.run(_event("普通后续", eid="m2", sender="u2", name="小红"), session)

    await _wait_for(lambda: len(provider.calls) >= 2)
    next_prompt = provider.calls[-1][0]
    visible = "\n".join(message.content for message in next_prompt)
    assert any(
        message.role == "assistant"
        and message.tool_calls
        and message.tool_calls[0].name == "reply_to_message"
        and '"message_id": "m1"' in message.tool_calls[0].arguments
        and "可以，这条我回。" in message.tool_calls[0].arguments
        for message in next_prompt
    )
    assert any(
        message.role == "tool"
        and message.name == "reply_to_message"
        and '"action": "reply_to_message"' in message.content
        for message in next_prompt
    )
    assert '"sender_id": "u1"' in visible
    assert '"sender_name": "小明"' in visible
    assert "普通后续" in next_prompt[-1].content
    await dispatcher.stop()


async def test_group_batch_history_preserves_at_targets_for_next_prompt() -> None:
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
                                {"message_id": "m1", "text": "别看我，看小红。"},
                                ensure_ascii=False,
                            ),
                        )
                    ],
                )
            ),
            LLMResponse(message=Message(role="assistant", content="回复好了")),
        ]
    )
    inner = _HistoryAgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))
    first = _event(" 你看这个", eid="m1", sender="u1", name="小明")
    first.segments.insert(0, at("u2"))
    second = _event("我看到了", eid="m2", sender="u2", name="小红")

    await dispatcher.run(first, session)
    await dispatcher.run(second, session)

    await _wait_for(lambda: len(sent) == 1 and len(inner.recorded_messages) == 1)
    recorded = inner.recorded_messages[0]
    assert '"at_targets": ["小红"]' in recorded[0].content
    assert '"mentions_me"' not in recorded[0].content
    assert '"reply_to_me"' not in recorded[0].content
    assert '"at_others"' not in recorded[0].content
    assert "@u2" not in recorded[0].content
    await dispatcher.stop()


async def test_group_batch_plain_text_output_sends_group_message_and_history() -> None:
    provider = _ToolProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="send1",
                            name="send_group",
                            arguments=json.dumps({"text": "在呀，苏苏听到啦"}, ensure_ascii=False),
                        ),
                        ToolCall(
                            id="ft1",
                            name="finish_turn",
                            arguments=json.dumps({"summary": "回复了在吗"}, ensure_ascii=False),
                        ),
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

    await dispatcher.run(_event("苏苏在吗？", eid="m1"), session)

    await _wait_for(lambda: len(sent) == 1)
    assert sent[0].kind == "send"
    assert sent[0].segments[0].text == "在呀，苏苏听到啦"
    assert inner.recorded
    assert "苏苏在吗" in inner.recorded[0][1]
    assert inner.recorded[0][2] == "在呀，苏苏听到啦"
    await dispatcher.stop()


async def test_group_batch_no_reply_phrase_is_not_sent_to_group() -> None:
    provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="不需要回复。"))]
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

    await dispatcher.run(_event("普通闲聊", eid="m1"), session)

    await _wait_for(lambda: len(provider.calls) >= 1)
    assert sent == []
    assert inner.recorded == []
    await dispatcher.stop()


async def test_group_batch_reply_completed_phrase_is_not_sent_after_reply() -> None:
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
            ),
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="ft1",
                            name="finish_turn",
                            arguments=json.dumps({"summary": "回复完毕"}, ensure_ascii=False),
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

    await dispatcher.run(_event("苏苏在吗？", eid="m1"), session)

    await _wait_for(lambda: len(sent) == 1)
    assert len(provider.calls) == 2
    assert sent[0].kind == "reply"
    assert sent[0].segments[0].message_id == "m1"
    assert sent[0].segments[1].text == "可以，这条我回。"
    assert inner.recorded
    assert inner.recorded[0][2] == "可以，这条我回。"
    await dispatcher.stop()


async def test_group_batch_direct_text_refreshes_attention_window() -> None:
    kv = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with kv:
        provider = _ToolProvider(
            [
                LLMResponse(
                    message=Message(
                        role="assistant",
                        content="",
                        tool_calls=[
                            ToolCall(
                                id="send1",
                                name="send_group",
                                arguments=json.dumps({"text": "在呀"}, ensure_ascii=False),
                            ),
                            ToolCall(
                                id="ft1",
                                name="finish_turn",
                                arguments=json.dumps({"summary": "回复了在吗"}, ensure_ascii=False),
                            ),
                        ],
                    )
                ),
            ]
        )
        inner = _AgentInner(provider)
        dispatcher = GroupBatchChatDispatcher(
            inner=inner,
            config=GroupBatchConfig(
                enabled=True,
                window_s=0,
                require_attention=True,
                attention_window_s=300,
                bot_names=("苏苏",),
            ),
            kv=kv,
        )
        sent: list[Action] = []
        dispatcher.set_action_sink(sent.append)
        store = ConversationStore(rate_per_second=100, burst=100)
        session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

        await dispatcher.run(_event("苏苏在吗？", eid="m1", sender="u1"), session)

        await _wait_for(lambda: len(sent) == 1)
        stamp = await kv.read("啊/g1", "苏苏确认", "u1", default="")
        assert stamp
        await dispatcher.stop()


async def test_group_batch_probe_plain_no_reply_drops_before_main_llm() -> None:
    main_provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="不该到主模型"))]
    )
    probe_provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="no_reply"))]
    )
    inner = _AgentInner(main_provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0,
            require_attention=True,
            attention_probe_enabled=True,
        ),
        probe=_ProbeWithProvider(probe_provider),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("普通闲聊", eid="m1"), session)

    await _wait_for(lambda: len(probe_provider.calls) == 1)
    await asyncio.sleep(0.05)
    assert main_provider.calls == []
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_probe_plain_done_drops_before_main_llm() -> None:
    main_provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="不该到主模型"))]
    )
    probe_provider = _ToolProvider([LLMResponse(message=Message(role="assistant", content="done"))])
    inner = _AgentInner(main_provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0,
            require_attention=True,
            attention_probe_enabled=True,
        ),
        probe=_ProbeWithProvider(probe_provider),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("普通闲聊", eid="m1"), session)

    await _wait_for(lambda: len(probe_provider.calls) == 1)
    await asyncio.sleep(0.05)
    assert main_provider.calls == []
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_probe_plain_text_allows_main_llm() -> None:
    main_provider = _ToolProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="send1",
                            name="send_group",
                            arguments=json.dumps({"text": "主模型回复"}, ensure_ascii=False),
                        ),
                        ToolCall(
                            id="ft1",
                            name="finish_turn",
                            arguments=json.dumps({"summary": "回复了"}, ensure_ascii=False),
                        ),
                    ],
                )
            ),
        ]
    )
    probe_provider = _ToolProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="probe_send",
                            name="send_group",
                            arguments=json.dumps({"text": "可以接话"}, ensure_ascii=False),
                        )
                    ],
                )
            )
        ]
    )
    inner = _AgentInner(main_provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=0,
            require_attention=True,
            attention_probe_enabled=True,
        ),
        probe=_ProbeWithProvider(probe_provider),
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("普通闲聊", eid="m1"), session)

    await _wait_for(lambda: len(sent) == 1)
    assert len(probe_provider.calls) == 1
    assert len(main_provider.calls) == 1
    assert sent[0].segments[0].text == "主模型回复"
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
    payload = json.loads(tool_messages[0].content)
    # read_batch now returns natural language lines
    message_line = payload["messages"][0]
    assert "u1" in message_line
    assert "需要看完整内容" in message_line
    await dispatcher.stop()


async def test_group_batch_read_batch_preserves_at_targets() -> None:
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
            LLMResponse(message=Message(role="assistant", content="no_reply")),
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
    first = _event(" 需要看完整内容", eid="m1", sender="u1", name="小明")
    first.segments.insert(0, at("u2"))
    second = _event("我看到了", eid="m2", sender="u2", name="小红")

    await dispatcher.run(first, session)
    await dispatcher.run(second, session)

    await _wait_for(lambda: len(provider.calls) >= 2)
    second_prompt = provider.calls[1][0]
    tool_messages = [m for m in second_prompt if m.role == "tool"]
    assert tool_messages
    payload = json.loads(tool_messages[0].content)
    # read_batch now returns natural language lines
    message_line = payload["messages"][0]
    assert "小明" in message_line
    assert "小红" in message_line
    assert "需要看完整内容" in message_line
    assert "@u2" not in tool_messages[0].content
    assert sent == []
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

    await _wait_for(lambda: len(provider.calls) >= 1)
    assert inner.recorded == []
    await dispatcher.stop()


async def test_group_batch_toolcall_summarizes_shared_group_history_without_recording_noop() -> (
    None
):
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
                max_tokens=3_000,
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
        group_session = await conversations.get_or_create(ConversationKey("bot1", "g1", ""))
        for i in range(6):
            group_session.history.append(
                Message(role="user", content=f"old user {i} " + "很长" * 20)
            )
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


async def test_group_batch_daily_summary_forces_once_per_day() -> None:
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
                max_tokens=5_000,
                summary_trigger_tokens=4_000,
                summary_keep_recent_turns=8,
                summary_max_tokens=50,
            ),
        )
        conversations = ConversationStore(rate_per_second=100, burst=100, history_turns=20)
        dispatcher = GroupBatchChatDispatcher(
            inner=inner,
            config=GroupBatchConfig(
                enabled=True,
                window_s=0,
                require_attention=False,
                daily_summary_enabled=True,
                daily_summary_keep_recent_turns=1,
            ),
            conversations=conversations,
            bot_id="bot1",
            kv=kv,
        )
        sent: list[Action] = []
        dispatcher.set_action_sink(sent.append)
        group_session = await conversations.get_or_create(ConversationKey("bot1", "g1", ""))
        for i in range(3):
            group_session.history.append(Message(role="user", content=f"old user {i}"))
            group_session.history.append(Message(role="assistant", content=f"old assistant {i}"))

        await dispatcher.run(
            _event("普通闲聊", eid="m1", at=datetime(2026, 6, 1, 12, tzinfo=UTC)),
            group_session,
        )

        await _wait_for(lambda: len(provider.calls) >= 2)
        assert sent == []
        assert await history.load_summary("g1", "") == "group compressed facts"
        assert list(group_session.history) == [
            Message(role="user", content="old user 2"),
            Message(role="assistant", content="old assistant 2"),
        ]
        assert (
            sum(
                1
                for messages, _tools in provider.calls
                if len(messages) == 1 and messages[0].content.startswith("Summarize")
            )
            == 1
        )

        await dispatcher.run(
            _event("还是普通闲聊", eid="m2", at=datetime(2026, 6, 1, 13, tzinfo=UTC)),
            group_session,
        )

        await _wait_for(lambda: len(provider.calls) >= 3)
        assert (
            sum(
                1
                for messages, _tools in provider.calls
                if len(messages) == 1 and messages[0].content.startswith("Summarize")
            )
            == 1
        )
        await dispatcher.stop()


async def test_group_batch_daily_summary_marker_waits_for_successful_compaction() -> None:
    kv = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with kv:
        history = _FailingSummaryHistoryStore(kv, max_turns=20)
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
                max_tokens=5_000,
                summary_trigger_tokens=4_000,
                summary_keep_recent_turns=8,
                summary_max_tokens=50,
            ),
        )
        conversations = ConversationStore(rate_per_second=100, burst=100, history_turns=20)
        dispatcher = GroupBatchChatDispatcher(
            inner=inner,
            config=GroupBatchConfig(
                enabled=True,
                window_s=0,
                require_attention=False,
                daily_summary_enabled=True,
                daily_summary_keep_recent_turns=1,
            ),
            conversations=conversations,
            bot_id="bot1",
            kv=kv,
        )
        group_session = await conversations.get_or_create(ConversationKey("bot1", "g1", ""))
        for i in range(3):
            group_session.history.append(Message(role="user", content=f"old user {i}"))
            group_session.history.append(Message(role="assistant", content=f"old assistant {i}"))

        await dispatcher.run(
            _event("普通闲聊", eid="m1", at=datetime(2026, 6, 1, 12, tzinfo=UTC)),
            group_session,
        )

        await _wait_for(lambda: len(provider.calls) >= 2)
        marker = await kv.read(
            "__history_daily_summary__/g1",
            "_group",
            "last_date",
            default="",
        )
        assert marker == ""
        assert any("old user 0" in message.content for message in group_session.history)
        await dispatcher.stop()


async def test_group_batch_daily_summary_marks_done_when_history_too_short() -> None:
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
                max_tokens=5_000,
                summary_trigger_tokens=4_000,
                summary_keep_recent_turns=8,
                summary_max_tokens=50,
            ),
        )
        conversations = ConversationStore(rate_per_second=100, burst=100, history_turns=20)
        dispatcher = GroupBatchChatDispatcher(
            inner=inner,
            config=GroupBatchConfig(
                enabled=True,
                window_s=0,
                require_attention=False,
                daily_summary_enabled=True,
                daily_summary_keep_recent_turns=2,
            ),
            conversations=conversations,
            bot_id="bot1",
            kv=kv,
        )
        group_session = await conversations.get_or_create(ConversationKey("bot1", "g1", ""))
        group_session.history.append(Message(role="user", content="recent user"))
        group_session.history.append(Message(role="assistant", content="recent assistant"))

        await dispatcher.run(
            _event("普通闲聊", eid="m1", at=datetime(2026, 6, 1, 12, tzinfo=UTC)),
            group_session,
        )

        await _wait_for(lambda: len(provider.calls) >= 1)
        marker = await kv.read(
            "__history_daily_summary__/g1",
            "_group",
            "last_date",
            default="",
        )
        assert marker == "2026-06-01"
        assert not any(
            len(messages) == 1 and messages[0].content.startswith("Summarize")
            for messages, _tools in provider.calls
        )
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
    inner = _Inner("")
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
    # First message has no attention — batch dropped, provider never called
    provider = inner._default_provider
    assert provider is None or not provider.calls

    await dispatcher.run(_event("苏苏在吗？", eid="m2"), session)

    # Second message triggers attention — provider called
    _ = inner.agent
    await _wait_for(lambda: len(inner._default_provider.calls) >= 1)
    await dispatcher.stop()


async def test_group_batch_system_prompt_is_separate_from_candidate_messages() -> None:
    inner = _Inner("")
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

    # Force agent creation, then check provider messages
    _ = inner.agent
    provider = inner._default_provider
    await _wait_for(lambda: len(provider.calls) >= 1)
    messages = provider.calls[0][0]
    assert messages[0].role == "system"
    assert "群" in messages[0].content
    assert "Candidate messages" in messages[-1].content
    # The actual candidate content should not leak into the system prompt
    assert "普通闲聊" not in messages[0].content
    await dispatcher.stop()


async def test_group_batch_does_not_record_history_when_sink_missing() -> None:
    inner = _Inner("")
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
    )
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))

    await dispatcher.run(_event("苏苏在吗？"), session)

    _ = inner.agent
    await _wait_for(lambda: len(inner._default_provider.calls) >= 1)
    assert inner.recorded == []
    await dispatcher.stop()


async def test_group_batch_reply_without_source_does_not_attention() -> None:
    """Reply segment with no quoted-sender metadata must NOT bypass the
    attention gate.

    Standard OneBot v11 (and LLBot by default) only carries the reply
    segment's ``message_id`` — there's no ``reply``/``source``/
    ``reply_message`` field on the inbound event. The previous default
    of "treat missing metadata as attention" caused every cross-user
    quote-reply in a busy group to be promoted to attention, draining
    the LLM budget. We now default to ``False`` so unrelated cross-user
    quote-replies stay in the dropped-batch path.
    """
    inner = _Inner('{"actions":[]}')
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
    event = _event("接着说", eid="m1")
    event.segments.insert(0, reply("quoted"))

    await dispatcher.run(event, session)

    # Wait long enough for the max_hold_s to elapse; the batch should
    # be dropped as idle (no attention) without ever reaching the
    # inner LLM dispatcher.
    await asyncio.sleep(0.2)
    assert inner.calls == []
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_recognizes_raw_self_id_mentions() -> None:
    inner = _Inner("")
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

    # Self-ID mention triggers attention — provider called
    _ = inner.agent
    await _wait_for(lambda: len(inner._default_provider.calls) >= 1)
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


# ---------------------------------------------------------------------------
# Multimodal (vision) tests
# ---------------------------------------------------------------------------


class _FakeImageResolver:
    """Resolve image refs to fixed data URIs; records calls."""

    def __init__(self, data_uris: list[str] | None = None) -> None:
        self.data_uris = list(data_uris or [])
        self.calls: list[tuple[list[str], int | None]] = []

    async def resolve_batch(self, urls, *, limit=None):
        self.calls.append((list(urls), limit))
        return list(self.data_uris)


class _VisionAgentInner(_AgentInner):
    """AgentInner whose agent_def enables vision."""

    def __init__(self, provider: object, content: str = "") -> None:
        super().__init__(provider, content=content)
        self._agent = type(
            "Agent",
            (),
            {
                "provider": provider,
                "agent_def": AgentDef(
                    name="batch-agent",
                    model="mock",
                    system="",
                    vision_enabled=True,
                ),
            },
        )()


def _image_event(
    *,
    eid: str = "m1",
    sender: str = "u1",
    name: str = "小明",
    url: str = "https://x/a.png",
) -> Event:
    return _event("", eid=eid, sender=sender, name=name, segments=[ImageSegment(url=url)])


def test_buffered_message_image_refs_vision_off() -> None:
    dispatcher = GroupBatchChatDispatcher(
        inner=_Inner(""),
        config=GroupBatchConfig(enabled=True),
    )
    msg = dispatcher._to_buffered(_image_event(url="https://x/a.png"))
    assert msg.text == "[图片]"
    assert msg.image_refs == ()


def test_buffered_message_image_refs_vision_on() -> None:
    provider = _ToolProvider([])
    dispatcher = GroupBatchChatDispatcher(
        inner=_VisionAgentInner(provider),
        config=GroupBatchConfig(enabled=True),
        image_resolver=_FakeImageResolver(["data:image/png;base64,AAAA"]),
    )
    msg = dispatcher._to_buffered(_image_event(url="https://x/a.png"))
    assert msg.image_refs == ("https://x/a.png",)


def test_group_batch_tool_schemas_vision() -> None:
    from linling_agent.group_batch import _group_batch_tool_schemas

    names_on = {s.name for s in _group_batch_tool_schemas(vision_enabled=True)}
    assert {"save_sticker", "list_stickers", "send_sticker"} <= names_on

    names_off = {s.name for s in _group_batch_tool_schemas()}
    assert not {"save_sticker", "list_stickers", "send_sticker"} & names_off


def test_sticker_tool_schemas_shape() -> None:
    from linling_agent.group_batch import _sticker_tool_schemas

    schemas = _sticker_tool_schemas()
    assert [s.name for s in schemas] == ["save_sticker", "list_stickers", "send_sticker"]
    save = schemas[0]
    assert save.parameters["required"] == ["message_id", "name"]


def test_marker_rule_vision_variant() -> None:
    # Vision off: original marker rule explaining markers are invisible.
    plain = GroupBatchChatDispatcher(
        inner=_Inner(""),
        config=GroupBatchConfig(enabled=True),
    )
    prompt_off = plain._build_tool_system_prompt([plain._to_buffered(_image_event())])
    assert "不需要回复" in prompt_off
    assert "save_sticker" not in prompt_off

    # Vision on + images actually attached: the vision variant tells the
    # model it can see the images and may collect stickers via save_sticker.
    provider = _ToolProvider([])
    vision = GroupBatchChatDispatcher(
        inner=_VisionAgentInner(provider),
        config=GroupBatchConfig(enabled=True),
        image_resolver=_FakeImageResolver(["data:image/png;base64,AAAA"]),
    )
    prompt_on = vision._build_tool_system_prompt(
        [vision._to_buffered(_image_event())], images_attached=True
    )
    assert "save_sticker" in prompt_on
    assert "不需要回复" not in prompt_on

    # Vision on but nothing actually attached (resolution failed): fall back
    # to the plain rule — claiming visibility would mislead the model.
    prompt_miss = vision._build_tool_system_prompt(
        [vision._to_buffered(_image_event())], images_attached=False
    )
    assert "不需要回复" in prompt_miss
    assert "save_sticker" not in prompt_miss


async def test_multimodal_user_message_assembly() -> None:
    """The vision-enabled tool selector sends a user message carrying
    content parts (text + resolved image_urls) to the provider."""
    resolver = _FakeImageResolver(["data:image/png;base64,AAAA"])
    provider = _ToolProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="ft1",
                            name="finish_turn",
                            arguments='{"summary":"done"}',
                        )
                    ],
                )
            )
        ]
    )
    inner = _VisionAgentInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
        image_resolver=resolver,
    )
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))
    event = _image_event()
    batch = [dispatcher._to_buffered(event)]
    # Materialise the group state so the generation/current checks pass.
    dispatcher._state_for(event)

    await dispatcher._dispatch_batch_with_tools(
        event, session, batch, generation=0, had_attention=True
    )

    assert len(provider.calls) == 1
    user_msgs = [m for m in provider.calls[0][0] if m.role == "user"]
    assert user_msgs
    user_msg = user_msgs[-1]
    assert user_msg.content_parts is not None
    assert user_msg.content_parts[0].type == "text"
    assert any(p.type == "image_url" for p in user_msg.content_parts)
    await dispatcher.stop()


class _RecordSink:
    """Capture outbound actions so ``send_sticker`` delivery is observable."""

    def __init__(self) -> None:
        self.actions: list[Action] = []

    def __call__(self, action: Action) -> None:
        self.actions.append(action)


def _sticker_dispatcher(tmp_path, *, max_replies: int = 3) -> GroupBatchChatDispatcher:
    provider = _ToolProvider([])
    return GroupBatchChatDispatcher(
        inner=_VisionAgentInner(provider),
        config=GroupBatchConfig(enabled=True, max_replies=max_replies),
        kv=SqliteKVStore(bot_id="bot1", db_path=":memory:"),
        image_resolver=_FakeImageResolver(["data:image/png;base64,AAAA"]),
        sticker_dir=tmp_path / "stickers",
    )


# A minimal valid 1x1 transparent PNG — PIL must be able to decode it for
# the collage thumbnails (the sticker-send tests only need arbitrary bytes,
# but a valid image works for both).
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


async def _seed_sticker(dispatcher: GroupBatchChatDispatcher, name: str = "dog") -> str:
    from linling_agent.sticker_store import StickerStore

    store = StickerStore(dispatcher._kv, dispatcher._sticker_dir)
    return await store.save(_PNG_BYTES, name=name)


async def test_tool_sticker_send_respects_max_replies(tmp_path) -> None:
    dispatcher = _sticker_dispatcher(tmp_path, max_replies=3)
    await _seed_sticker(dispatcher)
    sink = _RecordSink()
    dispatcher.set_action_sink(sink)
    event = _image_event()
    dispatcher._state_for(event)
    batch = [dispatcher._to_buffered(_image_event())]
    assistant = Message(role="assistant", content="")
    tool_call = ToolCall(id="t1", name="send_sticker", arguments='{"name":"dog"}')

    result, record, _read_used, terminal = await dispatcher._tool_sticker(
        "send_sticker",
        {"name": "dog"},
        tool_call,
        assistant,
        event,
        batch,
        generation=0,
        sent_count=3,
    )
    assert json.loads(result) == {"ok": False, "error": "reply limit reached"}
    assert record is None
    assert terminal is True
    assert sink.actions == []


async def test_tool_sticker_send_creates_record_and_delivers(tmp_path) -> None:
    dispatcher = _sticker_dispatcher(tmp_path, max_replies=3)
    await _seed_sticker(dispatcher)
    sink = _RecordSink()
    dispatcher.set_action_sink(sink)
    event = _image_event()
    dispatcher._state_for(event)
    batch = [dispatcher._to_buffered(_image_event())]
    assistant = Message(role="assistant", content="")
    tool_call = ToolCall(id="t1", name="send_sticker", arguments='{"name":"dog"}')

    result, record, _read_used, terminal = await dispatcher._tool_sticker(
        "send_sticker",
        {"name": "dog"},
        tool_call,
        assistant,
        event,
        batch,
        generation=0,
        sent_count=0,
    )
    assert json.loads(result) == {"ok": True, "sent": "dog"}
    assert record is not None
    assert record.assistant_output == "[表情:dog]"  # history stays text-only
    assert terminal is False
    assert len(sink.actions) == 1
    assert sink.actions[0].kind == "send"
    assert sink.actions[0].segments
    seg = sink.actions[0].segments[0]
    assert isinstance(seg, ImageSegment)
    # 原图语义(subType=1):避免 QQ 服务端把收藏的小图压缩放大。
    assert seg.extras.get("subType") == 1


async def test_tool_sticker_save_persists_image(tmp_path) -> None:
    from linling_agent.sticker_store import StickerStore

    dispatcher = _sticker_dispatcher(tmp_path)
    event = _image_event()
    dispatcher._state_for(event)
    batch = [dispatcher._to_buffered(_image_event(url="https://x/a.png"))]
    assistant = Message(role="assistant", content="")
    tool_call = ToolCall(id="t1", name="save_sticker", arguments='{"message_id":"m1","name":"cat"}')

    result, record, _read_used, _terminal = await dispatcher._tool_sticker(
        "save_sticker",
        {"message_id": "m1", "name": "cat", "tags": "funny"},
        tool_call,
        assistant,
        event,
        batch,
        generation=0,
        sent_count=0,
    )
    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["saved"] == "cat"
    store = StickerStore(dispatcher._kv, dispatcher._sticker_dir)
    meta = await store.find_by_name("cat")
    assert meta is not None
    assert meta["tags"] == "funny"
    assert record is None  # saves never produce an outbound record


async def test_tool_sticker_rejected_when_vision_off(tmp_path) -> None:
    dispatcher = GroupBatchChatDispatcher(
        inner=_Inner(""),
        config=GroupBatchConfig(enabled=True),
        kv=SqliteKVStore(bot_id="bot1", db_path=":memory:"),
        sticker_dir=tmp_path / "stickers",
    )
    event = _image_event()
    dispatcher._state_for(event)
    result, record, _read_used, _terminal = await dispatcher._execute_batch_tool(
        ToolCall(id="t1", name="send_sticker", arguments='{"name":"dog"}'),
        assistant=Message(role="assistant", content=""),
        event=event,
        batch=[],
        generation=0,
        sent_count=0,
        read_calls=0,
    )
    assert json.loads(result) == {"ok": False, "error": "unknown tool: send_sticker"}
    assert record is None


def _collage_image_parts(user_msg: Message) -> list[ContentPart]:
    """Image_url parts of the final user message (candidate + collage)."""
    assert user_msg.content_parts is not None
    return [p for p in user_msg.content_parts if p.type == "image_url"]


async def test_collage_attached_when_stickers_saved(tmp_path) -> None:
    """Vision on + saved stickers → the request carries the collage as the
    last image part, and the system prompt explains it."""
    dispatcher = _sticker_dispatcher(tmp_path)
    await _seed_sticker(dispatcher)
    inner = dispatcher._inner
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))
    event = _image_event()
    dispatcher._state_for(event)
    batch = [dispatcher._to_buffered(event)]

    await dispatcher._dispatch_batch_with_tools(
        event, session, batch, generation=0, had_attention=True
    )

    provider = inner.agent.provider
    assert len(provider.calls) == 1
    user_msgs = [m for m in provider.calls[0][0] if m.role == "user"]
    image_parts = _collage_image_parts(user_msgs[-1])
    # 1 candidate image (from the fake resolver) + 1 collage.
    assert len(image_parts) == 2
    assert image_parts[-1].image_url.startswith("data:image/jpeg;base64,")
    system_msgs = [m for m in provider.calls[0][0] if m.role == "system"]
    assert any("九宫格" in m.content for m in system_msgs)
    await dispatcher.stop()


async def test_collage_omitted_when_no_stickers(tmp_path) -> None:
    """Vision on but nothing saved → only the candidate image, no collage."""
    dispatcher = _sticker_dispatcher(tmp_path)
    inner = dispatcher._inner
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))
    event = _image_event()
    dispatcher._state_for(event)
    batch = [dispatcher._to_buffered(event)]

    await dispatcher._dispatch_batch_with_tools(
        event, session, batch, generation=0, had_attention=True
    )

    provider = inner.agent.provider
    assert len(provider.calls) == 1
    user_msgs = [m for m in provider.calls[0][0] if m.role == "user"]
    assert len(_collage_image_parts(user_msgs[-1])) == 1
    system_msgs = [m for m in provider.calls[0][0] if m.role == "system"]
    assert not any("九宫格" in m.content for m in system_msgs)
    await dispatcher.stop()


async def test_collage_omitted_when_vision_off(tmp_path) -> None:
    """Vision off → no collage even with stickers saved."""
    dispatcher = GroupBatchChatDispatcher(
        inner=_Inner(""),
        config=GroupBatchConfig(enabled=True),
        kv=SqliteKVStore(bot_id="bot1", db_path=":memory:"),
        image_resolver=_FakeImageResolver(["data:image/png;base64,AAAA"]),
        sticker_dir=tmp_path / "stickers",
    )
    await _seed_sticker(dispatcher)
    inner = dispatcher._inner
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))
    event = _image_event()
    dispatcher._state_for(event)
    batch = [dispatcher._to_buffered(event)]

    await dispatcher._dispatch_batch_with_tools(
        event, session, batch, generation=0, had_attention=True
    )

    provider = inner.agent.provider
    assert len(provider.calls) == 1
    user_msgs = [m for m in provider.calls[0][0] if m.role == "user"]
    # Vision off → no image refs collected, no collage: content_parts is None.
    assert user_msgs[-1].content_parts is None
    await dispatcher.stop()


class _RecordingPrepareInner(_VisionAgentInner):
    """Inner that records what ``prepare_context_history_with_status`` got."""

    def __init__(self, provider: object) -> None:
        super().__init__(provider)
        self.prepare_calls: list[dict[str, object]] = []

    async def prepare_context_history_with_status(
        self,
        *,
        session,
        scope_id,
        sender_id,
        prefix_messages=None,
        extra_messages=None,
        system_text="",
        current_input_text="",
        current_image_count=0,
        reserve_tokens=0,
        allow_compaction=True,
        force_compaction=False,
        summary_keep_recent_turns=None,
        commit_replacement=False,
    ) -> tuple[list, bool, int]:
        self.prepare_calls.append(
            {
                "current_image_count": current_image_count,
                "reserve_tokens": reserve_tokens,
            }
        )
        return [], False, 0


async def test_prepare_receives_image_count(tmp_path) -> None:
    """The batch path passes the real image count (candidates + collage)
    into history preparation so the token budget reserves image headroom."""
    provider = _ToolProvider([])
    inner = _RecordingPrepareInner(provider)
    dispatcher = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True),
        kv=SqliteKVStore(bot_id="bot1", db_path=":memory:"),
        image_resolver=_FakeImageResolver(["data:image/png;base64,AAAA"]),
        sticker_dir=tmp_path / "stickers",
    )
    await _seed_sticker(dispatcher)
    store = ConversationStore(rate_per_second=100, burst=100)
    session = await store.get_or_create(ConversationKey("bot1", "g1", "u1"))
    event = _image_event()
    dispatcher._state_for(event)
    batch = [dispatcher._to_buffered(event)]

    await dispatcher._dispatch_batch_with_tools(
        event, session, batch, generation=0, had_attention=True
    )

    assert inner.prepare_calls
    # 1 candidate image + 1 collage = 2.
    assert inner.prepare_calls[0]["current_image_count"] == 2
    await dispatcher.stop()
