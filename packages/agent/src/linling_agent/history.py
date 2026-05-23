"""Persistent chat history for agent conversations.

Short-term memory lives in :attr:`linling_core.pipeline.Session.history`
(an in-memory deque bounded by ``history_turns``). That deque survives
within one process as long as the session is active, but resets on
restart and is invisible to other processes.

:class:`KVHistoryStore` mirrors that deque into the KV store so that:

* Conversations survive bot restarts.
* Multiple processes sharing a Postgres KV see a consistent view.
* Operators can inspect a user's recent turns from the WebUI.

Schema (under ``kv`` table):

* scope = ``__history__/<scope_id>``
* file  = sender_id (empty string for scope-wide group memory)
* key   = ``messages``
* value = JSON list of ``{"role": str, "content": str}``

The ``__history__`` prefix is stable across versions — the KV browser
can filter it out of user-facing listings. Bumping to a v2 schema means
writing under ``__history_v2__`` and leaving old rows alone.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from linling_agent.llm import Message

if TYPE_CHECKING:
    from linling_core.storage.kv import KVStore

logger = structlog.get_logger(__name__)


_HISTORY_SCOPE_PREFIX = "__history__"
_MESSAGES_KEY = "messages"


@runtime_checkable
class HistoryStore(Protocol):
    """Abstract persistent backing store for conversation history.

    Implementations are bot-bound (they close over the bot's KV store);
    the ``(scope_id, sender_id)`` pair is passed per call.
    """

    async def load(self, scope_id: str, sender_id: str) -> list[Message]: ...

    async def save(self, scope_id: str, sender_id: str, messages: Iterable[Message]) -> None: ...

    async def clear(self, scope_id: str, sender_id: str) -> None: ...


class KVHistoryStore:
    """Persist history JSON into a backing :class:`KVStore`.

    The store is bot-scoped via the KV's own ``bot_id`` column, so a
    multi-tenant deployment is isolated automatically. We never store
    tool-call metadata or system prompts — only the plain user /
    assistant turns the agent needs to reconstruct context.
    """

    def __init__(self, kv: KVStore, *, max_turns: int = 32) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be positive")
        self._kv = kv
        self._max_turns = max_turns

    async def load(self, scope_id: str, sender_id: str) -> list[Message]:
        raw = await self._kv.read(
            _HISTORY_SCOPE_PREFIX + "/" + scope_id,
            sender_id or "_group",
            _MESSAGES_KEY,
            default=None,
        )
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "history.load_parse_failed",
                scope_id=scope_id,
                sender_id=sender_id,
                # Don't log ``raw`` — may contain PII.
            )
            return []
        if not isinstance(payload, list):
            return []
        messages: list[Message] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content", "")
            if not isinstance(role, str) or not isinstance(content, str):
                continue
            # Only the two turn roles are safe to replay; tool and
            # system messages belong to the LLM orchestration layer and
            # are not part of user-facing history.
            if role not in ("user", "assistant"):
                continue
            messages.append(Message(role=role, content=content))
        return messages

    async def save(self, scope_id: str, sender_id: str, messages: Iterable[Message]) -> None:
        trimmed = [{"role": m.role, "content": m.content} for m in _only_turn_messages(messages)][
            -self._max_turns * 2 :
        ]  # *2 because turns come in user/assistant pairs
        payload = json.dumps(trimmed, ensure_ascii=False)
        await self._kv.write(
            _HISTORY_SCOPE_PREFIX + "/" + scope_id,
            sender_id or "_group",
            _MESSAGES_KEY,
            payload,
        )

    async def clear(self, scope_id: str, sender_id: str) -> None:
        await self._kv.delete(
            _HISTORY_SCOPE_PREFIX + "/" + scope_id,
            sender_id or "_group",
            _MESSAGES_KEY,
        )


def _only_turn_messages(messages: Iterable[Message]) -> list[Message]:
    """Strip system / tool messages before persisting.

    ``replace`` is used rather than a new ``Message`` construction so any
    future fields that may land on the dataclass (e.g. timestamps) are
    copied verbatim.
    """
    out: list[Message] = []
    for m in messages:
        if m.role not in ("user", "assistant"):
            continue
        # Drop tool_call metadata from persisted copies — only the plain
        # turn content matters for replay.
        stripped = replace(m, tool_calls=None) if m.tool_calls else m
        out.append(stripped)
    return out
