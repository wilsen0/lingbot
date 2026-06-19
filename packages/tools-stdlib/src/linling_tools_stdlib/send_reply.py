"""``send_reply`` tool — sends a message in the current conversation scope.

Used by the agent DM path (``AgentRuntime.invoke``) as the primary
outbound messaging mechanism.  The group batch path has its own
dedicated pseudo-tools (``send_group``, ``reply_to_message``) and does
not go through the global tool registry for sending.
"""

from __future__ import annotations

import logging
from typing import Any

from linling_core.events import Action, Scope
from linling_core.segments import TextSegment
from linling_core.tools import ToolCtx, tool

logger = logging.getLogger(__name__)


@tool(
    name="send_reply",
    dsl_name="",
    description="Send a message in the current conversation. Call multiple times to send multiple messages.",
    schema={"text": "string"},
)
async def send_reply(ctx: ToolCtx, text: str = "") -> str:
    """Send *text* as a message in the current conversation.

    The target scope is derived from ``ctx.event.scope`` (the inbound
    DM or group).  Delivery awaits the ``action_sink`` stored in
    ``ctx.extras`` so the caller sees real failures (and ordering is
    preserved across multiple calls in the same turn); this matches the
    group-batch path's ``_send_action`` contract.

    The sent text is also appended to the ``sent_texts`` list in
    ``ctx.extras`` so the runtime / dispatcher can record what was
    actually said in history (rather than a finish_turn summary).
    """
    if not text or not text.strip():
        return "error: text is empty"

    sink: Any = ctx.extras.get("action_sink")
    if sink is None:
        logger.info("send_reply.no_sink", text_preview=text[:100])
        return "error: no action sink available"

    event = ctx.event
    if event is None:
        logger.warning("send_reply.no_event")
        return "error: no event context"

    platform = event.platform if event.platform not in ("", "scheduler") else ""
    if not platform:
        platform = str(ctx.extras.get("primary_platform") or "")
    if not platform:
        logger.warning("send_reply.no_platform")
        return "error: cannot determine platform"

    cleaned = text.strip()
    scope = Scope(kind=event.scope.kind, id=event.scope.id, platform=platform)
    action = Action(kind="send", target=scope, segments=[TextSegment(text=cleaned)])

    # Await (do not fire-and-forget): a slow adapter is awaited in line
    # so the router's audit/metrics/error path and message ordering stay
    # meaningful — the DM path previously bypassed all of that by
    # spawning an unowned task.  Failures surface to the model as a tool
    # error so it can react (retry / apologise) instead of the message
    # vanishing silently.
    try:
        result = sink(action)
        if _is_awaitable(result):
            await result
    except Exception as exc:
        logger.exception("send_reply.sink_failed", text_preview=cleaned[:100])
        return f"error: delivery failed: {exc}"

    # Record what was actually sent so history reflects reality.
    sent_texts: list[str] | None = ctx.extras.get("sent_texts")
    if sent_texts is not None:
        sent_texts.append(cleaned)
    return "ok"


def _is_awaitable(value: Any) -> bool:
    import asyncio

    return asyncio.iscoroutine(value) or isinstance(value, asyncio.Future)
