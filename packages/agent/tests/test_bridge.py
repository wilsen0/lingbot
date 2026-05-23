"""Tests for linling_agent.bridge module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from linling_agent.bridge import AgentRegistry, agent_invoke
from linling_agent.runtime import AgentResult
from linling_core.tools import ToolCtx
from linling_core.tools import registry as global_registry


@pytest.fixture
def mock_kv() -> MagicMock:
    return MagicMock()


@pytest.fixture
def base_ctx(mock_kv: MagicMock) -> ToolCtx:
    return ToolCtx(kv=mock_kv, event=None, bot_id="test")


class TestAgentInvokeTool:
    def test_registered_in_global_registry(self) -> None:
        td = global_registry.get("agent_invoke")
        assert td is not None
        assert td.name == "agent_invoke"
        assert td.dsl_name == "agent"
        assert td.safe is False

    @pytest.mark.asyncio
    async def test_returns_error_when_no_registry(self, base_ctx: ToolCtx) -> None:
        # No agent_registry in extras
        result = await agent_invoke(base_ctx, agent_name="test", input="hi")
        assert "no agent registry configured" in result

    @pytest.mark.asyncio
    async def test_returns_error_when_agent_not_found(self, base_ctx: ToolCtx) -> None:
        base_ctx.extras["agent_registry"] = AgentRegistry()
        result = await agent_invoke(base_ctx, agent_name="missing", input="hi")
        assert "not found" in result
        assert "missing" in result

    @pytest.mark.asyncio
    async def test_calls_agent_and_returns_content(self, base_ctx: ToolCtx) -> None:
        mock_runtime = AsyncMock()
        mock_runtime.invoke.return_value = AgentResult(
            content="Hello from agent!", tool_calls_made=0, total_tokens=10
        )

        reg = AgentRegistry()
        reg.register("greeter", mock_runtime)
        base_ctx.extras["agent_registry"] = reg

        result = await agent_invoke(base_ctx, agent_name="greeter", input="hi there")
        assert result == "Hello from agent!"
        mock_runtime.invoke.assert_called_once_with("hi there", event=None)

    @pytest.mark.asyncio
    async def test_passes_event_to_agent(self, base_ctx: ToolCtx) -> None:
        mock_event = MagicMock()
        base_ctx.event = mock_event

        mock_runtime = AsyncMock()
        mock_runtime.invoke.return_value = AgentResult(content="ok")

        reg = AgentRegistry()
        reg.register("bot", mock_runtime)
        base_ctx.extras["agent_registry"] = reg

        await agent_invoke(base_ctx, agent_name="bot", input="test")
        mock_runtime.invoke.assert_called_once_with("test", event=mock_event)


class TestAgentRegistry:
    def test_register_and_get(self) -> None:
        reg = AgentRegistry()
        mock_runtime = MagicMock()
        reg.register("my_agent", mock_runtime)
        assert reg.get("my_agent") is mock_runtime

    def test_get_returns_none_for_unknown(self) -> None:
        reg = AgentRegistry()
        assert reg.get("unknown") is None

    def test_names_returns_registered_names(self) -> None:
        reg = AgentRegistry()
        reg.register("a", MagicMock())
        reg.register("b", MagicMock())
        names = reg.names()
        assert sorted(names) == ["a", "b"]

    def test_names_empty_initially(self) -> None:
        reg = AgentRegistry()
        assert reg.names() == []
