"""Tests for agent definition and runtime (ReAct loop)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import linling_core.tools_builtin  # noqa: F401 — registers built-in tools
import pytest
from linling_agent.agent_def import AgentDef, AgentGuardrails, AgentTrigger
from linling_agent.llm import (
    Delta,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from linling_agent.runtime import _MAX_TOOL_RESULT_CHARS, AgentRuntime
from linling_core.events import Event, Scope, User
from linling_core.segments import TextSegment
from linling_core.tools import ToolCtx, ToolRegistry
from linling_core.tools import registry as global_registry

# ---------------------------------------------------------------------------
# Mock LLM Provider
# ---------------------------------------------------------------------------


class MockProvider:
    """Mock LLM provider that returns pre-configured responses."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = responses
        self._call_count = 0

    @property
    def name(self) -> str:
        return "mock"

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        resp = self._responses[self._call_count]
        self._call_count += 1
        return resp

    async def chat_stream(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncIterator[Delta]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Mock KV Store
# ---------------------------------------------------------------------------


class MockKVStore:
    """In-memory KV store for testing."""

    def __init__(self):
        self._data: dict[str, str] = {}

    async def read(self, scope: str, file: str, key: str, default=None):
        return self._data.get(f"{scope}/{file}/{key}", default)

    async def write(self, scope: str, file: str, key: str, value: str):
        self._data[f"{scope}/{file}/{key}"] = value

    async def delete(self, scope: str, file=None, key=None):
        return 0

    async def keys(self, scope: str, file: str):
        return []

    async def files(self, scope: str):
        return []

    async def rank_rows(self, scope, file, *, order=None, top=10):
        return []

    async def rank(self, scope, file, *, order=None, top=10, sep="\n", fmt=""):
        return ""

    async def close(self):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kv() -> MockKVStore:
    return MockKVStore()


@pytest.fixture
def tool_registry() -> ToolRegistry:
    """Return the global registry (has built-in tools registered)."""
    return global_registry


def make_text_response(content: str, tokens: int = 10) -> LLMResponse:
    """Helper to create a simple text LLMResponse."""
    return LLMResponse(
        message=Message(role="assistant", content=content),
        usage=TokenUsage(
            prompt_tokens=tokens // 2, completion_tokens=tokens // 2, total_tokens=tokens
        ),
    )


def make_tool_call_response(
    tool_name: str,
    arguments: dict,
    call_id: str = "call_1",
    tokens: int = 15,
) -> LLMResponse:
    """Helper to create an LLMResponse with a tool call."""
    return LLMResponse(
        message=Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name=tool_name, arguments=json.dumps(arguments))],
        ),
        usage=TokenUsage(
            prompt_tokens=tokens // 2,
            completion_tokens=tokens // 2,
            total_tokens=tokens,
        ),
    )


# ---------------------------------------------------------------------------
# AgentDef tests
# ---------------------------------------------------------------------------


class TestAgentDefFromDict:
    def test_basic_parsing(self):
        data = {
            "name": "test-agent",
            "provider": "openai",
            "model": "gpt-4o",
            "system": "You are helpful.",
            "tools": ["read_kv", "write_kv"],
            "temperature": 0.5,
        }
        agent = AgentDef.from_dict(data)
        assert agent.name == "test-agent"
        assert agent.provider == "openai"
        assert agent.model == "gpt-4o"
        assert agent.system == "You are helpful."
        assert agent.tools == ["read_kv", "write_kv"]
        assert agent.temperature == 0.5

    def test_defaults(self):
        data = {"name": "minimal"}
        agent = AgentDef.from_dict(data)
        assert agent.provider == "openai"
        assert agent.model == "gpt-4o-mini"
        assert agent.system == ""
        assert agent.tools == []
        assert agent.triggers == []
        assert agent.temperature == 0.7

    def test_guardrails_parsing(self):
        data = {
            "name": "guarded",
            "guardrails": {
                "max_tool_calls": 3,
                "max_tokens": 500,
                "timeout_s": 10.0,
            },
        }
        agent = AgentDef.from_dict(data)
        assert agent.guardrails.max_tool_calls == 3
        assert agent.guardrails.max_tokens == 500
        assert agent.guardrails.timeout_s == 10.0

    def test_triggers_string_form(self):
        data = {
            "name": "triggered",
            "triggers": ["mention", "dm"],
        }
        agent = AgentDef.from_dict(data)
        assert len(agent.triggers) == 2
        assert agent.triggers[0].kind == "mention"
        assert agent.triggers[1].kind == "dm"
        assert agent.triggers[0].patterns == []

    def test_triggers_dict_form(self):
        data = {
            "name": "keyword-agent",
            "triggers": [
                {"kind": "keyword", "patterns": ["hello", "hi"]},
                {"kind": "fallback"},
            ],
        }
        agent = AgentDef.from_dict(data)
        assert len(agent.triggers) == 2
        assert agent.triggers[0].kind == "keyword"
        assert agent.triggers[0].patterns == ["hello", "hi"]
        assert agent.triggers[1].kind == "fallback"
        assert agent.triggers[1].patterns == []


