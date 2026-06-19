"""`/api/agents*` — list agents, fetch an agent's summary and memory, chat once.

Most of the interactive flow goes through ``/ws/agents/:name/stream`` (for
streaming deltas and tool-call visualisation); this REST surface is for
discovery + one-shot test-chat so pages that don't want a live socket can
still render.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from linling_webui.audit_reader import AuditReader
from linling_webui.deps import Caller, get_state, require_auth
from linling_webui.schemas import AgentSummary, TriggerSuggestion
from linling_webui.state import WebUIState

router = APIRouter(tags=["agents"])


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str = Field(min_length=1, max_length=4000)
    history_key: str | None = None  # reserved: which memory window
    # Override the scope id used to synthesise the inbound event.
    # When omitted the dispatcher uses a synthetic DM-shaped scope
    # (``%群号%==0``); pass a group id to drive rules that gate on
    # a specific group, or a different DM-shaped string for personal
    # state isolation.
    scope_id: str | None = None


class ChatSegment(BaseModel):
    """One piece of a rich chat reply (text or image).

    Mirrors the WS frame shape so the SPA can use the same renderer
    on both transports.
    """

    kind: str  # "text" | "image"
    text: str = ""
    url: str = ""
    alt: str = ""
    delay_before_s: float = 0.0


class ChatResponse(BaseModel):
    content: str
    tool_calls_made: int = 0
    total_tokens: int = 0
    latency_ms: float
    source: str = "agent"
    segments: list[ChatSegment] = []


async def _noop_capture_sink(action: Any) -> None:
    """No-op action sink for the HTTP chat fallback path.

    The single-shot ``/chat`` endpoint invokes the runtime directly when
    no history-aware web dispatcher is wired.  This surface is the
    browser, so we must not push ``send_reply`` actions to the bot's IM
    adapters; the outbound text is recovered from ``result.sent_texts``.
    """
    return None


def _require_runtime(name: str, state: WebUIState):  # type: ignore[no-untyped-def]
    """Look up an AgentRuntime or raise 404.

    Centralised so adding future introspection endpoints doesn't require
    repeating the None / registry handling in every route.
    """
    reg = state.agent_registry
    runtime = None if reg is None else reg.get(name)
    if runtime is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown agent")
    return runtime


def _summary_of(name: str, runtime) -> AgentSummary:  # type: ignore[no-untyped-def]
    """Build an :class:`AgentSummary` from an ``AgentRuntime``.

    Uses the runtime's public ``.provider_name`` / ``.model`` accessors;
    falls back to ``?`` if a custom registry registers a non-AgentRuntime
    object (future extension point).
    """
    return AgentSummary(
        name=name,
        provider=getattr(runtime, "provider_name", "?"),
        model=getattr(runtime, "model", "?"),
    )


@router.get("", response_model=list[AgentSummary])
async def list_agents(
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> list[AgentSummary]:
    reg = state.agent_registry
    if reg is None:
        return []
    _ = caller  # roles enforced at require_auth; agents are visible to all roles
    out: list[AgentSummary] = []
    for name in reg.names():
        runtime = reg.get(name)
        if runtime is None:
            continue
        out.append(_summary_of(name, runtime))
    return out


@router.get("/{name}", response_model=AgentSummary)
async def get_agent(
    name: str,
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> AgentSummary:
    _ = caller
    runtime = _require_runtime(name, state)
    return _summary_of(name, runtime)


class _MemoryView(BaseModel):
    short_term: list[dict[str, Any]]
    summary: str = ""
    long_term: list[dict[str, Any]] = []


@router.get("/{name}/triggers", response_model=list[TriggerSuggestion])
async def list_triggers(
    name: str,
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> list[TriggerSuggestion]:
    """Return the agent's bot's matchable DSL triggers for the inline-suggest panel.

    Cheap to call: a provider closure walks the live classifier each
    time so hot-reload reflects on the very next poll. When the agent
    isn't backed by a bot (test harness, agent-only deployments) we
    return an empty list — the composer panel hides itself in that case.

    Reused across both panels: the chat composer's autocomplete and any
    future ``/help`` mirror in the SPA. RBAC: same as ``/api/agents`` —
    visible to every authenticated role; triggers don't reveal anything
    a user couldn't discover by typing.
    """
    _ = caller
    # Agent must exist — return 404 to match the rest of the router.
    _require_runtime(name, state)
    provider = state.trigger_providers.get(name)
    if provider is None:
        return []
    try:
        items = provider()
    except Exception:
        # Hot-reload mid-flight could land here; degrade silently
        # rather than 500 the picker.
        return []
    return [
        TriggerSuggestion(
            raw=t.raw,
            label=t.label,
            has_args=t.has_args,
            literal_prefix=t.literal_prefix,
        )
        for t in items
    ]


@router.get("/{name}/memory", response_model=_MemoryView)
async def memory(
    name: str,
    user_id: str | None = Query(default=None),
    scope_id: str = Query(default="test"),
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> _MemoryView:
    """Return the agent memory snapshot for ``(user_id, scope_id)``.

    Bootstrapped bots provide a KV-backed snapshot with short-term history,
    running summary and the user's profile. Minimal test harnesses and
    standalone agent registries may still expose the legacy in-memory
    ``runtime.memory`` shape, so we keep that as a compatibility fallback.
    """
    runtime = _require_runtime(name, state)
    subject_user_id = caller.username if user_id is None else user_id
    if not caller.is_superadmin and subject_user_id != caller.username:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cannot read another user's memory")
    provider = state.memory_providers.get(name)
    if provider is not None:
        try:
            snapshot = await provider(subject_user_id, scope_id)
            return _MemoryView(
                short_term=snapshot.short_term,
                summary=snapshot.summary,
                long_term=snapshot.long_term,
            )
        except Exception:
            # A memory panel should not make the agent disappear from the UI.
            # Fall through to the legacy runtime accessor below.
            pass

    # Prefer a public ``memory`` accessor, but accept the current private
    # ``_memory`` used by :class:`AgentRuntime` (and the fake used in tests).
    mem = getattr(runtime, "memory", None) or getattr(runtime, "_memory", None)
    short: list[dict[str, Any]] = []
    if mem is not None and hasattr(mem, "get"):
        try:
            messages = mem.get(subject_user_id, scope_id)
        except Exception:
            messages = []
        for m in messages:
            short.append(
                {
                    "role": getattr(m, "role", "user"),
                    "content": getattr(m, "content", ""),
                    "name": getattr(m, "name", None),
                }
            )
    return _MemoryView(short_term=short, long_term=[])


@router.post("/{name}/chat", response_model=ChatResponse)
async def chat(
    name: str,
    body: ChatRequest,
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> ChatResponse:
    """Single-shot chat. Routes through the DSL classifier first.

    Mirrors ``/ws/agents/{name}/stream``: when ``attach_bot_to_webui``
    has installed a web chat dispatcher for this agent, we delegate
    so triggers like ``我的灵玉`` reach their ``.ling`` handler. Without
    a dispatcher (e.g. minimal test harness) we fall back to a direct
    ``runtime.invoke`` like before.
    """
    web_dispatcher = state.chat_dispatchers.get(name)
    runtime = _require_runtime(name, state) if web_dispatcher is None else None
    t0 = time.monotonic()
    outcome: str = "ok"
    content = ""
    tool_calls_made = 0
    total_tokens = 0
    source = "agent"
    segments: list[ChatSegment] = []
    try:
        if web_dispatcher is not None:
            reply = await web_dispatcher(body.input, caller.username, body.scope_id)
            content = reply.content
            tool_calls_made = reply.tool_calls_made
            total_tokens = reply.total_tokens
            source = reply.source
            segments = [
                ChatSegment(
                    kind=s.kind,
                    text=s.text,
                    url=s.url,
                    alt=s.alt,
                    delay_before_s=s.delay_before_s,
                )
                for s in reply.segments
            ]
        else:
            assert runtime is not None
            result = await runtime.invoke(body.input, action_sink=_noop_capture_sink)
            tool_calls_made = result.tool_calls_made
            total_tokens = result.total_tokens
            # Tool-based sending stores the actual words in sent_texts;
            # fall back to content for the legacy plaintext path.
            sent_texts = getattr(result, "sent_texts", None) or []
            content = "\n".join(sent_texts) if sent_texts else result.content
            if content:
                segments = [ChatSegment(kind="text", text=content)]
    except Exception:
        outcome = "err"
        raise
    finally:
        latency_ms = (time.monotonic() - t0) * 1000.0
        # Audit log (best-effort, in-memory). We do this in finally so the
        # trace line lands even on exception paths — otherwise the /ws path
        # would be the only source of agent-invocation audit entries.
        if state.audit is None:
            state.audit = AuditReader()
        state.audit.append(
            bot_id="webui",
            user_id=caller.username,
            scope_id=f"agents/{name}",
            kind="agent_chat",
            outcome=outcome,
            latency_ms=latency_ms,
            payload={"input_len": len(body.input)},
        )

    return ChatResponse(
        content=content,
        tool_calls_made=tool_calls_made,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        source=source,
        segments=segments,
    )
