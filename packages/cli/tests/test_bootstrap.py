"""Bootstrap tests — bot.yaml → RunningBot end to end.

These prove:

* A YAML string boots a real stack (kv + bus + router + classifier +
  rules + adapter sink) with no external I/O.
* Rules parsed from inline ``.ling`` files populate the classifier and
  the DSL VM runs them against inbound events.
* A published event reaches the adapter's ``send`` with the right
  segments, for both command and (fallback) chat paths.
* Multiple adapters are dispatched by ``Scope.platform``.
* ``fallback_reply`` is served when no agent is configured.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from linling_agent.group_batch import GroupBatchChatDispatcher, GroupBatchConfig
from linling_agent.runtime import AgentResult
from linling_cli.bootstrap import RunningBot, bootstrap_bot, build_sink
from linling_core.config import BotConfig
from linling_core.events import (
    ACTION_DELAY_BEFORE_OPTION,
    Action,
    Event,
    Scope,
    User,
)
from linling_core.pipeline import ConversationKey, ConversationStore, Session
from linling_core.segments import TextSegment

# ---------------------------------------------------------------------------
# Adapter test double — records outgoing actions, implements both a
# ``platform`` attribute (for multi-adapter dispatch) and a no-op ``run``
# so :meth:`RunningBot.start` has something to await.
# ---------------------------------------------------------------------------


class _RecordingAdapter:
    def __init__(self, *, platform: str = "test") -> None:
        self.platform = platform
        self.sent: list[Action] = []
        self.sent_event = asyncio.Event()

    async def run(self) -> None:  # pragma: no cover — tests drive events directly
        return None

    async def send(self, action: Action) -> None:
        self.sent.append(action)
        self.sent_event.set()

    async def stop(self) -> None:
        return None


class _FailingAdapter(_RecordingAdapter):
    async def send(self, action: Action) -> None:
        _ = action
        raise RuntimeError("send failed")


@pytest.mark.asyncio
async def test_sink_honors_action_delay_option(monkeypatch) -> None:
    rec = _RecordingAdapter(platform="test")
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("linling_cli.bootstrap.asyncio.sleep", fake_sleep)
    sink = build_sink([rec])
    action = Action(
        kind="send",
        target=Scope(kind="group", id="g1", platform="test"),
        segments=[TextSegment(text="later")],
        options={ACTION_DELAY_BEFORE_OPTION: 2.5},
    )

    await sink(action)

    assert sleeps == [2.5]
    assert rec.sent == [action]


class _BatchSendGroupProvider:
    """Provider that emits ``send_group("batch ok")`` then ``finish_turn``.

    Mirrors the tool-based group-batch contract: the group dispatcher
    drives the inner agent's provider and processes ``send_group`` /
    ``reply_to_message`` / ``finish_turn`` pseudo-tools itself.
    """

    def __init__(self) -> None:
        self.calls: list = []

    @property
    def name(self) -> str:
        return "batch-send-group"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):  # type: ignore[no-untyped-def]
        import json as _json

        from linling_agent.agent_def import AgentDef  # noqa: F401
        from linling_agent.llm import LLMResponse, Message, TokenUsage, ToolCall

        self.calls.append(list(messages))
        return LLMResponse(
            message=Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="sg1",
                        name="send_group",
                        arguments=_json.dumps({"text": "batch ok"}),
                    ),
                    ToolCall(
                        id="ft1",
                        name="finish_turn",
                        arguments=_json.dumps({"summary": "replied"}),
                    ),
                ],
            ),
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def chat_stream(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class _BatchInner:
    def __init__(self) -> None:
        self.recorded = False
        self._provider: _BatchSendGroupProvider | None = None

    @property
    def agent(self):
        if self._provider is None:
            self._provider = _BatchSendGroupProvider()
        from linling_agent.agent_def import AgentDef

        return type(
            "Agent",
            (),
            {
                "provider": self._provider,
                "agent_def": AgentDef(name="inner-agent", model="mock", system=""),
            },
        )()

    @property
    def context_max_tokens(self) -> int:
        return 4_000

    async def ensure_history(self, event: Event, session: Session) -> None:  # type: ignore[no-untyped-def]
        _ = event, session

    async def ensure_history_key(self, session: Session, scope_id: str, sender_id: str) -> None:  # type: ignore[no-untyped-def]
        _ = session, scope_id, sender_id

    async def record_messages(
        self, *, session: Session, scope_id: str, sender_id: str, messages: list  # # type: ignore[no-untyped-def]
    ) -> None:
        _ = session, scope_id, sender_id, messages

    async def dispatch(self, event: Event, session: Session) -> AgentResult:
        _ = event, session
        return AgentResult(content="")

    async def run(self, event: Event, session: Session) -> list[Action]:
        _ = event, session
        return []

    async def record_history(
        self,
        *,
        session: Session,
        scope_id: str,
        sender_id: str,
        user_input: str,
        assistant_output: str,
    ) -> None:
        _ = session, scope_id, sender_id, user_input, assistant_output
        self.recorded = True


def _event(text: str, *, platform: str = "test", sender: str = "u1", group: str = "g1") -> Event:
    return Event(
        id=f"e-{sender}-{text}-{group}",
        platform=platform,
        bot_id="linling",
        scope=Scope(kind="group", id=group, platform=platform),
        sender=User(id=sender, platform=platform, display_name=sender),
        segments=[TextSegment(text=text)],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp: Path, rel: str, content: str) -> Path:
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


async def _boot(tmp_path: Path, yaml: str, *, extra_adapters=None) -> RunningBot:
    cfg_path = _write(tmp_path, "bot.yaml", yaml)
    cfg = BotConfig.from_yaml(cfg_path)
    return await bootstrap_bot(cfg, base_dir=tmp_path, extra_adapters=extra_adapters)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_minimal_config_parses_and_binds(tmp_path: Path):
    """An empty-ruleset bot boots without crashing and replies via fallback."""
    yaml = """\
