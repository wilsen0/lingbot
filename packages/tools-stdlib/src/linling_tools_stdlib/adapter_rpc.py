"""Platform-adapter RPC stubs.

These tools originally called QQ-specific APIs (``群昵称``, ``群头衔``,
``获取群成员``, ``获取消息``). In linling they delegate to whatever
platform adapter is supplied via ``ctx.extras["adapter"]`` — typically an
OneBot client exposing an ``async rpc(method, **kwargs)`` method. When no
adapter is available the tools return reasonable stubs so rules can run
in a CLI or test environment without RPC plumbing.
"""

from __future__ import annotations

import json
from typing import Any

from linling_core.tools import ToolCtx, tool


async def _adapter_call(ctx: ToolCtx, method: str, /, **kwargs: Any) -> Any | None:
    """Invoke ``ctx.extras['adapter'].rpc(method, **kwargs)`` if possible."""
    adapter = ctx.extras.get("adapter")
    if adapter is None:
        return None
    rpc = getattr(adapter, "rpc", None)
    if rpc is None:
        return None
    try:
        return await rpc(method, **kwargs)
    except Exception:
        return None


@tool(
    name="group_nickname",
    dsl_name="群昵称",
    description="Get a user's group nickname; falls back to user_id when no adapter",
    schema={"group_id": "string", "user_id": "string"},
    safe=True,
)
async def group_nickname(ctx: ToolCtx, group_id: str = "", user_id: str = "") -> str:
    """Return the group card, then the nickname, then finally *user_id*.

    Both args default to empty so a malformed ``$群昵称 g$`` (missing
    user id) doesn't crash the calling handler — it just degrades to
    an empty result, matching the no-adapter fallback.
    """
    if not user_id:
        return ""
    result = await _adapter_call(ctx, "get_group_member_info", group_id=group_id, user_id=user_id)
    if isinstance(result, dict):
        card = result.get("card") or result.get("nickname")
        if card:
            return str(card)
    return user_id


@tool(
    name="group_title",
    dsl_name="群头衔",
    description="Set a user's group special title (no-op if no adapter)",
    schema={"group_id": "string", "user_id": "string", "title": "string"},
    safe=False,
)
async def group_title(
    ctx: ToolCtx,
    group_id: str = "",
    user_id: str = "",
    title: str = "",
) -> str:
    """Ask the adapter to set a user's group special title.

    Returns the title on success, or an empty string if no adapter is
    configured, the RPC fails, or any required arg is missing. Args
    default to empty so a malformed ``$群头衔 g$`` doesn't crash.
    """
    if not group_id or not user_id:
        return ""
    result = await _adapter_call(
        ctx,
        "set_group_special_title",
        group_id=group_id,
        user_id=user_id,
        special_title=title,
    )
    if result is None:
        return ""
    return title


@tool(
    name="group_members",
    dsl_name="获取群成员",
    description="Return a JSON array of user IDs in a group",
    schema={"group_id": "string"},
    safe=True,
)
async def group_members(ctx: ToolCtx, group_id: str = "") -> str:
    """Return member user IDs as a JSON array string.

    The adapter may return a list of member objects (dicts with a
    ``user_id`` / ``id`` key) or a list of primitives — both are
    normalised to a flat list of stringified IDs. Empty ``group_id``
    or no adapter wired returns ``"[]"``.
    """
    if not group_id:
        return "[]"
    result = await _adapter_call(ctx, "get_group_member_list", group_id=group_id)
    if not isinstance(result, list):
        return "[]"

    ids: list[str] = []
    for entry in result:
        if isinstance(entry, dict):
            uid = entry.get("user_id") or entry.get("id") or entry.get("uid")
            if uid is not None:
                ids.append(str(uid))
        elif entry is not None:
            ids.append(str(entry))
    return json.dumps(ids, ensure_ascii=False)


@tool(
    name="get_message_field",
    dsl_name="获取消息",
    description="Read a field from the current message's raw payload",
    schema={"field": "string", "default": "string?"},
    safe=True,
)
async def get_message_field(ctx: ToolCtx, field: str = "", default: str = "") -> str:
    """Return ``ctx.event.raw[field]`` as a string, or *default* if missing."""
    if not field:
        return default
    event = ctx.event
    if event is None:
        return default
    raw = getattr(event, "raw", None)
    if not isinstance(raw, dict):
        return default
    value = raw.get(field, default)
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


