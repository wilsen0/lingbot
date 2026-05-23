"""Pydantic response / request models exposed by the REST API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---- Auth ----------------------------------------------------------


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh: str


class TokenResponse(BaseModel):
    access: str
    refresh: str
    access_expires_at: int
    refresh_expires_at: int


class ProfileResponse(BaseModel):
    username: str
    role: Literal["superadmin", "bot_admin", "readonly"]
    bots: list[str]


# ---- Health --------------------------------------------------------


class BotStatus(BaseModel):
    id: str
    platform: str = "unknown"
    name: str = ""
    online: bool = False
    last_event_at: float | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    time: str
    bots: list[BotStatus]


# ---- Events --------------------------------------------------------


class EventEnvelope(BaseModel):
    """Slimmed event view sent to the browser.

    Avoids returning the raw adapter payload (may contain secrets). The
    full pydantic Event is not exposed; adapters own `.raw`.
    """

    seq: int
    id: str
    platform: str
    bot_id: str
    scope: dict[str, Any]
    sender: dict[str, Any]
    time: str
    kind: str
    segments: list[dict[str, Any]]
    text: str


class EventPage(BaseModel):
    items: list[EventEnvelope]
    next_cursor: int | None = None


# ---- KV ------------------------------------------------------------


class KvNamespace(BaseModel):
    scope: str
    file: str
    count: int


class KvRow(BaseModel):
    bot_id: str
    scope: str
    file: str
    key: str
    value: str
    updated_at: int


class KvPage(BaseModel):
    items: list[KvRow]
    next_cursor: str | None = None


class KvWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class KvRankRow(BaseModel):
    rank: int
    key: str
    value: str
    numeric: float


class KvRankResponse(BaseModel):
    rows: list[KvRankRow]
    formatted: str


# ---- Rules / Agents / Audit placeholders ---------------------------


class RuleSummary(BaseModel):
    name: str
    trigger: str
    hits_today: int = 0
    avg_latency_ms: float = 0
    last_error: str | None = None


class AgentSummary(BaseModel):
    name: str
    provider: str
    model: str
    token_today: int = 0


class TriggerSuggestion(BaseModel):
    """One DSL trigger surfaced to the chat composer's inline-suggest panel.

    The frontend uses ``label`` for display, ``literal_prefix`` for the
    pre-fill on click (so triggers with regex placeholders like ``(.*)``
    don't paste a still-not-matchable shape into the input), and
    ``has_args`` to decide whether a click should auto-send or just
    park the cursor at the placeholder.
    """

    raw: str
    label: str
    has_args: bool = False
    literal_prefix: str = ""


class AuditEntry(BaseModel):
    id: str
    time: str
    bot_id: str
    user_id: str
    scope_id: str
    kind: str
    outcome: Literal["ok", "err"]
    latency_ms: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ---- Rule file editing ---------------------------------------------


class RuleFile(BaseModel):
    """Metadata entry for one ``.ling`` rule file."""

    path: str  # bot-relative, forward slashes
    size: int  # bytes on disk
    handler_count: int = 0  # handlers parsed from the file


class RuleFileContent(BaseModel):
    """Full content of one rule file."""

    path: str
    content: str


class RuleLintIssue(BaseModel):
    """One lint finding."""

    line: int
    col: int = 0
    code: str  # e.g. "L100"
    severity: Literal["error", "warning", "info"]
    message: str


class RuleLintResult(BaseModel):
    """Response from linting a snippet."""

    issues: list[RuleLintIssue] = Field(default_factory=list)
    handler_count: int = 0


class RuleFileSaveRequest(BaseModel):
    """Body for updating a rule file."""

    content: str
    reload: bool = True
    lint_first: bool = True


class RuleFileSaveResult(BaseModel):
    """Response from a save+reload cycle."""

    saved: bool
    issues: list[RuleLintIssue] = Field(default_factory=list)
    reloaded: bool = False
    handlers: int = 0
