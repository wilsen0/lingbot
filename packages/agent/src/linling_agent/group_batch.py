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
from linling_core.segments import ReplySegment, TextSegment

from linling_agent.action_delay import with_random_delay_before
from linling_agent.actions_protocol import ParsedAction, parse_actions_envelope
from linling_agent.attention_probe import AttentionProbe, _ProbeBatchInput
from linling_agent.context import fit_messages_to_budget
from linling_agent.llm import Message, ToolCall, ToolSchema
from linling_agent.profile import ProfileStore

logger = structlog.get_logger(__name__)

_TOOL_READ_BATCH = "read_batch_messages"
_TOOL_REPLY_TO_MESSAGE = "reply_to_message"
_TOOL_SEND_GROUP = "send_group"
_TOOL_READ_PROFILE = "read_user_profile"
_TOOL_WRITE_PROFILE = "write_user_profile"
_MAX_TOOL_ROUNDS = 8
_MAX_READ_CALLS = 2
_MAX_READ_MESSAGES = 5
_BATCH_PREVIEW_CHARS = 80
_BATCH_PROMPT_TEXT_CHARS = 500
_BATCH_HISTORY_TEXT_CHARS = 500
_MAX_TOOL_RESULT_CHARS = 2_500


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
    # --- Expanded "I'm done replying" family. The model likes to emit
    # these as a closing acknowledgement after a precise
    # reply_to_message tool call, and they must never leak into the
    # group (would reveal the batching machinery). A few entries below
    # (好了 / 好啦 / 好嘞 / 搞定 / 完事) are mildly ambiguous with a
    # genuine one-word group reply ("好了" = "行/可以了"), so filtering
    # them carries a small false-positive risk — but per the design
    # call, leaking a meta "done" marker is the worse outcome, so we
    # swallow them. The tilde-decorated variants ("好了~", "搞定了~~~")
    # are handled by _normalize_control_text, not by separate entries.
    "好了",
    "好啦",
    "好嘞",
    "回好了",
    "回复了",
    "已经回复了",
    "已经回复",
    "回复完毕",
    "回复完毕了",
    "搞定",
    "搞定了",
    "完事",
    "完事了",
    "弄好了",
    "答好了",
    "回答好了",
    "回答完了",
    "回答完成",
})

# Trailing decorations the model tacks onto control tokens. Tildes
# (ASCII ~, fullwidth ～ U+FF5E, wave dash 〜 U+301C) are by far the
# most common ("好了~"); we strip a trailing run of them plus whitespace
# so every token matches its decorated variant without table doubling.
_TRAILING_DECORATIONS = "~～〜 \t\r\n"

# Sentence-final mood particles that are colloquial contractions of a
# trailing 了 (啦 = 了+啊, 咯/喽 = 了+喔, 嘞 ≈ 了). The model loves
# these ("好啦", "回复好啦", "搞定咯"). Folding a trailing particle back
# to 了 collapses the whole 了/啦/咯/喽/嘞 spelling family onto the
# canonical 了-form entries already in the tables, so we don't have to
# enumerate every word twice — and future 了-words are covered for free.
_FINAL_PARTICLES_TO_LE = "啦咯喽嘞"


