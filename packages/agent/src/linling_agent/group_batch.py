"""Selective group-chat batching for LLM fallback messages."""

from __future__ import annotations

import asyncio
import inspect
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
from linling_core.segments import (
    CardSegment,
    FaceSegment,
    FileSegment,
    ImageSegment,
    ReplySegment,
    Segment,
    TextSegment,
    VideoSegment,
    VoiceSegment,
    XmlSegment,
)

from linling_agent.action_delay import with_random_delay_before
from linling_agent.actions_protocol import ParsedAction
from linling_agent.attention_probe import AttentionProbe, _ProbeBatchInput
from linling_agent.context import fit_messages_to_budget
from linling_agent.llm import Message, ToolCall, ToolSchema
from linling_agent.profile import ProfileStore

logger = structlog.get_logger(__name__)

_TOOL_READ_BATCH = "read_batch_messages"
_TOOL_REPLY_TO_MESSAGE = "reply_to_message"
_TOOL_SEND_GROUP = "send_group"
_TOOL_FINISH_TURN = "finish_turn"
_TOOL_READ_PROFILE = "read_user_profile"
_TOOL_WRITE_PROFILE = "write_user_profile"
_MAX_TOOL_ROUNDS = 8
_MAX_READ_CALLS = 2
_MAX_READ_MESSAGES = 5
_BATCH_PREVIEW_CHARS = 80
_BATCH_PROMPT_TEXT_CHARS = 500
_BATCH_HISTORY_TEXT_CHARS = 500
_MAX_TOOL_RESULT_CHARS = 2_500
_NUDGE_LIMIT = 2
_NUDGE_PROMPT = "用工具回复消息，或调用 finish_turn 结束本回合。"


