"""Tests for linling_agent.memory module."""

from __future__ import annotations

from linling_agent.llm import Message
from linling_agent.memory import MemoryConfig, SlidingWindowMemory


class TestMemoryConfig:
    def test_defaults(self) -> None:
        cfg = MemoryConfig()
        assert cfg.kind == "sliding_window"
        assert cfg.turns == 8

    def test_custom(self) -> None:
        cfg = MemoryConfig(kind="none", turns=4)
        assert cfg.kind == "none"
        assert cfg.turns == 4


class TestSlidingWindowMemory:
    def test_get_returns_empty_for_unknown_key(self) -> None:
        mem = SlidingWindowMemory()
        result = mem.get("user1", "scope1")
        assert result == []

    def test_add_stores_messages(self) -> None:
        mem = SlidingWindowMemory()
        msgs = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
        ]
        mem.add("user1", "scope1", msgs)
        stored = mem.get("user1", "scope1")
        assert len(stored) == 2
        assert stored[0].content == "hello"
        assert stored[1].content == "hi"

    def test_get_returns_stored_messages(self) -> None:
        mem = SlidingWindowMemory()
        msgs = [Message(role="user", content="test")]
        mem.add("u", "s", msgs)
        result = mem.get("u", "s")
        assert result == msgs

    def test_sliding_window_trims_to_max_turns(self) -> None:
        mem = SlidingWindowMemory(max_turns=2)
        # Add 3 turns (6 messages), should keep only last 4
        for i in range(3):
            mem.add(
                "user1",
                "scope1",
                [
                    Message(role="user", content=f"q{i}"),
                    Message(role="assistant", content=f"a{i}"),
                ],
            )
        stored = mem.get("user1", "scope1")
        assert len(stored) == 4  # max_turns * 2
        assert stored[0].content == "q1"
        assert stored[1].content == "a1"
        assert stored[2].content == "q2"
        assert stored[3].content == "a2"

    def test_clear_removes_specific_user_scope(self) -> None:
        mem = SlidingWindowMemory()
        mem.add("u1", "s1", [Message(role="user", content="a")])
        mem.add("u2", "s1", [Message(role="user", content="b")])
        mem.clear("u1", "s1")
        assert mem.get("u1", "s1") == []
        assert len(mem.get("u2", "s1")) == 1

    def test_clear_all_removes_everything(self) -> None:
        mem = SlidingWindowMemory()
        mem.add("u1", "s1", [Message(role="user", content="a")])
        mem.add("u2", "s2", [Message(role="user", content="b")])
        mem.clear_all()
        assert mem.get("u1", "s1") == []
        assert mem.get("u2", "s2") == []

    def test_isolation_between_user_scope_pairs(self) -> None:
        mem = SlidingWindowMemory()
        mem.add("u1", "s1", [Message(role="user", content="msg1")])
        mem.add("u1", "s2", [Message(role="user", content="msg2")])
        mem.add("u2", "s1", [Message(role="user", content="msg3")])

        r1 = mem.get("u1", "s1")
        r2 = mem.get("u1", "s2")
        r3 = mem.get("u2", "s1")

        assert len(r1) == 1 and r1[0].content == "msg1"
        assert len(r2) == 1 and r2[0].content == "msg2"
        assert len(r3) == 1 and r3[0].content == "msg3"

    def test_returned_list_is_copy(self) -> None:
        """Modifying the returned list doesn't affect the store."""
        mem = SlidingWindowMemory()
        mem.add("u", "s", [Message(role="user", content="original")])
        result = mem.get("u", "s")
        result.append(Message(role="assistant", content="injected"))
        # Store should be unaffected
        assert len(mem.get("u", "s")) == 1