class TestAgentDefFromYaml:
    def test_loads_yaml_file(self, tmp_path: Path):
        yaml_content = """\
name: yaml-agent
provider: openai
model: gpt-4o
system: "You are a YAML-loaded agent."
tools:
  - read_kv
  - write_kv
triggers:
  - kind: keyword
    patterns:
      - test
guardrails:
  max_tool_calls: 4
  max_tokens: 800
  timeout_s: 15.0
temperature: 0.3
"""
        yaml_file = tmp_path / "agent.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        agent = AgentDef.from_yaml(yaml_file)
        assert agent.name == "yaml-agent"
        assert agent.model == "gpt-4o"
        assert agent.system == "You are a YAML-loaded agent."
        assert agent.tools == ["read_kv", "write_kv"]
        assert agent.triggers[0].kind == "keyword"
        assert agent.triggers[0].patterns == ["test"]
        assert agent.guardrails.max_tool_calls == 4
        assert agent.temperature == 0.3


class TestAgentTrigger:
    def test_creation_defaults(self):
        t = AgentTrigger(kind="mention")
        assert t.kind == "mention"
        assert t.patterns == []

    def test_creation_with_patterns(self):
        t = AgentTrigger(kind="keyword", patterns=["foo", "bar"])
        assert t.kind == "keyword"
        assert t.patterns == ["foo", "bar"]


class TestAgentGuardrails:
    def test_defaults(self):
        g = AgentGuardrails()
        assert g.max_tool_calls == 6
        assert g.max_tokens == 1200
        assert g.timeout_s == 20.0

    def test_custom_values(self):
        g = AgentGuardrails(max_tool_calls=10, max_tokens=2000, timeout_s=30.0)
        assert g.max_tool_calls == 10
        assert g.max_tokens == 2000
        assert g.timeout_s == 30.0


# ---------------------------------------------------------------------------
# AgentRuntime tests
# ---------------------------------------------------------------------------


class TestRuntimeSimpleResponse:
    async def test_simple_text_response(self, kv, tool_registry):
        """Agent returns text without making any tool calls."""
        agent_def = AgentDef.from_dict(
            {
                "name": "simple",
                "system": "You are helpful.",
                "tools": [],
            }
        )
        provider = MockProvider([make_text_response("Hello there!", tokens=20)])
        runtime = AgentRuntime(agent_def, provider, tool_registry, kv)

        result = await runtime.invoke("Hi")
        assert result.content == "Hello there!"
        assert result.tool_calls_made == 0
        assert result.total_tokens == 20

    async def test_no_system_prompt(self, kv, tool_registry):
        """Agent with empty system prompt still works."""
        agent_def = AgentDef.from_dict({"name": "no-sys", "system": ""})
        provider = MockProvider([make_text_response("Works fine")])
        runtime = AgentRuntime(agent_def, provider, tool_registry, kv)

        result = await runtime.invoke("Test")
        assert result.content == "Works fine"


