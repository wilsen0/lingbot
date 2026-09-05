"""Agent runtime — ReAct tool-calling loop.

The :class:`AgentRuntime` takes an :class:`AgentDef` and executes the
iterative LLM ↔ tool loop until the model produces a final text response
or a guardrail limit is hit.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from linling_core.events import Event
from linling_core.metrics import (
    LLM_CALLS_TOTAL,
    LLM_DURATION_SECONDS,
    LLM_TOKENS_TOTAL,
    MetricsSink,
    NullMetrics,
)
from linling_core.storage.kv import KVStore
from linling_core.tools import ToolCtx, ToolRegistry, tool_parameters_schema

from linling_agent.agent_def import AgentDef
from linling_agent.context import fit_messages_to_budget
from linling_agent.images import ImageContentResolver
from linling_agent.llm import ContentPart, LLMProvider, LLMResponse, Message, ToolSchema

logger = structlog.get_logger(__name__)

_MAX_TOOL_RESULT_CHARS = 8_000
_NUDGE_LIMIT = 2
_NUDGE_PROMPT = "用工具发送消息，或调用 finish_turn 结束本回合。"
_FINISH_TURN_TOOL_NAME = "finish_turn"


@dataclass
class AgentResult:
    """Result of an agent invocation."""

    content: str
    tool_calls_made: int = 0
    total_tokens: int = 0
    finish_turn_summary: str | None = None
    # The actual outbound message texts the agent sent this turn via the
    # ``send_reply`` tool, in send order.  Callers that persist history
    # (the DM dispatcher) use this instead of ``finish_turn_summary`` so
    # the next turn sees what was really said, not a meta-summary.
    sent_texts: list[str] = field(default_factory=list)


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
        action_sink: Any | None = None,
        image_resolver: ImageContentResolver | None = None,
        sticker_dir: Path | None = None,
    ) -> None:
        self._agent_def = agent_def
        self._provider = provider
        self._registry = tool_registry
        self._kv = kv
        self._bot_id = bot_id
        self._metrics: MetricsSink = metrics or NullMetrics()
        self._action_sink: Any | None = action_sink
        self._image_resolver = image_resolver
        self._sticker_dir = sticker_dir

    def set_action_sink(self, sink: Any) -> None:
        self._action_sink = sink

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
        """Convert allowed tools from the agent def into ToolSchema list.

        When ``vision_enabled`` is on, every ``vision_only`` tool in the
        registry is additionally attached even if the agent's tools
        allowlist does not mention it — the sticker tools are built-in
        capabilities of vision mode, and they only do anything once the
        bootstrap injects their ``image_resolver`` / ``sticker_dir``
        extras, so there is no accidental exposure. With vision disabled
        they stay filtered out as before.

        Always appends the ``finish_turn`` pseudo-tool — it is not in the
        global registry but must be visible to every LLM call so the model
        can explicitly end the turn.
        """
        schemas: list[ToolSchema] = []
        added: set[str] = set()
        for tool_name in self._agent_def.tools:
            td = self._registry.get(tool_name)
            if td is None:
                continue
            if td.vision_only and not self._agent_def.vision_enabled:
                continue
            schemas.append(
                ToolSchema(
                    name=td.name,
                    description=td.description,
                    parameters=tool_parameters_schema(td),
                )
            )
            added.add(td.name)
        if self._agent_def.vision_enabled:
            for td in self._registry.all():
                if td.vision_only and td.name not in added:
                    schemas.append(
                        ToolSchema(
                            name=td.name,
                            description=td.description,
                            parameters=tool_parameters_schema(td),
                        )
                    )
        schemas.append(
            ToolSchema(
                name=_FINISH_TURN_TOOL_NAME,
                description=(
                    "End this turn. Call this when you have finished sending all messages. "
                    "Provide a brief summary of the topic and your thoughts in the summary field."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Brief summary of this turn's topic and your thoughts.",
                        },
                    },
                    "required": ["summary"],
                    "additionalProperties": False,
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
        action_sink: Any | None = None,
        user_content_parts: list[ContentPart] | None = None,
    ) -> AgentResult:
        """Run the agent's ReAct loop until it calls ``finish_turn`` or hits limits.

        ``action_sink`` overrides the runtime's configured sink for this
        call only — used by transports (the WebUI) that must capture
        ``send_reply`` output instead of pushing it to the IM adapter
        sink.  Passing ``None`` (the default) keeps the configured sink.
        """
        disable_tools = bool(event is not None and event.raw.get("_linling_disable_tools"))
        # Build initial messages
        messages: list[Message] = []
        if self._agent_def.system:
            messages.append(Message(role="system", content=self._agent_def.system))
        if history:
            messages.extend(history)
        messages.append(
            Message(
                role="user",
                content=user_input,
                content_parts=tuple(user_content_parts) if user_content_parts else None,
            )
        )

        # Build tool schemas
        tool_schemas = [] if disable_tools else self._build_tool_schemas()
        tools_arg = tool_schemas if tool_schemas else None

        tool_calls_made = 0
        total_tokens = 0
        nudge_count = 0
        start_time = time.monotonic()
        guardrails = self._agent_def.guardrails
        # ``sent_texts`` accumulates the actual outbound message texts
        # the ``send_reply`` tool emits this turn.  It lives on ``extras``
        # (the same dict handed to every tool) so send_reply can append
        # and the runtime can drain it on every return path — including
        # the nudge-limit and timeout fall-backs where messages may have
        # already gone out before the agent gave up on finish_turn.
        sent_texts: list[str] = []
        effective_sink = self._action_sink if action_sink is None else action_sink
        extras: dict[str, Any] = {"action_sink": effective_sink, "sent_texts": sent_texts}
        if self._image_resolver is not None:
            extras["image_resolver"] = self._image_resolver
        if self._sticker_dir is not None:
            extras["sticker_dir"] = self._sticker_dir

        while True:
            # Check timeout
            elapsed = time.monotonic() - start_time
            if elapsed >= guardrails.timeout_s:
                return AgentResult(
                    content="[Agent stopped: timeout exceeded]",
                    tool_calls_made=tool_calls_made,
                    total_tokens=total_tokens,
                    sent_texts=list(sent_texts),
                )

            remaining = max(0.1, guardrails.timeout_s - elapsed)
            messages = fit_messages_to_budget(
                messages,
                _provider_prompt_budget(context_max_tokens=context_max_tokens),
            )

            provider_labels = {
                "provider": self._agent_def.provider,
                "model": self._agent_def.model,
            }
            llm_started = time.monotonic()
            try:
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
                self._metrics.counter_inc(LLM_CALLS_TOTAL, {**provider_labels, "outcome": "error"})
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

            # --- No tool calls: nudge or give up ----------------------------
            if not assistant_msg.tool_calls:
                nudge_count += 1
                if nudge_count > _NUDGE_LIMIT:
                    logger.info(
                        "runtime.nudge_limit_reached",
                        nudge_count=nudge_count,
                        content_preview=(assistant_msg.content or "")[:200],
                    )
                    return AgentResult(
                        content="",
                        tool_calls_made=tool_calls_made,
                        total_tokens=total_tokens,
                        sent_texts=list(sent_texts),
                    )
                logger.debug(
                    "runtime.nudge",
                    nudge_count=nudge_count,
                    content_preview=(assistant_msg.content or "")[:200],
                )
                messages.append(assistant_msg)
                messages.append(Message(role="user", content=_NUDGE_PROMPT))
                continue

            # --- Tool calls present ----------------------------------------
            nudge_count = 0
            messages.append(assistant_msg)
            tool_calls_made += 1

            if tool_calls_made > guardrails.max_tool_calls:
                return AgentResult(
                    content=assistant_msg.content or "[Agent stopped: max tool calls exceeded]",
                    tool_calls_made=tool_calls_made,
                    total_tokens=total_tokens,
                    sent_texts=list(sent_texts),
                )

            # Execute every non-finish_turn tool call in this message,
            # then honour finish_turn *afterwards*.  The previous code
            # checked finish_turn first and returned immediately, which
            # silently dropped any send_reply shipped in the same
            # assistant message (a very natural "send, then finish"
            # pattern).  Executing in order also preserves the model's
            # intended delivery sequence.
            ctx = ToolCtx(kv=self._kv, event=event, bot_id=self._bot_id, extras=extras)
            finish_summary: str | None = None
            for tc in assistant_msg.tool_calls:
                if tc.name == _FINISH_TURN_TOOL_NAME:
                    try:
                        ft_args = json.loads(tc.arguments) if tc.arguments else {}
                    except json.JSONDecodeError:
                        ft_args = {}
                    finish_summary = str(ft_args.get("summary", "")) or ""
                    messages.append(
                        Message(
                            role="tool",
                            content='{"ok": true}',
                            name=tc.name,
                            tool_call_id=tc.id,
                        )
                    )
                    continue

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

            if finish_summary is not None:
                return AgentResult(
                    content="",
                    tool_calls_made=tool_calls_made,
                    total_tokens=total_tokens,
                    finish_turn_summary=finish_summary,
                    sent_texts=list(sent_texts),
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
