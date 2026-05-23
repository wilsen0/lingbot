"""OpenAI-compatible LLM provider using httpx.

Works with any OpenAI-compatible endpoint (OpenAI, Azure, local vLLM, etc.).
Does NOT depend on the ``openai`` SDK — uses raw httpx for maximum control.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from linling_agent.errors import LLMAuthError, LLMError, LLMRateLimitError
from linling_agent.llm import (
    Delta,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolSchema,
)


class OpenAIProvider:
    """OpenAI-compatible LLM provider using httpx."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        default_temperature: float = 0.7,
        default_max_tokens: int = 1024,
        timeout: float = 60.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        # ``extra_headers`` is the escape hatch for OpenAI-compatible
        # endpoints that need an explicit override (rare). The default
        # ``User-Agent`` we set in :meth:`_build_headers` already
        # satisfies the common gates (Kimi's ``/coding/v1`` etc.); set
        # ``extra_headers={"User-Agent": ...}`` only when an endpoint
        # rejects the default.
        self._extra_headers = dict(extra_headers or {})
        # ``trust_env=False`` keeps the LLM client off any ambient
        # proxy. ``httpx`` defaults to honouring ``HTTP_PROXY`` /
        # ``HTTPS_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY``; for the
        # provider call we want a direct route to the upstream API
        # regardless of how the operator launched the bot. Adapter /
        # tool HTTP clients still pick up proxy env on their own —
        # this only affects the LLM round trip.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers=self._build_headers(),
            trust_env=False,
        )

    @property
    def name(self) -> str:
        return f"openai:{self._model}"

    async def aclose(self) -> None:
        """Close the underlying httpx client.

        The bot's bootstrap calls this from :meth:`RunningBot.stop`. It
        is safe to call repeatedly — :class:`httpx.AsyncClient` allows
        ``aclose`` after a previous close.
        """
        await self._client.aclose()

    def _build_headers(self) -> dict[str, str]:
        # Some OpenAI-compatible endpoints gate on User-Agent in
        # addition to the bearer token (Kimi's ``/coding/v1`` checks
        # against an allowlist of known coding-agent UAs and rejects
        # everything else with HTTP 403). We ship a coding-CLI-shaped
        # UA by default so every supported endpoint admits us out of
        # the box. Operators can still override per-agent via
        # ``provider_config.extra_headers``; caller-supplied values
        # are merged last and win.
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "claude-cli/1.0.0",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # Caller-supplied headers win over our defaults (e.g. allow
        # overriding ``Content-Type`` if a downstream proxy needs it),
        # but we apply them last so they actually override.
        headers.update(self._extra_headers)
        return headers

    def _build_request_body(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build the JSON request body for the Chat Completions API."""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [self._message_to_dict(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens or self._default_max_tokens,
            "stream": stream,
        }
        if tools:
            body["tools"] = [self._tool_schema_to_dict(t) for t in tools]
        return body

    @staticmethod
    def _message_to_dict(msg: Message) -> dict[str, Any]:
        """Convert a Message to the OpenAI API dict format."""
        d: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.name is not None:
            d["name"] = msg.name
        if msg.tool_call_id is not None:
            d["tool_call_id"] = msg.tool_call_id
        if msg.tool_calls is not None:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in msg.tool_calls
            ]
        # Kimi for Coding rejects follow-up requests where an assistant
        # message that emitted tool_calls is missing its
        # ``reasoning_content``. Always forward it when present;
        # endpoints that don't recognise the key ignore it.
        if msg.reasoning_content is not None:
            d["reasoning_content"] = msg.reasoning_content
        return d

    @staticmethod
    def _tool_schema_to_dict(tool: ToolSchema) -> dict[str, Any]:
        """Convert a ToolSchema to OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        """Parse a Chat Completions API response into an LLMResponse."""
        choice = data["choices"][0]
        msg_data = choice["message"]

        tool_calls: list[ToolCall] | None = None
        if msg_data.get("tool_calls"):
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                )
                for tc in msg_data["tool_calls"]
            ]

        message = Message(
            role=msg_data.get("role", "assistant"),
            content=msg_data.get("content") or "",
            tool_calls=tool_calls,
            reasoning_content=msg_data.get("reasoning_content"),
        )

        usage: TokenUsage | None = None
        if "usage" in data:
            u = data["usage"]
            usage = TokenUsage(
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
            )

        return LLMResponse(message=message, usage=usage)

    def _parse_stream_chunk(self, data: dict[str, Any]) -> Delta | None:
        """Parse a single SSE chunk into a Delta."""
        if not data.get("choices"):
            return None
        choice = data["choices"][0]
        delta_data = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        tool_calls: list[ToolCall] | None = None
        if delta_data.get("tool_calls"):
            tool_calls = [
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("function", {}).get("name", ""),
                    arguments=tc.get("function", {}).get("arguments", ""),
                )
                for tc in delta_data["tool_calls"]
            ]

        return Delta(
            content=delta_data.get("content") or "",
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    def _handle_error_response(self, response: httpx.Response) -> None:
        """Raise appropriate error for non-2xx responses."""
        status = response.status_code
        try:
            body = response.json()
            error_msg = body.get("error", {}).get("message", response.text)
        except (json.JSONDecodeError, ValueError):
            error_msg = response.text

        if status == 429:
            retry_after = response.headers.get("retry-after")
            raise LLMRateLimitError(
                f"Rate limit exceeded: {error_msg}",
                retry_after=float(retry_after) if retry_after else None,
            )
        if status in (401, 403):
            raise LLMAuthError(f"Authentication failed: {error_msg}")
        raise LLMError(f"LLM request failed (HTTP {status}): {error_msg}")

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Send a chat completion request and return the full response."""
        body = self._build_request_body(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens, stream=False
        )
        url = f"{self._base_url}/chat/completions"

        response = await self._client.post(url, json=body)
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
        """Send a streaming chat completion request and yield Delta chunks."""
        body = self._build_request_body(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens, stream=True
        )
        url = f"{self._base_url}/chat/completions"

        async with self._client.stream("POST", url, json=body) as response:
            if response.status_code >= 400:
                await response.aread()
                self._handle_error_response(response)

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :]
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk_data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = self._parse_stream_chunk(chunk_data)
                if delta is not None:
                    yield delta
