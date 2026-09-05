"""Sticker collection tools — save, list, and send collected stickers.

These tools are ``vision_only``: they only appear in the LLM's tool
catalog when the agent has ``vision_enabled=True``. They rely on
``ctx.extras["image_resolver"]`` and ``ctx.extras["sticker_dir"]``
being injected by the AgentRuntime (see runtime.py extras injection).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from linling_agent.images import ImageContentResolver, collect_image_refs
from linling_agent.sticker_store import StickerStore
from linling_core.events import Action, Scope
from linling_core.segments import ImageSegment
from linling_core.tools import ToolCtx, tool

logger = logging.getLogger(__name__)


def _extras(ctx: ToolCtx) -> tuple[ImageContentResolver | None, Any, Any]:
    """Pull the sticker-tool extras out of ``ctx`` (never raises)."""
    return (
        ctx.extras.get("image_resolver"),
        ctx.extras.get("sticker_dir"),
        ctx.extras.get("action_sink"),
    )


def _decode_data_uri(data_uri: str) -> tuple[bytes, str] | None:
    """Split ``data:<mime>;base64,<payload>`` into (bytes, mime)."""
    try:
        header, payload = data_uri.split(";base64,", 1)
    except ValueError:
        return None
    if not header.startswith("data:"):
        return None
    mime = header[len("data:") :] or "image/jpeg"
    try:
        raw = base64.b64decode(payload)
    except ValueError:
        return None
    return raw, mime


@tool(
    name="save_sticker",
    dsl_name="",
    description="Save a sticker/image from the current conversation into your collection. Give it a memorable name.",
    schema={"name": "string", "tags": "string?"},
    safe=False,
    vision_only=True,
)
async def save_sticker(ctx: ToolCtx, name: str = "", tags: str = "") -> str:
    """Persist the first image in the current message to the sticker collection."""
    resolver, sticker_dir, _sink = _extras(ctx)
    if resolver is None or sticker_dir is None:
        return "error: sticker tools not available"

    if ctx.event is None or not ctx.event.segments:
        return "error: no image found in the current message"
    refs = collect_image_refs(ctx.event.segments)
    if not refs:
        return "error: no image found in the current message"

    data_uri = await resolver.resolve(refs[0])
    if data_uri is None:
        return "error: could not download the image"
    decoded = _decode_data_uri(data_uri)
    if decoded is None:
        return "error: could not download the image"
    image_bytes, mime = decoded

    saved_name = name or ""
    store = StickerStore(ctx.kv, sticker_dir)
    try:
        sticker_id = await store.save(
            image_bytes,
            # Pass the raw (possibly empty) name: StickerStore only refreshes
            # metadata on dedup when a non-empty value is provided, so an
            # unnamed save never overwrites an existing sticker's name.
            name=name,
            tags=tags,
            source_qq=ctx.event.sender.id if ctx.event else "",
            mime=mime,
        )
    except ValueError as exc:
        return f"error: {exc}"
    if saved_name:
        return f"ok: saved as '{saved_name}' (id={sticker_id})"
    return f"ok: saved (id={sticker_id})"


@tool(
    name="list_stickers",
    dsl_name="",
    description="List your collected stickers. Optionally filter by name/tag keyword.",
    schema={"query": "string?"},
    safe=False,
    vision_only=True,
)
async def list_stickers(ctx: ToolCtx, query: str = "") -> str:
    """List saved stickers, newest first, optionally filtered by ``query``."""
    _resolver, sticker_dir, _sink = _extras(ctx)
    if sticker_dir is None:
        return "error: sticker tools not available"

    store = StickerStore(ctx.kv, sticker_dir)
    items = await store.list(query)
    if not items:
        return "You have no collected stickers yet."

    shown = items[:20]
    lines = []
    for meta in shown:
        tags = meta.get("tags") or ""
        if tags:
            lines.append(f"- {meta.get('name', '')} (tags: {tags})")
        else:
            lines.append(f"- {meta.get('name', '')}")
    if len(items) > 20:
        lines.append(f"... and {len(items) - 20} more")
    return "\n".join(lines)


@tool(
    name="send_sticker",
    dsl_name="",
    description="Send a collected sticker to the current conversation by name.",
    schema={"name": "string"},
    safe=False,
    vision_only=True,
)
async def send_sticker(ctx: ToolCtx, name: str = "") -> str:
    """Send a previously saved sticker (matched by name) to the current scope."""
    _resolver, sticker_dir, sink = _extras(ctx)
    if sticker_dir is None or sink is None:
        return "error: sticker tools not available"

    if ctx.event is None:
        return "error: no event context"

    store = StickerStore(ctx.kv, sticker_dir)
    meta = await store.find_by_name(name)
    if meta is None:
        return f"error: no sticker named '{name}' found"

    raw = await store.load_bytes(meta["id"])
    if raw is None:
        return "error: sticker file missing"

    image_seg = ImageSegment(
        b64=base64.b64encode(raw).decode("ascii"),
        # 原图语义(subType=1):QQ 服务端对普通图(0)会压缩重编码、小图被
        # 放大;按原图发才保留收藏时的原始像素尺寸。
        extras={"subType": 1},
    )
    event = ctx.event
    # ``event.platform`` is empty for scheduler-originated events; fall back
    # to the runtime's primary platform exactly like ``send_reply``.
    platform = event.platform if event.platform not in ("", "scheduler") else ""
    if not platform:
        platform = str(ctx.extras.get("primary_platform") or "")
    if not platform:
        logger.warning("send_sticker.no_platform")
        return "error: cannot determine platform"
    scope = Scope(kind=event.scope.kind, id=event.scope.id, platform=platform)
    action = Action(kind="send", target=scope, segments=[image_seg])

    try:
        result = sink(action)
        if _is_awaitable(result):
            await result
    except Exception as exc:
        logger.exception("send_sticker.sink_failed: %s", name)
        return f"error: delivery failed: {exc}"
    return f"ok: sent sticker '{name}'"


def _is_awaitable(value: Any) -> bool:
    return asyncio.iscoroutine(value) or isinstance(value, asyncio.Future)
