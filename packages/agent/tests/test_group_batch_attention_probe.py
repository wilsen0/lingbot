"""Dispatcher-integration and property tests for the attention probe.

Mirrors the harness shape from ``test_group_batch.py`` (``_Inner``,
``_event``, ``_wait_for``) but routes through a dispatcher with the
``probe=...`` kwarg and the new ``attention_probe_enabled=True``
config flag. The probe itself is always a hand-rolled stub so we
never go near the network.

Each property test docstring carries the
``Feature: lightweight-attention-probe, Property N`` tag so failures
map back to the design document by name.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import structlog
from hypothesis import HealthCheck, given, settings, strategies as st

from linling_agent.attention_probe import AttentionProbe, _ProbeBatchInput
from linling_agent.errors import LLMAuthError, LLMError, LLMRateLimitError
from linling_agent.group_batch import GroupBatchChatDispatcher, GroupBatchConfig
from linling_agent.runtime import AgentResult
from linling_core.events import Action, Event, Scope, User
from linling_core.pipeline import ConversationKey, ConversationStore, Session
from linling_core.segments import TextSegment, at, reply


# ---------------------------------------------------------------------------
# Fakes — local copies of the harness shapes used in test_group_batch.py
# ---------------------------------------------------------------------------


class _Inner:
    """Stub inner dispatcher. Records every dispatch call."""

    def __init__(self, content: str = '{"actions":[]}') -> None:
        self.content = content
        self.calls: list[str] = []
        self.events: list[Event] = []
        self.recorded: list[tuple[str, str, str]] = []

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
        return None


class _ProbeStub:
    """Replacement for :class:`AttentionProbe` that does not hit a network.

    Each instance carries a verdict policy (``always_true`` /
    ``always_false`` / a callable / a queued sequence) plus call /
    aclose counters so tests can assert the exact invocation count.
    """

    def __init__(
        self,
        verdict: bool | BaseException | Callable[[list[_ProbeBatchInput]], bool | BaseException],
    ) -> None:
        self._verdict_spec = verdict
        self.call_count = 0
        self.last_batch: list[_ProbeBatchInput] | None = None
        self.last_scope: str | None = None
        self.aclose_count = 0
        self._block: asyncio.Event | None = None

    def block_until(self, event: asyncio.Event) -> None:
        """Make the next :meth:`judge` call wait on ``event`` before returning."""
        self._block = event

    async def judge(
        self, batch: list[_ProbeBatchInput], *, scope_id: str
    ) -> bool:
        self.call_count += 1
        self.last_batch = list(batch)
        self.last_scope = scope_id
        if self._block is not None:
            blocker = self._block
            self._block = None
            await blocker.wait()
        spec = self._verdict_spec
        if callable(spec):
            outcome = spec(batch)
        else:
            outcome = spec
        if isinstance(outcome, BaseException):
            raise outcome
        return bool(outcome)

    async def aclose(self) -> None:
        self.aclose_count += 1


def _event(
    text: str,
    *,
    eid: str = "m1",
    sender: str = "u1",
    scope_id: str = "g1",
) -> Event:
    return Event(
        id=eid,
        platform="test",
        bot_id="bot1",
        scope=Scope(kind="group", id=scope_id, platform="test"),
        sender=User(id=sender, platform="test", display_name=sender),
        segments=[TextSegment(text=text)],
    )


async def _wait_for(condition: Callable[[], bool], *, timeout: float = 1.0) -> None:
    """Poll ``condition`` until true or until ``timeout`` elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def _make_dispatcher(
    *,
    inner: _Inner,
    probe: _ProbeStub | None,
    require_attention: bool = True,
    attention_probe_enabled: bool = True,
    window_s: float = 0.0,
    max_hold_s: float = 0.5,
    bot_names: tuple[str, ...] = ("苏苏",),
) -> GroupBatchChatDispatcher:
    return GroupBatchChatDispatcher(
        inner=inner,
        config=GroupBatchConfig(
            enabled=True,
            window_s=window_s,
            require_attention=require_attention,
            max_hold_s=max_hold_s,
            attention_probe_enabled=attention_probe_enabled,
            bot_names=bot_names,
        ),
        probe=probe,  # type: ignore[arg-type]
    )


