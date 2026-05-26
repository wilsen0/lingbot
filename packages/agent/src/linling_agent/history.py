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
* value = JSON list of message objects. ``role`` and ``content`` are
  always present; assistant tool calls and tool results may also carry
  ``tool_calls``, ``tool_call_id`` and ``name``.

The ``__history__`` prefix is stable across versions — the KV browser
can filter it out of user-facing listings. Bumping to a v2 schema means
writing under ``__history_v2__`` and leaving old rows alone.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from linling_agent.llm import Message, ToolCall

if TYPE_CHECKING:
    from linling_core.storage.kv import KVStore

logger = structlog.get_logger(__name__)


_HISTORY_SCOPE_PREFIX = "__history__"
_MESSAGES_KEY = "messages"
_SUMMARY_KEY = "summary"


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
    multi-tenant deployment is isolated automatically. System prompts
    remain orchestration-only and are never stored. Valid assistant
    tool-call blocks are preserved so a later turn can see what action
    the model actually took.
    """

    def __init__(self, kv: KVStore, *, max_turns: int = 32) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be positive")
        self._kv = kv
        self._max_turns = max_turns

    async def load(self, scope_id: str, sender_id: str) -> list[Message]:
        raw = await self._kv.read(
            _history_scope(scope_id),
            _history_file(sender_id),
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
            if role not in ("user", "assistant", "tool"):
                continue
            messages.append(_message_from_history_item(item, role=role, content=content))
        return _history_messages(messages)

    async def save(self, scope_id: str, sender_id: str, messages: Iterable[Message]) -> None:
        trimmed = [_message_to_history_item(m) for m in _trim_history_turns(messages, self._max_turns)]
        payload = json.dumps(trimmed, ensure_ascii=False)
        await self._kv.write(
            _history_scope(scope_id),
            _history_file(sender_id),
            _MESSAGES_KEY,
            payload,
        )

    async def clear(self, scope_id: str, sender_id: str) -> None:
        await self._kv.delete(
            _history_scope(scope_id),
            _history_file(sender_id),
            _MESSAGES_KEY,
        )
        await self.clear_summary(scope_id, sender_id)

    async def load_summary(self, scope_id: str, sender_id: str) -> str:
        raw = await self._kv.read(
            _history_scope(scope_id),
            _history_file(sender_id),
            _SUMMARY_KEY,
            default="",
        )
        return raw if isinstance(raw, str) else ""

    async def save_summary(self, scope_id: str, sender_id: str, summary: str) -> None:
        await self._kv.write(
            _history_scope(scope_id),
            _history_file(sender_id),
            _SUMMARY_KEY,
            summary,
        )

    async def clear_summary(self, scope_id: str, sender_id: str) -> None:
        await self._kv.delete(
            _history_scope(scope_id),
            _history_file(sender_id),
            _SUMMARY_KEY,
        )


def _message_from_history_item(item: dict[str, object], *, role: str, content: str) -> Message:
    name = item.get("name")
    tool_call_id = item.get("tool_call_id")
    reasoning_content = item.get("reasoning_content")
    return Message(
        role=role,
        content=content,
        name=name if isinstance(name, str) else None,
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
        tool_calls=_tool_calls_from_history_item(item.get("tool_calls")),
        reasoning_content=reasoning_content if isinstance(reasoning_content, str) else None,
    )


def _history_scope(scope_id: str) -> str:
    return f"{_HISTORY_SCOPE_PREFIX}/{scope_id}"


def _history_file(sender_id: str) -> str:
    return sender_id or "_group"


def _tool_calls_from_history_item(raw: object) -> list[ToolCall] | None:
    if not isinstance(raw, list):
        return None
    tool_calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        call_id = item.get("id")
        name = item.get("name")
        arguments = item.get("arguments")
        if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, str):
            continue
        tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return tool_calls or None


def _message_to_history_item(message: Message) -> dict[str, object]:
    item: dict[str, object] = {"role": message.role, "content": message.content}
    if message.name is not None:
        item["name"] = message.name
    if message.tool_call_id is not None:
        item["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        item["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in message.tool_calls
        ]
    if message.reasoning_content is not None:
        item["reasoning_content"] = message.reasoning_content
    return item


def _trim_history_turns(messages: Iterable[Message], max_turns: int) -> list[Message]:
    turns: list[list[Message]] = []
    current: list[Message] = []
    for message in _history_messages(messages):
        if message.role == "user":
            if current:
                turns.append(current)
            current = [message]
            continue
        if current:
            current.append(message)
        else:
            current = [message]
    if current:
        turns.append(current)
    return [message for turn in turns[-max_turns:] for message in turn]


def _history_messages(messages: Iterable[Message]) -> list[Message]:
    """Strip system messages and orphan tool results before replay/persist."""
    source = [m for m in messages if m.role in ("user", "assistant", "tool")]
    out: list[Message] = []
    i = 0
    while i < len(source):
        message = source[i]
        if message.role == "assistant" and message.tool_calls:
            expected_ids = [tc.id for tc in message.tool_calls]
            block = [message]
            j = i + 1
            while j < len(source) and len(block) <= len(expected_ids):
                candidate = source[j]
                if candidate.role != "tool":
                    break
                block.append(candidate)
                j += 1
                if len(block) == len(expected_ids) + 1:
                    break
            tool_ids = [tool.tool_call_id for tool in block[1:]]
            if tool_ids == expected_ids:
                out.extend(block)
            i = j
            continue
        if message.role == "tool":
            i += 1
            continue
        out.append(message)
        i += 1
    return out