bot_id: bot1
name: tester
storage:
  kv: ":memory:"
"""
    bot = await _boot(tmp_path, yaml)
    try:
        rec = _RecordingAdapter(platform="test")
        # Route outbound actions through the new adapter.
        bot.attach_adapter(rec)

        await bot.bus.publish(_event("hi there"))

        assert len(rec.sent) == 1
        # Default agent config → fallback reply.
        assert "don't have a chat brain" in rec.sent[0].segments[0].text
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_rules_are_loaded_and_executed(tmp_path: Path):
    _write(
        tmp_path,
        "rules/main.ling",
        "打招呼\n你好，世界\n",
    )
    yaml = """\
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
"""
    rec = _RecordingAdapter(platform="test")
    bot = await _boot(tmp_path, yaml, extra_adapters=[rec])
    try:
        await bot.bus.publish(_event("打招呼"))
        assert len(rec.sent) == 1
        assert rec.sent[0].segments[0].text == "你好，世界"
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_prefix_command_and_unknown_command(tmp_path: Path):
    _write(tmp_path, "rules/cmd.ling", "ping\npong\n")
    yaml = """\
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
classifier:
  command_prefixes: ["/"]
"""
    rec = _RecordingAdapter(platform="test")
    bot = await _boot(tmp_path, yaml, extra_adapters=[rec])
    try:
        await bot.bus.publish(_event("/ping"))
        assert rec.sent[-1].segments[0].text == "pong"

        await bot.bus.publish(_event("/bogus"))
        # Unknown slash command gets a friendly reply, does not drop into chat.
        assert "Unknown command" in rec.sent[-1].segments[0].text
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_multiple_adapters_dispatched_by_platform(tmp_path: Path):
    _write(tmp_path, "rules/main.ling", "ping\npong\n")
    yaml = """\
bot_id: bot1
storage:
  kv: ":memory:"
rules:
  - "rules/*.ling"