async def _new_session() -> Session:
    store = ConversationStore(rate_per_second=100, burst=100)
    return await store.get_or_create(ConversationKey("bot1", "g1", "u1"))


# ---------------------------------------------------------------------------
# Property 1: rule fired → no probe
# ---------------------------------------------------------------------------


_RULE_TRIGGER_KINDS = ("mention", "reply", "name", "question")


def _make_rule_event(kind: str, text: str, *, eid: str) -> Event:
    """Construct a group event that the rule-based detector flags."""
    event = _event(text, eid=eid)
    if kind == "mention":
        event.segments.insert(0, at("bot1"))
    elif kind == "reply":
        event.segments.insert(0, reply("quoted"))
    elif kind == "name":
        # Inject the bot name into the event text by rebuilding the
        # text segment. ``bot_names=("苏苏",)`` is configured below.
        event.segments[-1] = TextSegment(text=f"苏苏 {text}")
    elif kind == "question":
        event.segments[-1] = TextSegment(text=f"{text}?")
    else:
        raise AssertionError(f"unknown rule kind: {kind!r}")
    return event


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    rule_kind=st.sampled_from(_RULE_TRIGGER_KINDS),
    text=st.text(min_size=1, max_size=20, alphabet=st.characters(min_codepoint=32, max_codepoint=0x4E00)),
)
async def test_rule_fired_suppresses_probe(rule_kind: str, text: str) -> None:
    """Feature: lightweight-attention-probe, Property 1: rule fired → no probe.

    For any batch where the rule-based detector fires, the probe
    is never invoked.
    """
    inner = _Inner(content='{"actions":[]}')
    spy = _ProbeStub(verdict=AssertionError("probe must not be called"))
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=0.3)
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    event = _make_rule_event(rule_kind, text, eid="m1")
    await dispatcher.run(event, session)

    # Wait for the rule-driven flush to complete (window_s=0 + rule
    # match → flush_ready immediately on next iteration).
    await _wait_for(lambda: inner.calls != [])
    assert spy.call_count == 0
    await dispatcher.stop()


# ---------------------------------------------------------------------------
# Property 2: not configured → no probe
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    has_probe=st.booleans(),
    flag_enabled=st.booleans(),
    require_attention=st.booleans(),
)
async def test_not_configured_suppresses_probe(
    has_probe: bool, flag_enabled: bool, require_attention: bool
) -> None:
    """Feature: lightweight-attention-probe, Property 2: not configured → no probe.

    The probe is invoked only when the dispatcher has a probe
    instance, the config flag is on, and ``require_attention`` is on.
    Every other configuration leaves the probe untouched.
    """
    inner = _Inner(content='{"actions":[]}')
    spy = _ProbeStub(verdict=False)
    dispatcher = _make_dispatcher(
        inner=inner,
        probe=spy if has_probe else None,
        attention_probe_enabled=flag_enabled,
        require_attention=require_attention,
        max_hold_s=0.15,
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    # Plain text — no rule trigger.
    await dispatcher.run(_event("普通闲聊", eid="m1"), session)
    # Wait long enough that any drop / flush has completed.
    await asyncio.sleep(0.25)

    expected_eligible = has_probe and flag_enabled and require_attention
    if expected_eligible:
        assert spy.call_count == 1
    else:
        assert spy.call_count == 0
    await dispatcher.stop()


# ---------------------------------------------------------------------------
# Property 3: verdict false → no main LLM
# ---------------------------------------------------------------------------


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    text=st.text(min_size=1, max_size=40, alphabet=st.characters(min_codepoint=32, max_codepoint=0x4E00, blacklist_characters="?？"))
)
async def test_probe_false_does_not_invoke_main_llm(text: str) -> None:
    """Feature: lightweight-attention-probe, Property 3: verdict false → no main LLM.

    A negative probe verdict on a non-rule batch leaves the inner
    dispatcher untouched and lets the existing drop path run to
    completion.
    """
    inner = _Inner(content='{"actions":[{"type":"send_group","text":"不该发"}]}')
    spy = _ProbeStub(verdict=False)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=0.15)
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    # Use literal text without question marks so the rule-based
    # detector cannot fire.
    safe_text = text.replace("苏", "X")
    await dispatcher.run(_event(safe_text or "x", eid="m1"), session)
    await asyncio.sleep(0.25)

    assert spy.call_count == 1
    assert inner.calls == []
    assert sent == []
    await dispatcher.stop()


