"""Tests for :class:`AnthropicProvider` (native Messages protocol)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from linling_agent import (
    AnthropicProvider,
    Delta,
    LLMProvider,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from linling_agent.errors import LLMAuthError, LLMError, LLMRateLimitError
from linling_agent.llm import ContentPart


def _provider(**kwargs) -> AnthropicProvider:
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("model", "claude-test")
    return AnthropicProvider(**kwargs)


# ---------------------------------------------------------------------------
# Request body / content conversion
# ---------------------------------------------------------------------------


class TestAnthropicRequestBody:
    def test_basic_request_body(self) -> None:
        provider = _provider()
        body = provider._build_request_body(
            [Message(role="user", content="Hello")],
            temperature=0.5,
            max_tokens=100,
        )
        assert body["model"] == "claude-test"
        assert body["temperature"] == 0.5
        assert body["max_tokens"] == 100
        assert body["stream"] is False
        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
        ]
        assert "system" not in body
        assert "tools" not in body

    def test_system_messages_are_hoisted_and_joined(self) -> None:
        provider = _provider()
        body = provider._build_request_body(
            [
                Message(role="system", content="You are helpful."),
                Message(role="system", content="Be concise."),
                Message(role="user", content="Hi"),
            ]
        )
        assert body["system"] == "You are helpful.\n\nBe concise."
        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Hi"}]}
        ]

    def test_default_max_tokens_used(self) -> None:
        provider = _provider(default_max_tokens=2048)
        body = provider._build_request_body([Message(role="user", content="Hi")])
        assert body["max_tokens"] == 2048

    def test_user_image_data_uri_becomes_base64_block(self) -> None:
        provider = _provider()
        body = provider._build_request_body(
            [
                Message(
                    role="user",
                    content="what is this?",
                    content_parts=(
                        ContentPart(type="text", text="what is this?"),
                        ContentPart(
                            type="image_url",
                            image_url="data:image/png;base64,AAAA",
                        ),
                    ),
                )
            ]
        )
        blocks = body["messages"][0]["content"]
        assert blocks == [
            {"type": "text", "text": "what is this?"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "AAAA",
                },
            },
        ]

    def test_user_image_http_url_becomes_url_block(self) -> None:
        provider = _provider()
        body = provider._build_request_body(
            [
                Message(
                    role="user",
                    content="",
                    content_parts=(
                        ContentPart(
                            type="image_url",
                            image_url="https://example.test/cat.png",
                        ),
                    ),
                )
            ]
        )
        assert body["messages"][0]["content"] == [
            {
                "type": "image",
                "source": {"type": "url", "url": "https://example.test/cat.png"},
            }
        ]

    def test_unsupported_image_ref_is_dropped(self) -> None:
        provider = _provider()
        body = provider._build_request_body(
            [
                Message(
                    role="user",
                    content="",
                    content_parts=(
                        ContentPart(type="image_url", image_url="ftp://example.test/x.png"),
                    ),
                )
            ]
        )
        assert body["messages"] == []

    def test_assistant_tool_calls_become_tool_use_blocks(self) -> None:
        provider = _provider()
        body = provider._build_request_body(
            [
                Message(
                    role="assistant",
                    content="Looking...",
                    tool_calls=[
                        ToolCall(
                            id="toolu_1",
                            name="get_weather",
                            arguments='{"city": "NYC"}',
                        )
                    ],
                )
            ]
        )
        assert body["messages"][0] == {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Looking..."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "input": {"city": "NYC"},
                },
            ],
        }

    def test_malformed_tool_arguments_are_preserved(self) -> None:
        provider = _provider()
        body = provider._build_request_body(
            [
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(id="toolu_1", name="bad", arguments="{not json")
                    ],
                )
            ]
        )
        block = body["messages"][0]["content"][0]
        assert block["input"] == {"raw": "{not json"}

    def test_tool_result_becomes_user_tool_result_block(self) -> None:
        provider = _provider()
        body = provider._build_request_body(
            [
                Message(
                    role="tool",
                    content="72°F",
                    name="get_weather",
                    tool_call_id="toolu_1",
                )
            ]
        )
        assert body["messages"] == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": "72°F"}],
                    }
                ],
            }
        ]

    def test_tool_result_can_carry_images(self) -> None:
        """The whole point: Anthropic accepts image blocks inside tool_result."""
        provider = _provider()
        body = provider._build_request_body(
            [
                Message(
                    role="tool",
                    content="",  # text travels via content_parts
                    name="render_chart",
                    tool_call_id="toolu_1",
                    content_parts=(
                        ContentPart(type="text", text="chart rendered"),
                        ContentPart(
                            type="image_url",
                            image_url="data:image/png;base64,AAAA",
                        ),
                    ),
                )
            ]
        )
        tool_result = body["messages"][0]["content"][0]
        assert tool_result["type"] == "tool_result"
        assert tool_result["tool_use_id"] == "toolu_1"
        assert tool_result["content"] == [
            {"type": "text", "text": "chart rendered"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "AAAA",
                },
            },
        ]

    def test_consecutive_tool_results_merge_into_one_user_turn(self) -> None:
        provider = _provider()
        body = provider._build_request_body(
            [
                Message(role="tool", content="one", tool_call_id="toolu_1"),
                Message(role="tool", content="two", tool_call_id="toolu_2"),
            ]
        )
        assert len(body["messages"]) == 1
        assert body["messages"][0]["role"] == "user"
        assert len(body["messages"][0]["content"]) == 2

    def test_tool_schema_uses_input_schema(self) -> None:
        provider = _provider()
        tool = ToolSchema(
            name="search",
            description="Search the web",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        body = provider._build_request_body(
            [Message(role="user", content="hi")], tools=[tool]
        )
        assert body["tools"] == [
            {
                "name": "search",
                "description": "Search the web",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestAnthropicParseResponse:
    def test_parse_text_and_usage(self) -> None:
        data = {
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }
        result = AnthropicProvider._parse_response(data)
        assert isinstance(result, LLMResponse)
        assert result.message.role == "assistant"
        assert result.message.content == "Hello!"
        assert result.message.tool_calls is None
        assert result.usage == TokenUsage(
            prompt_tokens=10, completion_tokens=4, total_tokens=14
        )

    def test_parse_tool_use(self) -> None:
        data = {
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "input": {"city": "NYC"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 8, "output_tokens": 6},
        }
        result = AnthropicProvider._parse_response(data)
        assert result.message.content == "Let me check."
        assert result.message.tool_calls is not None
        call = result.message.tool_calls[0]
        assert call.id == "toolu_1"
        assert call.name == "get_weather"
        assert json.loads(call.arguments) == {"city": "NYC"}

    def test_parse_thinking_block(self) -> None:
        data = {
            "content": [
                {"type": "thinking", "thinking": "Let me reason."},
                {"type": "text", "text": "Done."},
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        result = AnthropicProvider._parse_response(data)
        assert result.message.content == "Done."
        assert result.message.reasoning_content == "Let me reason."

    def test_parse_without_usage(self) -> None:
        result = AnthropicProvider._parse_response(
            {"content": [{"type": "text", "text": "Hi"}]}
        )
        assert result.usage is None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestAnthropicErrors:
    def _response(self, status: int, body: dict | str = "", headers=None):
        content = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
        return httpx.Response(
            status_code=status,
            content=content,
            headers=headers or {},
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )

    def test_401_raises_auth_error(self) -> None:
        with pytest.raises(LLMAuthError, match="Authentication failed"):
            AnthropicProvider._handle_error_response(
                self._response(
                    401,
                    {"type": "error", "error": {"message": "invalid x-api-key"}},
                )
            )

    def test_429_raises_rate_limit_with_retry_after(self) -> None:
        with pytest.raises(LLMRateLimitError) as exc_info:
            AnthropicProvider._handle_error_response(
                self._response(
                    429,
                    {"type": "error", "error": {"message": "slow down"}},
                    headers={"retry-after": "2.5"},
                )
            )
        assert exc_info.value.retry_after == 2.5

    def test_529_is_treated_as_rate_limit(self) -> None:
        with pytest.raises(LLMRateLimitError):
            AnthropicProvider._handle_error_response(
                self._response(
                    529,
                    {"type": "error", "error": {"message": "overloaded"}},
                )
            )

    def test_500_raises_llm_error(self) -> None:
        with pytest.raises(LLMError, match="HTTP 500"):
            AnthropicProvider._handle_error_response(
                self._response(500, {"error": {"message": "boom"}})
            )

    def test_non_json_error_body(self) -> None:
        with pytest.raises(LLMError, match="Something went wrong"):
            AnthropicProvider._handle_error_response(
                self._response(500, "Something went wrong")
            )


# ---------------------------------------------------------------------------
# chat / stream
# ---------------------------------------------------------------------------


class TestAnthropicChat:
    async def test_chat_success(self) -> None:
        provider = _provider()
        mock_response = httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Hello!"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
            request=httpx.Request(
                "POST", "https://api.anthropic.com/v1/messages"
            ),
        )
        with patch.object(provider._client, "post", new_callable=AsyncMock) as post:
            post.return_value = mock_response
            result = await provider.chat([Message(role="user", content="Hi")])

        assert result.message.content == "Hello!"
        assert result.usage is not None
        assert result.usage.total_tokens == 7
        post.assert_awaited_once()

    async def test_chat_error(self) -> None:
        provider = _provider()
        mock_response = httpx.Response(
            401,
            json={"error": {"message": "bad key"}},
            request=httpx.Request(
                "POST", "https://api.anthropic.com/v1/messages"
            ),
        )
        with patch.object(provider._client, "post", new_callable=AsyncMock) as post:
            post.return_value = mock_response
            with pytest.raises(LLMAuthError):
                await provider.chat([Message(role="user", content="Hi")])


class TestAnthropicStream:
    @staticmethod
    def _mock_stream(lines: list[str]) -> MagicMock:
        response = MagicMock()
        response.status_code = 200
        response.headers = {}

        async def aiter_lines():
            for line in lines:
                yield line

        response.aiter_lines = aiter_lines
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=response)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    async def test_stream_text_and_stop(self) -> None:
        provider = _provider()
        lines = [
            'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}',
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}',
            'data: {"type":"content_block_stop","index":0}',
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
            'data: {"type":"message_stop"}',
        ]
        with patch.object(provider._client, "stream", return_value=self._mock_stream(lines)):
            deltas: list[Delta] = []
            async for delta in provider.chat_stream([Message(role="user", content="Hi")]):
                deltas.append(delta)

        assert [d.content for d in deltas if d.content] == ["Hello", " world"]
        assert deltas[-1].finish_reason == "stop"

    async def test_stream_tool_use_is_accumulated(self) -> None:
        provider = _provider()
        lines = [
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"get_weather","input":{}}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":"}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\\"NYC\\"}"}}',
            'data: {"type":"content_block_stop","index":0}',
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
        ]
        with patch.object(provider._client, "stream", return_value=self._mock_stream(lines)):
            deltas: list[Delta] = []
            async for delta in provider.chat_stream([Message(role="user", content="Hi")]):
                deltas.append(delta)

        tool_deltas = [d for d in deltas if d.tool_calls]
        assert len(tool_deltas) == 1
        call = tool_deltas[0].tool_calls[0]
        assert call.id == "toolu_1"
        assert call.name == "get_weather"
        assert json.loads(call.arguments) == {"city": "NYC"}
        assert deltas[-1].finish_reason == "tool_calls"

    async def test_stream_error_event(self) -> None:
        provider = _provider()
        lines = [
            'data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}',
        ]
        with (
            patch.object(provider._client, "stream", return_value=self._mock_stream(lines)),
            pytest.raises(LLMError, match="busy"),
        ):
            async for _ in provider.chat_stream([Message(role="user", content="Hi")]):
                pass


# ---------------------------------------------------------------------------
# Protocol / headers / endpoint
# ---------------------------------------------------------------------------


class TestAnthropicProtocol:
    def test_satisfies_llm_provider_protocol(self) -> None:
        assert isinstance(_provider(), LLMProvider)

    def test_name(self) -> None:
        assert _provider(model="claude-opus").name == "anthropic:claude-opus"

    def test_headers_use_x_api_key_and_version(self) -> None:
        provider = _provider(api_key="sk-ant", anthropic_version="2024-01-01")
        headers = provider._build_headers()
        assert headers["x-api-key"] == "sk-ant"
        assert headers["anthropic-version"] == "2024-01-01"
        assert "authorization" not in headers

    def test_extra_headers_override(self) -> None:
        provider = _provider(extra_headers={"Authorization": "Bearer proxy-token"})
        headers = provider._build_headers()
        assert headers["Authorization"] == "Bearer proxy-token"

    @pytest.mark.parametrize(
        ("base", "expected"),
        [
            ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
            ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
            ("https://api.anthropic.com/v1/", "https://api.anthropic.com/v1/messages"),
            (
                "https://gateway.test/anthropic/v1/messages",
                "https://gateway.test/anthropic/v1/messages",
            ),
            ("https://gateway.test/anthropic", "https://gateway.test/anthropic/v1/messages"),
        ],
    )
    def test_messages_url(self, base: str, expected: str) -> None:
        assert _provider(base_url=base).messages_url == expected

    def test_supports_tool_images_flag(self) -> None:
        assert _provider().supports_tool_images is True
