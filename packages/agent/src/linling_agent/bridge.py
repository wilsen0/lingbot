"""Bridge between DSL and Agent — allows DSL to invoke agents and agents to call DSL handlers."""

from __future__ import annotations

from linling_core.tools import ToolCtx, tool

from linling_agent.runtime import AgentRuntime


@tool(
    name="agent_invoke",
    dsl_name="agent",
    description="Invoke an LLM agent by name with the given input text",
    schema={"agent_name": "string", "input": "string"},
    safe=False,
)
async def agent_invoke(ctx: ToolCtx, agent_name: str = "", input: str = "") -> str:
    """DSL tool: $agent 调用 <name> <input>$.

    Looks up the agent by name in ctx.extras["agent_registry"],
    invokes it, and returns the text response. Empty ``agent_name``
    or no registry returns an empty string so a malformed
    ``$agent$`` call doesn't tear down the calling handler.
    """
    if not agent_name:
        return ""
    agent_registry = ctx.extras.get("agent_registry")
    if agent_registry is None:
        return "[Error: no agent registry configured]"

    runtime = agent_registry.get(agent_name)
    if runtime is None:
        return f"[Error: agent '{agent_name}' not found]"

    result = await runtime.invoke(input, event=ctx.event)
    return str(result.content)


class AgentRegistry:
    """Registry of agent runtimes, keyed by name."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentRuntime] = {}

    def register(self, name: str, runtime: AgentRuntime) -> None:
        """Register an agent runtime by name."""
        self._agents[name] = runtime

    def get(self, name: str) -> AgentRuntime | None:
        """Get an agent runtime by name."""
        return self._agents.get(name)

    def names(self) -> list[str]:
        """List all registered agent names."""
        return list(self._agents.keys())