# ---------------------------------------------------------------------------
# Property 4: verdict true → main LLM exactly once
# ---------------------------------------------------------------------------


async def test_probe_true_invokes_main_llm_exactly_once() -> None:
    """Feature: lightweight-attention-probe, Property 4: verdict true → main LLM exactly once."""
    inner = _Inner(
        content=json.dumps(
            {
                "actions": [
                    {"type": "send_group", "text": "你好"}
                ]
            },
            ensure_ascii=False,
        )
    )
    spy = _ProbeStub(verdict=True)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=2.0)
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    await dispatcher.run(_event("非常普通的群闲聊", eid="m42"), session)

    await _wait_for(lambda: len(sent) == 1, timeout=2.0)
    assert spy.call_count == 1
    assert len(inner.calls) == 1
    # The probed batch's message_id must appear in the dispatched
    # prompt so we know the same batch was forwarded.
    assert "m42" in inner.calls[0]
    await dispatcher.stop()


# ---------------------------------------------------------------------------
# Property 5: at most one probe call per Batch_Lifecycle
# ---------------------------------------------------------------------------


async def test_probe_runs_at_most_once_per_lifecycle() -> None:
    """Feature: lightweight-attention-probe, Property 5: one call per Batch_Lifecycle.

    Multiple flush iterations within a single Batch_Lifecycle
    re-evaluate the eligibility predicate but ``attention_probed``
    short-circuits every iteration after the first.
    """
    inner = _Inner(content='{"actions":[]}')
    spy = _ProbeStub(verdict=False)
    dispatcher = _make_dispatcher(
        inner=inner,
        probe=spy,
        max_hold_s=0.4,
    )
    dispatcher.set_action_sink(lambda a: None)
    session = await _new_session()

    # Ingest a steady stream over the lifecycle. Each new message
    # wakes the flush loop, which re-evaluates eligibility — the
    # probe must still run at most once for the batch.
    for i in range(8):
        await dispatcher.run(_event(f"chat-{i}", eid=f"m{i}"), session)
        await asyncio.sleep(0.02)

    # Wait past max_hold_s so the lifecycle ends.
    await asyncio.sleep(0.5)
    assert spy.call_count <= 1
    await dispatcher.stop()


async def test_probe_runs_again_after_reset_history_clear() -> None:
    """A new Batch_Lifecycle (post-clear_history) is eligible for a fresh probe call."""
    inner = _Inner(content='{"actions":[]}')
    spy = _ProbeStub(verdict=False)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=0.2)
    dispatcher.set_action_sink(lambda a: None)
    session = await _new_session()

    await dispatcher.run(_event("first batch", eid="m1"), session)
    await asyncio.sleep(0.3)  # let lifecycle 1 drop
    assert spy.call_count == 1

    # New lifecycle.
    await dispatcher.run(_event("second batch", eid="m2"), session)
    await asyncio.sleep(0.3)
    assert spy.call_count == 2
    await dispatcher.stop()


# ---------------------------------------------------------------------------
# Property 6: failures contained
# ---------------------------------------------------------------------------


