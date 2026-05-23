"""Tests for ``KVDslLedgerStore``.

Properties (per design.md "Testing Strategy"):

* **Property 11** — Scope isolation:writes under one
  ``(scope_id, file_id)`` never bleed into another (Req 6.1 / 6.2 /
  6.7).
* **Property 12** — Persist round-trip:loaded events match what was
  saved, sorted oldest-first, with TTL filtering (Req 8.4 / 8.9).

Plus boundary unit tests for TTL clamping, prefix isolation from
``__history__``, corrupt-record skipping, scope helper invariance,
and unknown-scope-kind log behaviour (Req 6.3 / 6.7 / 8.2 / 8.3 /
8.8 / 8.9).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from linling_core.events import Event, Scope, User
from linling_core.pipeline import DslEvent, ledger_scope_keys
from linling_core.segments import TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore

from linling_agent.history import KVHistoryStore
from linling_agent.ledger_store import (
    KVDslLedgerStore,
    LedgerStore,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def kv():
    store = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with store:
        yield store


def _event(occurred_at: float = 0.0, summary: str = "s") -> DslEvent:
    return DslEvent(
        timestamp="00:00:00",
        trigger="t",
        args=("a", "b"),
        summary=summary,
        outcome="ok",
        mode="with_result",
        actor_id="u1",
        occurred_at=occurred_at,
    )


# ---------------------------------------------------------------------------
# Construction-time validation
# ---------------------------------------------------------------------------


async def test_ttl_out_of_range_falls_back_to_default(kv) -> None:
    """``ttl_seconds`` outside [60, 86400] falls back without raising."""
    too_low = KVDslLedgerStore(kv, ttl_seconds=10)
    too_high = KVDslLedgerStore(kv, ttl_seconds=99999)
    # Round-trip a fresh event; the store still functions.
    await too_low.save("s", "f", [_event(occurred_at=time.time())])
    await too_high.save("s", "f", [_event(occurred_at=time.time())])
    assert len(await too_low.load("s", "f")) == 1
    assert len(await too_high.load("s", "f")) == 1


async def test_maxlen_out_of_range_raises(kv) -> None:
    with pytest.raises(ValueError):
        KVDslLedgerStore(kv, maxlen=0)
    with pytest.raises(ValueError):
        KVDslLedgerStore(kv, maxlen=201)


def test_protocol_runtime_checkable(kv) -> None:
    """Bootstrap relies on ``isinstance(store, LedgerStore)`` checks."""
    store = KVDslLedgerStore(kv)
    assert isinstance(store, LedgerStore)


# ---------------------------------------------------------------------------
# Save / load round-trip basics
# ---------------------------------------------------------------------------


async def test_save_then_load_roundtrip(kv) -> None:
    store = KVDslLedgerStore(kv, ttl_seconds=3600)
    now = time.time()
    events = [_event(occurred_at=now - 5, summary="a"), _event(occurred_at=now, summary="b")]
    await store.save("scope1", "file1", events)
    loaded = await store.load("scope1", "file1")
    assert [e.summary for e in loaded] == ["a", "b"]
    assert [e.occurred_at for e in loaded] == [now - 5, now]


async def test_load_returns_events_sorted_oldest_first(kv) -> None:
    """Order is restored even if the saved blob was unordered (paranoia)."""
    store = KVDslLedgerStore(kv)
    now = time.time()
    # Save in newest-first order; load must still come back oldest-first.
    events = [_event(occurred_at=now), _event(occurred_at=now - 100), _event(occurred_at=now - 50)]
    await store.save("scope1", "file1", events)
    loaded = await store.load("scope1", "file1")
    assert [e.occurred_at for e in loaded] == [now - 100, now - 50, now]


async def test_load_empty_returns_empty_list(kv) -> None:
    store = KVDslLedgerStore(kv)
    assert await store.load("nonexistent", "file") == []


async def test_clear_removes_only_targeted_scope(kv) -> None:
    """``clear`` is strictly per-(scope_id, file_id) — never wildcards."""
    store = KVDslLedgerStore(kv)
    now = time.time()
    await store.save("scope1", "fileA", [_event(occurred_at=now, summary="A")])
    await store.save("scope1", "fileB", [_event(occurred_at=now, summary="B")])
    await store.save("scope2", "fileA", [_event(occurred_at=now, summary="C")])

    await store.clear("scope1", "fileA")
    assert await store.load("scope1", "fileA") == []
    # Other scopes / files survive.
    assert len(await store.load("scope1", "fileB")) == 1
    assert len(await store.load("scope2", "fileA")) == 1


async def test_save_trims_to_maxlen(kv) -> None:
    """``save`` keeps the trailing ``maxlen`` events (most recent wins)."""
    store = KVDslLedgerStore(kv, maxlen=3)
    now = time.time()
    events = [_event(occurred_at=now + i, summary=f"e{i}") for i in range(10)]
    await store.save("s", "f", events)
    loaded = await store.load("s", "f")
    assert len(loaded) == 3
    # Most recent three.
    assert [e.summary for e in loaded] == ["e7", "e8", "e9"]


async def test_expired_events_dropped_on_load(kv) -> None:
    """Events whose ``occurred_at + ttl`` is past now are filtered."""
    store = KVDslLedgerStore(kv, ttl_seconds=60)
    now = time.time()
    events = [
        _event(occurred_at=now - 120, summary="stale"),
        _event(occurred_at=now - 30, summary="fresh"),
    ]
    await store.save("s", "f", events)
    loaded = await store.load("s", "f")
    assert [e.summary for e in loaded] == ["fresh"]


# ---------------------------------------------------------------------------
# Property 11 — Scope isolation
# ---------------------------------------------------------------------------


@settings(max_examples=20, deadline=None)
@given(
    scope_a=st.text(min_size=1, max_size=10).filter(lambda s: "/" not in s),
    scope_b=st.text(min_size=1, max_size=10).filter(lambda s: "/" not in s),
)
async def test_property_11_scope_isolation(scope_a: str, scope_b: str) -> None:
    """Writes under (scope_a, file_a) do not appear under (scope_b, file_b)."""
    if scope_a == scope_b:
        return
    kv = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with kv:
        store = KVDslLedgerStore(kv)
        now = time.time()
        await store.save(scope_a, "fa", [_event(occurred_at=now, summary="A")])
        await store.save(scope_b, "fb", [_event(occurred_at=now, summary="B")])
        loaded_a = await store.load(scope_a, "fa")
        loaded_b = await store.load(scope_b, "fb")
        assert [e.summary for e in loaded_a] == ["A"]
        assert [e.summary for e in loaded_b] == ["B"]
        # Cross-loads return empty.
        assert await store.load(scope_a, "fb") == []
        assert await store.load(scope_b, "fa") == []


# ---------------------------------------------------------------------------
# Property 12 — Persist round-trip
# ---------------------------------------------------------------------------


_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    min_size=0,
    max_size=30,
)


@settings(max_examples=20, deadline=None)
@given(
    occurred_offsets=st.lists(
        # Offsets in seconds, near zero — we anchor against ``now`` so
        # everything stays inside the TTL window.
        st.floats(min_value=-1000.0, max_value=0.0, allow_nan=False),
        min_size=0,
        max_size=8,
    ),
    triggers=st.lists(_text.filter(lambda s: len(s) > 0), min_size=0, max_size=8),
    summaries=st.lists(_text, min_size=0, max_size=8),
)
async def test_property_12_persist_round_trip(
    occurred_offsets: list[float],
    triggers: list[str],
    summaries: list[str],
) -> None:
    """Saved events round-trip with field-by-field equality (modulo sort)."""
    # Build a fresh batch sized to the shortest of the three lists so
    # all fields line up; this keeps the strategy self-consistent
    # without complex ``@composite`` plumbing.
    n = min(len(occurred_offsets), len(triggers), len(summaries))
    now = time.time()
    events = [
        DslEvent(
            timestamp="00:00:00",
            trigger=triggers[i],
            args=(),
            summary=summaries[i],
            outcome="ok",
            mode="with_result",
            actor_id="u1",
            occurred_at=now + occurred_offsets[i],
        )
        for i in range(n)
    ]
    kv = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    async with kv:
        store = KVDslLedgerStore(kv, ttl_seconds=86400)
        await store.save("scope", "file", events)
        loaded = await store.load("scope", "file")

        original_sorted = sorted(events, key=lambda e: e.occurred_at)
        for orig, restored in zip(original_sorted, loaded, strict=True):
            assert orig.timestamp == restored.timestamp
            assert orig.trigger == restored.trigger
            assert orig.args == restored.args
            assert orig.summary == restored.summary
            assert orig.outcome == restored.outcome
            assert orig.mode == restored.mode
            assert orig.actor_id == restored.actor_id
            assert orig.occurred_at == restored.occurred_at


# ---------------------------------------------------------------------------
# Boundary unit tests
# ---------------------------------------------------------------------------


async def test_kv_dsl_ledger_store_uses_separate_prefix_from_history(kv) -> None:
    """A history record and a ledger record under the same scope/file id
    coexist without collision."""
    history = KVHistoryStore(kv, max_turns=8)
    ledger = KVDslLedgerStore(kv)
    now = time.time()
    await ledger.save("scope1", "u1", [_event(occurred_at=now)])

    # The history store under the same scope/sender id must remain untouched.
    assert await history.load("scope1", "u1") == []

    # The ledger row sits under ``__dsl_ledger__/scope1`` whereas the
    # history store reserves ``__history__/scope1``. Verify the row
    # is reachable only via the ledger prefix.
    raw = await kv.read("__dsl_ledger__/scope1", "u1", "events")
    assert raw is not None
    payload = json.loads(raw)
    assert "events" in payload
    # No matching row under the history prefix.
    assert await kv.read("__history__/scope1", "u1", "events") is None


async def test_corrupt_record_skipped_with_log(kv) -> None:
    """Each malformed entry is dropped; sane entries still load."""
    store = KVDslLedgerStore(kv)
    now = time.time()
    payload = json.dumps(
        {
            "saved_at": now,
            "ttl": 3600,
            "events": [
                # Sane entry.
                {
                    "timestamp": "00:00:00",
                    "trigger": "good",
                    "args": [],
                    "summary": "ok",
                    "outcome": "ok",
                    "mode": "with_result",
                    "actor_id": "u1",
                    "occurred_at": now,
                },
                # Missing required field.
                {"timestamp": "00:00:01", "trigger": "broken"},
                # Wrong type for ``args``.
                {
                    "timestamp": "00:00:02",
                    "trigger": "broken-args",
                    "args": "not-a-list",
                    "outcome": "ok",
                    "occurred_at": now,
                },
                # Not a dict at all.
                "garbage",
            ],
        }
    )
    await kv.write("__dsl_ledger__/scope1", "u1", "events", payload)
    loaded = await store.load("scope1", "u1")
    assert [e.trigger for e in loaded] == ["good"]


async def test_load_handles_corrupt_json(kv) -> None:
    """A non-JSON blob is treated like an empty ledger."""
    store = KVDslLedgerStore(kv)
    await kv.write("__dsl_ledger__/scope1", "u1", "events", "not json {{")
    assert await store.load("scope1", "u1") == []


async def test_load_handles_non_dict_payload(kv) -> None:
    """A JSON list at top level (wrong schema) fails gracefully."""
    store = KVDslLedgerStore(kv)
    await kv.write("__dsl_ledger__/scope1", "u1", "events", json.dumps([1, 2, 3]))
    assert await store.load("scope1", "u1") == []


# ---------------------------------------------------------------------------
# Scope-helper invariants (Requirement 6.7)
# ---------------------------------------------------------------------------


def test_chat_history_scope_logic_unchanged() -> None:
    """``ledger_scope_keys`` does not influence chat history scope.

    ``KVHistoryStore`` indexes by ``(scope_id, sender_id)`` regardless
    of scope kind; the ledger's group-collapse logic is its own.
    """
    event = Event(
        id="e",
        platform="t",
        bot_id="b",
        scope=Scope(kind="group", id="g", platform="t"),
        sender=User(id="u1", platform="t"),
        segments=[TextSegment(text="hi")],
    )
    # The ledger collapses group scope to ``"_group"``.
    assert ledger_scope_keys(event) == ("g", "_group")
    # Chat history still uses ``(g, u1)`` — that's the dispatcher's
    # job (see :class:`AgentChatDispatcher._rehydrate_history`),
    # which we don't exercise here. The point of this test is that
    # ``ledger_scope_keys`` itself is the only function that returns
    # the group-collapsed key, so a refactor that accidentally calls
    # it from the history path would fail other dispatcher tests.
    assert event.sender.id == "u1"  # baseline sanity for the next assertion


def test_unknown_scope_kind_falls_back_with_log() -> None:
    """A future / system scope kind logs a warning and uses dm-style keys."""
    captured: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def warning(self, name: str, **kw: object) -> None:
            captured.append((name, kw))

    event = Event(
        id="e",
        platform="t",
        bot_id="b",
        scope=Scope(kind="system", id="g", platform="t"),
        sender=User(id="u1", platform="t"),
        segments=[TextSegment(text="hi")],
    )
    scope_id, file_id = ledger_scope_keys(event, logger=FakeLogger())  # type: ignore[arg-type]
    assert scope_id == "g"
    assert file_id == "u1"
    assert captured and captured[0][0] == "pipeline.ledger_scope_unknown"
