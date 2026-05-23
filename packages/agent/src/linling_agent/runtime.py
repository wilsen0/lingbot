"""Agent runtime — ReAct tool-calling loop.

The :class:`AgentRuntime` takes an :class:`AgentDef` and executes the
iterative LLM ↔ tool loop until the model produces a final text response
or a guardrail limit is hit.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from linling_core.events import Event
from linling_core.metrics import (
    LLM_CALLS_TOTAL,
    LLM_DURATION_SECONDS,
    LLM_TOKENS_TOTAL,
    MetricsSink,
    NullMetrics,
)
from linling_core.storage.kv import KVStore
from linling_core.tools import ToolCtx, ToolRegistry

from linling_agent.agent_def import AgentDef
from linling_agent.context import fit_messages_to_budget
from linling_agent.llm import LLMProvider, LLMResponse, Message, ToolSchema

_MAX_TOOL_RESULT_CHARS = 8_000


@dataclass
class AgentResult:
    """Result of an agent invocation."""

    content: str
    tool_calls_made: int = 0
    total_tokens: int = 0


class AgentRuntime:
    """Executes an agent's ReAct tool-calling loop."""

    def __init__(
        self,
        agent_def: AgentDef,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        kv: KVStore,
        *,
        bot_id: str = "linling",
        metrics: MetricsSink | None = None,
    ) -> None:
        self._agent_def = agent_def
        self._provider = provider
        self._registry = tool_registry
        self._kv = kv
        self._bot_id = bot_id
        self._metrics: MetricsSink = metrics or NullMetrics()

    # ---- Public read-only views -----------------------------------------
    # These let callers (notably the WebUI) introspect an agent's
    # configuration without reaching into private attributes.

    @property
    def agent_def(self) -> AgentDef:
        """The :class:`AgentDef` backing this runtime. Read-only view."""
        return self._agent_def

    @property
    def name(self) -> str:
        """Convenience: the agent's configured name."""
        return self._agent_def.name

    @property
    def provider_name(self) -> str:
        """The provider identifier (e.g. ``openai``)."""
        return self._agent_def.provider

    @property
    def model(self) -> str:
        """The LLM model identifier (e.g. ``gpt-4o-mini``)."""
        return self._agent_def.model

    @property
    def provider(self) -> LLMProvider:
        """The provider used for LLM calls. Read-only view."""
        return self._provider

    def _build_tool_schemas(self) -> list[ToolSchema]:
        """Convert allowed tools from the agent def into ToolSchema list."""
        schemas: list[ToolSchema] = []
        for tool_name in self._agent_def.tools:
            td = self._registry.get(tool_name)
            if td is None:
                continue
            properties: dict[str, object] = {}
            required: list[str] = []
            for param_name, type_str in td.schema.items():
                optional = type_str.endswith("?")
                base_type = type_str.rstrip("?")
                type_map = {
                    "string": "string",
                    "int": "integer",
                    "float": "number",
                    "bool": "boolean",
                }
                json_type = type_map.get(base_type, "string")
                properties[param_name] = {"type": json_type}
                if not optional:
                    required.append(param_name)
            schemas.append(
                ToolSchema(
                    name=td.name,
                    description=td.description,
                    parameters={
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                )
            )
        return schemas

    async def invoke(
        self,
        user_input: str,
        *,
        event: Event | None = None,
        history: list[Message] | None = None,
        context_max_tokens: int | None = None,
    ) -> AgentResult:
        """Run the agent's ReAct loop until it produces a text response or hits limits."""
        disable_tools = bool(event is not None and event.raw.get("_linling_disable_tools"))
        # Build initial messages
        messages: list[Message] = []
        if self._agent_def.system:
            messages.append(Message(role="system", content=self._agent_def.system))
        if history:
            messages.extend(history)
        messages.append(Message(role="user", content=user_input))

        # Build tool schemas
        tool_schemas = [] if disable_tools else self._build_tool_schemas()
        tools_arg = tool_schemas if tool_schemas else None

        tool_calls_made = 0
        total_tokens = 0
        start_time = time.monotonic()
        guardrails = self._agent_def.guardrails

        while True:
            # Check timeout
            elapsed = time.monotonic() - start_time
            if elapsed >= guardrails.timeout_s:
                return AgentResult(
                    content="[Agent stopped: timeout exceeded]",
                    tool_calls_made=tool_calls_made,
                    total_tokens=total_tokens,
                )

            remaining = max(0.1, guardrails.timeout_s - elapsed)
            messages = fit_messages_to_budget(
                messages,
                _provider_prompt_budget(context_max_tokens=context_max_tokens),
            )

            # Call LLM — timed and counted so dashboards can show
            # token / latency / error rate per provider/model.
            provider_labels = {
                "provider": self._agent_def.provider,
                "model": self._agent_def.model,
            }
            llm_started = time.monotonic()
            try:
                # ``wait_for`` guards against a single LLM call hanging
                # past the agent-wide budget. The provider should also
                # honour cancellation — :class:`OpenAIProvider` uses
                # ``httpx.AsyncClient`` whose request is cancelled
                # cleanly on ``CancelledError``.
                response: LLMResponse = await asyncio.wait_for(
                    self._provider.chat(
                        messages,
                        tools=tools_arg,
                        temperature=self._agent_def.temperature,
                        max_tokens=guardrails.max_tokens,
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                self._metrics.counter_inc(
                    LLM_CALLS_TOTAL, {**provider_labels, "outcome": "error"}
                )
                self._metrics.histogram_observe(
                    LLM_DURATION_SECONDS,
                    provider_labels,
                    time.monotonic() - llm_started,
                )
                return AgentResult(
                    content="[Agent stopped: LLM timeout]",
                    tool_calls_made=tool_calls_made,
                    total_tokens=total_tokens,
                )
            except Exception:
                self._metrics.counter_inc(LLM_CALLS_TOTAL, {**provider_labels, "outcome": "error"})
                raise
            finally:
                self._metrics.histogram_observe(
                    LLM_DURATION_SECONDS,
                    provider_labels,
                    time.monotonic() - llm_started,
                )

            self._metrics.counter_inc(LLM_CALLS_TOTAL, {**provider_labels, "outcome": "ok"})

            # Accumulate token usage + ship to metrics.
            if response.usage:
                total_tokens += response.usage.total_tokens
                if response.usage.prompt_tokens:
                    self._metrics.counter_inc(
                        LLM_TOKENS_TOTAL,
                        {**provider_labels, "direction": "prompt"},
                        float(response.usage.prompt_tokens),
                    )
                if response.usage.completion_tokens:
                    self._metrics.counter_inc(
                        LLM_TOKENS_TOTAL,
                        {**provider_labels, "direction": "completion"},
                        float(response.usage.completion_tokens),
                    )

            assistant_msg = response.message

            # If no tool calls, return the text response
            if not assistant_msg.tool_calls:
                return AgentResult(
                    content=assistant_msg.content,
                    tool_calls_made=tool_calls_made,
                    total_tokens=total_tokens,
                )

            # Process tool calls
            messages.append(assistant_msg)
            tool_calls_made += 1

            # Check max_tool_calls guardrail
            if tool_calls_made > guardrails.max_tool_calls:
                content = assistant_msg.content or "[Agent stopped: max tool calls exceeded]"
                return AgentResult(
                    content=content,
                    tool_calls_made=tool_calls_made,
                    total_tokens=total_tokens,
                )

            # Execute each tool call
            ctx = ToolCtx(kv=self._kv, event=event, bot_id=self._bot_id)
            for tc in assistant_msg.tool_calls:
                tool_def = self._registry.get(tc.name)
                if tool_def is None:
                    result_str = f"Error: tool '{tc.name}' not found in registry"
                else:
                    try:
                        args = json.loads(tc.arguments) if tc.arguments else {}
                        result = await tool_def.fn(ctx, **args)
                        result_str = str(result) if result is not None else ""
                    except Exception as exc:
                        result_str = f"Error executing tool '{tc.name}': {exc}"
                result_str = _cap_tool_result(result_str)

                messages.append(
                    Message(
                        role="tool",
                        content=result_str,
                        name=tc.name,
                        tool_call_id=tc.id,
                    )
                )


def _cap_tool_result(text: str, max_chars: int = _MAX_TOOL_RESULT_CHARS) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n[tool result truncated: {omitted} chars omitted]"


def _provider_prompt_budget(
    *,
    context_max_tokens: int | None,
) -> int:
    if context_max_tokens is not None and context_max_tokens > 0:
        return context_max_tokens
    return 64_000