_ATTENTION_KV_SCOPE_PREFIX = "啊/"
_ATTENTION_KV_FILE = "苏苏确认"
_DAILY_SUMMARY_SCOPE_PREFIX = "__history_daily_summary__/"
_DAILY_SUMMARY_FILE = "_group"
_DAILY_SUMMARY_KEY = "last_date"
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
    multi_reply_delay_min_s: float = 0.0
    multi_reply_delay_max_s: float = 0.0
    daily_summary_enabled: bool = False
    daily_summary_keep_recent_turns: int = 2
    # Number of recent user turns from session.history to prepend as
    # "前情提要" before the candidate messages in the user prompt.
    # Gives the LLM conversational context when a batch is small.
    # 0 disables the feature.
    context_history_turns: int = 3

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
        if self.multi_reply_delay_min_s < 0:
            raise ValueError("multi_reply_delay_min_s must be non-negative")
        if self.multi_reply_delay_max_s < 0:
            raise ValueError("multi_reply_delay_max_s must be non-negative")
        if self.multi_reply_delay_max_s < self.multi_reply_delay_min_s:
            raise ValueError("multi_reply_delay_max_s must be >= multi_reply_delay_min_s")
        if self.daily_summary_keep_recent_turns < 0:
            raise ValueError("daily_summary_keep_recent_turns must be non-negative")
        if self.context_history_turns < 0:
            raise ValueError("context_history_turns must be non-negative")


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
    mentions_all: bool
    reply_to_bot: bool
    # User ids this message @-mentioned that are NOT the bot and not
    # ``@all``. ``plain_text`` strips every AtSegment, so without this
    # the directional intent of "@小红 你说得对" is lost — the text the
    # LLM sees becomes a floating "你说得对" that reads like it could be
    # aimed at the bot. We surface these (resolved to names where the
    # target also appears in the batch, else a neutral "某人" marker so
    # raw QQ ids never reach the LLM) via the ``at_targets`` field in the
    # candidate JSON. Default ``()`` keeps the field optional for any
    # direct ``_BufferedMessage`` construction.
    at_user_ids: tuple[str, ...] = ()
    # True iff the original message carried any non-text content segment
    # (image / face / mface sticker / voice / …) that ``_llm_visible_text``
    # rendered as a ``[图片]``/``[表情]``/… marker. Drives whether the
    # system prompt bothers explaining the markers — skipped on pure-text
    # batches so the prompt stays byte-identical to the pre-marker behaviour
    # (important for tight token budgets in tests and for not cluttering the
    # common case).
    has_non_text: bool = False


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
        inner_setter = getattr(self._inner, "set_action_sink", None)
        if inner_setter is not None:
            inner_setter(sink)

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

    async def _daily_summary_request(self, event: Event) -> tuple[bool, str]:
        if (
            not self._cfg.daily_summary_enabled
            or self._kv is None
            or not self._inner_context_compaction_enabled()
        ):
            return False, ""
        today = _event_local_date(event.time)
        try:
            raw = await self._kv.read(
                f"{_DAILY_SUMMARY_SCOPE_PREFIX}{event.scope.id}",
                _DAILY_SUMMARY_FILE,
                _DAILY_SUMMARY_KEY,
                default="",
            )
        except Exception:
            logger.debug(
                "group_batch.daily_summary.kv_read_failed",
                scope_id=event.scope.id,
                exc_info=True,
            )
            return False, today
        return raw != today, today

    async def _mark_daily_summary(self, scope_id: str, date_text: str) -> None:
        if self._kv is None or not date_text:
            return
        try:
            await self._kv.write(
                f"{_DAILY_SUMMARY_SCOPE_PREFIX}{scope_id}",
                _DAILY_SUMMARY_FILE,
                _DAILY_SUMMARY_KEY,
                date_text,
            )
        except Exception:
            logger.debug(
                "group_batch.daily_summary.kv_write_failed",
                scope_id=scope_id,
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
        selector_prompt = self._build_tool_system_prompt(batch)
        agent_system = str(getattr(agent_def, "system", "") or "")
        if agent_system:
            selector_prompt = f"{agent_system}\n\n{selector_prompt}"
        history_context = _extract_context_lines(
            list(session.history), self._cfg.context_history_turns
        )
        user_prompt = self._build_tool_prompt(batch, history_context=history_context)
        guardrails = getattr(agent_def, "guardrails", None)
        max_tokens = getattr(guardrails, "max_tokens", None)
        temperature = min(getattr(agent_def, "temperature", 0.7), 0.3)
        force_daily_summary, daily_summary_date = await self._daily_summary_request(event)
        history, history_compacted = await self._prepare_context_history(
            session,
            event,
            prefix_messages=[Message(role="system", content=selector_prompt)],
            current_input_text=user_prompt,
            reserve_tokens=max_tokens or 0,
            force_compaction=force_daily_summary,
            summary_keep_recent_turns=(
                self._cfg.daily_summary_keep_recent_turns if force_daily_summary else None
            ),
        )
        if force_daily_summary and history_compacted:
            await self._mark_daily_summary(event.scope.id, daily_summary_date)
        messages = [
            Message(role="system", content=selector_prompt),
            *history,
            Message(role="user", content=user_prompt),
        ]
        messages = fit_messages_to_budget(messages, self._context_max_tokens())
        # --- Pre-flight: probe runs only when the rule-based attention
        # gate did NOT fire. If the rule already said "respond" (e.g.
        # @me/@all, reply-to-me, question particle, bot_name in text), skip the
        # probe entirely — re-judging with a small model that lacks
        # full context can incorrectly veto an obvious reply.
        #
        # The probe deliberately sees a HISTORY-FREE view: only the
        # system prompt and the buffered batch (the same user_prompt the
        # main LLM gets), with NO prior conversation turns. Reasons:
        #   1. The probe is a cheap yes/no "is anything here worth a
        #      reply?" gate — past turns don't change that judgement and
        #      just inflate token cost on the small model.
        #   2. The probe model (e.g. Groq llama) is a different backend
        #      from the main LLM (e.g. DeepSeek). History assistant turns
        #      can carry provider-specific fields like ``reasoning_content``
        #      that the probe backend rejects outright (HTTP 400
        #      "reasoning_content is unsupported"), which would fail the
        #      whole batch closed and silently drop replies.
        # The main LLM below still gets the full ``messages`` (with
        # history); only the probe is trimmed.
        if (
            not had_attention
            and self._probe_wired()
            and self._probe is not None
        ):
            probe_messages = [
                Message(role="system", content=selector_prompt),
                Message(role="user", content=user_prompt),
            ]
            try:
                probe_has_action = await self._probe_has_action(
                    messages=probe_messages,
                    tools=_reply_tool_schemas(),
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
                    model=getattr(self._probe, "model", None),
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
        nudge_count = 0
        total_tokens = 0
        logger.info(
            "group_batch.tool_selector_start",
            scope_id=event.scope.id,
            batch_size=len(batch),
            had_attention=had_attention,
            history_msgs=len(history),
            at_target_messages=sum(1 for msg in batch if msg.at_user_ids),
            mentions_me_messages=sum(1 for msg in batch if msg.mentions_bot),
            mentions_all_messages=sum(1 for msg in batch if msg.mentions_all),
            reply_to_me_messages=sum(1 for msg in batch if msg.reply_to_bot),
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
                # No tool calls — nudge the LLM to use tools or finish_turn.
                nudge_count += 1
                if nudge_count > _NUDGE_LIMIT:
                    logger.info(
                        "group_batch.nudge_limit_reached",
                        scope_id=event.scope.id,
                        nudge_count=nudge_count,
                        content_preview=(assistant.content or "")[:200],
                    )
                    return _ToolSelection(records=records)
                messages.append(assistant)
                messages.append(Message(role="user", content=_NUDGE_PROMPT))
                continue
            nudge_count = 0
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
        # With tool-call-only sending, the presence of any reply-shaped
        # or finish_turn tool call is the action signal.  Plain text
        # content (no tool calls) is no longer interpreted as a send.
        _ = event, batch  # reserved for future policy
        tool_calls = message.tool_calls or []
        return any(
            tc.name
            in {
                _TOOL_READ_BATCH,
                _TOOL_REPLY_TO_MESSAGE,
                _TOOL_SEND_GROUP,
                _TOOL_FINISH_TURN,
            }
            for tc in tool_calls
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
            delayed_action = self._with_multi_reply_delay(
                action,
                sent_count=sent_count + len(records),
            )
            sent, _error = await self._send_action(delayed_action, event)
            if sent and await self._batch_is_current(event, generation):
                await self._refresh_attention_for_action(delayed_action, event, batch)
                records.append(_record_from_action(delayed_action, batch))
        return records

    def _with_multi_reply_delay(self, action: Action, *, sent_count: int) -> Action:
        if sent_count <= 0:
            return action
        return with_random_delay_before(
            action,
            min_s=self._cfg.multi_reply_delay_min_s,
            max_s=self._cfg.multi_reply_delay_max_s,
        )

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
        if tool_call.name == _TOOL_FINISH_TURN:
            summary = args.get("summary", "")
            logger.info(
                "group_batch.finish_turn",
                scope_id=event.scope.id,
                summary=str(summary)[:200],
            )
            return _tool_json({"ok": True}), None, False, True
        if tool_call.name in (_TOOL_READ_PROFILE, _TOOL_WRITE_PROFILE):
            return await self._tool_profile(tool_call.name, args), None, False, False
        return _tool_json({"ok": False, "error": f"unknown tool: {tool_call.name}"}), None, False, False

    async def _tool_profile(self, name: str, args: dict[str, object]) -> str:
        """Execute a profile read/write tool. Never produces an outbound action.

        Returns a JSON string for the tool message; the loop continues so the
        model can keep deciding what to reply. ``self._kv`` may be ``None`` in
        legacy test wiring — degrade to an error string rather than crash.
        """
        if self._kv is None:
            return _tool_json({"ok": False, "error": "profile store unavailable"})
        store = ProfileStore(self._kv)
        qq = args.get("qq")
        if not isinstance(qq, str) or not qq:
            return _tool_json({"ok": False, "error": "qq is required"})
        try:
            if name == _TOOL_READ_PROFILE:
                existing = await store.load(qq)
                nick = await store.load_name(qq)
                if not existing:
                    return _tool_json({"ok": True, "qq": qq, "profile": "", "note": "暂无画像"})
                return _tool_json({"ok": True, "qq": qq, "name": nick, "profile": existing})
            # write
            new_profile = args.get("profile")
            if not isinstance(new_profile, str):
                return _tool_json({"ok": False, "error": "profile is required"})
            name_arg = args.get("name")
            await store.save(qq, new_profile, name=name_arg if isinstance(name_arg, str) else None)
            return _tool_json({"ok": True, "qq": qq, "updated": True})
        except Exception:
            logger.warning("group_batch.profile_tool_failed", tool=name, qq=qq, exc_info=True)
            return _tool_json({"ok": False, "error": "profile tool failed"})

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
        action = self._with_multi_reply_delay(action, sent_count=sent_count)
        sent, error = await self._send_action(action, event)
        if not sent:
            return _tool_json({"ok": False, "error": error or "send failed"}), None, False, True
        # Refresh the "苏苏确认" attention window for the user we just
        # replied to. This is the bot's own attention-keeping signal:
        # if Susu just spoke to %QQ%, %QQ%'s next message in the same
        # group gets a free pass through the attention gate even if
        # rule patterns wouldn't fire on it.
        await self._refresh_attention_window(event.scope.id, msg.sender_id)
        name_by_id = _batch_name_index(batch)
        user_input = _message_history_line(msg, name_by_id=name_by_id)
        tool_result = _tool_json(
            {
                "ok": True,
                "action": "reply_to_message",
                "replied_to": msg.sender_name,
                "original_text": msg.text[:200],
                "reply": reply_text,
            }
        )
        record = _ToolSendRecord(
            messages=_tool_history_messages(
                user_input,
                assistant=assistant,
                tool_call=tool_call,
                tool_result=tool_result,
            ),
            user_input=user_input,
            assistant_output=reply_text,
        )
        return (
            tool_result,
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
        action = self._with_multi_reply_delay(action, sent_count=sent_count)
        sent, error = await self._send_action(action, event)
        if not sent:
            return _tool_json({"ok": False, "error": error or "send failed"}), None, False, True
        await self._refresh_attention_for_group_send(event, batch)
        batch_summary = _batch_history_summary(batch)
        tool_result = _tool_json(
            {
                "ok": True,
                "action": "send_group",
                "sent_text": reply_text,
            }
        )
        record = _ToolSendRecord(
            messages=_tool_history_messages(
                batch_summary,
                assistant=assistant,
                tool_call=tool_call,
                tool_result=tool_result,
            ),
            user_input=batch_summary,
            assistant_output=reply_text,
        )
        return (
            tool_result,
            record,
            False,
            False,
        )

    def _tool_read_batch(self, args: dict[str, object], batch: list[_BufferedMessage]) -> str:
        selected = _select_batch_messages(args, batch)
        if not selected:
            return _tool_json({"ok": False, "error": "no matching messages"})
        selected = selected[:_MAX_READ_MESSAGES]
        name_by_id = _batch_name_index(batch)
        lines: list[str] = []
        for _line_num, msg in selected:
            lines.append(
                f"[message_id={msg.message_id}] {_render_candidate_line(msg, name_by_id)}"
            )
        return _tool_json({"ok": True, "messages": lines})

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
        force_compaction: bool = False,
        summary_keep_recent_turns: int | None = None,
    ) -> tuple[list[Message], bool]:
        prepare_with_status = getattr(
            self._inner, "prepare_context_history_with_status", None
        )
        if prepare_with_status is None:
            prepare = getattr(self._inner, "prepare_context_history", None)
            if prepare is None:
                return list(session.history), False
            async with session.lock:
                prepare_kwargs = {
                    "session": session,
                    "scope_id": event.scope.id,
                    "sender_id": "",
                    "prefix_messages": prefix_messages,
                    "current_input_text": current_input_text,
                    "reserve_tokens": reserve_tokens,
                    "allow_compaction": True,
                    "commit_replacement": True,
                }
                prepare_params = inspect.signature(prepare).parameters
                if "force_compaction" in prepare_params:
                    prepare_kwargs["force_compaction"] = force_compaction
                if "summary_keep_recent_turns" in prepare_params:
                    prepare_kwargs["summary_keep_recent_turns"] = summary_keep_recent_turns
                prepared: list[Message] = await prepare(
                    **prepare_kwargs,
                )
                return prepared, False
        async with session.lock:
            prepared, replacement_committed, source_history_len = await prepare_with_status(
                session=session,
                scope_id=event.scope.id,
                sender_id="",
                prefix_messages=prefix_messages,
                current_input_text=current_input_text,
                reserve_tokens=reserve_tokens,
                allow_compaction=True,
                force_compaction=force_compaction,
                summary_keep_recent_turns=summary_keep_recent_turns,
                commit_replacement=True,
            )
            no_compactable_history = (
                force_compaction
                and summary_keep_recent_turns is not None
                and source_history_len <= summary_keep_recent_turns * 2
            )
            return prepared, bool(replacement_committed or no_compactable_history)

    def _inner_context_compaction_enabled(self) -> bool:
        return bool(getattr(self._inner, "context_compaction_enabled", False))

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
            text=_clip_text(_llm_visible_text(event.segments), self._cfg.max_chars),
            timestamp=_format_time(event.time),
            sent_at=_event_sort_timestamp(event.time),
            received_seq=next(self._message_seq),
            mentions_bot=_mentions_bot(event, self._cfg.bot_names),
            mentions_all=_mentions_all(event),
            reply_to_bot=_reply_to_bot(event),
            at_user_ids=_other_at_user_ids(event),
            has_non_text=_has_non_text_segment(event.segments),
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
        if msg.mentions_bot or msg.mentions_all or msg.reply_to_bot:
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
        name_by_id = _batch_name_index(batch)
        lines = ["候选消息如下（按时间升序）："]
        for m in batch:
            lines.append(_render_candidate_line(m, name_by_id))
        return "\n".join(lines)

    def _build_tool_system_prompt(self, batch: list[_BufferedMessage]) -> str:
        directed = sum(1 for m in batch if m.mentions_bot or m.reply_to_bot)
        silence_hint = (
            "没有消息在直接找你。不发言完全没问题，"
            "但要是大家在聊你感兴趣的话题，也可以自然地插一句。"
            if directed == 0
            else f"有 {directed} 条消息在直接找你，记得回应对方，别让人等着。"
        )
        return "\n".join(
            [
                "你在一个群里，群里的人都是你的朋友。",
                "",
                "## 什么时候说话",
                "- `[directed at you]` → 有人在找你：热情回应，认真聊，别冷场",
                "- `[addressed to all]` → 面向全体的消息：有兴趣就自然接话",
                "- `[not directed at you]` → 别人在互相聊：有想说的可以插一句，没话说就不打扰",
                "",
                f"**本批消息：{silence_hint}**",
                "",
                "## 说话风格",
                "- 用「苏苏」的性格说话：温柔可爱、俏皮，语气词和短句",
                "- 被点名要好好聊，别只回一句就走；可以多问一句、多关心一句",
                "- 看到有人难过、吐槽、开心，都可以主动接一下",
                "- 选择性发言：不每句都回，但说了就要有温度、有内容",
                *([_NON_TEXT_MARKER_RULE] if any(m.has_non_text for m in batch) else []),
                "",
                "## 消息协议（重要）",
                "所有消息必须通过工具发送，不能直接输出文本。",
                "",
                "| 意图 | 工具 |",
                "|---|---|",
                "| 跟群里说话 | `send_group(text=\"...\")` |",
                "| 引用回复某条消息（@+引用框） | `reply_to_message(message_id=\"...\", text=\"...\")` |",
                "| 查看被截断消息的完整内容 | `read_batch_messages(message_ids=[...])` |",
                "| 连发多条 | 多次调用 `send_group` 或 `reply_to_message` |",
                "| 结束回合（或没话说） | `finish_turn(summary=\"...\")` |",
                "",
                "每回合结束必须调用 `finish_turn`，即使你选择不说话。",
                f"每回合最多回复 {self._cfg.max_replies} 条消息，一次一条。",
            ]
        )

    def _build_tool_prompt(
        self,
        batch: list[_BufferedMessage],
        history_context: str = "",
    ) -> str:
        name_by_id = _batch_name_index(batch)
        lines: list[str] = []
        if history_context:
            lines.append(history_context)
            lines.append("")
        # Batch context: help the LLM gauge whether anyone is talking to it
        directed = sum(1 for m in batch if m.mentions_bot or m.reply_to_bot)
        senders = len({m.sender_id for m in batch if m.sender_id})
        header = f"Candidate messages: {len(batch)} messages from {senders} people"
        if directed:
            header += f" ({directed} directed at you)"
        else:
            header += " (none directed at you — silence is likely the best choice)"
        lines.append(header)
        for msg in batch:
            clipped = _clip_text(msg.text, _BATCH_PROMPT_TEXT_CHARS)
            truncated_msg = (
                _BufferedMessage(
                    message_id=msg.message_id,
                    sender_id=msg.sender_id,
                    sender_name=msg.sender_name,
                    text=clipped,
                    timestamp=msg.timestamp,
                    sent_at=msg.sent_at,
                    received_seq=msg.received_seq,
                    mentions_bot=msg.mentions_bot,
                    mentions_all=msg.mentions_all,
                    reply_to_bot=msg.reply_to_bot,
                    at_user_ids=msg.at_user_ids,
                    has_non_text=msg.has_non_text,
                )
                if len(clipped) < len(msg.text)
                else msg
            )
            line = _render_candidate_line(truncated_msg, name_by_id)
            if len(clipped) < len(msg.text):
                line += f" [text_truncated, message_id={msg.message_id}]"
            lines.append(line)
        return "\n".join(lines)

    def _state_key(self, event_or_scope: Event | str) -> str:
        if isinstance(event_or_scope, Event):
            return f"{self._bot_id}:{event_or_scope.scope.id}"
        return f"{self._bot_id}:{event_or_scope}"


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _event_local_date(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().date().isoformat()


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
        uid = str(getattr(seg, "user_id", ""))
        if getattr(seg, "kind", "") == "at" and uid in bot_ids:
            return True
    text = event.text
    return any(name and name in text for name in bot_names)


def _mentions_all(event: Event) -> bool:
    return any(
        getattr(seg, "kind", "") == "at" and str(getattr(seg, "user_id", "")) == "all"
        for seg in event.segments
    )


def _other_at_user_ids(event: Event) -> tuple[str, ...]:
    """At-segment user ids that target neither the bot nor ``@all``.

    ``plain_text`` (and therefore :attr:`Event.text`) drops every
    ``AtSegment``, so a message like "@小红 你说得对" reaches the LLM as a
    bare "你说得对" with no addressee. That floating text reads like it
    might be aimed at the bot. We capture the *other* targets here so the
    batch prompt can flag "this was aimed at someone else", preserving
    direction without re-injecting raw ``@id`` into the text stream
    (which :attr:`Event.match_text` deliberately keeps out of LLM-visible
    surfaces). ``@all`` is excluded — it's already treated as attention.
    Order-preserving and de-duplicated.
    """
    bot_ids = {str(event.bot_id)}
    raw_self_id = event.raw.get("self_id")
    if raw_self_id is not None:
        bot_ids.add(str(raw_self_id))
    seen: set[str] = set()
    out: list[str] = []
    for seg in event.segments:
        if getattr(seg, "kind", "") != "at":
            continue
        uid = str(getattr(seg, "user_id", ""))
        if not uid or uid == "all" or uid in bot_ids or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return tuple(out)


def _extract_context_lines(history: list[Message], turns: int) -> str:
    """Extract recent user messages from session history as 前情提要.

    Returns an empty string when ``turns <= 0`` or no user messages exist.
    Each user content block is truncated to 300 chars to keep the prompt compact.
    """
    if turns <= 0:
        return ""
    user_msgs = [m for m in history if m.role == "user"]
    recent = user_msgs[-turns:]
    if not recent:
        return ""
    lines = ["前情提要："]
    for m in recent:
        text = (m.content or "")[:300].strip()
        if text:
            lines.append(text)
    return "\n---\n".join(lines)


def _batch_name_index(batch: list[_BufferedMessage]) -> dict[str, str]:
    """Map ``sender_id -> sender_name`` for everyone who spoke in the batch.

    The only zero-cost way to turn an ``@<user_id>`` back into a human
    name: if the mentioned user also sent a message in the same batch,
    we already have their display name. No adapter RPC, no KV read.
    """
    index: dict[str, str] = {}
    for msg in batch:
        if msg.sender_id and msg.sender_name:
            index.setdefault(msg.sender_id, msg.sender_name)
    return index


def _render_at_targets(
    at_user_ids: tuple[str, ...], name_by_id: dict[str, str]
) -> list[str]:
    """Render @-target ids as names, falling back to a neutral marker.

    Resolves each id against the batch-local name index. Unknown targets
    collapse to ``"某人"`` so a raw QQ id never reaches the LLM (mirrors
    the ``match_text`` policy of keeping ``@<id>`` off LLM-visible
    surfaces). De-duplicates the neutral marker so three unknown @s
    don't render as ``["某人","某人","某人"]``.
    """
    out: list[str] = []
    saw_anon = False
    for uid in at_user_ids:
        name = name_by_id.get(uid)
        if name:
            out.append(name)
        elif not saw_anon:
            out.append("某人")
            saw_anon = True
    return out


def _render_candidate_line(
    msg: _BufferedMessage, name_by_id: dict[str, str]
) -> str:
    """Render a buffered message as a single natural-language line with relevance tag.

    Examples::

        小红回复你说：不是这样的 [directed at you]
        小明@你说：帮我看看 [directed at you]
        小明@全体说：大家早上好 [addressed to all]
        小红@小明说：你吃了吗 [not directed at you]
        小红说：今天好无聊 [not directed at you]
    """
    sender = msg.sender_name

    if msg.reply_to_bot:
        prefix = f"{sender}回复你"
        tag = " [directed at you]"
    elif msg.mentions_bot:
        prefix = f"{sender}@你"
        tag = " [directed at you]"
    elif msg.at_user_ids:
        targets = _render_at_targets(msg.at_user_ids, name_by_id)
        prefix = f"{sender}@{','.join(targets)}"
        tag = " [not directed at you]"
    elif msg.mentions_all:
        prefix = f"{sender}@全体"
        tag = " [addressed to all]"
    else:
        prefix = sender
        tag = " [not directed at you]"

    return f"{prefix}说：{msg.text}{tag}"


def _reply_to_bot(event: Event) -> bool:
    if not any(getattr(seg, "kind", "") == "reply" for seg in event.segments):
        return False
    bot_id = str(event.bot_id)
    candidates = _reply_source_candidates(event.raw)
    if not candidates:
        # Standard OneBot v11 (and LLBot by default) only carries the
        # reply segment's ``message_id`` — no quoted-sender metadata
        # ride along on the inbound event. We can't tell who's being
        # replied to from this alone, so defaulting to "yes, this is
        # a reply to the bot" caused every cross-user quote-reply in
        # a busy group to bypass the attention gate. Defaulting to
        # ``False`` here is the safer call: a real reply to the bot
        # almost always also @-mentions the bot (LLBot injects an
        # implicit ``@bot`` when you long-press → reply on mobile),
        # which the at-segment branch in :func:`_mentions_bot`
        # already catches separately.
        return False
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


# Non-text segments → short LLM-visible markers, in segment order. We render
# these into the buffered message ``text`` so a pure-sticker / image / voice
# message no longer appears as empty content (which made the LLM ask "what did
# you send?"). ``AtSegment`` / ``ReplySegment`` / ``PokeSegment`` are modeled
# separately (mentions_me / at_targets / reply_to_me) and deliberately not
# inlined here — they must not show up as stray text in the candidate.
_NON_TEXT_MARKERS: tuple[tuple[type[Segment], str], ...] = (
    (ImageSegment, "[图片]"),
    (FaceSegment, "[表情]"),  # QQ basic face + mface/bface 商城表情包/贴纸
    (VoiceSegment, "[语音]"),
    (VideoSegment, "[视频]"),
    (FileSegment, "[文件]"),
    (CardSegment, "[卡片]"),
    (XmlSegment, "[卡片]"),
)


def _llm_visible_text(segments: list[Segment]) -> str:
    """Render segments as the text the LLM should see.

    ``TextSegment``s pass through verbatim; each known non-text segment becomes
    its marker (``[图片]`` / ``[表情]`` / …). Unknown segment kinds contribute
    nothing. Leading/trailing whitespace is stripped, so a pure non-text message
    yields exactly its marker (e.g. ``"[表情]"``) rather than an empty string.
    """
    parts: list[str] = []
    for seg in segments:
        if isinstance(seg, TextSegment):
            parts.append(seg.text)
            continue
        for kind, marker in _NON_TEXT_MARKERS:
            if isinstance(seg, kind):
                parts.append(marker)
                break
    return "".join(parts).strip()


def _has_non_text_segment(segments: list[Segment]) -> bool:
    """True iff ``segments`` carry any non-text content we render as a marker."""
    for seg in segments:
        if isinstance(seg, TextSegment):
            continue
        if any(isinstance(seg, kind) for kind, _ in _NON_TEXT_MARKERS):
            return True
    return False


# Shared guidance explaining the non-text markers to the LLM. Only injected
# when the current batch actually contains a non-text message (see
# ``_BufferedMessage.has_non_text``), so pure-text batches keep the exact
# system prompt they had before this feature — both to avoid cluttering the
# common case and because the selector prompt is counted toward the context
# budget (Chinese-heavy text scores high under the byte-based token estimate),
# and the marker rule is irrelevant when there are no markers to explain.
_NON_TEXT_MARKER_RULE = (
    "候选或历史里的 text 若是 [图片]/[表情]/[语音]/[视频]/[文件]/[卡片] 这类标记，"
    "说明对方发的是非文字内容（你看不到实际内容）；这类消息通常不需要回复，"
    "也不要追问对方发了什么。"
)


def _clip_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def _group_batch_tool_schemas() -> list[ToolSchema]:
    """Full tool set for the main selective-replier loop."""
    return [*_reply_tool_schemas(), *_profile_tool_schemas()]


def _reply_tool_schemas() -> list[ToolSchema]:
    """Reply-oriented tools. This is also the set the attention probe sees —
    the probe judges 'is anything here worth replying to', so it must only be
    offered action-shaped tools. Profile read/write are deliberately excluded
    (a curiosity-driven ``read_user_profile`` is NOT a reply intent and would
    otherwise be mis-counted by ``_assistant_message_has_action``)."""
    return [
        ToolSchema(
            name=_TOOL_READ_BATCH,
            description="Read the full content of specified candidate messages in the current group batch.",
            parameters={
                "type": "object",
                "properties": {
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of message_ids to read.",
                    },
                    "lines": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of candidate line numbers to read (1-based).",
                    },
                    "start_line": {"type": "integer", "description": "Start line number of range to read."},
                    "end_line": {"type": "integer", "description": "End line number of range to read."},
                },
                "additionalProperties": False,
            },
        ),
        ToolSchema(
            name=_TOOL_REPLY_TO_MESSAGE,
            description="Quote-reply to a specific group message (with @mention and quote box). Use when you want to respond directly to someone. For casual remarks, use send_group instead.",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The candidate message ID to reply to."},
                    "text": {"type": "string", "description": "Short reply text to send."},
                },
                "required": ["message_id", "text"],
                "additionalProperties": False,
            },
        ),
        ToolSchema(
            name=_TOOL_SEND_GROUP,
            description="Send a short message to the group without quoting any specific message. Use for casual remarks or joining the conversation.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Short message text to send."},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        ),
        ToolSchema(
            name=_TOOL_FINISH_TURN,
            description="End this turn. Call when done sending messages or when nothing needs a reply. Provide a brief summary of the topic and your thoughts.",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Brief summary of this turn's topic and your thoughts.",
                    },
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
        ),
    ]


