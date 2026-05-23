"""Tests for the metrics abstraction + Prometheus backend."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from linling_core.classifier import HandlerMatch, MessageClassifier
from linling_core.events import Action, Event, Scope, User
from linling_core.metrics import (
    ACTIVE_SESSIONS,
    DISPATCH_DURATION_SECONDS,
    ROUTER_EVENTS_TOTAL,
    NullMetrics,
)
from linling_core.metrics_prometheus import PrometheusMetrics
from linling_core.pipeline import ConversationStore, Session
from linling_core.router import Router
from linling_core.segments import TextSegment

# ---------------------------------------------------------------------------
# Unit tests: NullMetrics + PrometheusMetrics shape
# ---------------------------------------------------------------------------


def test_null_metrics_accepts_every_operation():
    m = NullMetrics()
    m.counter_inc("x", {})
    m.histogram_observe("x", {}, 1.0)
    m.gauge_set("x", {}, 3.0)
    # Nothing to observe — just proving the surface is stable.


def test_prometheus_inc_histogram_gauge_roundtrip():
    m = PrometheusMetrics()
    m.counter_inc(
        ROUTER_EVENTS_TOTAL,
        {"bot_id": "b1", "platform": "cli", "kind": "command", "outcome": "ok"},
    )
    m.histogram_observe(DISPATCH_DURATION_SECONDS, {"bot_id": "b1", "kind": "command"}, 0.123)
    m.gauge_set(ACTIVE_SESSIONS, {"bot_id": "b1"}, 42.0)
    body, content_type = m.render()
    text = body.decode()
    assert "linling_events_total" in text
    assert 'bot_id="b1"' in text
    assert "linling_dispatch_duration_seconds" in text
    assert "linling_active_sessions" in text
    assert "text/plain" in content_type


def test_prometheus_ignores_unknown_metric_name_silently():
    """A counter_inc against a name we didn't register is a no-op."""
    m = PrometheusMetrics()
    m.counter_inc("linling_never_registered", {"bot_id": "x"})
    # Should not raise; nothing shows up in the scrape.
    assert b"linling_never_registered" not in m.render()[0]


def test_prometheus_fills_missing_labels_with_unknown():
    m = PrometheusMetrics()
    # Missing "platform" — should still record, filled with "unknown".
    m.counter_inc(
        ROUTER_EVENTS_TOTAL,
        {"bot_id": "b1", "kind": "chat", "outcome": "ok"},
    )
    body = m.render()[0].decode()
    assert 'platform="unknown"' in body


# ---------------------------------------------------------------------------
# Router integration
# ---------------------------------------------------------------------------


@dataclass
class _FakeHandler:
    trigger: str
    is_internal: bool = False


@dataclass
class _FakeScript:
    handlers: list[_FakeHandler] = field(default_factory=list)


class _FakeCommand:
    async def run(self, event: Event, match: HandlerMatch, session: Session) -> list[Action]:
        return []


class _FakeChat:
    async def run(self, event: Event, session: Session) -> list[Action]:
        return []


def _event(text: str, *, eid: str | None = None) -> Event:
    return Event(
        id=eid or f"e-{text}",
        platform="test",
        bot_id="b1",
        scope=Scope(kind="group", id="g1", platform="test"),
        sender=User(id="u1", platform="test", display_name="u"),
        segments=[TextSegment(text=text)],
    )


async def _sink(_a: Action) -> None:
    return None


async def test_router_increments_events_total():
    m = PrometheusMetrics()
    router = Router(
        classifier=MessageClassifier(
            _FakeScript(handlers=[_FakeHandler(trigger="ping")])  # type: ignore[arg-type]
        ),
        commands=_FakeCommand(),
        chats=_FakeChat(),
        sink=_sink,
        conversations=ConversationStore(rate_per_second=100, burst=100),
        metrics=m,
    )
    await router.handle(_event("/ping"))

    body = m.render()[0].decode()
    # One ``command`` outcome=``ok`` sample for bot_id=b1 on platform=test.
    assert 'kind="command"' in body
    assert 'outcome="ok"' in body
    assert "linling_dispatch_duration_seconds" in body