"""
    rec_cli = _RecordingAdapter(platform="cli")
    rec_onebot = _RecordingAdapter(platform="onebot")
    bot = await _boot(tmp_path, yaml, extra_adapters=[rec_cli, rec_onebot])
    try:
        await bot.bus.publish(_event("ping", platform="cli"))
        await bot.bus.publish(_event("ping", platform="onebot", sender="u2"))

        assert len(rec_cli.sent) == 1
        assert len(rec_onebot.sent) == 1
        assert rec_cli.sent[0].target.platform == "cli"
        assert rec_onebot.sent[0].target.platform == "onebot"
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_storage_is_isolated_by_bot_id(tmp_path: Path):
    """Two bots pointing at the same sqlite file must not see each other's writes."""
    _write(tmp_path, "rules/write.ling", "记 (.*)\n$写 s/f %QQ% %括号1%$\nok\n")
    _write(tmp_path, "rules/read.ling", "看\n玉:$读 s/f %QQ% none$\n%玉%\n")
    common = """
storage:
  kv: sqlite:///shared.db
rules:
  - "rules/*.ling"
"""
    # Two separate bots, same db file, different bot_id.
    bot_a = await _boot(tmp_path, f"bot_id: alpha\n{common}")
    bot_b = await _boot(tmp_path, f"bot_id: beta\n{common}")
    rec_a = _RecordingAdapter(platform="test")
    rec_b = _RecordingAdapter(platform="test")
    bot_a.attach_adapter(rec_a)
    bot_b.attach_adapter(rec_b)

    try:
        await bot_a.bus.publish(_event("记 hello"))
        await bot_b.bus.publish(_event("看"))
        # bot_b must not see bot_a's write.
        assert rec_b.sent[-1].segments[0].text == "none"

        await bot_b.bus.publish(_event("记 world"))
        await bot_a.bus.publish(_event("看"))
        assert rec_a.sent[-1].segments[0].text == "hello"
    finally:
        await bot_a.stop()
        await bot_b.stop()


@pytest.mark.asyncio
async def test_invalid_storage_url_raises(tmp_path: Path):
    yaml = """\
bot_id: bot1
storage:
  kv: "redis://localhost/0"
"""
    cfg_path = _write(tmp_path, "bot.yaml", yaml)
    cfg = BotConfig.from_yaml(cfg_path)
    with pytest.raises(ValueError, match=r"unsupported storage\.kv URL"):
        await bootstrap_bot(cfg, base_dir=tmp_path)


@pytest.mark.asyncio
async def test_stop_closes_kv_and_cancels_tasks(tmp_path: Path):
    """After stop, KV connection is closed and adapter tasks are cancelled."""
    yaml = """\
bot_id: bot1
storage:
  kv: ":memory:"
"""
    rec = _RecordingAdapter(platform="test")
    bot = await _boot(tmp_path, yaml, extra_adapters=[rec])
    # Touch the KV so the lazy connection is opened.
    await bot.kv.write("s", "f", "k", "v")
    await bot.start()
    assert bot.kv._conn is not None  # type: ignore[attr-defined]
    await bot.stop()
    assert bot.kv._conn is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_attach_adapter_rewires_group_batch_action_sink(tmp_path: Path):
    yaml = """\
bot_id: bot1
storage:
  kv: ":memory:"
"""
    bot = await _boot(tmp_path, yaml)
    rec = _RecordingAdapter(platform="test")
    conversations = ConversationStore(rate_per_second=100, burst=100)
    batch = GroupBatchChatDispatcher(
        inner=_BatchInner(),
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
        conversations=conversations,
        bot_id="bot1",
    )
    bot.chat_dispatcher = batch
    try:
        bot.attach_adapter(rec)
        session = await conversations.get_or_create(ConversationKey("bot1", "g1", "u1"))

        await batch.run(_event("hi"), session)
        await asyncio.wait_for(rec.sent_event.wait(), timeout=0.5)

        assert rec.sent
        assert rec.sent[0].segments[0].text == "batch ok"
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_group_batch_does_not_record_history_when_strict_sink_fails(tmp_path: Path):
    yaml = """\
bot_id: bot1
storage:
  kv: ":memory:"
"""
    failing = _FailingAdapter(platform="test")
    bot = await _boot(tmp_path, yaml, extra_adapters=[failing])
    conversations = ConversationStore(rate_per_second=100, burst=100)
    inner = _BatchInner()
    batch = GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(enabled=True, window_s=0, require_attention=False),
        conversations=conversations,
        bot_id="bot1",
    )
    bot.chat_dispatcher = batch
    bot.attach_adapter(failing)
    try:
        session = await conversations.get_or_create(ConversationKey("bot1", "g1", "u1"))

        await batch.run(_event("hi"), session)
        await asyncio.sleep(0.1)

        assert inner.recorded is False
    finally:
        await bot.stop()
