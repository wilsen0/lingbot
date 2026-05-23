"""Tests for the LLM provider abstraction layer."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from linling_agent import (
    Delta,
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    OpenAIProvider,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from linling_agent.errors import LLMAuthError, LLMRateLimitError

# ---------------------------------------------------------------------------
# Dataclass creation tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_message_creation(self):
        msg = Message(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.name is None
        assert msg.tool_call_id is None
        assert msg.tool_calls is None

    def test_message_with_tool_calls(self):
        tc = ToolCall(id="call_1", name="get_weather", arguments='{"city": "NYC"}')
        msg = Message(role="assistant", content="", tool_calls=[tc])
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].name == "get_weather"

    def test_message_tool_result(self):
        msg = Message(role="tool", content="72°F", name="get_weather", tool_call_id="call_1")
        assert msg.role == "tool"
        assert msg.name == "get_weather"
        assert msg.tool_call_id == "call_1"

    def test_tool_call_creation(self):
        tc = ToolCall(id="call_abc", name="search", arguments='{"q": "test"}')
        assert tc.id == "call_abc"
        assert tc.name == "search"
        assert tc.arguments == '{"q": "test"}'

    def test_delta_creation(self):
        d = Delta(content="Hello")
        assert d.content == "Hello"
        assert d.tool_calls is None
        assert d.finish_reason is None

    def test_delta_with_finish_reason(self):
        d = Delta(content="", finish_reason="stop")
        assert d.finish_reason == "stop"

    def test_delta_with_tool_calls(self):
        tc = ToolCall(id="call_1", name="fn", arguments="{}")
        d = Delta(tool_calls=[tc], finish_reason="tool_calls")
        assert d.tool_calls is not None
        assert d.finish_reason == "tool_calls"

    def test_token_usage_creation(self):
        usage = TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        assert usage.prompt_tokens == 10
        assert usage.completion_tokens == 20
        assert usage.total_tokens == 30

    def test_token_usage_defaults(self):
        usage = TokenUsage()
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0

    def test_llm_response_creation(self):
        msg = Message(role="assistant", content="Hi!")
        usage = TokenUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7)
        resp = LLMResponse(message=msg, usage=usage)
        assert resp.message.content == "Hi!"
        assert resp.usage is not None
        assert resp.usage.total_tokens == 7

    def test_tool_schema_creation(self):
        schema = ToolSchema(
            name="get_weather",
            description="Get weather for a city",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        )
        assert schema.name == "get_weather"
        assert schema.description == "Get weather for a city"
        assert schema.parameters["type"] == "object"


# ---------------------------------------------------------------------------
# OpenAIProvider unit tests
# ---------------------------------------------------------------------------


class TestOpenAIProviderBuildRequestBody:
    def test_basic_request_body(self):
        provider = OpenAIProvider(api_key="test-key", model="gpt-4o")
        messages = [Message(role="user", content="Hello")]
        body = provider._build_request_body(messages, temperature=0.5, max_tokens=100)

        assert body["model"] == "gpt-4o"
        assert body["temperature"] == 0.5
        assert body["max_tokens"] == 100
        assert body["stream"] is False
        assert len(body["messages"]) == 1
        assert body["messages"][0] == {"role": "user", "content": "Hello"}
        assert "tools" not in body

    def test_request_body_with_tools(self):
        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role="user", content="What's the weather?")]
        tools = [
            ToolSchema(
                name="get_weather",
                description="Get weather",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            )
        ]
        body = provider._build_request_body(messages, tools=tools)

        assert "tools" in body
        assert len(body["tools"]) == 1
        assert body["tools"][0] == {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }

    def test_request_body_stream(self):
        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role="user", content="Hi")]
        body = provider._build_request_body(messages, stream=True)
        assert body["stream"] is True

    def test_request_body_with_system_message(self):
        provider = OpenAIProvider(api_key="test-key")
        messages = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hi"),
        ]
        body = provider._build_request_body(messages)
        assert body["messages"][0] == {"role": "system", "content": "You are helpful."}
        assert body["messages"][1] == {"role": "user", "content": "Hi"}

    def test_request_body_with_tool_message(self):
        provider = OpenAIProvider(api_key="test-key")
        messages = [
            Message(role="tool", content="72°F", name="get_weather", tool_call_id="call_1"),
        ]
        body = provider._build_request_body(messages)
        msg = body["messages"][0]
        assert msg["role"] == "tool"
        assert msg["content"] == "72°F"
        assert msg["name"] == "get_weather"
        assert msg["tool_call_id"] == "call_1"

    def test_request_body_with_assistant_tool_calls(self):
        provider = OpenAIProvider(api_key="test-key")
        tc = ToolCall(id="call_1", name="get_weather", arguments='{"city": "NYC"}')
        messages = [Message(role="assistant", content="", tool_calls=[tc])]
        body = provider._build_request_body(messages)
        msg = body["messages"][0]
        assert msg["tool_calls"] == [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'},
            }
        ]

    def test_default_max_tokens_used(self):
        provider = OpenAIProvider(api_key="test-key", default_max_tokens=2048)
        messages = [Message(role="user", content="Hi")]
        body = provider._build_request_body(messages)
        assert body["max_tokens"] == 2048


class TestOpenAIProviderParseResponse:
    def test_parse_basic_response(self):
        provider = OpenAIProvider(api_key="test-key")
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello! How can I help?",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 8,
                "total_tokens": 18,
            },
        }
        result = provider._parse_response(data)
        assert isinstance(result, LLMResponse)
        assert result.message.role == "assistant"
        assert result.message.content == "Hello! How can I help?"
        assert result.message.tool_calls is None
        assert result.usage is not None
        assert result.usage.prompt_tokens == 10
        assert result.usage.completion_tokens == 8
        assert result.usage.total_tokens == 18

    def test_parse_response_with_tool_calls(self):
        provider = OpenAIProvider(api_key="test-key")
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc123",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "San Francisco"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 15,
                "completion_tokens": 12,
                "total_tokens": 27,
            },
        }
        result = provider._parse_response(data)
        assert result.message.content == ""
        assert result.message.tool_calls is not None
        assert len(result.message.tool_calls) == 1
        tc = result.message.tool_calls[0]
        assert tc.id == "call_abc123"
        assert tc.name == "get_weather"
        assert tc.arguments == '{"city": "San Francisco"}'

    def test_parse_response_no_usage(self):
        provider = OpenAIProvider(api_key="test-key")
        data = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
        }
        result = provider._parse_response(data)
        assert result.usage is None


class TestOpenAIProviderErrors:
    @pytest.fixture
    def provider(self):
        return OpenAIProvider(api_key="test-key")

    def _make_response(self, status_code: int, body: dict | str = "", headers=None):
        """Create a mock httpx.Response."""
        if isinstance(body, dict):
            content = json.dumps(body).encode()
        else:
            content = body.encode() if isinstance(body, str) else body
        return httpx.Response(
            status_code=status_code,
            content=content,
            headers=headers or {},
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )

    def test_401_raises_auth_error(self, provider):
        response = self._make_response(401, {"error": {"message": "Invalid API key"}})
        with pytest.raises(LLMAuthError, match="Authentication failed"):
            provider._handle_error_response(response)

    def test_403_raises_auth_error(self, provider):
        response = self._make_response(403, {"error": {"message": "Forbidden"}})
        with pytest.raises(LLMAuthError, match="Authentication failed"):
            provider._handle_error_response(response)

    def test_429_raises_rate_limit_error(self, provider):
        response = self._make_response(
            429,
            {"error": {"message": "Too many requests"}},
            headers={"retry-after": "2.5"},
        )
        with pytest.raises(LLMRateLimitError) as exc_info:
            provider._handle_error_response(response)
        assert exc_info.value.retry_after == 2.5

    def test_429_without_retry_after(self, provider):
        response = self._make_response(429, {"error": {"message": "Too many requests"}})
        with pytest.raises(LLMRateLimitError) as exc_info:
            provider._handle_error_response(response)
        assert exc_info.value.retry_after is None

    def test_500_raises_llm_error(self, provider):
        response = self._make_response(500, {"error": {"message": "Internal server error"}})
        with pytest.raises(LLMError, match="HTTP 500"):
            provider._handle_error_response(response)

    def test_error_with_non_json_body(self, provider):
        response = self._make_response(500, "Something went wrong")
        with pytest.raises(LLMError, match="Something went wrong"):
            provider._handle_error_response(response)


class TestOpenAIProviderChat:
    @pytest.fixture
    def provider(self):
        return OpenAIProvider(api_key="test-key", model="gpt-4o-mini")

    async def test_chat_success(self, provider):
        mock_response = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hello!"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await provider.chat([Message(role="user", content="Hi")])

        assert result.message.content == "Hello!"
        assert result.usage is not None
        assert result.usage.total_tokens == 7

    async def test_chat_http_error(self, provider):
        mock_response = httpx.Response(
            401,
            json={"error": {"message": "Unauthorized"}},
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            with pytest.raises(LLMAuthError):
                await provider.chat([Message(role="user", content="Hi")])


class TestOpenAIProviderStream:
    @pytest.fixture
    def provider(self):
        return OpenAIProvider(api_key="test-key", model="gpt-4o-mini")

    async def test_chat_stream_success(self, provider):
        sse_lines = [
            'data: {"choices":[{"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200

        async def mock_aiter_lines():
            for line in sse_lines:
                yield line

        mock_response.aiter_lines = mock_aiter_lines

        async_cm = AsyncMock()
        async_cm.__aenter__ = AsyncMock(return_value=mock_response)
        async_cm.__aexit__ = AsyncMock(return_value=False)

        with patch.object(provider._client, "stream", return_value=async_cm):
            deltas = []
            async for delta in provider.chat_stream([Message(role="user", content="Hi")]):
                deltas.append(delta)

        assert len(deltas) == 3
        assert deltas[0].content == "Hello"
        assert deltas[1].content == " world"
        assert deltas[2].finish_reason == "stop"

    async def test_chat_stream_error(self, provider):
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"retry-after": "1.0"}

        async def mock_aread():
            mock_response.text = "Rate limited"
            return b'{"error": {"message": "Rate limited"}}'

        mock_response.aread = mock_aread
        mock_response.text = '{"error": {"message": "Rate limited"}}'

        # Make json() work
        def mock_json():
            return {"error": {"message": "Rate limited"}}

        mock_response.json = mock_json

        async_cm = AsyncMock()
        async_cm.__aenter__ = AsyncMock(return_value=mock_response)
        async_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(provider._client, "stream", return_value=async_cm),
            pytest.raises(LLMRateLimitError),
        ):
            async for _ in provider.chat_stream([Message(role="user", content="Hi")]):
                pass


class TestToolSchemaConversion:
    def test_tool_schema_to_openai_format(self):
        provider = OpenAIProvider(api_key="test-key")
        tool = ToolSchema(
            name="search",
            description="Search the web",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results"},
                },
                "required": ["query"],
            },
        )
        result = provider._tool_schema_to_dict(tool)
        assert result == {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search the web",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Max results"},
                    },
                    "required": ["query"],
                },
            },
        }


class TestProtocolCompliance:
    def test_openai_provider_satisfies_protocol(self):
        """Verify OpenAIProvider is recognized as implementing LLMProvider."""
        provider = OpenAIProvider(api_key="test-key")
        assert isinstance(provider, LLMProvider)

    def test_provider_name_property(self):
        provider = OpenAIProvider(api_key="test-key", model="gpt-4o")
        assert provider.name == "openai:gpt-4o"

    def test_provider_custom_base_url(self):
        provider = OpenAIProvider(
            api_key="test-key",
            base_url="http://localhost:8000/v1/",
            model="local-model",
        )
        assert provider.name == "openai:local-model"
