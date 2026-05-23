"""Unified event and action models.

All platform adapters normalise inbound payloads into :class:`Event`
and expect outbound :class:`Action` instances. This decouples the DSL
interpreter, Agent runtime, and storage layers from any specific IM
protocol.

Fields intentionally mirror OneBot v11 semantics where practical
(``group_id``, ``user_id`` as strings for cross-platform safety) but the
model is platform-agnostic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from linling_core.segments import Segment, plain_text


class Scope(BaseModel):
    """Where a message lives / goes to.

    - ``kind='group'``: a group chat or channel. ``id`` is the group id.
    - ``kind='dm'``: a direct (private) chat. ``id`` is the peer user id.
    - ``kind='system'``: system-originated events (scheduler, notice).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["group", "dm", "system"]
    id: str
    platform: str  # mirrors Event.platform; convenient for routing
    # Optional sub-channel (e.g. Discord thread, Feishu topic).
    channel_id: str | None = None


class User(BaseModel):
    """A message sender or mention target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    platform: str
    display_name: str | None = None
    # Role within the current scope (group admin, owner, etc.).
    role: Literal["owner", "admin", "member", "bot", "unknown"] = "unknown"
    # Free-form tags an adapter may populate (e.g. "stranger", "friend").
    tags: tuple[str, ...] = ()


class Event(BaseModel):
    """An inbound event delivered by an adapter."""

    model_config = ConfigDict(extra="forbid")

    id: str  # platform message id (or synthetic id for non-message events)
    platform: str
    bot_id: str  # our own id on the platform
    scope: Scope
    sender: User
    time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["message", "notice", "request", "system"] = "message"
    segments: list[Segment] = Field(default_factory=list)
    # Raw payload from the platform; adapters only. DSL/Agent must not read.
    raw: dict[str, object] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """Concatenation of all text segments; useful for regex triggers."""
        return plain_text(self.segments)

    @property
    def is_group(self) -> bool:
        return self.scope.kind == "group"

    @property
    def is_dm(self) -> bool:
        return self.scope.kind == "dm"


ActionKind = Literal[
    "reply",  # reply to the current message (scope implied)
    "send",  # send a new message to an explicit scope
    "recall",  # recall / delete a message
    "mute",  # group-mute a user
    "unmute",
    "poke",  # poke a user (platforms that support it)
    "set_title",  # grant a group-specific title (QQ)
    "kick",
    "noop",  # explicit no-op for testing
]


class Action(BaseModel):
    """An outbound action instructing an adapter to do something."""

    model_config = ConfigDict(extra="forbid")

    kind: ActionKind
    target: Scope
    # For message-producing actions.
    segments: list[Segment] = Field(default_factory=list)
    # Generic options bag. Schema depends on ``kind`` and target platform.
    options: dict[str, object] = Field(default_factory=dict)

    def with_segments(self, segments: list[Segment]) -> Action:
        """Return a copy with replaced segments."""
        return self.model_copy(update={"segments": list(segments)})
