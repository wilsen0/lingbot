"""LLM provider protocol and types.

Defines the abstract interface that all LLM providers must implement,
along with shared data types for messages, tool calls, and streaming.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolCall:
    """A tool call requested by the LLM."""

    id: str
    name: str
    arguments: str  # JSON string


@dataclass(frozen=True)
class ContentPart:
    """A single part of a multimodal message content array.

    ``type`` is ``"text"`` (use ``text``) or ``"image_url"`` (use
    ``image_url``, a data URI like ``data:image/png;base64,...``).
    """

    type: str  # "text" | "image_url"
    text: str = ""
    image_url: str = ""


@dataclass(frozen=True)
class Message:
    """A single message in a conversation."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None  # for tool messages
    tool_call_id: str | None = None  # for tool result messages
    tool_calls: list[ToolCall] | None = None  # for assistant messages with tool calls
    # Kimi-style "thinking" trace. Some OpenAI-compatible endpoints
    # (notably ``api.kimi.com/coding/v1``) emit a separate
    # ``reasoning_content`` alongside ``content``, and *require* it to
    # be echoed back on the next turn when the assistant produced
    # ``tool_calls`` — otherwise the next call returns
    # ``HTTP 400: reasoning_content is missing``. Other providers
    # (vanilla OpenAI, Anthropic-via-proxy) ignore the field, so it's
    # safe to thread through unconditionally.
    reasoning_content: str | None = None
    # Transient multimodal field: when set, the content array (each part
    # ``text`` or ``image_url``) is serialised instead of the ``content``
    # string. Only used for a single LLM request — never persisted to
    # history, which stores the ``content`` string alone.
    content_parts: tuple[ContentPart, ...] | None = None


def count_image_parts(message: Message) -> int:
    """Number of image_url parts in a message (0 if none / no parts)."""
    if not message.content_parts:
        return 0
    return sum(1 for p in message.content_parts if p.type == "image_url")


def message_has_images(message: Message) -> bool:
    """True iff the message carries at least one image_url part."""
    return count_image_parts(message) > 0


@dataclass(frozen=True)
class Delta:
    """A streaming chunk from the LLM."""

    content: str = ""
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None  # "stop" | "tool_calls" | None


@dataclass(frozen=True)
class TokenUsage:
    """Token usage statistics."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """Complete (non-streaming) response from the LLM."""

    message: Message
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class ToolSchema:
    """Tool definition for LLM function calling."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@runtime_checkable
class LLMProvider(Protocol):
    """Abstract LLM provider interface."""

    @property
    def name(self) -> str: ...

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    # NOTE: not ``async def`` — this is an async-generator protocol.
    # ``async def foo(...) -> AsyncIterator[T]`` means "a coroutine that
    # returns an iterator", whereas concrete providers use ``async def``
    # + ``yield`` which produces an async iterator directly. Declaring
    # this as a regular method with ``AsyncIterator`` return type keeps
    # the two shapes compatible under the strict mypy config.
    def chat_stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[Delta]: ...
