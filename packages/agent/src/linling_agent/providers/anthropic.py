"""Anthropic Messages API provider using httpx.

Implements the native Anthropic Messages protocol (``POST /v1/messages``)
without depending on the ``anthropic`` SDK:

* ``x-api-key`` authentication + ``anthropic-version`` header
* top-level ``system`` prompt
* text / image / tool_use / tool_result content blocks
* multimodal images **inside tool results** — Anthropic allows image
  blocks in ``tool_result.content``, unlike OpenAI Chat Completions
  (where tool messages are text-only). This is what lets a vision model
  see a tool's image output directly instead of a ``[图片]`` placeholder.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from linling_agent.errors import LLMAuthError, LLMError, LLMRateLimitError
from linling_agent.llm import (
    ContentPart,
    Delta,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolSchema,
)

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


def _parse_data_uri(uri: str) -> tuple[str, str] | None:
    """Split ``data:<mime>[;param];base64,<payload>`` into ``(mime, payload)``.

    Returns ``None`` for malformed / non-base64 data URIs. The media type
    defaults to ``image/jpeg`` when the URI omits one, matching the
    resolver's conservative sniffing fallback.
    """
    if not uri.startswith("data:"):
        return None
    header, sep, payload = uri[len("data:") :].partition(",")
    if not sep or not payload:
        return None
    params = header.split(";")
    media_type = params[0].strip() or "image/jpeg"
    if "base64" not in {p.strip().lower() for p in params[1:]}:
        return None
    return media_type, payload


def _image_block(image_url: str) -> dict[str, Any] | None:
    """Convert an image reference to an Anthropic image content block.

    ``data:`` URIs become ``base64`` sources; HTTP(S) URLs use Anthropic's
    ``url`` source. Anything else is dropped — the caller degrades to
    text rather than fabricating a placeholder inside the image slot.
    """
    parsed = _parse_data_uri(image_url)
    if parsed is not None:
        media_type, data = parsed
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    if image_url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": image_url}}
    return None


def _text_block(text: str) -> dict[str, Any] | None:
    """Build a non-empty text block (Anthropic rejects empty text blocks)."""
    if not text:
        return None
    return {"type": "text", "text": text}


def _content_parts_to_blocks(
    parts: tuple[ContentPart, ...],
    *,
    allow_images: bool = True,
) -> list[dict[str, Any]]:
    """Convert internal multimodal parts to Anthropic content blocks."""
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if part.type == "image_url":
            if not allow_images:
                continue
            block = _image_block(part.image_url)
        else:
            block = _text_block(part.text)
        if block is not None:
            blocks.append(block)
    return blocks


def _tool_input(arguments: str) -> dict[str, Any]:
    """Parse a tool-call arguments JSON string into an object.

    Anthropic's ``tool_use.input`` is always a JSON object. Malformed
    arguments (which some local models emit) are preserved under a
    ``raw`` key instead of crashing the whole request.
    """
    if not arguments:
        return {}
    try:
        value = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {"raw": arguments}
    if isinstance(value, dict):
        return value
    return {"value": value}


def _map_stop_reason(reason: str | None) -> str | None:
    """Map Anthropic stop reasons onto the internal Delta vocabulary."""
    if reason == "tool_use":
        return "tool_calls"
    if reason in {"end_turn", "stop_sequence"}:
        return "stop"
    if reason == "max_tokens":
        return "length"
    return reason


class AnthropicProvider:
    """Anthropic Messages provider using httpx.

    ``base_url`` accepts the official API root (``https://api.anthropic.com``),
    a ``.../v1`` root, or a complete ``.../messages`` endpoint. The
    endpoint is derived so gateways that expose ``/anthropic/v1/messages``
    work without special casing.
    """

    #: Anthropic tool_result blocks can contain image blocks.
    supports_tool_images = True

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_ANTHROPIC_BASE_URL,
        model: str = "claude-sonnet-4-5",
        default_temperature: float = 0.7,
        default_max_tokens: int = 1024,
        timeout: float = 60.0,
        extra_headers: dict[str, str] | None = None,
        proxy: str | None = None,
        reasoning_effort: str | None = None,
        anthropic_version: str = DEFAULT_ANTHROPIC_VERSION,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._anthropic_version = anthropic_version
        # Accepted for parity with OpenAIProvider / ``AgentDef``. Native
        # extended thinking needs signed thinking blocks to be replayed,
        # which the current Message model doesn't carry; enabling it
        # silently would break the second tool turn, so it stays off.
        self._reasoning_effort = reasoning_effort
        self._extra_headers = dict(extra_headers or {})
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers=self._build_headers(),
            trust_env=False,
            proxy=proxy,
        )

    @property
    def name(self) -> str:
        return f"anthropic:{self._model}"

    @property
    def messages_url(self) -> str:
        """The resolved Messages endpoint for the configured base URL."""
        base = self._base_url.rstrip("/")
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    async def aclose(self) -> None:
        """Close the underlying httpx client (idempotent)."""
        await self._client.aclose()

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "content-type": "application/json",
            "anthropic-version": self._anthropic_version,
            "user-agent": "linling-agent/0.0.0",
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        # Caller-supplied headers win; useful for gateways that expect
        # ``Authorization: Bearer`` or a custom beta header.
        headers.update(self._extra_headers)
        return headers

    # ------------------------------------------------------------------
    # Request construction
    # ------------------------------------------------------------------

    def _build_request_body(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build the JSON body for ``POST /v1/messages``."""
        system, converted = self._convert_messages(messages)
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens or self._default_max_tokens,
            "messages": converted,
            "temperature": temperature,
            "stream": stream,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [self._tool_schema_to_dict(t) for t in tools]
        return body

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Translate internal messages into Anthropic's wire format.

        System messages are hoisted to the top-level ``system`` field.
        ``tool`` messages become ``tool_result`` blocks inside a *user*
        message; consecutive tool results and user messages are merged so
        the resulting history alternates user/assistant as required.
        """
        system_chunks: list[str] = []
        converted: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                text = self._message_text(msg)
                if text.strip():
                    system_chunks.append(text)
                continue
            if msg.role == "tool":
                self._append_blocks(converted, "user", self._tool_result_blocks(msg))
                continue
            if msg.role == "assistant":
                blocks = self._assistant_blocks(msg)
                if blocks:
                    self._append_blocks(converted, "assistant", blocks)
                continue
            # Unknown roles are treated as user content, matching the
            # permissive behaviour of the OpenAI provider.
            blocks = self._user_blocks(msg)
            if blocks:
                self._append_blocks(converted, "user", blocks)
        system = "\n\n".join(system_chunks) if system_chunks else None
        return system, converted

    @staticmethod
    def _append_blocks(
        converted: list[dict[str, Any]],
        role: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        if not blocks:
            return
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
            return
        converted.append({"role": role, "content": blocks})

    @staticmethod
    def _message_text(msg: Message) -> str:
        """Text-only projection of a message (used for system prompts)."""
        if msg.content_parts:
            return "".join(
                part.text for part in msg.content_parts if part.type != "image_url"
            )
        return msg.content

    def _user_blocks(self, msg: Message) -> list[dict[str, Any]]:
        if msg.content_parts:
            blocks = _content_parts_to_blocks(msg.content_parts)
            if blocks:
                return blocks
        block = _text_block(msg.content)
        return [block] if block is not None else []

    def _assistant_blocks(self, msg: Message) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if msg.content_parts:
            # Assistant content is text + tool_use; image blocks are a
            # user-turn feature, so multimodal assistant parts are ignored.
            blocks.extend(_content_parts_to_blocks(msg.content_parts, allow_images=False))
        else:
            block = _text_block(msg.content)
            if block is not None:
                blocks.append(block)
        for call in msg.tool_calls or []:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": _tool_input(call.arguments),
                }
            )
        return blocks

    def _tool_result_blocks(self, msg: Message) -> list[dict[str, Any]]:
        if msg.content_parts:
            content = _content_parts_to_blocks(msg.content_parts)
        else:
            block = _text_block(msg.content)
            content = [block] if block is not None else []
        if not content:
            # An empty tool_result is structurally valid but some gateways
            # reject an empty content array; keep a minimal text block.
            content = [{"type": "text", "text": "(no output)"}]
        return [
            {
                "type": "tool_result",
                "tool_use_id": msg.tool_call_id or "",
                "content": content,
            }
        ]

    @staticmethod
    def _tool_schema_to_dict(tool: ToolSchema) -> dict[str, Any]:
        """Convert a ToolSchema to Anthropic's tool definition shape."""
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> LLMResponse:
        """Parse a Messages response into an :class:`LLMResponse`."""
        content_blocks = data.get("content") or []
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    )
                )
            elif block_type == "thinking":
                thinking_parts.append(block.get("thinking", ""))

        message = Message(
            role="assistant",
            content="".join(text_parts),
            tool_calls=tool_calls or None,
            reasoning_content="".join(thinking_parts) or None,
        )

        usage: TokenUsage | None = None
        if isinstance(data.get("usage"), dict):
            u = data["usage"]
            prompt_tokens = int(u.get("input_tokens", 0) or 0)
            completion_tokens = int(u.get("output_tokens", 0) or 0)
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )
        return LLMResponse(message=message, usage=usage)

    @staticmethod
    def _handle_error_response(response: httpx.Response) -> None:
        """Raise the appropriate error for a non-2xx response."""
        status = response.status_code
        try:
            body = response.json()
            error = body.get("error") or {}
            error_msg = error.get("message") or response.text
        except (json.JSONDecodeError, ValueError):
            error_msg = response.text

        if status in (429, 529):
            retry_after = response.headers.get("retry-after")
            raise LLMRateLimitError(
                f"Rate limit exceeded: {error_msg}",
                retry_after=float(retry_after) if retry_after else None,
            )
        if status in (401, 403):
            raise LLMAuthError(f"Authentication failed: {error_msg}")
        raise LLMError(f"LLM request failed (HTTP {status}): {error_msg}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Send a Messages request and return the full response."""
        body = self._build_request_body(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        response = await self._client.post(self.messages_url, json=body)
        if response.status_code >= 400:
            self._handle_error_response(response)
        return self._parse_response(response.json())

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[Delta]:
        """Stream a Messages response as :class:`Delta` chunks.

        Text deltas are yielded as they arrive. Tool-use input JSON is
        accumulated per content block and emitted as a complete
        :class:`ToolCall` when the block closes, which matches the shape
        callers already consume from OpenAI-style streams.
        """
        body = self._build_request_body(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async with self._client.stream("POST", self.messages_url, json=body) as response:
            if response.status_code >= 400:
                await response.aread()
                self._handle_error_response(response)

            tool_buffers: dict[int, dict[str, str]] = {}
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                index = int(event.get("index", 0) or 0)
                if event_type == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        tool_buffers[index] = {
                            "id": str(block.get("id", "")),
                            "name": str(block.get("name", "")),
                            "json": "",
                        }
                    elif block.get("type") == "text" and block.get("text"):
                        yield Delta(content=block["text"])
                elif event_type == "content_block_delta":
                    delta = event.get("delta") or {}
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            yield Delta(content=text)
                    elif delta_type == "input_json_delta":
                        buf = tool_buffers.get(index)
                        if buf is not None:
                            buf["json"] += delta.get("partial_json", "")
                elif event_type == "content_block_stop":
                    buf = tool_buffers.pop(index, None)
                    if buf is not None:
                        yield Delta(
                            tool_calls=[
                                ToolCall(
                                    id=buf["id"],
                                    name=buf["name"],
                                    arguments=buf["json"] or "{}",
                                )
                            ]
                        )
                elif event_type == "message_delta":
                    stop_reason = (event.get("delta") or {}).get("stop_reason")
                    if stop_reason:
                        yield Delta(finish_reason=_map_stop_reason(stop_reason))
                elif event_type == "error":
                    error = event.get("error") or {}
                    raise LLMError(
                        f"Anthropic stream error: {error.get('message', event)}"
                    )

            # Defensive: some gateways omit ``content_block_stop`` for the
            # final tool block. Emit anything still buffered rather than
            # silently dropping the tool call.
            for buf in tool_buffers.values():
                yield Delta(
                    tool_calls=[
                        ToolCall(
                            id=buf["id"],
                            name=buf["name"],
                            arguments=buf["json"] or "{}",
                        )
                    ]
                )
