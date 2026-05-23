"""`/api/kv*` endpoints — browse, edit, rank KV data."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from linling_core.storage.kv import KVStore, RankOrder

from linling_webui.audit_reader import AuditReader
from linling_webui.config import WebUIConfig
from linling_webui.deps import (
    Caller,
    get_config,
    get_state,
    require_auth,
    require_role,
    verify_bot_visibility,
)
from linling_webui.schemas import (
    KvNamespace,
    KvPage,
    KvRankResponse,
    KvRankRow,
    KvRow,
    KvWriteRequest,
)
from linling_webui.state import WebUIState

router = APIRouter(tags=["kv"])


def _record_audit(
    state: WebUIState,
    caller: Caller,
    bot_id: str,
    scope: str,
    file: str,
    key: str,
    *,
    kind: str,
    outcome: str = "ok",
) -> None:
    """Append a best-effort audit row for a KV write/delete."""
    if state.audit is None:
        state.audit = AuditReader()
    state.audit.append(
        bot_id=bot_id,
        user_id=caller.username,
        scope_id=f"kv/{scope}/{file}/{key}",
        kind=kind,
        outcome=outcome,
        payload={"scope": scope, "file": file, "key": key},
    )


def _default_bot(caller: Caller, state: WebUIState) -> str:
    """Resolve the default bot when the caller omits ?bot_id=.

    Prefers the first visible bot with a KV store.
    """
    for b in state.visible_bots(caller.bots):
        if b.id in state.kv_stores:
            return b.id
    raise HTTPException(status.HTTP_404_NOT_FOUND, "no bot with KV store is visible")


def _get_store(bot_id: str, caller: Caller, state: WebUIState) -> KVStore:
    verify_bot_visibility(caller, bot_id)
    store = state.kv_stores.get(bot_id)
    if store is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no KV store for bot")
    return store


@router.get("", response_model=list[KvNamespace])
async def list_namespaces(
    bot_id: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> list[KvNamespace]:
    bid = bot_id or _default_bot(caller, state)
    store = _get_store(bid, caller, state)
    out: list[KvNamespace] = []
    scopes_to_list: list[str]
    if scope is None:
        # Enumerate every scope that exists in the store. SqliteKVStore
        # implements ``scopes()`` natively; protocol-only stores may not.
        scopes_fn = getattr(store, "scopes", None)
        if callable(scopes_fn):
            try:
                scopes_to_list = await scopes_fn()
            except Exception:
                scopes_to_list = []
        else:
            scopes_to_list = []
    else:
        scopes_to_list = [scope]
    for s in scopes_to_list:
        files = await store.files(s)
        for f in files:
            keys = await store.keys(s, f)
            out.append(KvNamespace(scope=s, file=f, count=len(keys)))
    return out


@router.get("/{scope}/{file}", response_model=KvPage)
async def list_keys(
    scope: str,
    file: str,
    bot_id: str | None = Query(default=None),
    prefix: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> KvPage:
    bid = bot_id or _default_bot(caller, state)
    store = _get_store(bid, caller, state)
    keys = await store.keys(scope, file)
    if prefix:
        keys = [k for k in keys if k.startswith(prefix)]
    keys.sort()

    # cursor-based pagination (cursor = last-seen key)
    start_idx = 0
    if cursor:
        for i, k in enumerate(keys):
            if k > cursor:
                start_idx = i
                break
        else:
            start_idx = len(keys)

    slice_ = keys[start_idx : start_idx + limit]
    rows: list[KvRow] = []
    for k in slice_:
        v = await store.read(scope, file, k)
        if v is None:
            continue
        rows.append(KvRow(bot_id=bid, scope=scope, file=file, key=k, value=v, updated_at=0))

    next_cursor = slice_[-1] if len(slice_) == limit else None
    return KvPage(items=rows, next_cursor=next_cursor)


@router.get("/{scope}/{file}/rank", response_model=KvRankResponse)
async def rank(
    scope: str,
    file: str,
    bot_id: str | None = Query(default=None),
    order: str = Query(default="desc"),
    top: int = Query(default=10, ge=1, le=500),
    sep: str = Query(default="\n"),
    fmt: str = Query(default="[序号]. [键] [值]"),
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> KvRankResponse:
    bid = bot_id or _default_bot(caller, state)
    store = _get_store(bid, caller, state)
    try:
        order_enum = RankOrder.parse(order)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    rows = await store.rank_rows(scope, file, order=order_enum, top=top)
    formatted = await store.rank(scope, file, order=order_enum, top=top, sep=sep, fmt=fmt)
    return KvRankResponse(
        rows=[KvRankRow(rank=r.rank, key=r.key, value=r.value, numeric=r.numeric) for r in rows],
        formatted=formatted,
    )


@router.get("/{scope}/{file}/{key}", response_model=KvRow)
async def read_key(
    scope: str,
    file: str,
    key: str,
    response: Response,
    bot_id: str | None = Query(default=None),
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> KvRow:
    bid = bot_id or _default_bot(caller, state)
    store = _get_store(bid, caller, state)
    value = await store.read(scope, file, key)
    if value is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "key not found")
    # The KV interface doesn't expose updated_at per-read; we approximate.
    row = KvRow(
        bot_id=bid, scope=scope, file=file, key=key, value=value, updated_at=int(time.time())
    )
    # ETag uses hash-of-current-value so conditional PATCH is symmetric with
    # write_key. (Using updated_at as ETag would fail because read_key
    # regenerates it on every call.)
    response.headers["ETag"] = f'"{hash(value)}"'
    return row


def _rate_limit_write(request_ip: str, app_state: Any, config: WebUIConfig) -> None:
    ok = app_state.rate_limiter.check("write", request_ip, config.write_rate_per_minute, 60.0)
    if not ok:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many writes")


@router.patch("/{scope}/{file}/{key}", response_model=KvRow)
async def write_key(
    scope: str,
    file: str,
    key: str,
    body: KvWriteRequest,
    request: Request,
    response: Response,
    bot_id: str | None = Query(default=None),
    if_match: str | None = Header(default=None, alias="If-Match"),
    caller: Caller = Depends(require_role("bot_admin")),
    state: WebUIState = Depends(get_state),
    config: WebUIConfig = Depends(get_config),
) -> KvRow:
    bid = bot_id or _default_bot(caller, state)
    store = _get_store(bid, caller, state)

    # Per-user write rate-limit (design §15: 60 writes/min/user).
    limiter = request.app.state.rate_limiter
    if not limiter.check("kv_write", caller.username, config.write_rate_per_minute, 60.0):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many writes")

    # If-Match optimistic concurrency: compare against current value hash,
    # which is the same formula read_key / write_key expose via the ETag
    # header. Works as a pure in-memory check without extra metadata.
    if if_match is not None:
        current = await store.read(scope, file, key)
        current_etag = f'"{hash(current)}"' if current is not None else '"null"'
        if current_etag != if_match:
            raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, "etag mismatch")

    await store.write(scope, file, key, body.value)
    row = KvRow(
        bot_id=bid,
        scope=scope,
        file=file,
        key=key,
        value=body.value,
        updated_at=int(time.time()),
    )
    # Audit the write so the "命格" tab and /ws/rules/hits see it.
    _record_audit(state, caller, bid, scope, file, key, kind="kv_write")
    response.headers["ETag"] = f'"{hash(body.value)}"'
    return row


@router.delete(
    "/{scope}/{file}/{key}",
    status_code=204,
    responses={204: {"description": "Key deleted"}},
)
async def delete_key(
    scope: str,
    file: str,
    key: str,
    bot_id: str | None = Query(default=None),
    caller: Caller = Depends(require_role("bot_admin")),
    state: WebUIState = Depends(get_state),
) -> None:
    bid = bot_id or _default_bot(caller, state)
    store = _get_store(bid, caller, state)
    removed = await store.delete(scope, file, key)
    if removed == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "key not found")
    _record_audit(state, caller, bid, scope, file, key, kind="kv_delete")
