"""Tests for ContextManager.on_before_compact hook (Phase 4, Properties 6/7)."""

from __future__ import annotations

import asyncio

import pytest
from linling_agent.context import ContextBudget, ContextManager
from linling_agent.llm import LLMResponse, Message, TokenUsage


class _SummaryProvider:
    """Records when _summarize is invoked (single-message 'Summarize...' prompt)."""

    def __init__(self) -> None:
        self.summarize_calls = 0
        self.events: list[str] = []

    @property
    def name(self) -> str:
        return "summary"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        if len(messages) == 1 and messages[0].content.startswith("Summarize"):
            self.summarize_calls += 1
            self.events.append("summarize")
        return LLMResponse(
            message=Message(role="assistant", content="compressed"),
            usage=TokenUsage(total_tokens=3),
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


class _MemSummaryStore:
    def __init__(self) -> None:
        self._summaries: dict[tuple[str, str], str] = {}

    async def load_summary(self, scope_id: str, sender_id: str) -> str:
        return self._summaries.get((scope_id, sender_id), "")

    async def save_summary(self, scope_id: str, sender_id: str, summary: str) -> None:
        self._summaries[(scope_id, sender_id)] = summary


def _budget() -> ContextBudget:
    return ContextBudget(
        max_tokens=200,
        summary_trigger_tokens=80,
        summary_keep_recent_turns=1,
        summary_max_tokens=50,
    )


def _long_history() -> list[Message]:
    history: list[Message] = []
    for i in range(6):
        history.append(Message(role="user", content=f"old user {i} " + "很长" * 20))
        history.append(Message(role="assistant", content=f"old assistant {i}"))
    return history


async def test_hook_called_before_summarize_once() -> None:
    """Property 6: on_before_compact runs before _summarize, exactly once."""
    provider = _SummaryProvider()
    order: list[str] = []
    captured: dict[str, object] = {}

    async def hook(scope_id, sender_id, older):
        order.append("hook")
        provider.events.append("hook")
        captured["older"] = list(older)
        captured["scope_id"] = scope_id

    cm = ContextManager(
        provider=provider,
        model="mock",
        temperature=0.3,
        budget=_budget(),
        store=_MemSummaryStore(),
        on_before_compact=hook,
    )
    history = _long_history()
    await cm.prepare(scope_id="s1", sender_id="u1", history=history, current_input_text="now")

    # Hook fired before summarize, and exactly once.
    assert provider.events[0] == "hook"
    assert provider.events.count("hook") == 1
    assert provider.summarize_calls == 1
    assert captured["scope_id"] == "s1"
    # older is the folded prefix (everything except the kept recent turn).
    assert captured["older"]
    assert any("old user 0" in m.content for m in captured["older"])  # type: ignore[index]


async def test_hook_exception_failopen_summary_still_made() -> None:
    """Property 7: a hook exception is swallowed; summary still generated."""
    provider = _SummaryProvider()

    async def boom(scope_id, sender_id, older):
        raise RuntimeError("distill failed")

    store = _MemSummaryStore()
    cm = ContextManager(
        provider=provider,
        model="mock",
        temperature=0.3,
        budget=_budget(),
        store=store,
        on_before_compact=boom,
    )
    history = _long_history()
    # Must not raise.
    _messages, replacement = await cm.prepare(
        scope_id="s1", sender_id="u1", history=history, current_input_text="now"
    )
    assert provider.summarize_calls == 1
    assert await store.load_summary("s1", "u1") == "compressed"
    assert replacement is not None  # compaction happened


async def test_hook_timeout_failopen() -> None:
    """A hook TimeoutError is swallowed; summary still generated."""
    provider = _SummaryProvider()

    async def slow(scope_id, sender_id, older):
        raise TimeoutError

    store = _MemSummaryStore()
    cm = ContextManager(
        provider=provider,
        model="mock",
        temperature=0.3,
        budget=_budget(),
        store=store,
        on_before_compact=slow,
    )
    await cm.prepare(scope_id="s1", sender_id="u1", history=_long_history(), current_input_text="n")
    assert provider.summarize_calls == 1


async def test_hook_cancelled_propagates() -> None:
    """CancelledError must propagate (clean shutdown), not become fail-open."""
    provider = _SummaryProvider()

    async def cancel(scope_id, sender_id, older):
        raise asyncio.CancelledError

    cm = ContextManager(
        provider=provider,
        model="mock",
        temperature=0.3,
        budget=_budget(),
        store=_MemSummaryStore(),
        on_before_compact=cancel,
    )
    with pytest.raises(asyncio.CancelledError):
        await cm.prepare(
            scope_id="s1", sender_id="u1", history=_long_history(), current_input_text="n"
        )


async def test_no_hook_no_change() -> None:
    """Without a hook, prepare behaves exactly as before (compaction still works)."""
    provider = _SummaryProvider()
    store = _MemSummaryStore()
    cm = ContextManager(
        provider=provider,
        model="mock",
        temperature=0.3,
        budget=_budget(),
        store=store,
    )
    await cm.prepare(scope_id="s1", sender_id="u1", history=_long_history(), current_input_text="n")
    assert provider.summarize_calls == 1


async def test_hook_not_called_without_compaction() -> None:
    """No compaction (short history) → hook never fires."""
    provider = _SummaryProvider()
    called = False

    async def hook(scope_id, sender_id, older):
        nonlocal called
        called = True

    cm = ContextManager(
        provider=provider,
        model="mock",
        temperature=0.3,
        budget=_budget(),
        store=_MemSummaryStore(),
        on_before_compact=hook,
    )
    await cm.prepare(
        scope_id="s1",
        sender_id="u1",
        history=[Message(role="user", content="hi")],
        current_input_text="n",
    )
    assert called is False
    assert provider.summarize_calls == 0


async def test_force_compaction_runs_hook_below_token_trigger() -> None:
    provider = _SummaryProvider()
    called = False

    async def hook(scope_id, sender_id, older):
        nonlocal called
        _ = scope_id, sender_id, older
        called = True

    store = _MemSummaryStore()
    cm = ContextManager(
        provider=provider,
        model="mock",
        temperature=0.3,
        budget=ContextBudget(
            max_tokens=5_000,
            summary_trigger_tokens=4_000,
            summary_keep_recent_turns=1,
            summary_max_tokens=50,
        ),
        store=store,
        on_before_compact=hook,
    )
    _visible, replacement = await cm.prepare(
        scope_id="s1",
        sender_id="u1",
        history=[
            Message(role="user", content="old user"),
            Message(role="assistant", content="old assistant"),
            Message(role="user", content="recent user"),
            Message(role="assistant", content="recent assistant"),
        ],
        current_input_text="now",
        force_compaction=True,
        summary_keep_recent_turns=1,
    )

    assert called is True
    assert provider.summarize_calls == 1
    assert await store.load_summary("s1", "u1") == "compressed"
    assert replacement == [
        Message(role="user", content="recent user"),
        Message(role="assistant", content="recent assistant"),
    ]