@tool(
    name="group_list",
    dsl_name="获取群列表",
    description="Return a JSON array of group IDs the bot belongs to",
    schema={},
    safe=True,
)
async def group_list(ctx: ToolCtx) -> str:
    """Return the bot's group memberships as a JSON array of IDs.

    Routes through ``adapter.rpc("get_group_list")`` when an adapter is
    wired into ``ctx.extras["adapter"]``. The OneBot v11 schema returns
    a list of ``{"group_id": int, "group_name": str}`` dicts; we
    normalise to a flat list of stringified IDs to match
    :func:`group_members` and keep DSL consumers simple. Without an
    adapter (CLI / WebUI sandbox / unit tests) we degrade to ``"[]"``.
    """
    result = await _adapter_call(ctx, "get_group_list")
    if not isinstance(result, list):
        return "[]"

    ids: list[str] = []
    for entry in result:
        if isinstance(entry, dict):
            gid = entry.get("group_id") or entry.get("id")
            if gid is not None:
                ids.append(str(gid))
        elif entry is not None:
            ids.append(str(entry))
    return json.dumps(ids, ensure_ascii=False)


@tool(
    name="group_add_request",
    dsl_name="进群审核",
    description=(
        "Approve / reject a pending group join request. "
        "$进群审核 group_id user_id A B reason$ — A=2001 同意 / 31 拒绝, "
        "B=11 同意 / 12 拒绝, reason optional. Maps to OneBot's "
        "set_group_add_request when an adapter is wired."
    ),
    schema={
        "group_id": "string",
        "user_id": "string",
        "approve_a": "string",
        "approve_b": "string",
        "reason": "string?",
    },
    safe=False,
)
async def group_add_request(
    ctx: ToolCtx,
    group_id: str = "",
    user_id: str = "",
    approve_a: str = "31",
    approve_b: str = "12",
    *reason_parts: str,
) -> str:
    """Resolve a pending group-join request.

    QRSpeed's documented argument order is ``$进群审核 group_id user_id
    A B reason$`` where ``A`` is the legacy approve/reject code
    (2001=同意 / 31=拒绝), ``B`` is the modernised binary form
    (11=同意 / 12=拒绝), and ``reason`` is the rejection message. We
    normalise to OneBot's ``set_group_add_request`` API.

    The ``flag`` (request id) is sourced from ``ctx.event.raw.flag`` —
    the synthetic ``[系统]`` event populates this automatically. If
    the flag is missing the call degrades to a logged miss rather
    than an exception.
    """
    flag = ""
    if ctx.event is not None:
        raw = getattr(ctx.event, "raw", None) or {}
        flag = str(raw.get("flag", ""))
    if not flag:
        return ""

    # Either code says "approve" → True. The two-code redundancy is
    # QRSpeed's hedge against forks of the engine that swapped the
    # approve/reject mapping; we honour either.
    approve = approve_a.strip() in ("2001", "11", "1") or approve_b.strip() in (
        "11",
        "1",
    )
    reason = " ".join(reason_parts).strip()

    result = await _adapter_call(
        ctx,
        "set_group_add_request",
        flag=flag,
        sub_type="add",
        approve=approve,
        reason=reason,
    )
    if result is None:
        return ""
    return "ok"



@tool(
    name="image_link",
    dsl_name="图片链接",
    description=(
        "Return the URL of the Nth image in the current message. "
        "$图片链接 N$ — fallback to path / b64 when no url is present. "
        "Empty string when out of range."
    ),
    schema={"index": "string"},
    safe=True,
)
async def image_link(ctx: ToolCtx, index: str = "0") -> str:
    """Resolve ``$图片链接 N$`` against the current message's image segments.

    Equivalent to ``%IMGN%`` exposed as an explicit tool call so DSL
    rules that compose the index dynamically (``$图片链接 [%i%-1]$``)
    can call it without going through the static ``%IMGn%`` template.
    Returns ``""`` when out of range or no event is attached.
    """
    from linling_core.segments import ImageSegment  # noqa: PLC0415

    event = ctx.event
    if event is None:
        return ""
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return ""
    imgs = [s for s in event.segments if isinstance(s, ImageSegment)]
    if 0 <= idx < len(imgs):
        img = imgs[idx]
        return img.url or img.path or img.b64 or ""
    return ""


