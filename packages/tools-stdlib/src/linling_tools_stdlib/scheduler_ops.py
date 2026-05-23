"""Bridge from DSL ``$调用 ms handler args$`` to the runtime scheduler.

The scheduler is injected into every :class:`ToolCtx` via
``ctx.extras["scheduler"]`` by the DSL dispatcher. When no scheduler
is wired (e.g. unit-test VMs) ``$调用$`` becomes a no-op — that's
fine because tests should drive scheduler behaviour through
:class:`linling_core.scheduler` directly anyway.

Why a tool wrapper instead of a hard-coded VM op: the scheduler is
the runtime's responsibility, but the DSL syntax is part of the rule
contract. Going through the registry keeps the VM ignorant of
scheduling — same separation we have for KV / JSON / random.

``ms == 0`` is a special case: the rule author meant "do this right
now, just out of band". On QQ that distinction (next-tick vs inline)
is invisible because the IM client renders both as separate bubbles.
On the WebUI's single-bubble chat surface a fire-and-forget
zero-delay schedule produces silent rules — the inner handler emits
into nowhere because the scheduled task fires after the WebUI
dispatch already returned. We therefore promote ``ms == 0`` to a
synchronous inline emit through ``ctx.extras["_inline_emit"]`` so
the inner handler's segments land on the calling rule's output
buffer. ``ms > 0`` keeps the legacy scheduler semantics unchanged.
"""

from __future__ import annotations

import structlog
from linling_core.tools import ToolCtx, tool

from linling_tools_stdlib.legacy_stubs import invoke_internal_handler

logger = structlog.get_logger(__name__)


@tool(
    name="schedule_handler",
    dsl_name="调用",
    description=(
        "QRDic-style delayed handler call: $调用 ms handler arg1 arg2 ...$ — "
        "fires the named handler after `ms` milliseconds."
    ),
    schema={
        "ms": "string",
        "handler_name": "string",
        # Args are positional and variable-length; we accept the rest
        # as one merged blob to keep the schema tractable.
        "args_blob": "string?",
    },
    safe=False,
    llm_visible=False,
)
async def schedule_handler(
    ctx: ToolCtx, ms: str = "", handler_name: str = "", *extra_args: str
) -> str:
    """Schedule a handler to fire after ``ms`` milliseconds, or run inline when ``ms == 0``.

    Returns the scheduler task id when a scheduler dispatched it, or
    the empty string when:
    * ``ms == 0`` and the inline path handled it (the inner handler's
      segments were emitted via ``_inline_emit``);
    * no scheduler is wired and ``ms > 0`` (logged stub);
    * the call shape is malformed (empty handler name).

    ``ms`` is parsed leniently — non-numeric values fall back to
    ``0`` so a typo doesn't crash the dispatch (and now also means
    "run inline"). Variadic ``*extra_args`` lets the DSL caller
    pass arbitrary handler arguments through unchanged.
    """
    if not handler_name:
        return ""

    try:
        delay_ms = int(ms)
    except (TypeError, ValueError):
        delay_ms = 0
    delay_ms = max(delay_ms, 0)

    args_list = [str(a) for a in extra_args]

    # Inline path: ``$调用 0 handler ...$``. We run the inner handler
    # in a sub-VM that shares the current KV / registry / extras and
    # promote its segments onto the caller's output buffer through
    # the per-call ``_inline_emit`` hook the VM injects into
    # ``ctx.extras``. This makes the WebUI single-bubble surface
    # hear ``[内部]我在`` etc. on the same turn, while QQ deployments
    # see the emit one bubble earlier with no behavioural change
    # (the previous separate-bubble shape was an accident of
    # scheduler latency, not a contract).
    if delay_ms == 0:
        inline_emit = ctx.extras.get("_inline_emit")
        if callable(inline_emit):
            result = await invoke_internal_handler(ctx, handler_name, *args_list)
            if result is not None and result.segments:
                inline_emit(list(result.segments))
            return ""
        # No inline-emit hook (e.g. legacy callers that build a
        # ToolCtx by hand). Fall through to the scheduler path so
        # the call still reaches the inner handler — just with the
        # original next-tick latency.

    scheduler = ctx.extras.get("scheduler")
    if scheduler is None:
        logger.info(
            "schedule_handler.no_scheduler",
            handler=handler_name,
            ms=ms,
        )
        return ""

    # Persist the inbound platform / scope kind alongside the
    # routable ids. ``_on_scheduled_fire`` reconstructs ``event.scope``
    # from these so the resulting reply Action targets the same
    # adapter (``onebot`` for QQ-side dispatches, ``cli`` for local
    # debugging) as the originating user message. Without this,
    # scheduled handlers' replies would be tagged ``platform="scheduler"``
    # and silently dropped by ``build_sink._multi`` because no real
    # adapter advertises that platform.
    if ctx.event is not None:
        scope_id = ctx.event.scope.id
        sender_id = ctx.event.sender.id
        scope_platform = ctx.event.scope.platform
        scope_kind = ctx.event.scope.kind
    else:
        scope_id = ""
        sender_id = ""
        scope_platform = ""
        scope_kind = ""
    scope_payload: dict[str, str] = {
        "scope_id": scope_id,
        "sender_id": sender_id,
    }
    # Only store these when present so legacy persisted tasks
    # (created before this field existed) round-trip unchanged
    # through ``_row_to_task``.
    if scope_platform:
        scope_payload["platform"] = scope_platform
    if scope_kind:
        scope_payload["scope_kind"] = scope_kind
    task_id = scheduler.schedule(
        after_seconds=delay_ms / 1000.0,
        handler_name=handler_name,
        args=args_list,
        scope=scope_payload,
        bot_id=ctx.bot_id,
    )
    return str(task_id) if task_id else ""
