from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime

from linling_agent.agent_def import AgentDef
from linling_agent.context import ContextBudget
from linling_agent.dispatcher import AgentChatDispatcher
from linling_agent.group_batch import (
    GroupBatchChatDispatcher,
    GroupBatchConfig,
    _llm_visible_text,
)
from linling_agent.history import KVHistoryStore
from linling_agent.llm import LLMResponse, Message, ToolCall, ToolSchema
from linling_agent.runtime import AgentResult, AgentRuntime
from linling_core.events import Action, Event, Scope, User
from linling_core.pipeline import ConversationKey, ConversationStore, Session
from linling_core.segments import (
    FaceSegment,
    ImageSegment,
    ReplySegment,
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
                "\n".join(
                    message.content for message in messages if message.role == "assistant"
                ),
            )
        )
        session.history.extend(messages)


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
    assert _llm_visible_text([ImageSegment(url="https://x/a.png")]) == "[图片]"
    # FaceSegment covers QQ basic face + mface/bface 商城表情包.
    assert _llm_visible_text([FaceSegment(face_id="1")]) == "[表情]"
    # Mixed text + image keeps both sides.
    assert (
        _llm_visible_text([TextSegment(text="看这个"), ImageSegment(url="https://x/a.png")])
        == "看这个[图片]"
    )
    # At/Reply are modeled elsewhere (mentions_me / at_targets / reply_to_me),
    # never inlined into the LLM-visible text.
    assert _llm_visible_text([at("u2"), TextSegment(text="你好")]) == "你好"
    # Plain text passes through, stripped.
    assert _llm_visible_text([TextSegment(text="  hi  ")]) == "hi"


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
    provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="no_reply"))]
    )
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

    await _wait_for(lambda: len(provider.calls) == 1)
    system_prompt = provider.calls[0][0][0].content
    user_prompt = provider.calls[0][0][-1].content
    records = [
        json.loads(line) for line in user_prompt.splitlines() if line.startswith("{")
    ]
    # The non-text message reaches the LLM as a marker, not empty text.
    assert records[0]["text"] == "[图片]"
    # And the system prompt teaches the model what the markers mean.
    assert "[图片]" in system_prompt
    assert "非文字内容" in system_prompt
    assert sent == []
    await dispatcher.stop()


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
    assert inner.recorded[0][2] == "可以，这条我回。"
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
    assert '"sender_id": "u1"' in inner.recorded[0][1]
    assert inner.recorded[0][2] == "可以，这条我回。"
    assert inner.ensure_calls == 0
    assert inner.ensure_keys == [("g1", "")]
    await dispatcher.stop()


async def test_group_batch_tool_prompt_orders_by_send_time_and_includes_identity() -> None:
    provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="no_reply"))]
    )
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

    await _wait_for(lambda: len(provider.calls) == 1)
    prompt = provider.calls[0][0][-1].content
    records = [
        json.loads(line)
        for line in prompt.splitlines()
        if line.startswith("{")
    ]
    assert "按时间升序" in prompt
    assert [record["message_id"] for record in records] == ["m1", "m2"]
    assert [record["sender_id"] for record in records] == ["u1", "u2"]
    assert [record["sender_name"] for record in records] == ["同名", "同名"]
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_tool_prompt_marks_member_at_as_target_not_me() -> None:
    provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="no_reply"))]
    )
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

    await _wait_for(lambda: len(provider.calls) == 1)
    prompt = provider.calls[0][0][-1].content
    records = [
        json.loads(line)
        for line in prompt.splitlines()
        if line.startswith("{")
    ]
    assert "at_targets=被@的其他成员" in prompt
    assert "未找你/全体时别随意插话" in prompt
    assert records[0]["message_id"] == "m1"
    assert records[0]["text"] == "你看这个"
    assert "mentions_me" not in records[0]
    assert "mentions_all" not in records[0]
    assert "reply_to_me" not in records[0]
    assert records[0]["at_targets"] == ["小红"]
    assert "at_targets" not in records[1]
    assert "@u2" not in prompt
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_llbot_self_id_at_is_mentions_me_not_at_target() -> None:
    provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="no_reply"))]
    )
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

    await _wait_for(lambda: len(provider.calls) == 1)
    prompt = provider.calls[0][0][-1].content
    records = [
        json.loads(line)
        for line in prompt.splitlines()
        if line.startswith("{")
    ]
    assert records[0]["text"] == "在吗"
    assert records[0]["mentions_me"] is True
    assert "mentions_all" not in records[0]
    assert "at_targets" not in records[0]
    assert "1707476110" not in prompt
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_at_all_is_attention_but_not_mentions_me() -> None:
    provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="no_reply"))]
    )
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

    await _wait_for(lambda: len(provider.calls) == 1)
    prompt = provider.calls[0][0][-1].content
    records = [
        json.loads(line)
        for line in prompt.splitlines()
        if line.startswith("{")
    ]
    assert "mentions_me" not in records[0]
    assert records[0]["mentions_all"] is True
    assert "at_targets" not in records[0]
    assert sent == []
    await dispatcher.stop()


