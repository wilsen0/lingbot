"""``_ScopeGatedChatDispatcher`` must forward ``/reset`` calls to its inner.

Without these forwarders the wrapper silently swallowed ``clear_history``
and ``clear_ledger`` invocations from :class:`linling_core.router.Router`,
which means a gated deployment would lose persistent reset semantics for
both chat history and the DSL action ledger on every ``/reset``.

These tests pin the forwarder behaviour so a future refactor can't
accidentally drop it again.
"""

from __future__ import annotations

from typing import Any

from linling_cli.bootstrap import _ScopeGatedChatDispatcher
from linling_core.events import Action, Event, Scope, User
from linling_core.pipeline import ConversationKey, Session
from linling_core.router import HistoryReset, LedgerReset
from linling_core.segments import TextSegment


class _RecordingInner:
    """Inner dispatcher that records every ``run``/``clear_*`` call."""

    def __init__(self) -> None:
        self.runs: list[Event] = []
        self.history_clears: list[tuple[str, str]] = []
        self.ledger_clears: list[tuple[str, str]] = []
        self.sinks: list[Any] = []
        self.stopped = False

    async def run(self, event: Event, session: Session) -> list[Action]:
        self.runs.append(event)
        return [
            Action(
                kind="reply",
                target=event.scope,
                segments=[TextSegment(text="ok")],
            )
        ]

    async def clear_history(self, scope_id: str, sender_id: str) -> None:
        self.history_clears.append((scope_id, sender_id))

    async def clear_ledger(self, scope_id: str, file_id: str) -> None:
        self.ledger_clears.append((scope_id, file_id))

    def set_action_sink(self, sink: Any) -> None:
        self.sinks.append(sink)

    async def stop(self) -> None:
        self.stopped = True


def _gate(inner: Any, *, allowed: frozenset[str] = frozenset({"g1"})) -> _ScopeGatedChatDispatcher:
    return _ScopeGatedChatDispatcher(
        inner=inner,
        allowed=allowed,
        fallback_text="denied",
    )


# ---------------------------------------------------------------------------
# clear_history forwarding
# ---------------------------------------------------------------------------


async def test_gate_forwards_clear_history_to_inner() -> None:
    inner = _RecordingInner()
    gate = _gate(inner)
    await gate.clear_history("g1", "u1")
    assert inner.history_clears == [("g1", "u1")]


async def test_gate_satisfies_history_reset_protocol() -> None:
    """The runtime-checkable Protocol picks up ``clear_history``."""
    gate = _gate(_RecordingInner())
    assert isinstance(gate, HistoryReset)


async def test_gate_clear_history_silent_when_inner_missing() -> None:
    """An inner without ``clear_history`` is a no-op rather than AttributeError."""

    class _Bare:
        async def run(self, event: Event, session: Session) -> list[Action]:
            return []

    gate = _gate(_Bare())
    # Must not raise.
    await gate.clear_history("g1", "u1")


# ---------------------------------------------------------------------------
# clear_ledger forwarding
# ---------------------------------------------------------------------------


async def test_gate_forwards_clear_ledger_to_inner() -> None:
    inner = _RecordingInner()
    gate = _gate(inner)
    await gate.clear_ledger("g1", "_group")
    assert inner.ledger_clears == [("g1", "_group")]


async def test_gate_satisfies_ledger_reset_protocol() -> None:
    """The runtime-checkable Protocol picks up ``clear_ledger``."""
    gate = _gate(_RecordingInner())
    assert isinstance(gate, LedgerReset)


async def test_gate_clear_ledger_silent_when_inner_missing() -> None:
    """An inner without ``clear_ledger`` is a no-op rather than AttributeError."""

    class _Bare:
        async def run(self, event: Event, session: Session) -> list[Action]:
            return []

    gate = _gate(_Bare())
    await gate.clear_ledger("g1", "_group")


async def test_gate_forwards_action_sink_to_inner() -> None:
    inner = _RecordingInner()
    gate = _gate(inner)
    sink = object()

    gate.set_action_sink(sink)

    assert inner.sinks == [sink]


async def test_gate_forwards_stop_to_inner() -> None:
    inner = _RecordingInner()
    gate = _gate(inner)

    await gate.stop()

    assert inner.stopped is True


# ---------------------------------------------------------------------------
# Reset forwarders work regardless of allowlist membership
# ---------------------------------------------------------------------------