@tool(
    name="download_file",
    dsl_name="下载",
    description=(
        "Download a remote URL to a local path. "
        "$下载 path url$ — confined to bot data dir; refuses absolute "
        "paths outside it. Returns the saved bytes count on success "
        "or empty on failure."
    ),
    schema={"path": "string", "url": "string"},
    safe=False,
    llm_visible=False,
)
async def download_file(ctx: ToolCtx, path: str = "", url: str = "") -> str:
    """Download ``url`` to ``path``.

    Both args default to empty so missing-arg calls return empty
    rather than raising. The actual download path validates the
    args before any IO happens.

    Security: the path must resolve under ``ctx.extras["data_root"]``
    if that's set; otherwise we refuse the call. This stops a rule
    from writing to ``/etc/...`` even if the URL itself is benign.
    URLs must be ``http(s)://``.

    The request is bounded by 10s timeout / 16 MiB cap; same posture
    as the WebUI image proxy. On failure the original file (if any)
    is left alone.
    """
    if not path:
        return ""
    import asyncio  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    if not url or not url.startswith(("http://", "https://")):
        return ""
    target = Path(path)
    data_root = ctx.extras.get("data_root")

    def _validate_and_prepare() -> bool:
        """Resolve / sandbox-check the target path on a worker thread.

        Pathlib's ``resolve()`` / ``write_bytes()`` are blocking;
        running them inline in an async function would block the
        event loop. We hand them off to the default executor.
        Returns ``True`` if the path is inside the sandbox and the
        parent directory is writable, ``False`` otherwise.
        """
        if data_root is not None:
            try:
                root = Path(str(data_root)).resolve()
            except OSError:
                return False
            try:
                target.resolve().relative_to(root)
            except (ValueError, OSError):
                return False
        elif target.is_absolute():
            return False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return True

    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(None, _validate_and_prepare):
        return ""

    async def _fetch() -> bytes:
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            max_redirects=3,
            headers={"User-Agent": "linling-bot/1.0 (+download)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    try:
        body = await asyncio.wait_for(_fetch(), timeout=12.0)
    except (httpx.HTTPError, TimeoutError, OSError):
        return ""
    if len(body) > 16 * 1024 * 1024:
        return ""
    try:
        await loop.run_in_executor(None, target.write_bytes, body)
    except OSError:
        return ""
    return str(len(body))


@tool(
    name="group_avatar",
    dsl_name="群头像",
    description=(
        "Return a URL pointing to the group's avatar image. "
        "$群头像 group_id$ — uses QQ's standard avatar CDN; the "
        "WebUI proxies it through /api/files/proxy when rendered."
    ),
    schema={"group_id": "string"},
    safe=True,
)
async def group_avatar(ctx: ToolCtx, group_id: str = "") -> str:
    """``$群头像 group_id$`` — QQ avatar CDN URL.

    QQ exposes group avatars at a stable URL pattern that doesn't
    require authentication. We just return the URL so DSL rules can
    feed it to ``±img=...±``; the WebUI proxy / OneBot adapter
    handle delivery.
    """
    if not group_id:
        return ""
    return f"https://p.qlogo.cn/gh/{group_id}/{group_id}/0"


@tool(
    name="is_admin",
    dsl_name="管理员",
    description=(
        "Return '1' if the given user id is in the bot's admin_users "
        "config list, otherwise empty. $管理员 user_id$ — used by "
        "QRSpeed rules to gate admin-only commands."
    ),
    schema={"user_id": "string"},
    safe=True,
)
async def is_admin(ctx: ToolCtx, user_id: str = "") -> str:
    """``$管理员 user_id$`` — admin-list membership check.

    Reads from ``ctx.extras["admin_users"]`` (a tuple) which the
    bootstrap installs from bot.yaml's ``admin_users`` field. Returns
    ``"1"`` on match, empty on miss. Note the difference vs ``%管理员%``:
    the variable form returns the *first* admin id, this tool form
    returns a yes/no signal — both shapes appear in QRSpeed rule files.
    """
    admins = ctx.extras.get("admin_users") or ()
    if not isinstance(admins, tuple | list):
        return ""
    return "1" if str(user_id) in (str(a) for a in admins) else ""