class TestRuntimeToolCalls:
    async def test_single_tool_call(self, kv, tool_registry):
        """Agent makes one tool call then returns text."""
        agent_def = AgentDef.from_dict(
            {
                "name": "tool-user",
                "tools": ["random_int"],
            }
        )
        responses = [
            make_tool_call_response("random_int", {"min": 1, "max": 10}, call_id="c1", tokens=15),
            make_text_response("The random number is 5.", tokens=10),
        ]
        provider = MockProvider(responses)
        runtime = AgentRuntime(agent_def, provider, tool_registry, kv)

        result = await runtime.invoke("Give me a random number")
        assert "random number" in result.content or result.content
        assert result.tool_calls_made == 1
        assert result.total_tokens == 25

    async def test_multiple_tool_calls_in_sequence(self, kv, tool_registry):
        """Agent makes multiple sequential tool calls."""
        agent_def = AgentDef.from_dict(
            {
                "name": "multi-tool",
                "tools": ["random_int"],
                "guardrails": {"max_tool_calls": 10},
            }
        )
        responses = [
            make_tool_call_response("random_int", {"min": 1, "max": 6}, call_id="c1", tokens=10),
            make_tool_call_response("random_int", {"min": 1, "max": 6}, call_id="c2", tokens=10),
            make_tool_call_response("random_int", {"min": 1, "max": 6}, call_id="c3", tokens=10),
            make_text_response("I rolled three dice.", tokens=10),
        ]
        provider = MockProvider(responses)
        runtime = AgentRuntime(agent_def, provider, tool_registry, kv)

        result = await runtime.invoke("Roll three dice")
        assert result.tool_calls_made == 3
        assert result.total_tokens == 40

    async def test_tool_not_found(self, kv, tool_registry):
        """Tool not in registry → error message returned to LLM."""
        agent_def = AgentDef.from_dict(
            {
                "name": "missing-tool",
                "tools": ["nonexistent_tool"],
            }
        )
        responses = [
            make_tool_call_response("nonexistent_tool", {}, call_id="c1"),
            make_text_response("Sorry, I couldn't use that tool."),
        ]
        provider = MockProvider(responses)
        runtime = AgentRuntime(agent_def, provider, tool_registry, kv)

        result = await runtime.invoke("Do something")
        assert result.content == "Sorry, I couldn't use that tool."
        assert result.tool_calls_made == 1

    async def test_tool_execution_error(self, kv):
        """Tool raises an exception → error string returned to LLM."""
        # Create a registry with a tool that raises
        reg = ToolRegistry()
        from linling_core.tools import ToolDef

        async def bad_tool(ctx: ToolCtx, **kwargs):
            raise ValueError("Something went wrong")

        reg.register(
            ToolDef(
                name="bad_tool",
                dsl_name="坏工具",
                description="A tool that always fails",
                schema={},
                safe=True,
                fn=bad_tool,
            )
        )

        agent_def = AgentDef.from_dict(
            {
                "name": "error-agent",
                "tools": ["bad_tool"],
            }
        )
        responses = [
            make_tool_call_response("bad_tool", {}, call_id="c1"),
            make_text_response("The tool failed, sorry."),
        ]
        provider = MockProvider(responses)
        runtime = AgentRuntime(agent_def, provider, reg, kv)

        result = await runtime.invoke("Use the bad tool")
        assert result.content == "The tool failed, sorry."
        assert result.tool_calls_made == 1

    async def test_tool_result_is_truncated_before_next_llm_call(self, kv):
        """Large tool outputs are capped before being replayed to the LLM."""
        reg = ToolRegistry()
        from linling_core.tools import ToolDef

        async def big_tool(ctx: ToolCtx):
            _ = ctx
            return "x" * (_MAX_TOOL_RESULT_CHARS + 500)

        reg.register(
            ToolDef(
                name="big_tool",
                dsl_name="大工具",
                description="Returns a large result",
                schema={},
                safe=True,
                fn=big_tool,
            )
        )

        agent_def = AgentDef.from_dict({"name": "big-agent", "tools": ["big_tool"]})
        captured: list[list[Message]] = []

        class CapturingProvider(MockProvider):
            async def chat(
                self,
                messages: list[Message],
                *,
                tools: list[ToolSchema] | None = None,
                temperature: float = 0.7,
                max_tokens: int | None = None,
            ) -> LLMResponse:
                captured.append(list(messages))
                return await super().chat(
                    messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

        provider = CapturingProvider(
            [
                make_tool_call_response("big_tool", {}, call_id="c1"),
                make_text_response("Done"),
            ]
        )
        runtime = AgentRuntime(agent_def, provider, reg, kv)

        await runtime.invoke("Use big tool")

        tool_messages = [m for m in captured[-1] if m.role == "tool"]
        assert len(tool_messages) == 1
        assert len(tool_messages[0].content) < _MAX_TOOL_RESULT_CHARS + 500
        assert "tool result truncated" in tool_messages[0].content


class TestRuntimeGuardrails:
    async def test_max_tool_calls_exceeded(self, kv, tool_registry):
        """Guardrail stops the loop when max_tool_calls is exceeded."""
        agent_def = AgentDef.from_dict(
            {
                "name": "limited",
                "tools": ["random_int"],
                "guardrails": {"max_tool_calls": 2},
            }
        )
        # The agent keeps calling tools beyond the limit
        responses = [
            make_tool_call_response("random_int", {"min": 1, "max": 6}, call_id="c1"),
            make_tool_call_response("random_int", {"min": 1, "max": 6}, call_id="c2"),
            # Third call would exceed limit
            make_tool_call_response("random_int", {"min": 1, "max": 6}, call_id="c3"),
        ]
        provider = MockProvider(responses)
        runtime = AgentRuntime(agent_def, provider, tool_registry, kv)

        result = await runtime.invoke("Keep rolling")
        assert "max tool calls exceeded" in result.content
        # The loop should stop after exceeding the limit
        assert result.tool_calls_made > 0


class TestRuntimeMessages:
    async def test_system_prompt_included(self, kv, tool_registry):
        """System prompt is included in messages sent to LLM."""
        agent_def = AgentDef.from_dict(
            {
                "name": "sys-prompt",
                "system": "You are a test bot.",
            }
        )

        captured_messages: list[list[Message]] = []

        class CapturingProvider:
            @property
            def name(self):
                return "capturing"

            async def chat(self, messages, **kwargs):
                captured_messages.append(list(messages))
                return make_text_response("OK")

            async def chat_stream(self, messages, **kwargs):
                raise NotImplementedError

        runtime = AgentRuntime(agent_def, CapturingProvider(), tool_registry, kv)
        await runtime.invoke("Hello")

        assert len(captured_messages) == 1
        msgs = captured_messages[0]
        assert msgs[0].role == "system"
        assert msgs[0].content == "You are a test bot."
        assert msgs[1].role == "user"
        assert msgs[1].content == "Hello"

    async def test_history_preserved(self, kv, tool_registry):
        """History messages are included between system and user."""
        agent_def = AgentDef.from_dict(
            {
                "name": "history-agent",
                "system": "System.",
            }
        )

        captured_messages: list[list[Message]] = []

        class CapturingProvider:
            @property
            def name(self):
                return "capturing"

            async def chat(self, messages, **kwargs):
                captured_messages.append(list(messages))
                return make_text_response("OK")

            async def chat_stream(self, messages, **kwargs):
                raise NotImplementedError

        history = [
            Message(role="user", content="Previous question"),
            Message(role="assistant", content="Previous answer"),
        ]
        runtime = AgentRuntime(agent_def, CapturingProvider(), tool_registry, kv)
        await runtime.invoke("New question", history=history)

        msgs = captured_messages[0]
        assert msgs[0].role == "system"
        assert msgs[1].role == "user"
        assert msgs[1].content == "Previous question"
        assert msgs[2].role == "assistant"
        assert msgs[2].content == "Previous answer"
        assert msgs[3].role == "user"
        assert msgs[3].content == "New question"

    async def test_context_max_tokens_controls_runtime_prompt_clip(self, kv, tool_registry):
        """Dispatcher-provided context caps should override the legacy 8x completion heuristic."""
        agent_def = AgentDef.from_dict(
            {
                "name": "ctx",
                "system": "System.",
                "guardrails": {"max_tokens": 100},
            }
        )

        captured_messages: list[list[Message]] = []

        class CapturingProvider:
            @property
            def name(self):
                return "capturing"

            async def chat(self, messages, **kwargs):
                captured_messages.append(list(messages))
                return make_text_response("OK")

            async def chat_stream(self, messages, **kwargs):
                raise NotImplementedError

        history = [Message(role="user", content="x" * 1_500)]
        runtime = AgentRuntime(agent_def, CapturingProvider(), tool_registry, kv)
        await runtime.invoke("New question", history=history, context_max_tokens=2_000)

        assert "x" * 500 in "".join(m.content for m in captured_messages[0])


class TestRuntimeToolSchemas:
    async def test_tool_schemas_built_from_registry(self, kv, tool_registry):
        """Tool schemas are correctly built from registry entries."""
        agent_def = AgentDef.from_dict(
            {
                "name": "schema-test",
                "tools": ["read_kv"],
            }
        )

        captured_tools: list[list[ToolSchema] | None] = []

        class CapturingProvider:
            @property
            def name(self):
                return "capturing"

            async def chat(self, messages, *, tools=None, **kwargs):
                captured_tools.append(tools)
                return make_text_response("OK")

            async def chat_stream(self, messages, **kwargs):
                raise NotImplementedError

        runtime = AgentRuntime(agent_def, CapturingProvider(), tool_registry, kv)
        await runtime.invoke("Test")

        assert captured_tools[0] is not None
        schemas = captured_tools[0]
        assert len(schemas) == 1
        assert schemas[0].name == "read_kv"
        assert schemas[0].description == "Read a key-value pair from storage"
        assert "scope" in schemas[0].parameters["properties"]
        assert "file" in schemas[0].parameters["properties"]
        assert "key" in schemas[0].parameters["properties"]

    async def test_empty_tool_list_no_tools_passed(self, kv, tool_registry):
        """Empty tool list means no tools passed to LLM."""
        agent_def = AgentDef.from_dict(
            {
                "name": "no-tools",
                "tools": [],
            }
        )

        captured_tools: list[list[ToolSchema] | None] = []

        class CapturingProvider:
            @property
            def name(self):
                return "capturing"

            async def chat(self, messages, *, tools=None, **kwargs):
                captured_tools.append(tools)
                return make_text_response("OK")

            async def chat_stream(self, messages, **kwargs):
                raise NotImplementedError

        runtime = AgentRuntime(agent_def, CapturingProvider(), tool_registry, kv)
        await runtime.invoke("Test")

        assert captured_tools[0] is None

    async def test_event_flag_can_disable_tools(self, kv, tool_registry):
        """Internal decision prompts can force a no-tools LLM call."""
        agent_def = AgentDef.from_dict(
            {
                "name": "schema-test",
                "tools": ["read_kv"],
            }
        )

        captured_tools: list[list[ToolSchema] | None] = []

        class CapturingProvider:
            @property
            def name(self):
                return "capturing"

            async def chat(self, messages, *, tools=None, **kwargs):
                captured_tools.append(tools)
                return make_text_response("OK")

            async def chat_stream(self, messages, **kwargs):
                raise NotImplementedError

        event = Event(
            id="e1",
            platform="test",
            bot_id="bot1",
            scope=Scope(kind="group", id="g1", platform="test"),
            sender=User(id="u1", platform="test"),
            segments=[TextSegment(text="Test")],
            raw={"_linling_disable_tools": True},
        )
        runtime = AgentRuntime(agent_def, CapturingProvider(), tool_registry, kv)
        await runtime.invoke("Test", event=event)

        assert captured_tools == [None]


class TestRuntimeTokenAccumulation:
    async def test_tokens_accumulated_across_calls(self, kv, tool_registry):
        """Token usage is accumulated across multiple LLM calls."""
        agent_def = AgentDef.from_dict(
            {
                "name": "token-counter",
                "tools": ["random_int"],
            }
        )
        responses = [
            make_tool_call_response("random_int", {"min": 1, "max": 10}, call_id="c1", tokens=20),
            make_tool_call_response("random_int", {"min": 1, "max": 10}, call_id="c2", tokens=25),
            make_text_response("Done!", tokens=15),
        ]
        provider = MockProvider(responses)
        runtime = AgentRuntime(agent_def, provider, tool_registry, kv)

        result = await runtime.invoke("Count tokens")
        assert result.total_tokens == 60  # 20 + 25 + 15

    async def test_tokens_zero_when_no_usage(self, kv, tool_registry):
        """Token count stays 0 when provider returns no usage info."""
        agent_def = AgentDef.from_dict({"name": "no-usage"})
        response = LLMResponse(
            message=Message(role="assistant", content="No usage info"),
            usage=None,
        )
        provider = MockProvider([response])
        runtime = AgentRuntime(agent_def, provider, tool_registry, kv)

        result = await runtime.invoke("Test")
        assert result.total_tokens == 0


class TestRuntimeWithRealTools:
    async def test_write_and_read_kv(self, tool_registry):
        """Integration: agent writes then reads from KV store."""
        kv = MockKVStore()
        agent_def = AgentDef.from_dict(
            {
                "name": "kv-agent",
                "tools": ["write_kv", "read_kv"],
                "guardrails": {"max_tool_calls": 10},
            }
        )
        responses = [
            make_tool_call_response(
                "write_kv",
                {"scope": "test", "file": "data", "key": "name", "value": "linling"},
                call_id="c1",
            ),
            make_tool_call_response(
                "read_kv",
                {"scope": "test", "file": "data", "key": "name"},
                call_id="c2",
            ),
            make_text_response("The name is linling."),
        ]
        provider = MockProvider(responses)
        runtime = AgentRuntime(agent_def, provider, tool_registry, kv)

        result = await runtime.invoke("Write and read my name")
        assert result.content == "The name is linling."
        assert result.tool_calls_made == 2
        # Verify the KV store was actually written to
        stored = await kv.read("test", "data", "name")
        assert stored == "linling"
