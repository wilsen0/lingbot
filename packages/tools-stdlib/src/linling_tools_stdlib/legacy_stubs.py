"""Best-effort stubs for QRDic primitives we haven't fully ported yet.

These stand-ins keep ``dicpro.txt``-style rules from crashing the VM
when they hit a tool we haven't implemented for real. Each stub logs
its invocation at INFO so an operator can see what the rule wanted to
do, and returns a sensible empty value (the empty string for most,
``"0"`` for counters) so downstream interpolation keeps flowing.

When a stub gets a real implementation, replace it here — the DSL
name and arity stay the same, so the rule files don't change.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from linling_core.tools import ToolCtx, tool

if TYPE_CHECKING:
    from linling_core.tools import ToolRegistry
    from linling_dsl.vm import VMResult

logger = structlog.get_logger(__name__)


# Cap on nested ``$回调$`` chain depth — guards against an
# A→B→A→B→… infinite-callback that would otherwise blow the Python
# call stack and hang the dispatcher. 16 is well above the deepest
# legitimate chain in dicpro.txt (3 levels — see
# ``test_three_level_callback_chain_propagates_text``) but well below
# Python's default recursion limit so the abort fires before we
# crash.
_CALLBACK_MAX_DEPTH = 16


# ---------------------------------------------------------------------------
# Shared helper: synchronous internal-handler invocation
# ---------------------------------------------------------------------------


async def invoke_internal_handler(
    ctx: ToolCtx, handler: str, *extra: str
) -> VMResult | None:
    """Run an ``[内部]`` handler in a fresh sub-VM and return its :class:`VMResult`.

    Backs both ``$回调$`` (``callback_stub`` — text return) and
    synchronous ``$调用 0 ...$`` (``schedule_handler`` — segment
    return). Centralised so the lookup / depth-guard / capture
    forwarding stay consistent across both call sites.

    Returns ``None`` when:
    * ``handler_lookup`` isn't wired into ``ctx.extras`` (e.g. a unit
      test using a bare VM);
    * the named handler can't be located in the live script;
    * ``ctx.event`` is missing (no event → no scope/sender to seed
      the inner VM with);
    * the depth budget would be exceeded.

    Errors raised by the inner VM are logged and swallowed:
    ``$回调$`` / ``$调用$`` are best-effort by design — a single
    failed inner handler should not take down the calling rule.
    """
    handler_lookup = ctx.extras.get("handler_lookup")
    if handler_lookup is None:
        logger.info("legacy_stub.callback_no_lookup", handler=handler, extra=list(extra))
        return None

    lookup_result = handler_lookup(handler) if callable(handler_lookup) else None
    target: Any | None
    regex_captures: list[str] = []
    if isinstance(lookup_result, tuple):
        target = lookup_result[0]
        regex_captures = list(lookup_result[1] or [])
    else:
        target = lookup_result

    # Space-joined fallback for triggers with literal spaces. Real
    # rules write ``$回调 游戏判断 %a%$`` against an internal handler
    # ``[内部]游戏判断 ([0-9]+)`` — the parser splits the call into
    # ``handler="游戏判断"`` + ``extra=["12345"]``, so the literal
    # lookup misses and the regex fullmatch on ``"游戏判断"`` alone
    # also misses (the trigger expects a space + digits suffix). Try
    # again with ``handler + " " + " ".join(extra)`` so the regex
    # lookup sees the fully reconstructed call shape; on hit we
    # forward the captures and *don't* re-pass ``extra`` (the regex
    # already consumed them) to avoid duplicate args.
    consumed_extras = False
    if target is None and extra and callable(handler_lookup):
        joined = handler + " " + " ".join(str(a) for a in extra)
        joined_result = handler_lookup(joined)
        if isinstance(joined_result, tuple):
            target = joined_result[0]
            regex_captures = list(joined_result[1] or [])
            consumed_extras = True
        elif joined_result is not None:
            target = joined_result
            consumed_extras = True

    if target is None:
        logger.info("legacy_stub.callback_unknown_handler", handler=handler)
        return None

    if ctx.event is None:
        return None

    # Cap nested $回调$ / $调用 0$ depth — without this, two handlers
    # calling each other (``[内部]A: $回调 B$`` / ``[内部]B: $回调 A$``)
    # blow the Python call stack and hang the dispatcher. Each child
    # VM has its own ``max_steps`` / ``timeout_ms`` budget so a single
    # inner step is cheap; only the *unbounded chain* is dangerous.
    # We track depth through ``ctx.extras["_callback_depth"]`` so the
    # limit applies across nested inline calls in the same dispatch
    # tree.
    depth = int(ctx.extras.get("_callback_depth", 0)) + 1
    if depth > _CALLBACK_MAX_DEPTH:
        logger.warning(
            "legacy_stub.callback_depth_exceeded",
            handler=handler,
            depth=depth,
            max_depth=_CALLBACK_MAX_DEPTH,
        )
        return None

    # Late import: the DSL VM lives in a downstream package; importing
    # eagerly would create a cycle (linling_core ↔ linling_dsl).
    from linling_dsl.vm import VM  # noqa: PLC0415

    inner_extras = dict(ctx.extras)
    inner_extras["_callback_depth"] = depth
    # Strip the per-call inline-emit hook from the *child* VM's
    # extras: the inner handler emits via its own
    # ``self.segments`` and the helper returns a VMResult to the
    # caller. Forwarding ``_inline_emit`` to the child would cause
    # nested ``$调用 0$`` to write into both this VM's segments
    # *and* the parent's, double-counting the inner output.
    inner_extras.pop("_inline_emit", None)
    vm = VM(
        tool_registry=_registry_from_ctx(ctx),
        kv=ctx.kv,
        bot_id=ctx.bot_id,
        extras=inner_extras,
    )
    if consumed_extras:
        all_captures = regex_captures
    else:
        all_captures = regex_captures + list(extra)
    try:
        return await vm.execute_handler(target, ctx.event, captures=all_captures)
    except Exception:
        logger.exception("legacy_stub.callback_failed", handler=handler)
        return None


# ---------------------------------------------------------------------------
# Filesystem-shaped tools (QRDic stored data in flat .txt files).
# ---------------------------------------------------------------------------


@tool(
    name="read_file_stub",
    dsl_name="读文件",
    description="QRDic legacy filesystem read — currently a no-op stub returning empty",
    schema={"path": "string", "default": "string?"},
    safe=True,
    llm_visible=False,
)
async def read_file_stub(ctx: ToolCtx, path: str = "", default: str = "") -> str:
    """Return ``default`` and log the request.

    ``$读文件 path default$`` would read a flat file in QRDic. We
    intentionally do *not* do disk IO here — the KV store covers all
    legitimate persistence needs. Rules that still call this tool are
    almost certainly using it for migrated data; the migrator should
    convert them.
    """
    logger.info("legacy_stub.read_file", path=path)
    return default


@tool(
    name="write_file_stub",
    dsl_name="写文件",
    description="QRDic legacy filesystem write — currently a no-op stub",
    schema={"path": "string", "content": "string"},
    safe=False,
    llm_visible=False,
)
async def write_file_stub(ctx: ToolCtx, path: str = "", content: str = "") -> str:
    """Drop the write and log it. Returns the empty string."""
    logger.info("legacy_stub.write_file", path=path, bytes=len(content))
    return ""


@tool(
    name="dict_op_stub",
    dsl_name="词库操作",
    description="QRDic dictionary mutation — currently a no-op stub",
    schema={"action": "string", "target": "string"},
    safe=False,
    llm_visible=False,
)
async def dict_op_stub(ctx: ToolCtx, action: str = "", target: str = "") -> str:
    """``$词库操作 添加|删除 path.txt$`` — placeholder for runtime dictionary edits.

    The migrated handler set is loaded once at boot today, so per-call
    mutation has no effect. We log the intent so operators can spot
    rules that depend on this and request a proper implementation.
    """
    logger.info("legacy_stub.dict_op", action=action, target=target)
    return ""


# ---------------------------------------------------------------------------
# Internal-handler call shapes that aren't quite ``$调用$``.
# ---------------------------------------------------------------------------


@tool(
    name="callback_stub",
    dsl_name="回调",
    description="QRDic synchronous internal-handler call: $回调 handler args...$ — returns the handler's text output",
    schema={"handler": "string", "args_blob": "string?"},
    safe=False,
    llm_visible=False,
)
async def callback_stub(ctx: ToolCtx, handler: str = "", *extra: str) -> str:
    """Invoke an ``[内部]`` handler synchronously and return its output.

    QRDic's ``$回调 handler args...$`` is the inline counterpart to
    ``$调用$``: it runs the named internal handler *now*, captures
    the handler's text output, and yields it as the tool's return
    value. Many game-side rules do things like::

        s:$回调 游戏判断 %a%$
        $JSON 删除 r %s%$

    where ``s`` is expected to be the index returned by ``游戏判断``.

    Implementation: delegates to :func:`invoke_internal_handler`,
    which spins up a fresh VM that shares the current KV / registry /
    extras, looks the handler up via ``ctx.extras["handler_lookup"]``,
    and runs it against ``ctx.event``. We concatenate the resulting
    text segments because ``$回调$`` is the assignment-form primitive
    (``s:$回调 ...$``); image segments don't fit a string variable so
    they're dropped here. Standalone ``$回调 ...$`` lines stay silent
    by design — that matches QRSpeed semantics. Use ``$调用 0 ...$``
    if you want the inner handler's full output (text + images) to
    appear inline.
    """
    if not handler:
        return ""

    result = await invoke_internal_handler(ctx, handler, *extra)
    if result is None:
        return ""

    # Concatenate text segments — that's the output users assigned to
    # ``s:`` etc. expect.
    parts: list[str] = []
    for seg in result.segments:
        text = getattr(seg, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _registry_from_ctx(ctx: ToolCtx) -> ToolRegistry:
    """Pull the active tool registry from ``ctx.extras`` or fall back to global.

    The DSL dispatcher always passes ``registry`` to its VM but does
    not currently surface it via ``ctx.extras``. As a safe default we
    reach into the global registry here. Future refactors can plug a
    per-bot registry into ``ctx.extras["registry"]`` and this lookup
    will pick it up automatically.
    """
    from linling_core.tools import ToolRegistry as _RegistryCls  # noqa: PLC0415
    from linling_core.tools import registry as global_registry  # noqa: PLC0415

    explicit = ctx.extras.get("registry")
    if isinstance(explicit, _RegistryCls):
        return explicit
    return global_registry


@tool(
    name="exec_stub",
    dsl_name="执行",
    description="QRDic shell-eval primitive — currently a no-op stub",
    schema={"code": "string"},
    safe=False,
    llm_visible=False,
)
async def exec_stub(ctx: ToolCtx, code: str = "") -> str:
    """``$执行 ...$`` — arbitrary script eval. Refused by design.

    Letting rule files run arbitrary Python (or Java BSH, as the
    original engine did) is a fundamental safety hole. The stub logs
    a warning so operators can spot which rules need refactoring.
    """
    logger.warning("legacy_stub.exec_refused", chars=len(code))
    return ""


@tool(
    name="bsh_stub",
    dsl_name="BSH",
    description="QRDic BeanShell eval — currently a no-op stub",
    schema={"code": "string"},
    safe=False,
    llm_visible=False,
)
async def bsh_stub(ctx: ToolCtx, code: str = "") -> str:
    """``$BSH ...$`` — Java BeanShell eval. Refused for the same reason as ``$执行$``."""
    logger.warning("legacy_stub.bsh_refused", chars=len(code))
    return ""


# ---------------------------------------------------------------------------
# Adapter-side actions that need the live IM client (QQ etc.).
# ---------------------------------------------------------------------------


@tool(
    name="recall_stub",
    dsl_name="撤回",
    description="QRDic message recall — no-op without a live adapter",
    schema={"group_id": "string", "message_id": "string"},
    safe=False,
    llm_visible=False,
)
async def recall_stub(ctx: ToolCtx, group_id: str = "", message_id: str = "") -> str:
    """``$撤回 group msg_id$`` — would call the adapter's recall API.

    Until the DSL dispatcher exposes the active adapter through
    ``ctx.extras`` we degrade to logging. Args default to empty so
    malformed calls don't raise.
    """
    logger.info("legacy_stub.recall", group_id=group_id, message_id=message_id)
    return ""


@tool(
    name="mute_stub",
    dsl_name="禁",
    description="QRDic per-user mute — no-op without a live adapter",
    schema={"group_id": "string", "user_id": "string", "duration_s": "string"},
    safe=False,
    llm_visible=False,
)
async def mute_stub(
    ctx: ToolCtx, group_id: str = "", user_id: str = "", duration_s: str = "60"
) -> str:
    logger.info(
        "legacy_stub.mute",
        group_id=group_id,
        user_id=user_id,
        duration_s=duration_s,
    )
    return ""


@tool(
    name="mute_all_stub",
    dsl_name="全体禁言",
    description="QRDic group-wide mute — no-op without a live adapter",
    schema={"group_id": "string", "enabled": "string"},
    safe=False,
    llm_visible=False,
)
async def mute_all_stub(ctx: ToolCtx, group_id: str = "", enabled: str = "1") -> str:
    logger.info("legacy_stub.mute_all", group_id=group_id, enabled=enabled)
    return ""


@tool(
    name="set_group_status_stub",
    dsl_name="设置群状态",
    description="QRDic group status setter — no-op without a live adapter",
    schema={"group_id": "string", "status": "string"},
    safe=False,
    llm_visible=False,
)
async def set_group_status_stub(ctx: ToolCtx, group_id: str = "", status: str = "") -> str:
    logger.info("legacy_stub.set_group_status", group_id=group_id, status=status)
    return ""


@tool(
    name="leave_group_stub",
    dsl_name="退出群",
    description="QRDic leave-group — no-op without a live adapter",
    schema={"group_id": "string"},
    safe=False,
    llm_visible=False,
)
async def leave_group_stub(ctx: ToolCtx, group_id: str = "") -> str:
    logger.info("legacy_stub.leave_group", group_id=group_id)
    return ""


@tool(
    name="apply_group_stub",
    dsl_name="申请群",
    description="QRDic apply-to-group — no-op without a live adapter",
    schema={"group_id": "string", "comment": "string?"},
    safe=False,
    llm_visible=False,
)
async def apply_group_stub(ctx: ToolCtx, group_id: str = "", comment: str = "") -> str:
    logger.info("legacy_stub.apply_group", group_id=group_id, comment=comment)
    return ""


@tool(
    name="rename_group_stub",
    dsl_name="改",
    description="QRDic group nickname change — no-op without a live adapter",
    schema={"group_id": "string", "user_id": "string", "name": "string"},
    safe=False,
    llm_visible=False,
)
async def rename_group_stub(
    ctx: ToolCtx, group_id: str = "", user_id: str = "", name: str = ""
) -> str:
    """``$改 group_id user_id new_name$`` — set a member's group nickname."""
    logger.info(
        "legacy_stub.rename_group",
        group_id=group_id,
        user_id=user_id,
        new_name=name,
    )
    return ""