_FAILURE_FACTORIES: list[Callable[[], BaseException]] = [
    lambda: httpx.TimeoutException("slow"),
    lambda: httpx.ConnectError("dns"),
    lambda: LLMAuthError("401"),
    lambda: LLMRateLimitError("429"),
    lambda: LLMError("HTTP 500: kaboom"),
    lambda: ValueError("bad json"),
    lambda: RuntimeError("surprise"),
]


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(failure_idx=st.integers(min_value=0, max_value=len(_FAILURE_FACTORIES) - 1))
async def test_failures_are_contained(failure_idx: int) -> None:
    """Feature: lightweight-attention-probe, Property 6: failures contained.

    Any non-:class:`asyncio.CancelledError` exception raised by the
    probe is converted into ``verdict=False`` plus one warning log
    record. The flush task remains alive and the inner dispatcher
    is never invoked.

    The dispatcher wraps :meth:`judge` in its own ``try/except
    Exception`` for defence-in-depth, which means even bare
    :class:`RuntimeError` (a deliberately uncategorised failure
    here, simulating a probe-side bug rather than a probe-internal
    failure) still routes to the drop path without crashing.
    """
    exc_factory = _FAILURE_FACTORIES[failure_idx]
    inner = _Inner(content='{"actions":[{"type":"send_group","text":"不该发"}]}')

    def raiser(_batch: list[_ProbeBatchInput]) -> bool:
        raise exc_factory()

    spy = _ProbeStub(verdict=raiser)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=0.2)
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    # The probe stub raises directly (not through the real probe's
    # try/except). The dispatcher's own except clause catches it.
    await dispatcher.run(_event("普通闲聊", eid="m1"), session)
    await asyncio.sleep(0.3)

    assert spy.call_count == 1
    assert inner.calls == []
    assert sent == []
    await dispatcher.stop()


# ---------------------------------------------------------------------------
# Property 9: auth error not sticky
# ---------------------------------------------------------------------------


async def test_auth_error_not_sticky_across_batches() -> None:
    """Feature: lightweight-attention-probe, Property 9: auth-error not sticky.

    An :class:`LLMAuthError` raised on Batch_Lifecycle ``k`` does
    not prevent Batch_Lifecycle ``k+1`` from issuing its own probe
    call. The probe is reconsidered for every fresh lifecycle.
    """
    inner = _Inner(content='{"actions":[]}')

    counter = {"n": 0}

    def policy(_batch: list[_ProbeBatchInput]) -> bool | BaseException:
        counter["n"] += 1
        if counter["n"] == 1:
            return LLMAuthError("first call fails")
        return False

    spy = _ProbeStub(verdict=policy)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=0.15)
    dispatcher.set_action_sink(lambda a: None)
    session = await _new_session()

    await dispatcher.run(_event("batch-1", eid="m1"), session)
    await asyncio.sleep(0.25)
    assert spy.call_count == 1

    await dispatcher.run(_event("batch-2", eid="m2"), session)
    await asyncio.sleep(0.25)
    assert spy.call_count == 2
    await dispatcher.stop()


# ---------------------------------------------------------------------------
# Integration smoke tests (R7 / R8 / R9 / R12)
# ---------------------------------------------------------------------------


async def test_probe_yes_routes_to_main_llm_and_emits_action() -> None:
    inner = _Inner(
        content=json.dumps(
            {"actions": [{"type": "send_group", "text": "可以"}]},
            ensure_ascii=False,
        )
    )
    spy = _ProbeStub(verdict=True)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=2.0)
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    await dispatcher.run(_event("普通闲聊", eid="m1"), session)
    await _wait_for(lambda: len(sent) == 1, timeout=2.0)

    assert sent[0].kind == "send"
    assert spy.call_count == 1
    await dispatcher.stop()


async def test_probe_no_drops_batch() -> None:
    inner = _Inner(content='{"actions":[{"type":"send_group","text":"不该发"}]}')
    spy = _ProbeStub(verdict=False)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=0.2)
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    await dispatcher.run(_event("普通闲聊", eid="m1"), session)
    await asyncio.sleep(0.3)

    assert spy.call_count == 1
    assert inner.calls == []
    assert sent == []
    await dispatcher.stop()


async def test_probe_disabled_falls_back_to_legacy_drop_path() -> None:
    """When ``probe=None`` and ``require_attention=True``, dispatcher
    behaviour is identical to the existing
    ``test_group_batch_drops_uninteresting_when_required`` scenario.
    """
    inner = _Inner(content='{"actions":[{"type":"send_group","text":"不该发"}]}')
    dispatcher = _make_dispatcher(
        inner=inner,
        probe=None,
        attention_probe_enabled=False,
        max_hold_s=0.1,
    )
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    await dispatcher.run(_event("哈哈哈"), session)
    await asyncio.sleep(0.2)

    assert inner.calls == []
    assert sent == []
    await dispatcher.stop()