async def test_router_records_duplicates():
    m = PrometheusMetrics()
    router = Router(
        classifier=MessageClassifier(_FakeScript()),
        commands=_FakeCommand(),
        chats=_FakeChat(),
        sink=_sink,
        conversations=ConversationStore(rate_per_second=100, burst=100),
        metrics=m,
    )
    ev = _event("hi", eid="fixed")
    await router.handle(ev)
    await router.handle(ev)  # duplicate

    body = m.render()[0].decode()
    assert "linling_router_duplicates_total" in body


async def test_router_records_active_sessions_gauge():
    m = PrometheusMetrics()
    store = ConversationStore(rate_per_second=100, burst=100)
    router = Router(
        classifier=MessageClassifier(_FakeScript()),
        commands=_FakeCommand(),
        chats=_FakeChat(),
        sink=_sink,
        conversations=store,
        metrics=m,
    )
    await router.handle(_event("hello 1", eid="a"))
    await router.handle(_event("hello 2", eid="b"))

    body = m.render()[0].decode()
    # After two distinct senders we should see the gauge populated.
    assert "linling_active_sessions" in body


# ---------------------------------------------------------------------------
# Agent runtime integration
# ---------------------------------------------------------------------------


async def test_agent_runtime_emits_token_and_duration_metrics():
    from linling_agent.agent_def import AgentDef
    from linling_agent.llm import LLMResponse, Message, TokenUsage
    from linling_agent.runtime import AgentRuntime
    from linling_core.storage.sqlite_kv import SqliteKVStore
    from linling_core.tools import registry

    class _Provider:
        @property
        def name(self) -> str:
            return "mock"

        async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
            return LLMResponse(
                message=Message(role="assistant", content="hi"),
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

        async def chat_stream(self, messages, **kwargs):
            raise NotImplementedError

    m = PrometheusMetrics()
    async with SqliteKVStore(bot_id="b1", db_path=":memory:") as kv:
        agent_def = AgentDef(name="a", provider="openai", model="gpt-4o", system="")
        runtime = AgentRuntime(
            agent_def=agent_def,
            provider=_Provider(),
            tool_registry=registry,
            kv=kv,
            bot_id="b1",
            metrics=m,
        )
        await runtime.invoke("hello")

    body = m.render()[0].decode()
    assert "linling_llm_calls_total" in body
    assert 'outcome="ok"' in body
    assert 'direction="prompt"' in body
    assert 'direction="completion"' in body
    assert "linling_llm_duration_seconds" in body


async def test_agent_runtime_records_error_on_provider_exception():
    from linling_agent.agent_def import AgentDef
    from linling_agent.runtime import AgentRuntime
    from linling_core.storage.sqlite_kv import SqliteKVStore
    from linling_core.tools import registry

    class _BrokenProvider:
        @property
        def name(self) -> str:
            return "broken"

        async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
            raise RuntimeError("boom")

        async def chat_stream(self, messages, **kwargs):
            raise NotImplementedError

    m = PrometheusMetrics()
    async with SqliteKVStore(bot_id="b1", db_path=":memory:") as kv:
        agent_def = AgentDef(name="a", model="gpt", system="")
        runtime = AgentRuntime(
            agent_def=agent_def,
            provider=_BrokenProvider(),
            tool_registry=registry,
            kv=kv,
            bot_id="b1",
            metrics=m,
        )
        with pytest.raises(RuntimeError):
            await runtime.invoke("hello")

    body = m.render()[0].decode()
    # One error labelled call; no successful call.
    assert 'outcome="error"' in body
    # The duration histogram still observed the attempt.
    assert "linling_llm_duration_seconds" in body