# ---------------------------------------------------------------------------
# 输出为 — currently a sugar over plain output; map to identity.
# ---------------------------------------------------------------------------


@tool(
    name="emit_var",
    dsl_name="输出为",
    description="Echo the given value (QRDic ``$输出为 %x%$``)",
    schema={"value": "string"},
    safe=True,
    llm_visible=False,
)
async def emit_var(ctx: ToolCtx, value: str = "", *extra: str) -> str:
    """Return the value verbatim — QRDic uses this as a sugar to emit
    a variable's value through the standard output stream.

    Variadic over trailing args so a rule that accidentally writes
    ``$输出为 hello world$`` (instead of the canonical
    ``$输出为 %var%$``) joins the words instead of dropping ``world``
    on the floor. Real dicpro.txt rules always pass one
    interpolated variable, but the join is the safer fallback.
    """
    if not value and not extra:
        return ""
    if extra:
        return value + " " + " ".join(str(p) for p in extra)
    return value


# ---------------------------------------------------------------------------
# 发送 — outbound message routed through the adapter sink.
# ---------------------------------------------------------------------------


@tool(
    name="send_message",
    dsl_name="发送",
    description=(
        "QRDic-style outbound message: $发送 群|好友|临时 msg|img target body$ — "
        "routed through the adapter that owns ctx.event.platform."
    ),
    schema={
        "scope_kind": "string?",
        "media": "string?",
        "target": "string?",
        "body": "string?",
    },
    safe=False,
    llm_visible=False,
)
async def send_message(
    ctx: ToolCtx,
    scope_kind: str = "",
    media: str = "",
    target: str = "",
    *body_parts: str,
) -> str:
    """Synthesise an outbound :class:`Action` and hand it to the sink.

    The DSL ``$发送 群 msg 12345 hello$`` becomes a ``send`` action
    targeted at group ``12345``. Multiple trailing words are joined
    with single spaces — matches how QRDic eats whitespace inside the
    command tail.

    All four required positional args have defaults of ``""`` so a
    malformed ``$发送$`` (which appears in ``dicpro.txt`` as a no-op
    placeholder) degrades to a logged miss rather than crashing the
    handler. Without an active adapter wired into ``ctx.extras
    ['action_sink']`` we similarly degrade — the message is still
    observable in the audit trail through the logger.
    """
    from linling_core.events import Action, Scope  # noqa: PLC0415
    from linling_core.segments import ImageSegment, Segment, TextSegment  # noqa: PLC0415

    # ``$发送$`` with no positional args appears in dicpro.txt as a
    # placeholder/no-op (see ``加入...`` flow). Honour it: log, return.
    if not scope_kind and not target:
        return ""

    sink = ctx.extras.get("action_sink")
    body = " ".join([str(p) for p in body_parts])

    # Pick a routing platform. The DSL's ``$发送$`` doesn't carry one
    # — it inherits from the inbound event. For user-triggered
    # dispatches that's the IM platform we want; for scheduler-fired
    # dispatches the inbound event has ``platform="scheduler"`` which
    # no real adapter handles. Fall back to the routing hint the
    # bootstrap stuffs into extras (the primary adapter's platform).
    platform = ctx.event.platform if ctx.event is not None else ""
    if platform in ("", "scheduler"):
        platform = str(ctx.extras.get("primary_platform") or "")
    if not platform:
        # No routable platform → log and bail. This also covers the
        # unit-test path where neither an event nor a primary hint is
        # provided.
        logger.warning("send_message.no_platform", scope_kind=scope_kind, target=target)
        return ""

    if scope_kind in ("群", "group"):
        scope = Scope(kind="group", id=target, platform=platform)
    elif scope_kind in ("好友", "dm", "private"):
        scope = Scope(kind="dm", id=target, platform=platform)
    elif scope_kind in ("临时", "temp"):
        # Temp messages target a user via a group context. We model
        # them as a DM with the group hint stored in options so the
        # OneBot adapter can pick the right API.
        scope = Scope(kind="dm", id=target, platform=platform)
    else:
        logger.warning("send_message.unknown_scope_kind", scope_kind=scope_kind)
        return ""

    segments: list[Segment]
    if media in ("img", "image"):
        # Image URLs are never escape-decoded — see vm._decode_qrdic_escapes
        # for the rationale; URLs containing literal ``\n`` are real (rare
        # but legal CDN paths). Send as-is.
        segments = [ImageSegment(url=body)]
    else:
        # Text bodies follow the same OutputText escape contract: authored
        # ``\n``/``\r``/``\t`` decode to real control chars. Without this
        # decode, a rule like ``$发送 群 msg %群号% line1\nline2$`` ships
        # the literal two-character ``\n`` to the chat client, which renders
        # it verbatim instead of as a line break. Mirrors the on-screen
        # behaviour of plain ``OutputText`` lines that already decode.
        from linling_dsl.vm import _decode_qrdic_escapes  # noqa: PLC0415
        segments = [TextSegment(text=_decode_qrdic_escapes(body))]

    action = Action(kind="send", target=scope, segments=segments)

    if sink is None:
        logger.info(
            "legacy_stub.send_no_sink",
            scope_kind=scope_kind,
            target=target,
            body_len=len(body),
        )
        return ""

    # Fire-and-forget: ``$发送$`` semantically means "throw it out the
    # door". Awaiting the sink would block the calling handler on the
    # adapter's WebSocket round-trip — which, when the QQ side fails
    # (kicked from group, image URL fetch timeout, rate limit) leaves
    # us holding the session lock for the full ``call_api`` budget.
    # Spawn the sink as a background task and return immediately so
    # the handler's other steps run in parallel. Errors are logged at
    # task-completion time; we never raise back into the rule.
    async def _deliver() -> None:
        try:
            await sink(action)
        except Exception:
            logger.exception(
                "send_message.sink_failed",
                target=target,
                scope_kind=scope_kind,
            )

    asyncio.create_task(_deliver(), name=f"send_message:{scope_kind}:{target}")
    return ""