async def test_clear_calls_bypass_scope_gate() -> None:
    """A scope NOT on the allowlist can still ``/reset`` its session.

    Otherwise users in a denied scope would be unable to reset their
    own state, even though the gate only restricts *new* chat
    dispatches.
    """
    inner = _RecordingInner()
    gate = _gate(inner, allowed=frozenset({"only-this-one"}))
    # ``g1`` is not on the allowlist:
    await gate.clear_history("g1", "u1")
    await gate.clear_ledger("g1", "_group")
    assert inner.history_clears == [("g1", "u1")]
    assert inner.ledger_clears == [("g1", "_group")]


# ---------------------------------------------------------------------------
# kind-aware allow policy: DMs always allowed, groups gated
# ---------------------------------------------------------------------------


def _event(*, scope_kind: str, scope_id: str) -> Event:
    return Event(
        id=f"e:{scope_kind}:{scope_id}",
        platform="t",
        bot_id="b",
        scope=Scope(kind=scope_kind, id=scope_id, platform="t"),
        sender=User(id="u1", platform="t"),
        kind="message",
        segments=[TextSegment(text="hi")],
    )


async def test_gate_allows_dm_regardless_of_allowlist() -> None:
    """DM scopes bypass the group-chat allowlist.

    Private chats are 1:1 conversations the operator opted into by
    messaging the bot directly. The allowlist is for group blast
    radius, not per-user opt-in. A DM event therefore reaches the
    inner dispatcher even when its id is not on the list.
    """
    inner = _RecordingInner()
    gate = _gate(inner, allowed=frozenset({"only-this-group"}))
    session = Session(
        key=ConversationKey(bot_id="b", scope_id="any-dm", sender_id="u1")
    )
    actions = await gate.run(_event(scope_kind="dm", scope_id="any-dm"), session)
    # Inner ran (recorded the event) and produced its standard reply.
    assert len(inner.runs) == 1
    assert actions and actions[0].kind == "reply"


async def test_gate_denies_group_not_on_allowlist() -> None:
    """A group scope outside the allowlist gets the static fallback reply."""
    inner = _RecordingInner()
    gate = _gate(inner, allowed=frozenset({"allowed-group"}))
    session = Session(
        key=ConversationKey(bot_id="b", scope_id="other-group", sender_id="u1")
    )
    actions = await gate.run(_event(scope_kind="group", scope_id="other-group"), session)
    # Inner did NOT run; the gate produced a fallback reply.
    assert inner.runs == []
    assert actions and actions[0].kind == "reply"
    text = actions[0].segments[0].text
    assert text == "denied"


async def test_gate_allows_group_on_allowlist() -> None:
    """A group scope on the allowlist reaches the inner dispatcher."""
    inner = _RecordingInner()
    gate = _gate(inner, allowed=frozenset({"allowed-group"}))
    session = Session(
        key=ConversationKey(bot_id="b", scope_id="allowed-group", sender_id="u1")
    )
    await gate.run(_event(scope_kind="group", scope_id="allowed-group"), session)
    assert len(inner.runs) == 1


async def test_gate_dispatch_returns_inner_result_for_dm() -> None:
    """``dispatch`` (WebUI path) also bypasses the gate for DMs."""
    from linling_agent.runtime import AgentResult

    class _DispatchInner(_RecordingInner):
        async def dispatch(self, event: Event, session: Session) -> AgentResult:
            return AgentResult(content="real-reply", tool_calls_made=0, total_tokens=0)

    inner = _DispatchInner()
    gate = _gate(inner, allowed=frozenset({"only-this-group"}))
    session = Session(
        key=ConversationKey(bot_id="b", scope_id="any-dm", sender_id="u1")
    )
    result = await gate.dispatch(_event(scope_kind="dm", scope_id="any-dm"), session)
    assert isinstance(result, AgentResult)
    assert result.content == "real-reply"


async def test_gate_dispatch_returns_fallback_for_denied_group() -> None:
    """``dispatch`` returns a synthesised AgentResult on group deny."""
    from linling_agent.runtime import AgentResult

    inner = _RecordingInner()
    gate = _gate(inner, allowed=frozenset({"allowed-group"}))
    session = Session(
        key=ConversationKey(bot_id="b", scope_id="other-group", sender_id="u1")
    )
    result = await gate.dispatch(_event(scope_kind="group", scope_id="other-group"), session)
    assert isinstance(result, AgentResult)
    assert result.content == "denied"
    assert result.tool_calls_made == 0