async def test_group_batch_member_at_question_passes_at_targets_to_llm() -> None:
    provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="no_reply"))]
    )
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

    await _wait_for(lambda: len(provider.calls) == 1)
    prompt = provider.calls[0][0][-1].content
    records = [
        json.loads(line)
        for line in prompt.splitlines()
        if line.startswith("{")
    ]
    assert records[0]["message_id"] == "m1"
    assert records[0]["at_targets"] == ["小红"]
    assert "mentions_me" not in records[0]
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
                        )
                    ],
                )
            ),
            LLMResponse(message=Message(role="assistant", content="回复好了")),
            LLMResponse(message=Message(role="assistant", content="no_reply")),
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

    await _wait_for(lambda: len(provider.calls) == 3)
    next_prompt = provider.calls[2][0]
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
        [LLMResponse(message=Message(role="assistant", content="在呀，苏苏听到啦"))]
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
    assert "我随后在群里直接发言" in inner.recorded[0][1]
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

    await _wait_for(lambda: len(provider.calls) == 1)
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
            LLMResponse(message=Message(role="assistant", content="回复好了")),
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


async def test_group_batch_legacy_json_content_is_executed_not_sent_verbatim() -> None:
    provider = _ToolProvider(
        [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=json.dumps(
                        {"actions": [{"type": "send_group", "text": "legacy ok"}]},
                        ensure_ascii=False,
                    ),
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
    assert sent[0].kind == "send"
    assert sent[0].segments[0].text == "legacy ok"
    assert "actions" not in sent[0].segments[0].text
    await dispatcher.stop()


async def test_group_batch_direct_text_refreshes_attention_window() -> None:
    kv = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with kv:
        provider = _ToolProvider(
            [LLMResponse(message=Message(role="assistant", content="在呀"))]
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
    probe_provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="done"))]
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


async def test_group_batch_probe_plain_text_allows_main_llm() -> None:
    main_provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="主模型回复"))]
    )
    probe_provider = _ToolProvider(
        [LLMResponse(message=Message(role="assistant", content="可以接话"))]
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
    assert payload["messages"][0]["sender_id"] == "u1"
    assert payload["messages"][0]["sender_name"] == "u1"
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

    await _wait_for(lambda: len(provider.calls) == 2)
    second_prompt = provider.calls[1][0]
    tool_messages = [m for m in second_prompt if m.role == "tool"]
    assert tool_messages
    payload = json.loads(tool_messages[0].content)
    message = payload["messages"][0]
    assert message["sender_name"] == "小明"
    assert "mentions_me" not in message
    assert "mentions_all" not in message
    assert "reply_to_me" not in message
    assert message["at_targets"] == ["小红"]
    assert "at_others" not in message
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
        group_session = await conversations.get_or_create(
            ConversationKey("bot1", "g1", "")
        )
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
        assert sum(
            1
            for messages, _tools in provider.calls
            if len(messages) == 1 and messages[0].content.startswith("Summarize")
        ) == 1

        await dispatcher.run(
            _event("还是普通闲聊", eid="m2", at=datetime(2026, 6, 1, 13, tzinfo=UTC)),
            group_session,
        )

        await _wait_for(lambda: len(provider.calls) >= 3)
        assert sum(
            1
            for messages, _tools in provider.calls
            if len(messages) == 1 and messages[0].content.startswith("Summarize")
        ) == 1
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
        group_session = await conversations.get_or_create(
            ConversationKey("bot1", "g1", "")
        )
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
        group_session = await conversations.get_or_create(
            ConversationKey("bot1", "g1", "")
        )
        group_session.history.append(Message(role="user", content="recent user"))
        group_session.history.append(Message(role="assistant", content="recent assistant"))

        await dispatcher.run(
            _event("普通闲聊", eid="m1", at=datetime(2026, 6, 1, 12, tzinfo=UTC)),
            group_session,
        )

        await _wait_for(lambda: len(provider.calls) == 1)
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
    assert "群聊" in inner.messages[0][0].content
    assert "候选消息" in inner.messages[0][1].content
    assert "候选消息" not in inner.messages[0][0].content
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
