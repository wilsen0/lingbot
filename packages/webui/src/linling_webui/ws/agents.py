"""`/ws/agents/:name/stream` — streaming agent invocation.

Wire protocol (JSON):

client → server:
    {"t": "input",  "content": "..."}
    {"t": "cancel"}
    {"t": "ping"}

server → client:
    {"t": "hello",  "agent": "...", "server_time": 169...}
    {"t": "delta",  "text": "..."}       # streamed chunk of assistant reply
    {"t": "tool_call",   "id": "...", "name": "...", "args": {...}}
    {"t": "tool_result", "id": "...", "result": "..."}
    {"t": "done",   "tool_calls_made": N, "total_tokens": M}
    {"t": "error",  "msg": "..."}
    {"t": "ping"}

Until the :class:`AgentRuntime` grows a proper streaming ``invoke_stream``,
we emit the completed reply as a single ``delta`` then ``done``. The UI
contract already handles the streaming shape, so a later switch is
backwards-compatible.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from linling_webui.audit_reader import AuditReader
from linling_webui.auth import decode_token

router = APIRouter()


async def _noop_capture_sink(action: Any) -> None:
    """No-op action sink for direct WS agent invocation.

    The streaming ``_dispatch`` path invokes the runtime without an IM
    adapter sink (this surface is the browser, not QQ).  We pass this
    no-op so ``send_reply`` awaits cleanly; the outbound text is
    recovered from ``result.sent_texts``.
    """
    return None


async def _authorize(ws: WebSocket, token: str | None) -> dict[str, Any] | None:
    """Validate the access token, closing the socket if not authorised."""
    config = ws.app.state.config
    claims = decode_token(token or "", secret=config.jwt_secret, algorithm=config.jwt_algorithm)
    if claims is None or claims.get("typ") != "access":
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return None
    return claims


async def _dispatch(ws: WebSocket, runtime: Any, user_input: str) -> None:
    """Run the agent once and stream back a pseudo-delta.

    :class:`AgentRuntime.invoke` returns the full result today. When
    :pep:`Task 17.2` lands a real streaming loop, replace the single
    delta with incremental emits — the ``done`` terminator is already
    in place.
    """
    try:
        result = await runtime.invoke(user_input, action_sink=_noop_capture_sink)
        # With tool-based sending the agent's actual words live in
        # ``result.sent_texts`` (what ``send_reply`` emitted).  Fall
        # back to content / finish_turn_summary for the legacy
        # direct-runtime path that never adopted tool-based sending.
        sent_texts = getattr(result, "sent_texts", None) or []
        if sent_texts:
            visible_text = "\n".join(sent_texts)
        else:
            visible_text = result.content or getattr(result, "finish_turn_summary", "") or ""
        if visible_text:
            await ws.send_json({"t": "delta", "text": visible_text})
        done_payload: dict[str, Any] = {
            "t": "done",
            "tool_calls_made": result.tool_calls_made,
            "total_tokens": result.total_tokens,
        }
        summary = getattr(result, "finish_turn_summary", None)
        if summary is not None:
            done_payload["finish_turn_summary"] = summary
        await ws.send_json(done_payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await ws.send_json({"t": "error", "msg": str(exc)})


async def _dispatch_via_chat(
    ws: WebSocket,
    dispatcher: Any,
    user_input: str,
    user_id: str,
    scope_id: str | None,
) -> None:
    """Drive the WebUI chat dispatcher and stream the result.

    Used when ``attach_bot_to_webui`` registered a per-agent web
    dispatcher — that path runs the bot's DSL classifier first and
    only falls back to the LLM for free-form chat. We surface the
    same wire frames (``delta`` / ``done`` / ``error``) so the
    client doesn't need to know which path served the message.

    ``scope_id`` lets the client pin a specific group id to test
    rules in (defaults to a synthetic DM scope ``%群号%==0`` when
    omitted).

    The ``done`` frame additionally carries a structured ``segments``
    list (text + image entries, in emit order) so the frontend can
    render mixed text-and-image chat bubbles — QRDic rules emit lots
    of those (``±img=...±`` interleaved with text lines) and a
    plain-text concatenation would lose the pictures entirely.
    """
    try:
        reply = await dispatcher(user_input, user_id, scope_id)
        await ws.send_json({"t": "delta", "text": reply.content})
        await ws.send_json(
            {
                "t": "done",
                "tool_calls_made": reply.tool_calls_made,
                "total_tokens": reply.total_tokens,
                "source": reply.source,
                "segments": [
                    {
                        "kind": s.kind,
                        "text": s.text,
                        "url": s.url,
                        "alt": s.alt,
                        "delay_before_s": s.delay_before_s,
                    }
                    for s in reply.segments
                ],
            }
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await ws.send_json({"t": "error", "msg": str(exc)})
        # Re-raise so the task surfaces as failed to ``_record_chat_audit``.
        # Without this, the except-and-swallow path makes ``task.exception()``
        # return None and audit lies "ok" for what was actually a hard error
        # (e.g. provider 4xx). We still send the ``error`` frame above so
        # the WS client UX is unchanged.
        raise


def _record_chat_audit(
    state: Any, *, sub: str, agent_name: str, input_len: int, task: asyncio.Task[None]
) -> None:
    """Append an audit row reflecting the outcome of a chat dispatch task.

    Called as a ``Task.add_done_callback`` so cancel / exception / success
    all flow through the same recording. Best-effort: a failure to append
    must not take down the socket.
    """
    try:
        if task.cancelled() or task.exception() is not None:
            outcome = "err"
        else:
            outcome = "ok"
    except asyncio.CancelledError:
        outcome = "err"
    if state.audit is None:
        state.audit = AuditReader()
    with contextlib.suppress(Exception):
        state.audit.append(
            bot_id="webui",
            user_id=sub,
            scope_id=f"agents/{agent_name}",
            kind="agent_chat_stream",
            outcome=outcome,
            payload={"input_len": input_len},
        )


@router.websocket("/ws/agents/{name}/stream")
async def agent_stream(ws: WebSocket, name: str, token: str = Query(default="")) -> None:
    await ws.accept()
    claims = await _authorize(ws, token)
    if claims is None:
        return

    state = ws.app.state.runtime
    registry = state.agent_registry
    runtime = None if registry is None else registry.get(name)
    # Prefer a web chat dispatcher (DSL-first; LLM fallback) when one
    # was wired by ``attach_bot_to_webui``. Falling back to ``runtime``
    # directly preserves the legacy path used by tests that build a
    # WebUI without a real bot.
    web_dispatcher = state.chat_dispatchers.get(name)
    if runtime is None and web_dispatcher is None:
        await ws.send_json({"t": "error", "msg": f"unknown agent '{name}'"})
        await ws.close(code=status.WS_1003_UNSUPPORTED_DATA)
        return

    sub = str(claims.get("sub", "unknown"))
    await ws.send_json({"t": "hello", "server_time": int(time.time()), "agent": name})

    current_task: asyncio.Task[None] | None = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("t")
            if kind == "ping":
                await ws.send_json({"t": "ping"})
                continue
            if kind == "cancel":
                if current_task is not None and not current_task.done():
                    current_task.cancel()
                continue
            if kind == "input":
                content = str(msg.get("content", "")).strip()
                if not content:
                    continue
                if current_task is not None and not current_task.done():
                    await ws.send_json({"t": "error", "msg": "previous invocation still running"})
                    continue
                # Optional per-frame scope override — a future Chat.vue
                # group picker can stuff a string here. Empty / missing
                # → dispatcher falls back to its synthetic DM scope.
                raw_scope = msg.get("scope_id")
                scope_id = str(raw_scope).strip() if isinstance(raw_scope, str) else None
                if not scope_id:
                    scope_id = None
                if web_dispatcher is not None:
                    current_task = asyncio.create_task(
                        _dispatch_via_chat(ws, web_dispatcher, content, sub, scope_id)
                    )
                else:
                    current_task = asyncio.create_task(_dispatch(ws, runtime, content))
                # Audit recording piggybacks on task completion so the
                # trace line lands regardless of success / cancel / error.
                input_len = len(content)

                def _on_complete(
                    t: asyncio.Task[None],
                    _sub: str = sub,
                    _name: str = name,
                    _len: int = input_len,
                ) -> None:
                    _record_chat_audit(state, sub=_sub, agent_name=_name, input_len=_len, task=t)

                current_task.add_done_callback(_on_complete)
    except WebSocketDisconnect:
        if current_task is not None and not current_task.done():
            current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await current_task