async def test_probe_failure_via_internal_handler_drops_batch() -> None:
    """A probe that follows the contract (catch internally, return False)
    routes through the dispatcher's normal "verdict false" path.

    The probe stub here mimics the real :class:`AttentionProbe` —
    catches the exception, emits ``group_batch.attention_probe.failed``,
    and returns ``False``. The dispatcher should treat this exactly
    like a model-said-no: drop the batch, no main LLM call, no
    ``unexpected_failure`` log on the dispatcher side.
    """
    inner = _Inner(content='{"actions":[]}')

    class _ContractAbidingProbe(_ProbeStub):
        async def judge(
            self, batch: list[_ProbeBatchInput], *, scope_id: str
        ) -> bool:
            self.call_count += 1
            self.last_batch = list(batch)
            self.last_scope = scope_id
            structlog.get_logger(__name__).warning(
                "group_batch.attention_probe.failed",
                scope_id=scope_id,
                category="http_5xx",
            )
            return False

    spy = _ContractAbidingProbe(verdict=False)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=0.15)
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    with structlog.testing.capture_logs() as records:
        await dispatcher.run(_event("普通闲聊", eid="m1"), session)
        await asyncio.sleep(0.3)

    assert spy.call_count == 1
    assert inner.calls == []
    assert sent == []
    failed = [
        r for r in records if r.get("event") == "group_batch.attention_probe.failed"
    ]
    assert len(failed) == 1
    assert failed[0]["category"] == "http_5xx"
    # The dispatcher's *own* defence-in-depth handler must NOT fire
    # when the probe absorbed the failure correctly.
    assert all(
        r.get("event") != "group_batch.attention_probe.unexpected_failure"
        for r in records
    )
    await dispatcher.stop()


async def test_probe_failure_via_dispatcher_defence_in_depth() -> None:
    """A probe that *violates* the contract (lets the exception leak)
    is caught by the dispatcher's outer ``except Exception`` and
    routed to the same drop path with an ``unexpected_failure`` log.
    """
    inner = _Inner(content='{"actions":[]}')

    def raiser(_b: list[_ProbeBatchInput]) -> bool:
        raise LLMError("HTTP 500: server is sad")

    spy = _ProbeStub(verdict=raiser)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=0.15)
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    with structlog.testing.capture_logs() as records:
        await dispatcher.run(_event("普通闲聊", eid="m1"), session)
        await asyncio.sleep(0.3)

    assert spy.call_count == 1
    assert inner.calls == []
    unexpected = [
        r
        for r in records
        if r.get("event") == "group_batch.attention_probe.unexpected_failure"
    ]
    assert len(unexpected) == 1
    await dispatcher.stop()


async def test_dispatcher_stop_awaits_probe_aclose() -> None:
    inner = _Inner(content='{"actions":[]}')
    spy = _ProbeStub(verdict=False)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=0.2)
    dispatcher.set_action_sink(lambda a: None)

    await dispatcher.stop()
    assert spy.aclose_count == 1


async def test_clear_history_during_inflight_probe_invalidates_verdict() -> None:
    """Mirrors ``test_group_batch_clear_history_marks_inflight_tool_batch_stale``.

    The probe blocks on a release-event; meanwhile we issue
    ``clear_history`` which bumps the generation counter. When the
    probe releases with verdict ``True``, the generation guard
    discards it and the batch never reaches the inner dispatcher.
    """
    inner = _Inner(content='{"actions":[{"type":"send_group","text":"不该发"}]}')
    release = asyncio.Event()
    spy = _ProbeStub(verdict=True)
    spy.block_until(release)
    dispatcher = _make_dispatcher(inner=inner, probe=spy, max_hold_s=2.0)
    sent: list[Action] = []
    dispatcher.set_action_sink(sent.append)
    session = await _new_session()

    await dispatcher.run(_event("普通闲聊", eid="m1"), session)

    # Wait until the probe is engaged.
    await _wait_for(lambda: spy.call_count == 1, timeout=2.0)
    await dispatcher.clear_history("g1", "u1")
    release.set()
    await asyncio.sleep(0.3)

    assert inner.calls == []
    assert sent == []
    await dispatcher.stop()
