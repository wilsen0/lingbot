"""Tests for trace-id context + audit sink integration on the router."""

from __future__ import annotations

from dataclasses import dataclass, field

from linling_core.audit import AuditEntry, AuditSink
from linling_core.classifier import HandlerMatch, MessageClassifier
from linling_core.events import Action, Event, Scope, User
from linling_core.pipeline import ConversationStore, Session
from linling_core.router import Router, RouterConfig, current_trace_id
from linling_core.segments import TextSegment

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeHandler:
    trigger: str
    is_internal: bool = False


@dataclass
class _FakeScript:
    handlers: list[_FakeHandler] = field(default_factory=list)


class _RecordingAudit:
    """Captures every entry written by the router."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def write(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


class _TraceCaptureCommand:
    """Command dispatcher that records the trace id it runs under."""

    def __init__(self) -> None:
        self.seen_traces: list[str] = []

    async def run(self, event: Event, match: HandlerMatch, session: Session) -> list[Action]:
        self.seen_traces.append(current_trace_id())
        return []


class _SilentChat:
    async def run(self, event: Event, session: Session) -> list[Action]:
        return []


class _FailingChat:
    async def run(self, event: Event, session: Session) -> list[Action]:
        raise RuntimeError("upstream gateway timeout")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _event(text: str, *, sender: str = "u1", eid: str | None = None) -> Event:
    return Event(
        id=eid or f"e-{text}",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id="g1", platform="test"),
        sender=User(id=sender, platform="test", display_name=sender),
        segments=[TextSegment(text=text)],
    )


def _make_router(*, audit: AuditSink, script: _FakeScript | None = None, commands=None):
    classifier = MessageClassifier(script or _FakeScript())
    commands = commands or _TraceCaptureCommand()
    chats = _SilentChat()

    async def sink(_a: Action) -> None:
        return None

    router = Router(
        classifier=classifier,
        commands=commands,
        chats=chats,
        sink=sink,
        conversations=ConversationStore(rate_per_second=100, burst=100),
        audit=audit,
    )
    return router, commands


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_audit_entry_written_for_every_dispatch():
    audit = _RecordingAudit()
    router, _ = _make_router(
        audit=audit,
        script=_FakeScript(handlers=[_FakeHandler(trigger="ping")]),
    )
    await router.handle(_event("/ping"))
    assert len(audit.entries) == 1
    e = audit.entries[0]
    assert e.kind == "command"
    assert e.bot_id == "linling"
    assert e.scope_id == "g1"
    assert e.user_id == "u1"
    assert e.outcome == "ok"
    assert e.verdict.startswith("command:")
    assert e.latency_ms is not None and e.latency_ms >= 0
    assert "event_id" in e.payload
    assert "message" in e.payload


async def test_trace_id_is_unique_per_event_and_stable_within_dispatch():
    audit = _RecordingAudit()
    router, cmd = _make_router(
        audit=audit,
        script=_FakeScript(handlers=[_FakeHandler(trigger="ping")]),
    )
    await router.handle(_event("/ping", eid="1"))
    await router.handle(_event("/ping", eid="2"))

    # Two entries, two trace ids.
    assert len({e.trace_id for e in audit.entries}) == 2
    # Each audit row's trace_id matches the id the dispatcher observed.
    assert [e.trace_id for e in audit.entries] == cmd.seen_traces


async def test_trace_context_is_cleared_after_dispatch():
    """``current_trace_id()`` outside a handle() call is empty."""
    audit = _RecordingAudit()
    router, _ = _make_router(
        audit=audit,
        script=_FakeScript(handlers=[_FakeHandler(trigger="ping")]),
    )
    assert current_trace_id() == ""
    await router.handle(_event("/ping"))
    assert current_trace_id() == ""


async def test_audit_records_backpressure_rejection():
    import asyncio

    audit = _RecordingAudit()
    block = asyncio.Event()

    class _Blocker:
        async def run(self, event: Event, session: Session) -> list[Action]:
            await block.wait()
            return []

    classifier = MessageClassifier(_FakeScript())
    router = Router(
        classifier=classifier,
        commands=_TraceCaptureCommand(),
        chats=_Blocker(),
        sink=_no_sink,
        conversations=ConversationStore(rate_per_second=100, burst=100),
        config=RouterConfig(max_concurrent_events=1, enqueue_timeout_s=0.05),
        audit=audit,
    )

    slow = asyncio.create_task(router.handle(_event("hi first", eid="1")))
    await asyncio.sleep(0)
    await router.handle(_event("hi second", sender="u2", eid="2"))

    verdicts = [e.verdict for e in audit.entries]
    assert "backpressure" in verdicts
    rejected = next(e for e in audit.entries if e.verdict == "backpressure")
    assert rejected.outcome == "rate-limited"

    block.set()
    await slow


async def test_audit_entry_payload_clips_long_text():
    audit = _RecordingAudit()
    router, _ = _make_router(audit=audit)
    long = "x" * 10_000
    await router.handle(_event(long))
    e = audit.entries[0]
    # payload["message"] is clipped to 500 chars.
    assert len(e.payload["message"]) == 500


async def test_audit_records_dispatcher_exception_details():
    audit = _RecordingAudit()
    classifier = MessageClassifier(_FakeScript())
    router = Router(
        classifier=classifier,
        commands=_TraceCaptureCommand(),
        chats=_FailingChat(),
        sink=_no_sink,
        conversations=ConversationStore(rate_per_second=100, burst=100),
        audit=audit,
    )

    await router.handle(_event("hi"))

    assert len(audit.entries) == 1
    e = audit.entries[0]
    assert e.kind == "chat"
    assert e.outcome == "error"
    assert e.verdict == "chat:fallback:error"
    assert e.payload["error_dispatcher"] == "chat"
    assert e.payload["error_type"] == "RuntimeError"
    assert e.payload["error_message"] == "upstream gateway timeout"


async def test_null_audit_is_default_and_never_raises():
    """If no audit is provided the router silently discards entries."""
    classifier = MessageClassifier(_FakeScript(handlers=[_FakeHandler(trigger="ping")]))
    router = Router(
        classifier=classifier,
        commands=_TraceCaptureCommand(),
        chats=_SilentChat(),
        sink=_no_sink,
    )
    # Should not raise.
    await router.handle(_event("/ping"))


async def _no_sink(_action: Action) -> None:
    return None