def _profile_tool_schemas() -> list[ToolSchema]:
    """Per-user profile tools, available to the main loop (not the probe)."""
    return [
        ToolSchema(
            name=_TOOL_READ_PROFILE,
            description="Look up a user's long-term profile by QQ number. Use when you want to know who someone is or what was discussed before.",
            parameters={
                "type": "object",
                "properties": {
                    "qq": {"type": "string", "description": "The user's QQ number (sender_id from candidate messages)."},
                },
                "required": ["qq"],
                "additionalProperties": False,
            },
        ),
        ToolSchema(
            name=_TOOL_WRITE_PROFILE,
            description="Fully rewrite a user's long-term profile by QQ number. Call when you learn something worth remembering long-term. Always read the old profile first, then write a complete new version (≤400 chars, only stable facts/preferences/relationships/commitments).",
            parameters={
                "type": "object",
                "properties": {
                    "qq": {"type": "string", "description": "The user's QQ number."},
                    "profile": {"type": "string", "description": "Complete new profile text."},
                    "name": {"type": "string", "description": "The user's display name (optional)."},
                },
                "required": ["qq", "profile"],
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
    name_by_id: dict[str, str] | None = None,
) -> dict[str, object]:
    text = msg.text if text_limit is None else _clip_text(msg.text, text_limit)
    return _message_payload(
        msg,
        name_by_id=name_by_id or {},
        text=text,
        text_truncated=len(text) < len(msg.text),
    )


def _message_payload(
    msg: _BufferedMessage,
    *,
    name_by_id: dict[str, str],
    line: int | None = None,
    text: str | None = None,
    text_truncated: bool | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "message_id": msg.message_id,
        "time": msg.timestamp,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "text": msg.text if text is None else text,
    }
    if line is not None:
        payload = {"line": line, **payload}
    if msg.mentions_bot:
        payload["mentions_me"] = True
    if msg.mentions_all:
        payload["mentions_all"] = True
    if msg.reply_to_bot:
        payload["reply_to_me"] = True
    if text_truncated:
        payload["text_truncated"] = True
    at_targets = _render_at_targets(msg.at_user_ids, name_by_id)
    if at_targets:
        # Direction marker: this message @-mentioned other group
        # members (not the bot). Resolved to names when the target also
        # spoke in this batch, else "某人".
        payload["at_targets"] = at_targets
    return payload


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
    name_by_id: dict[str, str] | None = None,
) -> dict[str, object]:
    text = _clip_text(msg.text, _BATCH_HISTORY_TEXT_CHARS)
    payload = _message_payload(
        msg,
        name_by_id=name_by_id or {},
        line=line,
        text=text,
        text_truncated=len(text) < len(msg.text),
    )
    if note:
        payload["note"] = note
    return payload


def _message_history_line(
    msg: _BufferedMessage,
    *,
    note: str = "",
    name_by_id: dict[str, str] | None = None,
) -> str:
    return "\n".join(
        [
            "群聊历史消息：",
            json.dumps(
                _history_message_payload(msg, note=note, name_by_id=name_by_id),
                ensure_ascii=False,
            ),
        ]
    )


def _batch_history_summary(batch: list[_BufferedMessage]) -> str:
    lines = ["群聊历史片段（按发送时间从早到晚；我随后在群里直接发言）："]
    name_by_id = _batch_name_index(batch)
    for line, msg in enumerate(batch, start=1):
        lines.append(
            json.dumps(
                _history_message_payload(msg, line=line, name_by_id=name_by_id),
                ensure_ascii=False,
            )
        )
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
        name_by_id = _batch_name_index(batch)
        user_input = _message_history_line(msg, name_by_id=name_by_id)
        return _ToolSendRecord(
            messages=[
                Message(role="user", content=user_input),
                Message(role="assistant", content=reply_text),
            ],
            user_input=user_input,
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


def _normalise_group_entry(
    entry: ParsedAction,
    event: Event,
    batch: list[_BufferedMessage],
) -> ParsedAction | None:
    """Validate one parsed action against the current group batch.

    Returns ``None`` for entries the group dispatcher should discard:

    * ``reply`` entries pointing at a ``message_id`` we don't have in the
      buffer — we can't quote a message we never received.
    * (Hook for future scope-specific filtering.)

    A ``send`` entry is always accepted; the caller still applies the
    ``max_chars`` clip and ``max_replies`` cap.

    The unused ``event`` parameter is reserved for future per-scope
    policy (e.g. allowlists), kept in the signature so the call site
    doesn't have to change when we add it.
    """
    _ = event  # reserved
    if entry.kind == "reply":
        if not entry.message_id:
            return None
        known_ids = {m.message_id for m in batch}
        if entry.message_id not in known_ids:
            return None
    return entry