def _normalize_control_text(content: str) -> str:
    if not content:
        return ""
    text = content.strip().strip("\"'`“”‘’").strip()
    # Strip trailing tildes/whitespace ("好了~", "搞定了 ~~~", "done～")
    # so the tables don't need a separate entry per decoration.
    text = text.rstrip(_TRAILING_DECORATIONS)
    # Fold a trailing mood particle to 了 ("好啦"->"好了",
    # "回复好啦"->"回复好了"), then collapse a doubled trailing 了 so
    # the 了+啦 emphatic form ("好了啦"->"好了了"->"好了") also lands on
    # the canonical entry.
    if text and text[-1] in _FINAL_PARTICLES_TO_LE:
        text = text[:-1] + "了"
        if text.endswith("了了"):
            text = text[:-1]
    return text.lower()


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
            "已经回复了",
            "已经回复完了",
            "回复好了",
            "回复完成",
            "回复完了",
            "回复完毕",
            "回答好了",
            "回答完了",
            "回答完成",
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
    multi_reply_delay_min_s: float = 0.0
    multi_reply_delay_max_s: float = 0.0
    daily_summary_enabled: bool = False
    daily_summary_keep_recent_turns: int = 2

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
    # User ids this message @-mentioned that are NOT the bot and not
    # ``@all``. ``plain_text`` strips every AtSegment, so without this
    # the directional intent of "@小红 你说得对" is lost — the text the
    # LLM sees becomes a floating "你说得对" that reads like it could be
    # aimed at the bot. We surface these (resolved to names where the
    # target also appears in the batch, else a neutral "某人" marker so
    # raw QQ ids never reach the LLM) via the ``at`` field in the
    # candidate JSON. Default ``()`` keeps the field optional for any
    # direct ``_BufferedMessage`` construction.
    at_user_ids: tuple[str, ...] = ()


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
        # mentions_bot, question particle, bot_name in text), skip the
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
        envelope = parse_actions_envelope(content)
        if envelope.recognised:
            # Recognised the actions wire shape — there's an action iff
            # at least one entry survived schema normalisation. Empty
            # envelopes (``{"actions": []}``) count as "model said
            # nothing to do".
            return any(_normalise_group_entry(entry, event, batch) is not None
                       for entry in envelope.entries)
        return True

    def _actions_from_envelope(
        self,
        content: str,
        event: Event,
        batch: list[_BufferedMessage],
    ) -> list[Action] | None:
        """Parse an actions envelope and shape it for the current group.

        Returns ``None`` when the content is plain prose (caller falls back
        to treating it as a single ``send_group``). Returns a (possibly
        empty) list when the envelope was recognised; an empty list means
        the LLM asked for nothing to be sent.
        """
        envelope = parse_actions_envelope(content)
        if not envelope.recognised:
            return None
        actions: list[Action] = []
        for entry in envelope.entries:
            if len(actions) >= self._cfg.max_replies:
                break
            shaped = _normalise_group_entry(entry, event, batch)
            if shaped is None:
                continue
            text = shaped.text[: self._cfg.max_reply_chars]
            if not text:
                continue
            if shaped.kind == "reply":
                actions.append(
                    Action(
                        kind="reply",
                        target=event.scope,
                        segments=[
                            ReplySegment(message_id=shaped.message_id),
                            TextSegment(text=text),
                        ],
                    )
                )
            else:
                actions.append(
                    Action(
                        kind="send",
                        target=event.scope,
                        segments=[TextSegment(text=text)],
                    )
                )
        return actions

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
        envelope_actions = self._actions_from_envelope(text, event, batch)
        if envelope_actions is not None:
            # Recognised actions envelope — honor it verbatim, including
            # the empty list (which means "send nothing"). Do NOT fall
            # through to the plain-text branch in that case, otherwise
            # the raw JSON string would leak into a ``send_group``.
            return await self._send_actions_and_records(
                envelope_actions,
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
        action = self._with_multi_reply_delay(action, sent_count=sent_count)
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
            text=_clip_text(event.text.strip(), self._cfg.max_chars),
            timestamp=_format_time(event.time),
            sent_at=_event_sort_timestamp(event.time),
            received_seq=next(self._message_seq),
            mentions_bot=_mentions_bot(event, self._cfg.bot_names),
            reply_to_bot=_reply_to_bot(event),
            at_user_ids=_other_at_user_ids(event),
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
            "候选消息（按发送时间从早到晚；sender_id 稳定，sender_name 昵称；mentions_bot=找你；at_others=@别人非你）："
        ]
        name_by_id = _batch_name_index(batch)
        for m in batch:
            entry = {
                "message_id": m.message_id,
                "sender_id": m.sender_id,
                "sender_name": m.sender_name,
                "time": m.timestamp,
                "text": m.text,
                "mentions_bot": m.mentions_bot,
                "reply_to_bot": m.reply_to_bot,
            }
            at_others = _render_at_targets(m.at_user_ids, name_by_id)
            if at_others:
                entry["at_others"] = at_others
            lines.append(json.dumps(entry, ensure_ascii=False))
        return "\n".join(lines)

    def _build_system_prompt(self) -> str:
        return "\n".join(
            [
                "你现在在群聊里，大家七嘴八舌地说着话。",
                "你不需要每条都回——像平时在群里一样，看到感兴趣的、跟你有关的、或者有人找你说话的，再开口就好。",
                "觉得没什么好说的就安静待着，不用勉强。",
                "上下文里的历史 user 记录会写明你过去是在群里直接说，还是引用回复了谁的哪条消息；assistant 记录是你当时实际发出的正文。",
                "",
                "回复格式（按情况选一种）：",
                "A. 一句话就够 → 直接输出一条文字，不要任何前缀，不要空行分段。",
                "B. 只要想一次连发两条或更多短消息 → 必须输出严格 JSON：",
                "   {\"actions\":[{\"type\":\"send_group\",\"text\":\"先这一句\"},{\"type\":\"reply_to_message\",\"message_id\":\"xxx\",\"text\":\"再补一句\"}]}",
                "   按数组顺序逐条发送。type 支持 send_group（直接发）和 reply_to_message（引用回复，需要 message_id）。",
                "   JSON 外不要掺杂文字、解释、Markdown 围栏或空行。",
                "C. 想引用某条消息回复 → 也可以用 B 里的 reply_to_message。",
                "D. 如果整批都不用回 → 只输出 no_reply，不要解释。",
                "E. 如果已经回复完了 → 输出 done / 回复好了 / 回复完成，不要再补别的内容。",
                f"最多说 {self._cfg.max_replies} 句；多条必须走 actions JSON。",
            ]
        )

    def _build_tool_system_prompt(self) -> str:
        return "\n".join(
            [
                "你现在在群聊里，大家七嘴八舌地说着话。",
                "你不需要每条都回——像平时在群里一样，看到感兴趣的、跟你有关的、或者有人找你说话的，再开口就好。",
                "上下文里的历史 user 记录会写明你过去是在群里直接说，还是引用回复了谁的哪条消息；assistant 记录是你当时实际发出的正文。",
                "",
                "回复方式（按情况选一种或自由组合）：",
                "1. 想直接在群里说一句话 → 直接输出一条文字，不要任何前缀，不要空行分段（不要写'回复 XXX:'之类的，那是历史记录格式不是发送格式）。",
                "2. 想引用某条消息回复（@对方+引用框）→ 调 reply_to_message 工具。",
                "3. 候选里的 text_truncated=true 或看不清上下文时 → 调 read_batch_messages 工具看原文。",
                "4. 只要想一次连发两条或更多短消息（不打太长一段、也不想拆成多轮工具调用）→ 必须输出严格 JSON：",
                "   {\"actions\":[{\"type\":\"send_group\",\"text\":\"先这一句\"},{\"type\":\"reply_to_message\",\"message_id\":\"xxx\",\"text\":\"再补一句\"}]}",
                "   每个元素就是单独一条消息，按数组顺序发出去。type 支持 send_group 和 reply_to_message，与上面的工具一一对应。",
                "   JSON 外不要掺杂文字、解释、Markdown 围栏或空行。",
                "5. 如果已经通过 reply_to_message 精确回复完了 → 输出 done / 回复好了 / 回复完成。",
                "6. 如果整批都不用回 → 输出 no_reply。",
                "JSON 与工具调用不要混用：同一轮里，要么全部用 tool call，要么整段输出就是一个 JSON。",
                "",
                f"最多说 {self._cfg.max_replies} 句；多条必须走 actions JSON 或逐条 tool call。",
            ]
        )

    def _build_tool_prompt(self, batch: list[_BufferedMessage]) -> str:
        lines = [
            "群聊候选消息如下，按发送时间从早到晚。sender_id 稳定，sender_name 昵称；mentions_bot=找你；at_others=@别人非你；先判断是否值得回复；text_truncated=true 可用 line/message_id 读原文。",
            "候选索引：",
        ]
        name_by_id = _batch_name_index(batch)
        for line, msg in enumerate(batch, start=1):
            clipped_text = _clip_text(msg.text, _BATCH_PROMPT_TEXT_CHARS)
            entry = {
                "line": line,
                "message_id": msg.message_id,
                "time": msg.timestamp,
                "sender_id": msg.sender_id,
                "sender_name": msg.sender_name,
                "text": clipped_text,
                "text_truncated": len(clipped_text) < len(msg.text),
                "mentions_bot": msg.mentions_bot,
                "reply_to_bot": msg.reply_to_bot,
            }
            at_others = _render_at_targets(msg.at_user_ids, name_by_id)
            if at_others:
                # Direction marker: this message @-mentioned other group
                # members (not you). Resolved to names when the target
                # also spoke in this batch, else "某人". Helps avoid
                # treating an addressed-to-someone-else line as if it
                # were aimed at the bot.
                entry["at_others"] = at_others
            lines.append(json.dumps(entry, ensure_ascii=False))
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
        if getattr(seg, "kind", "") == "at" and getattr(seg, "user_id", "") in {*bot_ids, "all"}:
            return True
    text = event.text
    return any(name and name in text for name in bot_names)


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
        ToolSchema(
            name=_TOOL_SEND_GROUP,
            description="直接在当前群里发送一条简短消息，不引用任何候选消息。适合顺着群聊接一句普通话。",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要发送的简短群消息内容。"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        ),
    ]


def _profile_tool_schemas() -> list[ToolSchema]:
    """Per-user profile tools, available to the main loop (not the probe)."""
    return [
        ToolSchema(
            name=_TOOL_READ_PROFILE,
            description="查阅某个用户(按 QQ 号)的长期记忆画像。想了解群里某人是谁、之前聊过什么时调用。",
            parameters={
                "type": "object",
                "properties": {
                    "qq": {"type": "string", "description": "目标用户的 QQ 号(候选消息里的 sender_id)。"},
                },
                "required": ["qq"],
                "additionalProperties": False,
            },
        ),
        ToolSchema(
            name=_TOOL_WRITE_PROFILE,
            description="全量重写某个用户(按 QQ 号)的长期记忆画像。了解到值得长期记住的事时调用；每次都要先读旧画像再给完整新版本(≤400字，只记长期稳定的事实/偏好/关系/承诺)。",
            parameters={
                "type": "object",
                "properties": {
                    "qq": {"type": "string", "description": "目标用户的 QQ 号。"},
                    "profile": {"type": "string", "description": "完整的新画像正文。"},
                    "name": {"type": "string", "description": "该用户的昵称(可选)。"},
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
