"""`/api/rules*` — rule summary + hits + file editor.

Until the main `linling` spec exposes a live rule registry here, we
derive a summary from recent audit rows (kind='handler_dispatch').

The ``/files*`` endpoints are served by a ``RuleFileController`` that
the hosting process installs via ``attach_bot_to_webui`` — see
``linling_cli.wire_webui``. Without one installed the endpoints return
503 so the SPA can hide the editor.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from linling_webui.deps import Caller, get_state, require_auth, require_bot_visibility, require_role
from linling_webui.schemas import (
    RuleFile,
    RuleFileContent,
    RuleFileSaveRequest,
    RuleFileSaveResult,
    RuleLintIssue,
    RuleLintResult,
    RuleSummary,
)
from linling_webui.state import WebUIState

if TYPE_CHECKING:
    from linling_webui.audit_reader import AuditRow

router = APIRouter(tags=["rules"])


@router.get("", response_model=list[RuleSummary])
async def list_rules(
    limit: int = Query(default=50, ge=1, le=500),
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> list[RuleSummary]:
    if state.audit is None:
        return []
    bot_ids = caller.bots if caller.bots is not None else None
    rows = state.audit.search(bot_ids=bot_ids, kind="handler_dispatch", limit=2000)
    by_name: dict[str, list[AuditRow]] = defaultdict(list)
    for r in rows:
        name = r.payload.get("handler") if isinstance(r.payload, dict) else None
        if not name:
            continue
        by_name[str(name)].append(r)
    out: list[RuleSummary] = []
    for name, rs in by_name.items():
        lats = [r.latency_ms for r in rs if r.latency_ms is not None]
        last_err: str | None = None
        for r in reversed(rs):
            if r.outcome == "err":
                last_err = str(r.payload.get("error", "error"))
                break
        out.append(
            RuleSummary(
                name=name,
                trigger=str(rs[-1].payload.get("trigger", "")),
                hits_today=len(rs),
                avg_latency_ms=float(mean(lats)) if lats else 0.0,
                last_error=last_err,
            )
        )
    out.sort(key=lambda s: s.hits_today, reverse=True)
    return out[:limit]


@router.get("/{name}/hits")
async def hits_for(
    name: str,
    limit: int = Query(default=50, ge=1, le=500),
    caller: Caller = Depends(require_auth),
    state: WebUIState = Depends(get_state),
) -> list[dict[str, Any]]:
    """Return recent hits for a given handler name.

    Time is emitted as ISO-8601 (UTC) to match ``/api/audit`` and
    ``/api/events``; the UI should not have to switch formatters per
    endpoint.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    if state.audit is None:
        return []
    bot_ids = caller.bots if caller.bots is not None else None
    rows = state.audit.search(bot_ids=bot_ids, kind="handler_dispatch", limit=1000)
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r.payload, dict):
            continue
        if str(r.payload.get("handler", "")) != name:
            continue
        out.append(
            {
                "id": r.id,
                "time": datetime.fromtimestamp(r.time, UTC).isoformat(),
                "bot_id": r.bot_id,
                "user_id": r.user_id,
                "scope_id": r.scope_id,
                "outcome": r.outcome,
                "latency_ms": r.latency_ms,
                "matched": r.payload.get("matched", {}),
                "event_id": r.payload.get("event_id"),
            }
        )
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# /api/rules/files — live file editor backing endpoints
# ---------------------------------------------------------------------------


def _require_controller(
    state: WebUIState,
    bot_id: str,
) -> object:
    ctrl = state.rule_files.get(bot_id)
    if ctrl is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "rule-file editing not configured for this bot",
        )
    return ctrl


@router.get("/files", response_model=list[RuleFile])
async def list_rule_files(
    bot_id: str = Query(..., description="Bot id the files belong to"),
    _b: str = Depends(require_bot_visibility),
    state: WebUIState = Depends(get_state),
) -> list[RuleFile]:
    ctrl = _require_controller(state, bot_id)
    return [
        RuleFile(path=f.path, size=f.size, handler_count=f.handler_count)
        for f in ctrl.list_files()  # type: ignore[attr-defined]
    ]


@router.get("/files/content", response_model=RuleFileContent)
async def read_rule_file(
    bot_id: str = Query(...),
    path: str = Query(..., description="Bot-relative file path"),
    _b: str = Depends(require_bot_visibility),
    state: WebUIState = Depends(get_state),
) -> RuleFileContent:
    ctrl = _require_controller(state, bot_id)
    try:
        content = ctrl.read(path)  # type: ignore[attr-defined]
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return RuleFileContent(path=path, content=content)


@router.post("/lint", response_model=RuleLintResult)
async def lint_rule_source(
    req: dict[str, Any],  # {"content": str}
    _caller: Caller = Depends(require_auth),
) -> RuleLintResult:
    """Lint a raw ``.ling`` snippet; does not touch disk.

    We accept the raw content (rather than a bot/path pair) so the
    editor can live-lint without each keystroke requiring a file save.
    No bot visibility check because no data crosses tenant boundaries.
    """
    content = req.get("content")
    if not isinstance(content, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing content")

    # Import lazily: keeps the WebUI's cold-start path free of the DSL
    # parser for deployments that only serve the dashboards.
    from linling_cli.rule_files import RuleFileController  # noqa: PLC0415

    issues, handlers = RuleFileController.lint(content)
    return RuleLintResult(
        issues=[
            RuleLintIssue(
                line=i.line,
                col=i.col,
                code=i.code,
                severity=i.severity,
                message=i.message,
            )
            for i in issues
        ],
        handler_count=handlers,
    )


@router.put("/files/content", response_model=RuleFileSaveResult)
async def save_rule_file(
    req: RuleFileSaveRequest,
    bot_id: str = Query(...),
    path: str = Query(...),
    _b: str = Depends(require_bot_visibility),
    _admin: Caller = Depends(require_role("bot_admin")),
    state: WebUIState = Depends(get_state),
) -> RuleFileSaveResult:
    """Save a ``.ling`` file. Requires ``bot_admin`` — saves can hot-reload
    the live router which would otherwise let any read-only viewer
    rewrite handler logic.
    """
    ctrl = _require_controller(state, bot_id)
    try:
        result = await ctrl.save(  # type: ignore[attr-defined]
            path,
            req.content,
            reload=req.reload,
            lint_first=req.lint_first,
        )
    except PermissionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return RuleFileSaveResult(
        saved=result.saved,
        issues=[
            RuleLintIssue(
                line=i.line,
                col=i.col,
                code=i.code,
                severity=i.severity,
                message=i.message,
            )
            for i in result.issues
        ],
        reloaded=result.reloaded,
        handlers=result.handlers,
    )
