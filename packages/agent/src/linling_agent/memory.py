"""Conversation memory for agents.

Provides sliding-window memory that keeps the last N turns of conversation,
keyed by (user_id, scope_id) for isolation between users and contexts.
"""

from __future__ import annotations

from dataclasses import dataclass

from linling_agent.llm import Message


@dataclass
class MemoryConfig:
    """Memory configuration for an agent."""

    kind: str = "sliding_window"  # "sliding_window" | "none"
    turns: int = 8  # max conversation turns to keep


class SlidingWindowMemory:
    """Simple sliding window memory that keeps the last N turns.

    A "turn" is a user message + assistant response pair.
    Memory is keyed by (user_id, scope_id) for isolation.
    """

    def __init__(self, max_turns: int = 8) -> None:
        self._store: dict[str, list[Message]] = {}
        self._max_turns = max_turns

    def _key(self, user_id: str, scope_id: str) -> str:
        return f"{user_id}:{scope_id}"

    def get(self, user_id: str, scope_id: str) -> list[Message]:
        """Get conversation history for a user in a scope."""
        return list(self._store.get(self._key(user_id, scope_id), []))

    def add(self, user_id: str, scope_id: str, messages: list[Message]) -> None:
        """Add messages to history, trimming to max_turns."""
        key = self._key(user_id, scope_id)
        history = self._store.get(key, [])
        history.extend(messages)
        # Keep only last max_turns * 2 messages (user + assistant pairs)
        max_msgs = self._max_turns * 2
        if len(history) > max_msgs:
            history = history[-max_msgs:]
        self._store[key] = history

    def clear(self, user_id: str, scope_id: str) -> None:
        """Clear history for a user in a scope."""
        self._store.pop(self._key(user_id, scope_id), None)

    def clear_all(self) -> None:
        """Clear all memory."""
        self._store.clear()
