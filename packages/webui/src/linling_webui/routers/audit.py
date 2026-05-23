"""`/api/audit` — search and export audit log entries."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from linling_webui.audit_reader import AuditReader, AuditRow
from linling_webui.deps import Caller, get_state, require_auth, verify_bot_visibility
from linling_webui.schemas import AuditEntry
from linling_webui.state import WebUIState

router = APIRouter(tags=["audit"])


def _rows_to_entries(rows: list[AuditRow]) -> list[AuditEntry]:
    out: list[AuditEntry] = []
    for r in rows:
        out.append(
            AuditEntry(
                id=r.id,
                time=datetime.fromtimestamp(r.time, UTC).isoformat(),
                bot_id=r.bot_id,
                user_id=r.user_id,
                scope_id=r.scope_id,
                kind=r.kind,
                outcome=r.outcome,
                latency_ms=r.latency_ms,
                payload=r.payload,
            )
        )
    return out


def _ensure_audit(state: WebUIState) -> AuditReader:
    if state.audit is None:
        state.audit = AuditReader()
    audit: AuditReader = state.audit
    return audit


def _resolve_bot_ids(caller: Caller, requested: str | None) -> list[str] | None:
    """Apply jwt.bots visibility on top of an explicit ``?bot_id=`` filter.

    * ``requested`` set + caller restricted: must be one of caller.bots,
      otherwise 404 (treat as nonexistent for privacy).
    * ``requested`` set + caller superadmin: pass through.
    * ``requested`` unset + caller restricted: search the visible set.
    * ``requested`` unset + caller superadmin: ``None`` (search all).

    Centralised here so both ``search`` and ``export_csv`` use the
    exact same rules — a bug in one mustn't bypass the other.
    """
    if requested is not None:
        verify_bot_visibility(caller, requested)
        return [requested]
    if caller.bots is not None:
        return caller.bots
    return None


@router.get("", response_model=list[AuditEntry])
async def search(
    bot_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    since: float | None = Query(default=None),
    until: float | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> list[AuditEntry]:
    audit = _ensure_audit(state)
    bot_ids = _resolve_bot_ids(caller, bot_id)
    rows = audit.search(
        bot_ids=bot_ids,
        user_id=user_id,
        kind=kind,
        outcome=outcome,
        since=since,
        until=until,
        q=q,
        limit=limit,
    )
    return _rows_to_entries(rows)


@router.get(
    ".csv",
    responses={
        200: {
            "description": "CSV export of audit rows",
            "content": {"text/csv": {"schema": {"type": "string"}}},
        }
    },
)
async def export_csv(
    bot_id: str | None = Query(default=None),
    limit: int = Query(default=10000, ge=1, le=10000),
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> StreamingResponse:
    audit = _ensure_audit(state)
    bot_ids = _resolve_bot_ids(caller, bot_id)
    rows = audit.search(bot_ids=bot_ids, limit=limit)

    def _gen() -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "time", "bot_id", "user_id", "scope_id", "kind", "outcome", "latency_ms"])
        for r in rows:
            w.writerow(
                [
                    r.id,
                    datetime.fromtimestamp(r.time, UTC).isoformat(),
                    r.bot_id,
                    r.user_id,
                    r.scope_id,
                    r.kind,
                    r.outcome,
                    "" if r.latency_ms is None else r.latency_ms,
                ]
            )
        return buf.getvalue().encode("utf-8")

    return StreamingResponse(
        iter([_gen()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
    )
