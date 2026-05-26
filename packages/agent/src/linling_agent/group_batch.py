"""Selective group-chat batching for LLM fallback messages."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any

import structlog
from linling_core.events import Action, Event, User
from linling_core.pipeline import ConversationKey, ConversationStore, Session
from linling_core.segments import ReplySegment, TextSegment

from linling_agent.attention_probe import AttentionProbe, _ProbeBatchInput
from linling_agent.context import fit_messages_to_budget
from linling_agent.llm import Message, ToolCall, ToolSchema

logger = structlog.get_logger(__name__)

_TOOL_READ_BATCH = "read_batch_messages"
_TOOL_REPLY_TO_MESSAGE = "reply_to_message"
_TOOL_SEND_GROUP = "send_group"
_MAX_TOOL_ROUNDS = 8
_MAX_READ_CALLS = 2
_MAX_READ_MESSAGES = 5
_BATCH_PREVIEW_CHARS = 80
_BATCH_PROMPT_TEXT_CHARS = 500
_BATCH_HISTORY_TEXT_CHARS = 500
_MAX_TOOL_RESULT_CHARS = 2_500


_ATTENTION_KV_SCOPE_PREFIX = "啊/"
_ATTENTION_KV_FILE = "苏苏确认"
# Threshold separating HHmm (0..2359) from unix epoch seconds.
# HHmm fits in 4 digits; any value above this is treated as epoch.
_HHMM_MAX = 9999


def _parse_attention_stamp(
    raw: str, now_epoch: float, now_hhmm_minutes: int, window_s: float
) -> bool:
    """Decide whether a stored "苏苏确认" stamp is still within the window.

    Accepts two formats so the LLM and DSL paths can share KV state:

    * Unix epoch seconds (what we write) — direct ``now - stamp <= window``.
    * HHmm 0..2359 (what main.ling writes) — converted to minutes-of-day
      and compared via the shorter wraparound distance, so a stamp at
      23:58 still matches "now" at 00:03 (5 minutes apart, not 1435).

    Empty / non-numeric / explicit ``"0"`` (the DSL "never written"
    default) all return False.
    """
    if not raw or raw == "0":
        return False
    try:
        stamped = float(raw)
    except (TypeError, ValueError):
        return False
    if stamped <= 0:
        return False
    if stamped > _HHMM_MAX:
        return (now_epoch - stamped) <= window_s
    # HHmm fallback. Convert both sides to minutes-of-day and pick the
    # shorter wraparound distance so cross-hour and cross-midnight gaps
    # are measured correctly. main.ling's check is sloppy here; we
    # tighten it without changing the user-visible window length.
    try:
        hhmm = int(stamped)
    except (TypeError, ValueError):
        return False
    if not (0 <= hhmm <= 2359):
        return False
    stamped_minutes = (hhmm // 100) * 60 + (hhmm % 100)
    diff = abs(now_hhmm_minutes - stamped_minutes)
    diff = min(diff, 1440 - diff)
    window_minutes = max(1, int(window_s // 60))
    return diff <= window_minutes


_NO_REPLY_TOKENS: frozenset[str] = frozenset({
    "no_reply",
    "no-reply",
    "noreply",
    "skip",
    "none",
    "null",
    "不回",
    "不回复",
    "不用回",
    "不用回复",
    "不需要回",
    "不需要回复",
    "无需回",
    "无需回复",
    "不用说话",
    "不说话",
    "保持沉默",
})

_DONE_TOKENS: frozenset[str] = frozenset({
    "done",
    "reply_done",
    "reply-done",
    "reply done",
    "回复好了",
    "回复完成",
    "回复完了",
    "已回复",
    "已完成",
    "已经回复好了",
    "已经回复完成",
})


def _normalize_control_text(content: str) -> str:
    if not content:
        return ""
    return content.strip().strip("\"'`“”‘’").strip().lower()


def _classify_stop_token(content: str) -> str | None:
    normalized = _normalize_control_text(content)
    if not normalized:
        return None
    if normalized in {"[]", "{}", "{\"actions\":[]}"}:
        return "no_reply"
    if normalized in _NO_REPLY_TOKENS:
        return "no_reply"
    first = normalized.split(maxsplit=1)[0].strip(",.!?，。！？:：;；")
    if first in _NO_REPLY_TOKENS:
        return "no_reply"
    if any(
        normalized.startswith(prefix)
        for prefix in (
            "no reply",
            "do not reply",
            "don't reply",
            "不需要回复",
            "不用回复",
            "无需回复",
            "不回复",
            "保持沉默",
        )
    ):
        return "no_reply"
    if normalized in _DONE_TOKENS:
        return "done"
    if first in _DONE_TOKENS:
        return "done"
    if any(
        normalized.startswith(prefix)
        for prefix in (
            "reply done",
            "already replied",
            "已经回复好了",
            "已经回复完成",
            "回复好了",
            "回复完成",
            "回复完了",
        )
    ):
        return "done"
    return None


def _is_no_reply_token(content: str) -> bool:
    """Detect whether the model's output is the 'don't reply' sentinel."""
    return _classify_stop_token(content) == "no_reply"


def _is_done_token(content: str) -> bool:
    """Detect whether the model's output is the 'reply completed' sentinel."""
    return _classify_stop_token(content) == "done"


def _is_stop_token(content: str) -> bool:
    return _classify_stop_token(content) is not None


@dataclass(frozen=True)
class GroupBatchConfig:
    enabled: bool = False
    window_s: float = 8.0
    max_messages: int = 20
    max_chars: int = 6_000
    max_replies: int = 3
    max_reply_chars: int = 500
    require_attention: bool = True
    max_hold_s: float = 30.0
    bot_names: tuple[str, ...] = ()
    # Second-stage probe gate. ``False`` keeps today's behaviour exactly
    # — the eligibility predicate in :meth:`_flush_loop` short-circuits
    # before any probe call regardless of whether a probe instance was
    # injected. The bootstrap flips this to ``True`` only when it has
    # also constructed an :class:`AttentionProbe`, so the
    # ``(attention_probe_enabled=True, probe=None)`` combination never
    # occurs in practice; tests that omit both stay on the legacy path.
    attention_probe_enabled: bool = False
    # Sliding "苏苏确认" attention window in seconds. When the bot
    # successfully replies to a user, we stamp ``(group, user) -> now``
    # in the KV store. Any further message from that user within
    # ``attention_window_s`` is treated as if the rule-based
    # attention gate fired — the bot is still "listening" to them.
    # Mirrors the ``啊/{group}/苏苏确认`` semantics of main.ling
    # (which uses HHmm + 5min hardcoded).
    attention_window_s: float = 300.0

    def __post_init__(self) -> None:
        if self.window_s < 0:
            raise ValueError("window_s must be non-negative")
        if self.max_messages <= 0:
            raise ValueError("max_messages must be positive")
        if self.max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if self.max_replies < 0:
            raise ValueError("max_replies must be non-negative")
        if self.max_reply_chars <= 0:
            raise ValueError("max_reply_chars must be positive")
        if self.max_hold_s <= 0:
            raise ValueError("max_hold_s must be positive")
        if self.attention_window_s < 0:
            raise ValueError("attention_window_s must be non-negative")


