"""Agent chat dispatcher — adapter between :class:`Router` and :class:`AgentRuntime`.

Implements :class:`linling_core.ChatDispatcher`. Responsibilities:

* Ensure the session's short-term history is populated — lazily
  restored from an optional :class:`HistoryStore` the first time a
  conversation is touched after a restart.
* Invoke the agent with that history.
* Append the new user/assistant turn to the in-memory deque.
* Fire-and-forget persist the turn via the history store.
* Wrap the reply text as a single reply :class:`Action`.

Guardrails / rate limiting / locking are all the router's job; this
module does not know about them.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog
from linling_core.events import Action, Event
from linling_core.pipeline import ledger_scope_keys
from linling_core.segments import ReplySegment, TextSegment

from linling_agent.actions_protocol import ParsedAction, parse_actions_envelope
from linling_agent.context import ContextBudget, ContextManager, SummaryStore
from linling_agent.llm import Message
from linling_agent.runtime import AgentResult, AgentRuntime

if TYPE_CHECKING:
    from linling_core.pipeline import Session
    from linling_dsl.ledger import LedgerStore

    from linling_agent.history import HistoryStore
    from linling_agent.ledger import LedgerRenderer

logger = structlog.get_logger(__name__)


# Marker inserted into the Session's ``extras``-style slot once we've
# rehydrated that session from the HistoryStore. The Session dataclass
# itself doesn't need to know anything about this; we pin the flag on a
# private attribute so re-entering the same live session short-circuits
# the KV round-trip.
_HYDRATED_FLAG = "_linling_history_hydrated"
# Independent twin for the DSL Action Ledger rehydrate path. Kept as a
# distinct attribute so a transient ledger-load failure cannot mark the
# history as hydrated (and vice-versa). Requirement 8.5.
_LEDGER_HYDRATED_FLAG = "_linling_ledger_hydrated"


class AgentChatDispatcher:
    """Invokes an agent for free-form chat messages."""

    def __init__(
        self,
        *,
        agent: AgentRuntime,
        empty_reply: str = "...",
        history_store: HistoryStore | None = None,
        ledger_store: LedgerStore | None = None,
        ledger_renderer: LedgerRenderer | None = None,
        context_budget: ContextBudget | None = None,
        max_replies: int = 3,
        max_reply_chars: int = 500,
    ) -> None:
        self._agent = agent
        self._empty_reply = empty_reply
        self._history_store = history_store
        # Optional DSL Action Ledger backing store + renderer. ``None``
        # for either disables the corresponding code path; existing
        # call sites that omit them keep their current semantics
        # exactly (Requirement 1.11 / 1.12 / 11.5).
        self._ledger_store = ledger_store
        self._ledger_renderer = ledger_renderer
        # Multi-message reply caps. The dispatcher's ``run`` parses an
        # optional ``{"actions":[...]}`` payload from the assistant
        # content; ``max_replies`` bounds how many segments we'll emit
        # per turn, ``max_reply_chars`` clips each segment. Plain-text
        # replies are unaffected — they still go out as a single
        # message regardless of these caps.
        self._max_replies = max(1, max_replies)
        self._max_reply_chars = max(1, max_reply_chars)
        self._context = (
            ContextManager(
                provider=agent.provider,
                model=agent.model,
                temperature=agent.agent_def.temperature,
                budget=context_budget,
                store=history_store if isinstance(history_store, SummaryStore) else None,
            )
            if context_budget is not None
            else None
        )

    @property
    def agent(self) -> AgentRuntime:
        """Read-only view of the underlying :class:`AgentRuntime`.

        Exposed so transports (e.g. the WebUI) can introspect the
        configured provider/model without reaching into private
        attributes — *not* a hook for direct invocation, which would
        bypass the history rehydration this dispatcher owns.
        """
        return self._agent

    @property
    def context_max_tokens(self) -> int | None:
        return self._context.max_tokens if self._context is not None else None

    async def dispatch(self, event: Event, session: Session) -> AgentResult | None:
        """Run one chat turn end-to-end and return the raw :class:`AgentResult`.

        Handles:

        * Stale-cancel-flag cleanup (the cancel event is single-shot per turn).
        * Lazy rehydration of session history from the persistent store.
        * Racing the LLM call against ``session.cancel_event`` so
          ``/cancel`` aborts cleanly without writing the truncated
          turn to history.
        * Appending the new user / assistant pair to ``session.history``.
        * Fire-and-forget persistence to the :class:`HistoryStore`.

        Returns ``None`` when the turn was cancelled before the agent
        completed (no reply was delivered, so nothing should be
        recorded). Returns the populated :class:`AgentResult` on the
        normal path.

        The router's :meth:`run` wrapper turns the return value into
        an :class:`Action` list; transports that need the raw token /
        tool-call counts (the WebUI surfaces them in audit metadata)
        can call ``dispatch`` directly.

        ``user_input`` is taken from ``event.text`` so any caller that
        synthesises an :class:`Event` (DSL, scheduler, WebUI) gets the
        same handling as the IM adapters.

        Concurrency contract: callers **must** hold ``session.lock``
        across this call. The dispatcher mutates ``session.history``
        and the persistent store; concurrent turns on the same session
        would interleave history entries and corrupt the deque order.
        The router takes care of this for IM adapters; transport
        layers (WebUI) acquire it themselves.
        """
        raw_user_input = event.text
        if not raw_user_input:
            return None

        # Clear any stale cancel flag from a prior turn. The event is a
        # single-shot signal: set by ``/cancel`` → observed here →
        # cleared for the next turn. Doing this under the session lock
        # (which the caller always holds) is race-free: ``/cancel``
        # runs outside the lock and only *sets*, never *clears*.
        session.cancel_event.clear()

        await self._maybe_rehydrate(session, event)

        history = list(session.history)

        # Requirement 1.9 / 1.10 / 1.11:render the LLM-visible ledger
        # immediately *before* the LLM call. The rendered Message is
        # transient — it is appended to the per-call ``injected``
        # list but never reaches ``session.history`` or the
        # HistoryStore. ``None`` means "nothing visible to inject"
        # (Requirement 1.12) and the LLM gets the legacy history-only
        # input.
        extra_messages: list[Message] = []
        prefix_messages: list[Message] = []
        ledger_msg = self._render_ledger(session, event)
        if ledger_msg is not None:
            extra_messages.append(ledger_msg)
        batch_system = str(event.raw.get("_linling_prompt_system") or "")
        if batch_system:
            prefix_messages.append(Message(role="system", content=batch_system))
        reserve_tokens = self._agent.agent_def.guardrails.max_tokens
        user_input = raw_user_input
        if self._context is not None:
            user_input = self._context.fit_current_input(
                raw_user_input,
                prefix_messages=prefix_messages,
                extra_messages=extra_messages,
                system_text=self._agent.agent_def.system,
                reserve_tokens=reserve_tokens,
            )
        if not user_input:
            logger.warning(
                "chat_dispatcher.input_over_budget",
                scope_id=event.scope.id,
                sender_id=event.sender.id,
            )
            return AgentResult(content=self._empty_reply)
        replacement_history: list[Message] | None = None
        skip_history = bool(event.raw.get("_linling_skip_history"))
        if self._context is not None:
            injected, replacement_history = await self._context.prepare(
                scope_id=event.scope.id,
                sender_id=event.sender.id,
                history=history,
                prefix_messages=prefix_messages,
                extra_messages=extra_messages,
                system_text=self._agent.agent_def.system,
                current_input_text=user_input,
                reserve_tokens=reserve_tokens,
                allow_compaction=not skip_history and not bool(event.raw.get("_linling_group_batch")),
            )
            injected = [*prefix_messages, *injected, *extra_messages]
        else:
            injected = list(history)
            injected = [*prefix_messages, *injected, *extra_messages]

        agent_task = asyncio.create_task(
            self._agent.invoke(
                user_input,
                event=event,
                history=injected,
                context_max_tokens=self._context.max_tokens if self._context is not None else None,
            ),
            name="agent_invoke",
        )
        cancel_task = asyncio.create_task(session.cancel_event.wait(), name="agent_cancel_wait")
        done, _pending = await asyncio.wait(
            {agent_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if cancel_task in done and agent_task not in done:
            # User said ``/cancel`` before the agent finished. Abort the
            # LLM call; do not write this turn to history — the reply
            # was never delivered, so we shouldn't remember it.
            agent_task.cancel()
            # Drain the cancelled task so its CancelledError / other
            # exceptions are observed (otherwise asyncio warns about
            # unretrieved exceptions at GC time).
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await agent_task
            logger.info(
                "chat_dispatcher.cancelled",
                scope_id=event.scope.id,
                sender_id=event.sender.id,
            )
            return None

        # Normal path — the agent completed first. If the cancel event
        # also fired in the same tick we still honor the completed
        # response: the round-trip paid, the user gets the reply,
        # history reflects reality.
        cancel_task.cancel()
        # Drain the cancelled task to keep asyncio from warning about
        # an unretrieved cancellation at GC time.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await cancel_task
        result = agent_task.result()

        if skip_history:
            return result

        if replacement_history is not None:
            session.history.clear()
            session.history.extend(replacement_history)

        # Persist both turns; the deque's maxlen bounds memory.
        session.history.append(Message(role="user", content=user_input))
        session.history.append(Message(role="assistant", content=result.content))

        if self._history_store is not None:
            # Don't block the user on IO if the KV round-trip is slow —
            # but do wait in tests (we'd have no way to assert otherwise).
            # The caller's session lock keeps this ordering sane: saves
            # from the same session never interleave.
            await self._persist(session, event)

        return result

    async def record_history(
        self,
        *,
        session: Session,
        scope_id: str,
        sender_id: str,
        user_input: str,
        assistant_output: str,
    ) -> None:
        """Append a synthetic turn without invoking the agent.

        Used by group batching: the LLM sees a structured decision
        prompt, but history should retain the selected human messages
        and the actual outbound text, not the internal JSON plan.
        """
        if not user_input and not assistant_output:
            return
        session.history.append(Message(role="user", content=user_input))
        session.history.append(Message(role="assistant", content=assistant_output))
        if self._history_store is not None:
            await self._persist_key(session, scope_id, sender_id)

    async def record_messages(
        self,
        *,
        session: Session,
        scope_id: str,
        sender_id: str,
        messages: list[Message],
    ) -> None:
        """Append already-constructed history messages without invoking the agent.

        Group batching uses this for ReAct-style records: the user
        message contains the relevant group message(s), the assistant
        message may carry a tool call, and the tool message records the
        completed external action.
        """
        if not messages:
            return
        session.history.extend(messages)
        if self._history_store is not None:
            await self._persist_key(session, scope_id, sender_id)

    async def prepare_context_history(
        self,
        *,
        session: Session,
        scope_id: str,
        sender_id: str,
        prefix_messages: list[Message] | None = None,
        extra_messages: list[Message] | None = None,
        system_text: str = "",
        current_input_text: str = "",
        reserve_tokens: int = 0,
        allow_compaction: bool = True,
        commit_replacement: bool = False,
    ) -> list[Message]:
        """Return LLM-visible history for custom dispatcher loops.

        ``commit_replacement`` lets wrappers that own a complete LLM
        loop, such as group batching, persist summary compaction even
        when they do not call :meth:`dispatch` for the current turn.
        """
        history = list(session.history)
        if self._context is None:
            return history
        visible, replacement_history = await self._context.prepare(
            scope_id=scope_id,
            sender_id=sender_id,
            history=history,
            prefix_messages=prefix_messages,
            extra_messages=extra_messages,
            system_text=system_text,
            current_input_text=current_input_text,
            reserve_tokens=reserve_tokens,
            allow_compaction=allow_compaction,
        )
        if commit_replacement and replacement_history is not None:
            session.history.clear()
            session.history.extend(replacement_history)
            if self._history_store is not None:
                await self._persist_key(session, scope_id, sender_id)
        return visible

    async def ensure_history(self, session: Session, event: Event) -> None:
        """Hydrate ``session.history`` for callers that own a custom LLM loop."""
        await self._maybe_rehydrate(session, event)

    async def ensure_history_key(self, session: Session, scope_id: str, sender_id: str) -> None:
        """Hydrate chat history for an explicit conversation key."""
        if self._history_store is None or getattr(session, _HYDRATED_FLAG, False):
            return
        await self._rehydrate_history_key(session, scope_id, sender_id)

    async def run(self, event: Event, session: Session) -> list[Action]:
        result = await self.dispatch(event, session)
        if result is None:
            return []
        text = result.content or self._empty_reply
        try:
            outcome = parse_actions_envelope(text)
        except Exception:
            # Defensive: parsing the LLM's content must never crash the
            # dispatcher — that path turns the user's reply into the
            # router's "Something went wrong" error_reply, which is far
            # worse than losing the multi-message split. Log and treat
            # the content as plain prose.
            logger.exception(
                "chat_dispatcher.actions_parse_failed",
                scope_id=event.scope.id,
                sender_id=event.sender.id,
                content_preview=text[:200],
            )
            outcome = None
        if outcome is not None and outcome.recognised:
            try:
                expanded = _expand_actions_for_dm(
                    outcome.entries,
                    event=event,
                    max_actions=self._max_replies,
                    max_chars=self._max_reply_chars,
                )
            except Exception:
                logger.exception(
                    "chat_dispatcher.actions_expand_failed",
                    scope_id=event.scope.id,
                    sender_id=event.sender.id,
                )
                expanded = None
            if expanded is not None:
                if expanded:
                    return expanded
                # Recognised envelope but every entry was filtered out
                # (empty texts, or all messages clipped to zero). The LLM
                # essentially asked for silence, but emitting nothing
                # would let the router think the dispatcher returned
                # cleanly with no work to do — same as "no_reply".
                # That's the right semantic: stay silent.
                logger.info(
                    "chat_dispatcher.actions_empty_envelope",
                    scope_id=event.scope.id,
                    sender_id=event.sender.id,
                    raw_entries=len(outcome.entries),
                )
                return []
        return [Action(kind="reply", target=event.scope, segments=[TextSegment(text=text)])]

    # ---- history plumbing -------------------------------------------

    async def _maybe_rehydrate(self, session: Session, event: Event) -> None:
        """Concurrently restore chat history and DSL ledger from KV.

        Requirement 8.5:both rehydrate paths fire under
        :func:`asyncio.gather` with ``return_exceptions=True`` so a
        failure in either does not block the other; each tracks an
        independent hydrated flag so a transient KV blip surfaces as
        empty state, not a permanently sticky "hydrated" marker.
        """
        tasks: list[asyncio.Task[None]] = []
        if self._history_store is not None and not getattr(session, _HYDRATED_FLAG, False):
            tasks.append(
                asyncio.create_task(
                    self._rehydrate_history(session, event),
                    name="chat_rehydrate_history",
                )
            )
        if self._ledger_store is not None and not getattr(
            session, _LEDGER_HYDRATED_FLAG, False
        ):
            tasks.append(
                asyncio.create_task(
                    self._rehydrate_ledger(session, event),
                    name="chat_rehydrate_ledger",
                )
            )
        if not tasks:
            return
        # Both helpers swallow their own exceptions and set the
        # hydrated flag in their finally clause; ``return_exceptions``
        # guards against the helper itself crashing in a way we
        # didn't anticipate (e.g. ``CancelledError``).
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _rehydrate_history(self, session: Session, event: Event) -> None:
        await self._rehydrate_history_key(session, event.scope.id, event.sender.id)

    async def _rehydrate_history_key(
        self,
        session: Session,
        scope_id: str,
        sender_id: str,
    ) -> None:
        assert self._history_store is not None
        try:
            restored = await self._history_store.load(scope_id, sender_id)
        except Exception:
            logger.exception("chat_dispatcher.history_load_failed")
            restored = []
        # Only rehydrate when the in-memory deque is empty; if the
        # session lived through another turn already we trust the
        # in-process copy.
        if not session.history:
            session.history.extend(restored)
        object.__setattr__(session, _HYDRATED_FLAG, True)

    async def _rehydrate_ledger(self, session: Session, event: Event) -> None:
        """Restore the DSL action ledger from persistent storage.

        Requirement 8.4 / 8.6:ledger rehydrate uses its own scope
        helper (group → ``"_group"``, dm → sender id) and must not
        block the chat path on KV failure. The flag is always set
        in the finally branch so a slow KV failure path can't trap
        the dispatcher in a permanent "loading" loop.
        """
        assert self._ledger_store is not None
        try:
            scope_id, file_id = ledger_scope_keys(event, logger=logger)
            try:
                restored = await self._ledger_store.load(scope_id, file_id)
            except Exception:
                logger.exception(
                    "chat_dispatcher.ledger_load_failed",
                    scope_id=event.scope.id,
                    sender_id=event.sender.id,
                )
                restored = []
            # Mirror the history rehydrate semantics:only seed the
            # in-memory deque when it is empty so a session that has
            # already accrued in-process events isn't double-counted.
            # Restored events arrive sorted oldest-first by
            # ``occurred_at`` (KVDslLedgerStore guarantees this); the
            # deque's ``maxlen`` enforces FIFO eviction if the load
            # exceeded ``Ledger_Maxlen``.
            if not session.dsl_events:
                for ev in restored:
                    session.dsl_events.append(ev)
        finally:
            object.__setattr__(session, _LEDGER_HYDRATED_FLAG, True)

    def _render_ledger(self, session: Session, event: Event) -> Message | None:
        """Render the LLM-visible ledger snapshot for this turn.

        Returns ``None`` when no renderer is configured, the deque is
        empty, or every event is filtered (Requirement 1.12 / 4.10 /
        11.7). Group scope flips ``include_actor`` on via the renderer's
        zero-allocation ``with_actor`` factory; DM keeps the default.
        """
        if self._ledger_renderer is None:
            return None
        if not session.dsl_events:
            return None
        # ``with_actor`` is a no-op when the requested flag matches the
        # cached renderer's, so passing the boolean directly is both
        # cheaper than a branch and identical in behaviour.
        renderer = self._ledger_renderer.with_actor(event.scope.kind == "group")
        return renderer.render(session.dsl_events)

    async def _persist(self, session: Session, event: Event) -> None:
        await self._persist_key(session, event.scope.id, event.sender.id)

    async def _persist_key(self, session: Session, scope_id: str, sender_id: str) -> None:
        assert self._history_store is not None
        try:
            await self._history_store.save(scope_id, sender_id, list(session.history))
        except Exception:
            logger.exception("chat_dispatcher.history_save_failed")

    # ---- public admin ----------------------------------------------

    async def clear_history(self, scope_id: str, sender_id: str) -> None:
        """Drop the persisted history for a conversation.

        The in-memory deque inside the (possibly still alive) Session
        is not our concern here — the router gives us fresh sessions
        via LRU eviction and TTL, so callers who need an immediate
        reset should also ``ConversationStore.drop`` the key.
        """
        if self._history_store is None:
            return
        await asyncio.wait_for(self._history_store.clear(scope_id, sender_id), timeout=2.0)

    async def clear_ledger(self, scope_id: str, file_id: str) -> None:
        """Drop the persisted DSL action ledger for one ledger scope.

        Implements the :class:`~linling_core.router.LedgerReset`
        protocol so the Router's ``/reset`` command can clear chat
        history and the ledger atomically (Requirement 7.2). Mirrors
        :meth:`clear_history`'s 2-second timeout and silent-on-no-store
        behaviour so the two paths degrade identically when a backing
        store isn't configured.
        """
        if self._ledger_store is None:
            return
        await asyncio.wait_for(self._ledger_store.clear(scope_id, file_id), timeout=2.0)


# ---------------------------------------------------------------------------
# Multi-message reply expansion (DM / WebUI side)
# ---------------------------------------------------------------------------

# When the agent's content parses as a JSON ``{"actions": [...]}`` envelope
# we expand it into multiple :class:`Action` objects so a single LLM turn can
# fan out into several outbound messages. The actual JSON parsing lives in
# :mod:`linling_agent.actions_protocol` so the group-batch dispatcher can
# share the same wire shape; this helper just decides how to shape each
# normalised :class:`ParsedAction` into an :class:`Action` for the DM /
# WebUI scope.
#
# Plain text — anything that isn't a recognised actions envelope — falls
# through to the single-message path in :meth:`AgentChatDispatcher.run`.
# Behaviour for callers/models that ignore this contract is unchanged.


def _expand_actions_for_dm(
    entries: list[ParsedAction],
    *,
    event: Event,
    max_actions: int,
    max_chars: int,
) -> list[Action]:
    """Materialise normalised actions for a DM / WebUI conversation.

    DMs are 1:1, so ``message_id`` is informational — there's no incoming
    batch to validate it against, and the OneBot adapter wraps the segment
    list identically for ``kind="reply"`` and ``kind="send"`` in the
    private-chat case. To keep the behaviour close to "the dispatcher's
    historic single-message default" we emit ``kind="reply"`` for both
    intents in DM scope. The group-batch dispatcher has its own expander
    that honors ``message_id`` and the group-vs-DM ``kind`` split.

    ``max_actions`` and ``max_chars`` mirror the group-batch
    ``max_replies`` / ``max_reply_chars`` semantics; both must be ≥ 1.
    Returns an empty list when there is nothing to send (the LLM asked
    for silence via ``{"actions":[]}`` or every entry was malformed).
    """
    if max_actions <= 0 or max_chars <= 0:
        return []
    actions: list[Action] = []
    is_group = event.scope.kind == "group"
    for entry in entries:
        if len(actions) >= max_actions:
            break
        text = entry.text[:max_chars]
        if not text:
            continue
        if entry.kind == "reply" and is_group and entry.message_id:
            # Defensive: DM dispatcher shouldn't normally see groups,
            # but if it does (custom wiring), emit a real quote-reply.
            actions.append(
                Action(
                    kind="reply",
                    target=event.scope,
                    segments=[
                        ReplySegment(message_id=entry.message_id),
                        TextSegment(text=text),
                    ],
                )
            )
            continue
        # DM ``send`` and ``reply`` collapse to the same wire shape; we
        # keep ``kind="reply"`` to mirror the historic single-message
        # default of :meth:`AgentChatDispatcher.run`.
        actions.append(
            Action(
                kind="reply" if not is_group else "send",
                target=event.scope,
                segments=[TextSegment(text=text)],
            )
        )
    return actions