@dataclass(frozen=True)
class _BufferedMessage:
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: str
    sent_at: float
    received_seq: int
    mentions_bot: bool
    reply_to_bot: bool


@dataclass(frozen=True)
class _ToolSendRecord:
    messages: list[Message]
    user_input: str
    assistant_output: str


@dataclass(frozen=True)
class _ToolSelection:
    records: list[_ToolSendRecord]


@dataclass
class _GroupState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    messages: deque[_BufferedMessage] = field(default_factory=deque)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    flush_task: asyncio.Task[None] | None = None
    first_seen_at: float | None = None
    attention_seen: bool = False
    template_event: Event | None = None
    last_session: Session | None = None
    generation: int = 0


class GroupBatchChatDispatcher:
    """Batch group chat fallback before the agent sees it.

    This wrapper does not block the caller while waiting for the batch
    window. It enqueues messages, starts a background flush task for the
    group, and returns immediately. The task later calls the inner chat
    dispatcher with a synthetic batch prompt.
    """

    def __init__(
        self,
        *,
        inner: Any,
        config: GroupBatchConfig,
        conversations: ConversationStore | None = None,
        bot_id: str = "linling",
        probe: AttentionProbe | None = None,
        kv: Any = None,
    ) -> None:
        self._inner = inner
        self._cfg = config
        self._conversations = conversations
        self._bot_id = bot_id
        # Optional KV store for the "苏苏确认" sliding-attention window.
        # When wired, every successful ``reply_to_message`` stamps
        # ``啊/{group}/苏苏确认`` with the target user's monotonic
        # send timestamp, and incoming messages from that user
        # within ``attention_window_s`` get a free attention pass.
        # Shares the same KV namespace as main.ling's hand-written
        # rules so the DSL and LLM paths see one another's stamps.
        self._kv: Any = kv
        # Optional second-stage attention probe. Wired up by the
        # bootstrap when both the YAML toggle and a usable API key
        # resolve; left as ``None`` by every existing test in
        # ``test_group_batch.py`` so the new code path is dead weight
        # for the legacy harness. The eligibility predicate in
        # :meth:`_flush_loop` short-circuits on either ``self._probe
        # is None`` or ``self._cfg.attention_probe_enabled is False``,
        # so the impossible ``(probe=instance, enabled=False)``
        # combination still does nothing — bootstrap flips both
        # together.
        self._probe: AttentionProbe | None = probe
        self._states: defaultdict[str, _GroupState] = defaultdict(_GroupState)
        self._dispatch_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._action_sink: Any = None
        self._message_seq = count()
        self._closed = False

    @property
    def agent(self) -> Any:
        return getattr(self._inner, "agent", None)

    def set_action_sink(self, sink: Any) -> None:
        self._action_sink = sink

    async def _is_within_attention_window(
        self, scope_id: str, sender_id: str
    ) -> bool:
        """Return True iff ``sender_id`` was recently replied to by the bot.

        Reads ``啊/{scope_id}/苏苏确认`` and delegates the format check
        to :func:`_parse_attention_stamp` (which accepts both the epoch
        seconds we write and the HHmm form main.ling uses, so the two
        paths share state).
        """
        if (
            self._kv is None
            or not sender_id
            or self._cfg.attention_window_s <= 0
        ):
            return False
        try:
            raw = await self._kv.read(
                f"{_ATTENTION_KV_SCOPE_PREFIX}{scope_id}",
                _ATTENTION_KV_FILE,
                sender_id,
                default=None,
            )
        except Exception:
            logger.debug(
                "group_batch.attention_window.kv_read_failed",
                scope_id=scope_id,
                sender_id=sender_id,
                exc_info=True,
            )
            return False
        if not raw:
            return False
        now = datetime.now()
        return _parse_attention_stamp(
            raw,
            now_epoch=time.time(),
            now_hhmm_minutes=now.hour * 60 + now.minute,
            window_s=self._cfg.attention_window_s,
        )

    async def _refresh_attention_window(
        self, scope_id: str, sender_id: str
    ) -> None:
        """Stamp ``(scope, sender) -> now`` after a successful bot reply."""
        if (
            self._kv is None
            or not sender_id
            or self._cfg.attention_window_s <= 0
        ):
            return
        try:
            await self._kv.write(
                f"{_ATTENTION_KV_SCOPE_PREFIX}{scope_id}",
                _ATTENTION_KV_FILE,
                sender_id,
                str(int(time.time())),
            )
        except Exception:
            logger.debug(
                "group_batch.attention_window.kv_write_failed",
                scope_id=scope_id,
                sender_id=sender_id,
                exc_info=True,
            )

    async def stop(self) -> None:
        self._closed = True
        tasks: list[asyncio.Task[None]] = []
        for state in self._states.values():
            async with state.lock:
                if state.flush_task is not None:
                    tasks.append(state.flush_task)
                    state.flush_task = None
                state.wakeup.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Close the attention probe's httpx client *after* cancelling
        # the flush tasks so any in-flight ``judge`` call sees
        # :class:`asyncio.CancelledError` first. ``aclose`` itself is
        # idempotent (httpx tolerates re-close) so duplicate calls
        # in nested shutdown paths are safe.
        if self._probe is not None:
            try:
                await self._probe.aclose()
            except Exception:
                logger.exception("group_batch.attention_probe.aclose_failed")

    async def run(self, event: Event, session: Session) -> list[Action]:
        if not self._cfg.enabled or event.scope.kind != "group":
            actions: list[Action] = await self._inner.run(event, session)
            return actions

        state = self._state_for(event)
        msg = self._to_buffered(event)
        # Check the sliding "苏苏确认" attention window BEFORE acquiring
        # the state lock so the KV read doesn't block message ingestion.
        # If this user is in the window, treat the batch as already
        # rule-attended — same as if `_is_attention_candidate` had fired.
        within_window = await self._is_within_attention_window(
            event.scope.id, event.sender.id
        )
        async with state.lock:
            state.messages.append(msg)
            self._trim_locked(state)
            state.last_session = session
            state.template_event = event
            now = time.monotonic()
            if state.first_seen_at is None:
                state.first_seen_at = now
            if within_window or self._is_attention_candidate(msg):
                state.attention_seen = True
            state.wakeup.set()
            if state.flush_task is None:
                state.flush_task = asyncio.create_task(
                    self._flush_loop(self._state_key(event)),
                    name=f"group-batch:{self._state_key(event)}",
                )
        return []

    async def dispatch(self, event: Event, session: Session) -> Any:
        inner_dispatch = getattr(self._inner, "dispatch", None)
        if inner_dispatch is None:
            return None
        return await inner_dispatch(event, session)

    async def clear_history(self, scope_id: str, sender_id: str) -> None:
        state = self._states.get(self._state_key(scope_id))
        if state is not None:
            async with state.lock:
                state.generation += 1
                self._reset_state_locked(state, cancel_task=True)
        inner_clear = getattr(self._inner, "clear_history", None)
        if inner_clear is not None:
            await inner_clear(scope_id, sender_id)
            if sender_id and self._conversations is None:
                await inner_clear(scope_id, "")
        if self._conversations is not None:
            group_session = await self._conversations.get_or_create(
                ConversationKey(bot_id=self._bot_id, scope_id=scope_id, sender_id="")
            )
            async with group_session.lock:
                group_session.history.clear()
                if sender_id and inner_clear is not None:
                    await inner_clear(scope_id, "")
            await self._conversations.drop(group_session.key)

    def _state_for(self, event: Event) -> _GroupState:
        return self._states[self._state_key(event)]

    async def clear_ledger(self, scope_id: str, file_id: str) -> None:
        inner_clear = getattr(self._inner, "clear_ledger", None)
        if inner_clear is not None:
            await inner_clear(scope_id, file_id)

    async def _flush_loop(self, key: str) -> None:
        state = self._states[key]
        try:
            while not self._closed:
                async with state.lock:
                    if not state.messages:
                        self._reset_state_locked(state, cancel_task=False)
                        return
                    elapsed = 0.0 if state.first_seen_at is None else time.monotonic() - state.first_seen_at
                    has_attention = state.attention_seen
                    # When probe is wired, window_s expiry is enough to
                    # flush — the probe now runs inside _dispatch_batch_with_tools
                    # with full context, so we don't need a separate gate here.
                    may_consult_llm = (
                        has_attention
                        or not self._cfg.require_attention
                        or (self._probe_wired() and elapsed >= self._cfg.window_s)
                    )
                    count_ready = len(state.messages) >= self._cfg.max_messages
                    window_ready = elapsed >= self._cfg.window_s
                    hold_ready = elapsed >= self._cfg.max_hold_s
                    flush_ready = may_consult_llm and (count_ready or window_ready or hold_ready)
                    drop_ready = hold_ready and not may_consult_llm
                    if flush_ready:
                        batch = list(state.messages)
                        template_event = state.template_event
                        session = state.last_session
                        generation = state.generation
                        had_attention = state.attention_seen
                        self._reset_state_locked(state, cancel_task=False)
                    elif drop_ready:
                        dropped = len(state.messages)
                        self._reset_state_locked(state, cancel_task=False)
                        logger.debug("group_batch.dropped_idle_batch", key=key, messages=dropped)
                        return
                    else:
                        wait_for = self._next_wait_s(
                            elapsed=elapsed,
                            has_attention=has_attention,
                            probe_pending=self._probe_wired(),
                        )
                        state.wakeup.clear()
                        batch = []
                        template_event = None
                        session = None
                        generation = state.generation
                        had_attention = False
                if not batch or template_event is None or session is None:
                    try:
                        await asyncio.wait_for(state.wakeup.wait(), timeout=wait_for)
                    except TimeoutError:
                        continue
                    continue
                try:
                    await self._dispatch_batch(template_event, session, batch, generation, had_attention)
                except Exception:
                    logger.exception("group_batch.flush_failed", key=key)
                return
        finally:
            async with state.lock:
                if state.flush_task is asyncio.current_task():
                    state.flush_task = None

    def _next_wait_s(
        self,
        *,
        elapsed: float,
        has_attention: bool,
        probe_pending: bool = False,
    ) -> float:
        deadlines: list[float] = []
        if has_attention or not self._cfg.require_attention or probe_pending:
            # ``probe_pending`` is the new third reason to wake at the
            # ``window_s`` boundary: when a probe is configured but
            # hasn't run yet, we must not sleep past ``window_s`` —
            # otherwise the eligibility predicate in
            # :meth:`_flush_loop` only re-evaluates at
            # ``max_hold_s``, making the probe no different from the
            # legacy drop path.
            deadlines.append(self._cfg.window_s - elapsed)
        deadlines.append(self._cfg.max_hold_s - elapsed)
        positive = [d for d in deadlines if d > 0]
        return max(0.05, min(positive) if positive else 0.05)

    async def _dispatch_batch(
        self,
        event: Event,
        session: Session,
        batch: list[_BufferedMessage],
        generation: int,
        had_attention: bool = False,
    ) -> None:
        if self._closed:
            return
        async with self._dispatch_locks[self._state_key(event)]:
            if await self._batch_is_current(event, generation):
                await self._dispatch_batch_locked(event, session, batch, generation, had_attention)

    async def _dispatch_batch_locked(
        self,
        event: Event,
        session: Session,
        batch: list[_BufferedMessage],
        generation: int,
        had_attention: bool = False,
    ) -> None:
        batch_session = await self._batch_session(event, session)
        tool_selection = await self._dispatch_batch_with_tools(
            event,
            batch_session,
            batch,
            generation,
            had_attention,
        )
        if tool_selection is not None:
            if tool_selection.records and await self._batch_is_current(event, generation):
                await self._record_tool_history(batch_session, event, tool_selection.records)
            return

        inner_dispatch = getattr(self._inner, "dispatch", None)
        if inner_dispatch is None:
            return
        batch_event = event.model_copy(
            update={
                "id": f"group-batch:{event.bot_id}:{event.scope.id}:{batch[-1].message_id}",
                "sender": User(id="", platform=event.platform, display_name="_group"),
                "segments": [TextSegment(text=self._build_prompt(batch))],
                "raw": {
                    **event.raw,
                    "_linling_group_batch": True,
                    "_linling_group_batch_ids": [m.message_id for m in batch],
                    "_linling_skip_history": True,
                    "_linling_disable_tools": True,
                    "_linling_prompt_system": self._build_system_prompt(),
                },
            }
        )
        async with batch_session.lock:
            result = await inner_dispatch(batch_event, batch_session)
        if result is None:
            return
        if not await self._batch_is_current(event, generation):
            return
        records = await self._handle_plain_assistant_content(
            result.content,
            event=event,
            batch=batch,
            generation=generation,
            sent_count=0,
        )
        if not records:
            return
        if await self._batch_is_current(event, generation):
            await self._record_tool_history(batch_session, event, records)

    async def _dispatch_batch_with_tools(
        self,
        event: Event,
        session: Session,
        batch: list[_BufferedMessage],
        generation: int,
        had_attention: bool = False,
    ) -> _ToolSelection | None:
        agent = self.agent
        provider = getattr(agent, "provider", None)
        agent_def = getattr(agent, "agent_def", None)
        if provider is None or agent_def is None:
            if (
                not had_attention
                and self._probe_wired()
                and self._probe is not None
                and getattr(self._probe, "judge", None) is not None
            ):
                try:
                    probe_has_action = await self._legacy_probe_has_action(event, batch)
                    logger.info(
                        "group_batch.attention_probe.judged",
                        scope_id=event.scope.id,
                        batch_size=len(batch),
                        verdict=probe_has_action,
                        model=getattr(self._probe, "model", "(legacy)"),
                    )
                    if not probe_has_action or not await self._batch_is_current(event, generation):
                        return _ToolSelection(records=[])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "group_batch.attention_probe.unexpected_failure",
                        scope_id=event.scope.id,
                        exc_info=True,
                    )
                    return _ToolSelection(records=[])
            return None
        await self._ensure_history(session, event)
        tools = _group_batch_tool_schemas()
        selector_prompt = self._build_tool_system_prompt()
        agent_system = str(getattr(agent_def, "system", "") or "")
        if agent_system:
            selector_prompt = f"{agent_system}\n\n{selector_prompt}"
        user_prompt = self._build_tool_prompt(batch)
        guardrails = getattr(agent_def, "guardrails", None)
        max_tokens = getattr(guardrails, "max_tokens", None)
        temperature = min(getattr(agent_def, "temperature", 0.7), 0.3)
        history = await self._prepare_context_history(
            session,
            event,
            prefix_messages=[Message(role="system", content=selector_prompt)],
            current_input_text=user_prompt,
            reserve_tokens=max_tokens or 0,
        )
        messages = [
            Message(role="system", content=selector_prompt),
            *history,
            Message(role="user", content=user_prompt),
        ]
        messages = fit_messages_to_budget(messages, self._context_max_tokens())
        # --- Pre-flight: probe runs only when the rule-based attention
        # gate did NOT fire. If the rule already said "respond" (e.g.
        # mentions_bot, question particle, bot_name in text), skip the
        # probe entirely — re-judging with a small model that lacks
        # full context can incorrectly veto an obvious reply.
        #
        # The probe sees EXACTLY the same messages the main LLM will see
        # — same system prompt, same history, same batch, same tools,
        # same max_tokens. Think of it as "what would the agent do if
        # we swapped the model to Groq?". If the small model decides
        # not to act (no tool_calls), we trust that and skip the main
        # LLM call entirely. If the probe fails (TPM cap, network),
        # fail-open to the main LLM. ---
        if (
            not had_attention
            and self._probe_wired()
            and self._probe is not None
        ):
            try:
                probe_has_action = await self._probe_has_action(
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    event=event,
                    batch=batch,
                )
                logger.info(
                    "group_batch.attention_probe.judged",
                    scope_id=event.scope.id,
                    batch_size=len(batch),
                    verdict=probe_has_action,
                    model=self._probe.model,
                )
                if not probe_has_action:
                    return _ToolSelection(records=[])
            except asyncio.CancelledError:
                raise
            except Exception:
                # Probe failure on a non-attention batch fails closed:
                # direct mentions/questions already bypass the probe via
                # had_attention, while idle chatter should not become
                # chatty just because the cheap gate is unhealthy.
                logger.warning(
                    "group_batch.attention_probe.failed",
                    scope_id=event.scope.id,
                    category="preflight_error",
                    exc_info=True,
                )
                return _ToolSelection(records=[])
        records: list[_ToolSendRecord] = []
        read_calls = 0
        total_tokens = 0
        logger.info(
            "group_batch.tool_selector_start",
            scope_id=event.scope.id,
            batch_size=len(batch),
            had_attention=had_attention,
            history_msgs=len(history),
        )
        for _ in range(_MAX_TOOL_ROUNDS):
            if not await self._batch_is_current(event, generation):
                return _ToolSelection(records=records)
            try:
                response = await provider.chat(
                    messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception:
                if records:
                    logger.exception("group_batch.tool_selector_failed_after_send")
                    return _ToolSelection(records=records)
                logger.warning("group_batch.tool_selector_unavailable", exc_info=True)
                return None
            if response.usage is not None:
                total_tokens += response.usage.total_tokens
            assistant = response.message
            if not await self._batch_is_current(event, generation):
                return _ToolSelection(records=records)
            if not assistant.tool_calls:
                # No tool calls — interpret the plain content as either
                # an explicit "no_reply" decision, a legacy JSON action
                # payload, or a direct group send.
                new_records = await self._handle_plain_assistant_content(
                    assistant.content,
                    event=event,
                    batch=batch,
                    generation=generation,
                    sent_count=len(records),
                )
                if not new_records:
                    stop_kind = _classify_stop_token(assistant.content or "")
                    logger.info(
                        "group_batch.tool_selector_no_reply",
                        scope_id=event.scope.id,
                        had_attention=had_attention,
                        actions_so_far=len(records),
                        stop_kind=stop_kind or "empty",
                    )
                    return _ToolSelection(records=records)
                records.extend(new_records)
                logger.info(
                    "group_batch.tool_selector_text_send",
                    scope_id=event.scope.id,
                    had_attention=had_attention,
                    text_preview="\n".join(r.assistant_output for r in new_records)[:200],
                )
                return _ToolSelection(records=records)
            messages.append(assistant)
            terminal = False
            for tool_call in assistant.tool_calls:
                tool_result, record, read_used, terminal = await self._execute_batch_tool(
                    tool_call,
                    assistant=assistant,
                    event=event,
                    batch=batch,
                    generation=generation,
                    sent_count=len(records),
                    read_calls=read_calls,
                )
                if read_used:
                    read_calls += 1
                if record is not None:
                    records.append(record)
                messages.append(
                    Message(
                        role="tool",
                        content=tool_result,
                        name=tool_call.name,
                        tool_call_id=tool_call.id,
                    )
                )
                if len(records) >= self._cfg.max_replies:
                    break
                if terminal:
                    break
            messages = fit_messages_to_budget(messages, self._context_max_tokens())
            if terminal or len(records) >= self._cfg.max_replies:
                break
        logger.info(
            "group_batch.tool_selector_done",
            scope_id=event.scope.id,
            actions=len(records),
            read_calls=read_calls,
            total_tokens=total_tokens,
            had_attention=had_attention,
        )
        return _ToolSelection(records=records)

    async def _probe_has_action(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        event: Event,
        batch: list[_BufferedMessage],
    ) -> bool:
        assert self._probe is not None
        probe_provider = getattr(self._probe, "provider", None)
        if probe_provider is not None:
            response = await probe_provider.chat(
                messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return self._assistant_message_has_action(response.message, event, batch)

        # Compatibility for older local tests / injected probes. Real
        # bootstrap-created probes always expose ``provider`` and use
        # the full-context path above.
        judge = getattr(self._probe, "judge", None)
        if judge is not None:
            return await self._legacy_probe_has_action(event, batch)

        raise RuntimeError("attention probe has neither provider nor judge")

    async def _legacy_probe_has_action(
        self,
        event: Event,
        batch: list[_BufferedMessage],
    ) -> bool:
        assert self._probe is not None
        judge = self._probe.judge
        probe_batch = [
            _ProbeBatchInput(
                message_id=msg.message_id,
                sender_name=msg.sender_name,
                timestamp=msg.timestamp,
                text=msg.text,
            )
            for msg in batch
        ]
        return bool(await judge(probe_batch, scope_id=event.scope.id))

    def _assistant_message_has_action(
        self,
        message: Message,
        event: Event,
        batch: list[_BufferedMessage],
    ) -> bool:
        tool_calls = message.tool_calls or []
        if any(
            tc.name in {_TOOL_READ_BATCH, _TOOL_REPLY_TO_MESSAGE, _TOOL_SEND_GROUP}
            for tc in tool_calls
        ):
            return True
        content = (message.content or "").strip()
        if not content or _is_stop_token(content):
            return False
        legacy_actions = self._legacy_actions_from_content(content, event, batch)
        if legacy_actions is not None:
            return bool(legacy_actions)
        return True

    def _legacy_actions_from_content(
        self,
        content: str,
        event: Event,
        batch: list[_BufferedMessage],
    ) -> list[Action] | None:
        stripped = content.strip()
        if not stripped:
            return []
        candidate = stripped
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
            candidate = re.sub(r"\s*```$", "", candidate).strip()
        if not candidate.startswith("{") or "\"actions\"" not in candidate:
            return None
        payload = _parse_json_object(candidate)
        if not isinstance(payload, dict) or not isinstance(payload.get("actions"), list):
            return None
        return self._actions_from_result(candidate, event, batch)

    async def _handle_plain_assistant_content(
        self,
        content: str,
        *,
        event: Event,
        batch: list[_BufferedMessage],
        generation: int,
        sent_count: int,
    ) -> list[_ToolSendRecord]:
        text = (content or "").strip()
        if not text or _is_stop_token(text):
            return []
        legacy_actions = self._legacy_actions_from_content(text, event, batch)
        if legacy_actions is not None:
            return await self._send_actions_and_records(
                legacy_actions,
                event=event,
                batch=batch,
                generation=generation,
                sent_count=sent_count,
            )
        if sent_count >= self._cfg.max_replies:
            return []
        action = Action(
            kind="send",
            target=event.scope,
            segments=[TextSegment(text=text[: self._cfg.max_reply_chars])],
        )
        return await self._send_actions_and_records(
            [action],
            event=event,
            batch=batch,
            generation=generation,
            sent_count=sent_count,
        )

    async def _send_actions_and_records(
        self,
        actions: list[Action],
        *,
        event: Event,
        batch: list[_BufferedMessage],
        generation: int,
        sent_count: int,
    ) -> list[_ToolSendRecord]:
        records: list[_ToolSendRecord] = []
        remaining = max(0, self._cfg.max_replies - sent_count)
        for action in actions[:remaining]:
            if not await self._batch_is_current(event, generation):
                break
            sent, _error = await self._send_action(action, event)
            if sent and await self._batch_is_current(event, generation):
                await self._refresh_attention_for_action(action, event, batch)
                records.append(_record_from_action(action, batch))
        return records

    async def _refresh_attention_for_action(
        self,
        action: Action,
        event: Event,
        batch: list[_BufferedMessage],
    ) -> None:
        if action.kind == "reply":
            reply_id = next(
                (
                    getattr(seg, "message_id", "")
                    for seg in action.segments
                    if getattr(seg, "kind", "") == "reply"
                ),
                "",
            )
            msg = _message_by_id(batch).get(reply_id)
            if msg is not None:
                await self._refresh_attention_window(event.scope.id, msg.sender_id)
            return
        if action.kind == "send":
            await self._refresh_attention_for_group_send(event, batch)

    async def _refresh_attention_for_group_send(
        self,
        event: Event,
        batch: list[_BufferedMessage],
    ) -> None:
        unique_sender_ids = _ordered_unique(
            msg.sender_id for msg in batch if msg.sender_id
        )
        candidate_sender_ids = _ordered_unique(
            msg.sender_id
            for msg in batch
            if msg.sender_id and self._is_attention_candidate(msg)
        )
        targets = candidate_sender_ids
        if not targets and len(unique_sender_ids) == 1:
            targets = unique_sender_ids
        for sender_id in targets:
            await self._refresh_attention_window(event.scope.id, sender_id)

    async def _execute_batch_tool(
        self,
        tool_call: ToolCall,
        *,
        assistant: Message,
        event: Event,
        batch: list[_BufferedMessage],
        generation: int,
        sent_count: int,
        read_calls: int,
    ) -> tuple[str, _ToolSendRecord | None, bool, bool]:
        args = _parse_tool_args(tool_call.arguments)
        if args is None:
            return _tool_json({"ok": False, "error": "invalid JSON arguments"}), None, False, False
        if tool_call.name == _TOOL_READ_BATCH:
            if read_calls >= _MAX_READ_CALLS:
                return _tool_json({"ok": False, "error": "read limit exceeded"}), None, False, False
            return self._tool_read_batch(args, batch), None, True, False
        if tool_call.name == _TOOL_REPLY_TO_MESSAGE:
            return await self._tool_reply_to_message(
                args,
                tool_call,
                assistant,
                event,
                batch,
                generation,
                sent_count,
            )
        if tool_call.name == _TOOL_SEND_GROUP:
            return await self._tool_send_group(
                args,
                tool_call,
                assistant,
                event,
                batch,
                generation,
                sent_count,
            )
        return _tool_json({"ok": False, "error": f"unknown tool: {tool_call.name}"}), None, False, False

    async def _tool_reply_to_message(
        self,
        args: dict[str, object],
        tool_call: ToolCall,
        assistant: Message,
        event: Event,
        batch: list[_BufferedMessage],
        generation: int,
        sent_count: int,
    ) -> tuple[str, _ToolSendRecord | None, bool, bool]:
        if sent_count >= self._cfg.max_replies:
            return _tool_json({"ok": False, "error": "reply limit reached"}), None, False, True
        message_id = args.get("message_id")
        text = args.get("text")
        if not isinstance(message_id, str) or not isinstance(text, str):
            return (
                _tool_json({"ok": False, "error": "message_id and text are required"}),
                None,
                False,
                False,
            )
        msg = _message_by_id(batch).get(message_id)
        if msg is None:
            return _tool_json({"ok": False, "error": "unknown message_id"}), None, False, False
        reply_text = text.strip()[: self._cfg.max_reply_chars]
        if not reply_text:
            return _tool_json({"ok": False, "error": "text is empty"}), None, False, False
        if not await self._batch_is_current(event, generation):
            return _tool_json({"ok": False, "error": "stale batch"}), None, False, True
        action = Action(
            kind="reply",
            target=event.scope,
            segments=[ReplySegment(message_id=message_id), TextSegment(text=reply_text)],
        )
        sent, error = await self._send_action(action, event)
        if not sent:
            return _tool_json({"ok": False, "error": error or "send failed"}), None, False, True
        # Refresh the "苏苏确认" attention window for the user we just
        # replied to. This is the bot's own attention-keeping signal:
        # if Susu just spoke to %QQ%, %QQ%'s next message in the same
        # group gets a free pass through the attention gate even if
        # rule patterns wouldn't fire on it.
        await self._refresh_attention_window(event.scope.id, msg.sender_id)
        record = _ToolSendRecord(
            messages=_tool_history_messages(
                _message_history_line(msg),
                assistant=assistant,
                tool_call=tool_call,
                tool_result="回复完成",
            ),
            user_input=_message_history_line(msg),
            assistant_output=reply_text,
        )
        return (
            "回复完成",
            record,
            False,
            False,
        )

    async def _tool_send_group(
        self,
        args: dict[str, object],
        tool_call: ToolCall,
        assistant: Message,
        event: Event,
        batch: list[_BufferedMessage],
        generation: int,
        sent_count: int,
    ) -> tuple[str, _ToolSendRecord | None, bool, bool]:
        if sent_count >= self._cfg.max_replies:
            return _tool_json({"ok": False, "error": "reply limit reached"}), None, False, True
        text = args.get("text")
        if not isinstance(text, str):
            return _tool_json({"ok": False, "error": "text is required"}), None, False, False
        reply_text = text.strip()[: self._cfg.max_reply_chars]
        if not reply_text:
            return _tool_json({"ok": False, "error": "text is empty"}), None, False, False
        if not await self._batch_is_current(event, generation):
            return _tool_json({"ok": False, "error": "stale batch"}), None, False, True
        action = Action(kind="send", target=event.scope, segments=[TextSegment(text=reply_text)])
        sent, error = await self._send_action(action, event)
        if not sent:
            return _tool_json({"ok": False, "error": error or "send failed"}), None, False, True
        await self._refresh_attention_for_group_send(event, batch)
        record = _ToolSendRecord(
            messages=_tool_history_messages(
                _batch_history_summary(batch),
                assistant=assistant,
                tool_call=tool_call,
                tool_result="发送完成",
            ),
            user_input=_batch_history_summary(batch),
            assistant_output=reply_text,
        )
        return (
            "发送完成",
            record,
            False,
            False,
        )

    def _tool_read_batch(self, args: dict[str, object], batch: list[_BufferedMessage]) -> str:
        selected = _select_batch_messages(args, batch)
        if not selected:
            return _tool_json({"ok": False, "error": "no matching messages"})
        selected = selected[:_MAX_READ_MESSAGES]
        text_limit = max(200, min(1_200, _MAX_TOOL_RESULT_CHARS // max(1, len(selected)) - 160))
        return _tool_json(
            {
                "ok": True,
                "messages": [
                    {
                        "line": line,
                        **_message_tool_payload(msg, text_limit=text_limit),
                    }
                    for line, msg in selected
                ],
            }
        )

    async def _send_action(self, action: Action, event: Event) -> tuple[bool, str]:
        sink = self._action_sink
        if sink is None:
            logger.warning("group_batch.no_action_sink", scope_id=event.scope.id)
            return False, "no action sink"
        try:
            sent = sink(action)
            if asyncio.iscoroutine(sent):
                await sent
        except Exception as exc:
            logger.exception("group_batch.send_failed", scope_id=event.scope.id, action=action.kind)
            return False, str(exc)
        return True, ""

    async def _record_tool_history(
        self,
        session: Session,
        event: Event,
        records: list[_ToolSendRecord],
    ) -> None:
        record_messages = getattr(self._inner, "record_messages", None)
        messages = [message for record in records for message in record.messages]
        if record_messages is not None and messages:
            async with session.lock:
                await record_messages(
                    session=session,
                    scope_id=event.scope.id,
                    sender_id="",
                    messages=messages,
                )
            return
        recorder = getattr(self._inner, "record_history", None)
        if recorder is None:
            return
        user_input = "\n".join(record.user_input for record in records)
        assistant_output = "\n".join(record.assistant_output for record in records)
        if not user_input and not assistant_output:
            return
        async with session.lock:
            await recorder(
                session=session,
                scope_id=event.scope.id,
                sender_id="",
                user_input=user_input,
                assistant_output=assistant_output,
            )

    async def _ensure_history(self, session: Session, event: Event) -> None:
        ensure_history_key = getattr(self._inner, "ensure_history_key", None)
        if ensure_history_key is None:
            ensure_history = getattr(self._inner, "ensure_history", None)
            if ensure_history is None:
                return
            async with session.lock:
                await ensure_history(session, event)
            return
        async with session.lock:
            await ensure_history_key(session, event.scope.id, "")

    async def _prepare_context_history(
        self,
        session: Session,
        event: Event,
        *,
        prefix_messages: list[Message],
        current_input_text: str,
        reserve_tokens: int,
    ) -> list[Message]:
        prepare = getattr(self._inner, "prepare_context_history", None)
        if prepare is None:
            return list(session.history)
        async with session.lock:
            prepared: list[Message] = await prepare(
                session=session,
                scope_id=event.scope.id,
                sender_id="",
                prefix_messages=prefix_messages,
                current_input_text=current_input_text,
                reserve_tokens=reserve_tokens,
                allow_compaction=True,
                commit_replacement=True,
            )
            return prepared

    async def _batch_is_current(self, event: Event, generation: int) -> bool:
        state = self._states.get(self._state_key(event))
        if state is None:
            return False
        async with state.lock:
            return not self._closed and state.generation == generation

    async def _batch_session(self, event: Event, fallback: Session) -> Session:
        if self._conversations is None:
            return fallback
        return await self._conversations.get_or_create(
            ConversationKey(bot_id=self._bot_id, scope_id=event.scope.id, sender_id="")
        )

    def _context_max_tokens(self) -> int:
        raw = getattr(self._inner, "context_max_tokens", None)
        if isinstance(raw, int) and raw > 0:
            return raw
        return 64_000

    def _to_buffered(self, event: Event) -> _BufferedMessage:
        return _BufferedMessage(
            message_id=event.id,
            sender_id=event.sender.id,
            sender_name=event.sender.display_name or event.sender.id,
            text=_clip_text(event.text.strip(), self._cfg.max_chars),
            timestamp=_format_time(event.time),
            sent_at=_event_sort_timestamp(event.time),
            received_seq=next(self._message_seq),
            mentions_bot=_mentions_bot(event, self._cfg.bot_names),
            reply_to_bot=_reply_to_bot(event),
        )

    def _trim_locked(self, state: _GroupState) -> None:
        state.messages = deque(_chronological_messages(state.messages))
        total_chars = sum(len(m.text) for m in state.messages)
        while len(state.messages) > self._cfg.max_messages:
            dropped = state.messages.popleft()
            total_chars -= len(dropped.text)
        while total_chars > self._cfg.max_chars and len(state.messages) > 1:
            dropped = state.messages.popleft()
            total_chars -= len(dropped.text)

    def _reset_state_locked(self, state: _GroupState, *, cancel_task: bool) -> None:
        state.messages.clear()
        state.attention_seen = False
        state.first_seen_at = None
        state.template_event = None
        state.last_session = None
        if state.flush_task is not None:
            if cancel_task:
                state.flush_task.cancel()
            state.flush_task = None

    def _is_attention_candidate(self, msg: _BufferedMessage) -> bool:
        if msg.mentions_bot or msg.reply_to_bot:
            return True
        if any(name and name in msg.text for name in self._cfg.bot_names):
            return True
        return _looks_like_question(msg.text)

    def _probe_wired(self) -> bool:
        """Whether a probe is configured and could run as pre-flight gate.

        When True, ``_flush_loop`` treats ``window_s`` expiry as flush-ready
        (the actual yes/no decision happens inside ``_dispatch_batch_with_tools``
        using the full context), and ``_next_wait_s`` schedules a wake at
        ``window_s`` so the batch doesn't sleep until ``max_hold_s``.
        """
        return (
            self._probe is not None
            and self._cfg.attention_probe_enabled
            and self._cfg.require_attention
        )

    def _build_prompt(self, batch: list[_BufferedMessage]) -> str:
        lines = [
            "候选消息（按发送时间从早到晚；sender_id 是稳定身份，sender_name 是昵称）："
        ]
        for m in batch:
            lines.append(
                json.dumps(
                    {
                        "message_id": m.message_id,
                        "sender_id": m.sender_id,
                        "sender_name": m.sender_name,
                        "time": m.timestamp,
                        "text": m.text,
                        "mentions_bot": m.mentions_bot,
                        "reply_to_bot": m.reply_to_bot,
                    },
                    ensure_ascii=False,
                )
            )
        return "\n".join(lines)

    def _build_system_prompt(self) -> str:
        return "\n".join(
            [
                "你现在在群聊里，大家七嘴八舌地说着话。",
                "你不需要每条都回——像平时在群里一样，看到感兴趣的、跟你有关的、或者有人找你说话的，再开口就好。",
                "觉得没什么好说的就安静待着，不用勉强。",
                "上下文里的历史 user 记录会写明你过去是在群里直接说，还是引用回复了谁的哪条消息；assistant 记录是你当时实际发出的正文。",
                "",
                "回复格式：想直接在群里说话就直接输出文字，不要任何前缀。",
                "如果整批都不用回，就只输出 no_reply，不要解释。",
                "如果已经通过 reply_to_message 精确回复完了，就输出 done / 回复好了 / 回复完成，不要再补别的内容。",
                "如果必须引用某条消息回复，才输出严格 JSON：{\"actions\":[{\"type\":\"reply_to_message\",\"message_id\":\"对应ID\",\"text\":\"你的话\"}]}",
                f"最多说 {self._cfg.max_replies} 句，简短自然就好。",
            ]
        )

    def _build_tool_system_prompt(self) -> str:
        return "\n".join(
            [
                "你现在在群聊里，大家七嘴八舌地说着话。",
                "你不需要每条都回——像平时在群里一样，看到感兴趣的、跟你有关的、或者有人找你说话的，再开口就好。",
                "上下文里的历史 user 记录会写明你过去是在群里直接说，还是引用回复了谁的哪条消息；assistant 记录是你当时实际发出的正文。",
                "",
                "回复方式（按情况选一种）：",
                "1. 想直接在群里说话 → 直接输出文字，不要任何前缀（不要写'回复 XXX:'之类的，那是历史记录格式不是发送格式）。",
                "2. 想引用某条消息回复（@对方+引用框）→ 调 reply_to_message 工具。",
                "3. 候选里的 text_truncated=true 或看不清上下文时 → 调 read_batch_messages 工具看原文。",
                "4. 如果已经通过 reply_to_message 精确回复完了 → 输出 done / 回复好了 / 回复完成。",
                "5. 如果整批都不用回 → 输出 no_reply。",
                "不要输出 JSON；JSON actions 只是旧兼容格式，不是当前首选格式。",
                "",
                f"最多说 {self._cfg.max_replies} 句，简短自然就好。",
            ]
        )

    def _build_tool_prompt(self, batch: list[_BufferedMessage]) -> str:
        lines = [
            "群聊候选消息如下，按发送时间从早到晚排列。sender_id 是稳定身份，sender_name 是昵称；先判断是否值得回复；text_truncated=true 时可用 line/message_id 读取原文。",
            "候选索引：",
        ]
        for line, msg in enumerate(batch, start=1):
            clipped_text = _clip_text(msg.text, _BATCH_PROMPT_TEXT_CHARS)
            lines.append(
                json.dumps(
                    {
                        "line": line,
                        "message_id": msg.message_id,
                        "time": msg.timestamp,
                        "sender_id": msg.sender_id,
                        "sender_name": msg.sender_name,
                        "text": clipped_text,
                        "text_truncated": len(clipped_text) < len(msg.text),
                        "mentions_bot": msg.mentions_bot,
                        "reply_to_bot": msg.reply_to_bot,
                    },
                    ensure_ascii=False,
                )
            )
        return "\n".join(lines)

    def _actions_from_result(
        self,
        content: str,
        event: Event,
        batch: list[_BufferedMessage],
    ) -> list[Action]:
        payload = _parse_json_object(content)
        if not isinstance(payload, dict):
            return []
        raw_actions = payload.get("actions")
        if not isinstance(raw_actions, list):
            return []
        known_ids = {m.message_id for m in batch}
        actions: list[Action] = []
        for item in raw_actions:
            if len(actions) >= self._cfg.max_replies:
                break
            if not isinstance(item, dict):
                continue
            typ = item.get("type")
            text = item.get("text")
            if not isinstance(typ, str) or not isinstance(text, str):
                continue
            text = text.strip()
            if not text:
                continue
            text = text[: self._cfg.max_reply_chars]
            if typ == "reply_to_message":
                message_id = item.get("message_id")
                if not isinstance(message_id, str) or message_id not in known_ids:
                    continue
                actions.append(
                    Action(
                        kind="reply",
                        target=event.scope,
                        segments=[ReplySegment(message_id=message_id), TextSegment(text=text)],
                    )
                )
            elif typ == "send_group":
                actions.append(Action(kind="send", target=event.scope, segments=[TextSegment(text=text)]))
        return actions

    def _state_key(self, event_or_scope: Event | str) -> str:
        if isinstance(event_or_scope, Event):
            return f"{self._bot_id}:{event_or_scope.scope.id}"
        return f"{self._bot_id}:{event_or_scope}"


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _event_sort_timestamp(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def _chronological_messages(messages: Iterable[_BufferedMessage]) -> list[_BufferedMessage]:
    return sorted(messages, key=lambda msg: (msg.sent_at, msg.received_seq))


def _mentions_bot(event: Event, bot_names: tuple[str, ...]) -> bool:
    bot_ids = {str(event.bot_id)}
    raw_self_id = event.raw.get("self_id")
    if raw_self_id is not None:
        bot_ids.add(str(raw_self_id))
    for seg in event.segments:
        if getattr(seg, "kind", "") == "at" and getattr(seg, "user_id", "") in {*bot_ids, "all"}:
            return True
    text = event.text
    return any(name and name in text for name in bot_names)


def _reply_to_bot(event: Event) -> bool:
    if not any(getattr(seg, "kind", "") == "reply" for seg in event.segments):
        return False
    bot_id = str(event.bot_id)
    candidates = _reply_source_candidates(event.raw)
    if not candidates:
        # Some adapters only expose a reply segment with no quoted
        # sender metadata. Missing metadata should not make the bot
        # ignore a direct reply in a group, so err on the side of
        # batching it for the LLM selector.
        return True
    for candidate in _reply_source_candidates(event.raw):
        if _candidate_sender_id(candidate) == bot_id:
            return True
    return False


def _reply_source_candidates(raw: dict[str, object]) -> list[object]:
    candidates: list[object] = []
    for key in ("reply", "source", "reply_message"):
        value = raw.get(key)
        if value is not None:
            candidates.append(value)
    return candidates


def _candidate_sender_id(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    sender = value.get("sender")
    if isinstance(sender, dict):
        raw = sender.get("user_id") or sender.get("id")
        if raw is not None:
            return str(raw)
    raw = value.get("user_id") or value.get("sender_id") or value.get("qq")
    return str(raw) if raw is not None else ""


def _looks_like_question(text: str) -> bool:
    if not text:
        return False
    if "?" in text or "？" in text:
        return True
    return bool(re.search(r"(吗|嘛|么|怎么|如何|为啥|为什么|能不能|可不可以|是不是)", text))


def _clip_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def _group_batch_tool_schemas() -> list[ToolSchema]:
    return [
        ToolSchema(
            name=_TOOL_READ_BATCH,
            description="读取当前群聊批次中指定候选消息的完整内容。",
            parameters={
                "type": "object",
                "properties": {
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要读取的 message_id 列表。",
                    },
                    "lines": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "要读取的候选行号列表，从 1 开始。",
                    },
                    "start_line": {"type": "integer", "description": "读取范围起始行号。"},
                    "end_line": {"type": "integer", "description": "读取范围结束行号。"},
                },
                "additionalProperties": False,
            },
        ),
        ToolSchema(
            name=_TOOL_REPLY_TO_MESSAGE,
            description="引用回复当前批次中的某一条群消息（带@和引用框）。只在明确想针对某条消息回应时调用；普通发言直接输出文本即可。",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "要回复的候选消息 ID。"},
                    "text": {"type": "string", "description": "要发送的简短回复内容。"},
                },
                "required": ["message_id", "text"],
                "additionalProperties": False,
            },
        ),
    ]


def _parse_tool_args(raw: str) -> dict[str, object] | None:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _tool_json(payload: dict[str, object]) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    return json.dumps(
        {
            "ok": False,
            "error": "tool result too large",
        },
        ensure_ascii=False,
    )


def _message_by_id(batch: list[_BufferedMessage]) -> dict[str, _BufferedMessage]:
    return {msg.message_id: msg for msg in batch}


def _message_tool_payload(
    msg: _BufferedMessage,
    *,
    text_limit: int | None = None,
) -> dict[str, object]:
    text = msg.text if text_limit is None else _clip_text(msg.text, text_limit)
    return {
        "message_id": msg.message_id,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "time": msg.timestamp,
        "text": text,
        "mentions_bot": msg.mentions_bot,
        "reply_to_bot": msg.reply_to_bot,
    }


def _select_batch_messages(
    args: dict[str, object],
    batch: list[_BufferedMessage],
) -> list[tuple[int, _BufferedMessage]]:
    by_id = _message_by_id(batch)
    selected: list[tuple[int, _BufferedMessage]] = []
    ids = args.get("message_ids")
    if isinstance(ids, list):
        for message_id in ids:
            if isinstance(message_id, str) and message_id in by_id:
                msg = by_id[message_id]
                selected.append((batch.index(msg) + 1, msg))
    lines = args.get("lines")
    if isinstance(lines, list):
        for line in lines:
            if isinstance(line, int) and 1 <= line <= len(batch):
                selected.append((line, batch[line - 1]))
    start_line = args.get("start_line")
    end_line = args.get("end_line")
    if isinstance(start_line, int) and isinstance(end_line, int):
        start = max(1, start_line)
        end = min(len(batch), end_line)
        if start <= end:
            selected.extend((line, batch[line - 1]) for line in range(start, end + 1))
    deduped: dict[str, tuple[int, _BufferedMessage]] = {}
    for line, msg in selected:
        deduped.setdefault(msg.message_id, (line, msg))
    return sorted(deduped.values(), key=lambda item: item[0])


def _history_message_payload(
    msg: _BufferedMessage,
    *,
    line: int | None = None,
    note: str = "",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "message_id": msg.message_id,
        "time": msg.timestamp,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "text": _clip_text(msg.text, _BATCH_HISTORY_TEXT_CHARS),
        "mentions_bot": msg.mentions_bot,
        "reply_to_bot": msg.reply_to_bot,
    }
    if line is not None:
        payload = {"line": line, **payload}
    if note:
        payload["note"] = note
    return payload


def _message_history_line(msg: _BufferedMessage, *, note: str = "") -> str:
    return "\n".join(
        [
            "群聊历史消息：",
            json.dumps(_history_message_payload(msg, note=note), ensure_ascii=False),
        ]
    )


def _batch_history_summary(batch: list[_BufferedMessage]) -> str:
    lines = ["群聊历史片段（按发送时间从早到晚；我随后在群里直接发言）："]
    for line, msg in enumerate(batch, start=1):
        lines.append(json.dumps(_history_message_payload(msg, line=line), ensure_ascii=False))
    return "\n".join(lines)


def _assistant_tool_history_message(assistant: Message, tool_call: ToolCall) -> Message:
    return Message(
        role="assistant",
        content=assistant.content or "",
        tool_calls=[tool_call],
        reasoning_content=assistant.reasoning_content,
    )


def _tool_history_messages(
    user_content: str,
    *,
    assistant: Message,
    tool_call: ToolCall,
    tool_result: str,
) -> list[Message]:
    return [
        Message(role="user", content=user_content),
        _assistant_tool_history_message(assistant, tool_call),
        Message(
            role="tool",
            content=tool_result,
            name=tool_call.name,
            tool_call_id=tool_call.id,
        ),
    ]


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _record_from_action(action: Action, batch: list[_BufferedMessage]) -> _ToolSendRecord:
    message_by_id = _message_by_id(batch)
    reply_id = next(
        (
            getattr(seg, "message_id", "")
            for seg in action.segments
            if getattr(seg, "kind", "") == "reply"
        ),
        "",
    )
    reply_text = "\n".join(
        getattr(seg, "text", "")
        for seg in action.segments
        if getattr(seg, "kind", "") == "text" and getattr(seg, "text", "")
    )
    if reply_id and reply_id in message_by_id:
        msg = message_by_id[reply_id]
        return _ToolSendRecord(
            messages=[
                Message(role="user", content=_message_history_line(msg)),
                Message(role="assistant", content=reply_text),
            ],
            user_input=_message_history_line(msg),
            assistant_output=reply_text,
        )
    return _ToolSendRecord(
        messages=[
            Message(role="user", content=_batch_history_summary(batch)),
            Message(role="assistant", content=reply_text),
        ],
        user_input=_batch_history_summary(batch),
        assistant_output=reply_text,
    )


def _parse_json_object(content: str) -> object | None:
    text = content.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed: object = json.loads(text)
        return parsed
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed_window: object = json.loads(text[start : end + 1])
            return parsed_window
        except json.JSONDecodeError:
            logger.warning("group_batch.bad_json", preview=text[:200])
            return None
