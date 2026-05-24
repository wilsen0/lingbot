"""Message segment types shared by Event and Action.

A segment is the smallest addressable unit of a message. Messages in all
platforms are modelled as ``list[Segment]`` so that adapters can losslessly
map inbound/outbound payloads to a common shape while still allowing
platform-specific fields via ``extras``.

Design notes
------------

- Discriminated union on the ``kind`` field; pydantic v2 handles dispatch.
- Fields are immutable (``frozen=True``) so segments can be safely shared
  between the DSL interpreter, Agent runtime and outgoing actions.
- Unknown/platform-specific metadata lives under ``extras`` rather than
  polluting the public schema.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _SegmentBase(BaseModel):
    """Base class for all segments.

    ``extras`` holds opaque platform-specific fields that we do not model
    first-class (e.g. OneBot ``file_id``). Never inspect it in the core.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    extras: dict[str, object] = Field(default_factory=dict)


class TextSegment(_SegmentBase):
    kind: Literal["text"] = "text"
    text: str


class ImageSegment(_SegmentBase):
    """Image segment. Exactly one of ``url``/``path``/``b64`` must be set."""

    kind: Literal["image"] = "image"
    url: str | None = None
    path: str | None = None
    b64: str | None = None
    # summary/alt text for accessibility or downgrade to text on platforms
    # that cannot render images.
    alt: str | None = None


class AtSegment(_SegmentBase):
    """@-mention targeting a specific user id (or ``"all"``)."""

    kind: Literal["at"] = "at"
    user_id: str


class ReplySegment(_SegmentBase):
    """Indicates the outgoing message is a reply to ``message_id``."""

    kind: Literal["reply"] = "reply"
    message_id: str


class FaceSegment(_SegmentBase):
    """Platform emoji/face. ``face_id`` is platform-specific."""

    kind: Literal["face"] = "face"
    face_id: str


class PokeSegment(_SegmentBase):
    """A 戳一戳/poke event or action. ``target`` required when outgoing."""

    kind: Literal["poke"] = "poke"
    target_user_id: str | None = None


class FileSegment(_SegmentBase):
    kind: Literal["file"] = "file"
    url: str | None = None
    path: str | None = None
    name: str | None = None
    size: int | None = None


class VoiceSegment(_SegmentBase):
    kind: Literal["voice"] = "voice"
    url: str | None = None
    path: str | None = None
    duration_s: int | None = None


class VideoSegment(_SegmentBase):
    kind: Literal["video"] = "video"
    url: str | None = None
    path: str | None = None


class CardSegment(_SegmentBase):
    """Rich card (e.g. QQ XML card, Feishu interactive card)."""

    kind: Literal["card"] = "card"
    payload: str  # serialised payload; schema varies by platform


class XmlSegment(_SegmentBase):
    """Raw XML, used by QQ for some legacy cards."""

    kind: Literal["xml"] = "xml"
    xml: str


Segment = Annotated[
    TextSegment
    | ImageSegment
    | AtSegment
    | ReplySegment
    | FaceSegment
    | PokeSegment
    | FileSegment
    | VoiceSegment
    | VideoSegment
    | CardSegment
    | XmlSegment,
    Field(discriminator="kind"),
]
"""Discriminated union of all message segments."""


def text(content: str) -> TextSegment:
    """Shorthand for ``TextSegment(text=content)``."""
    return TextSegment(text=content)


def image(
    *,
    url: str | None = None,
    path: str | None = None,
    b64: str | None = None,
    alt: str | None = None,
) -> ImageSegment:
    """Shorthand constructor; enforces mutual exclusion at runtime."""
    provided = sum(v is not None for v in (url, path, b64))
    if provided != 1:
        raise ValueError("ImageSegment requires exactly one of url/path/b64")
    return ImageSegment(url=url, path=path, b64=b64, alt=alt)


def at(user_id: str) -> AtSegment:
    return AtSegment(user_id=user_id)


def reply(message_id: str) -> ReplySegment:
    return ReplySegment(message_id=message_id)


def plain_text(segments: list[Segment]) -> str:
    """Concatenate all ``TextSegment``s; useful for regex matching."""
    return "".join(s.text for s in segments if isinstance(s, TextSegment))


def match_text(segments: list[Segment]) -> str:
    """Build a *trigger-matching* view of ``segments``.

    Like :func:`plain_text` but also re-projects ``AtSegment`` mentions
    back into the string as ``@<user_id>`` tokens, in segment order.
    This is what classifier-style regex triggers want: a QQ user typing
    ``赠送大飞龙@小苏苏`` arrives as
    ``[TextSegment("赠送大飞龙"), AtSegment(user_id="…")]`` over OneBot,
    with the literal ``@<id>`` no longer in any text segment. Triggers
    such as ``赠送大飞龙@.*`` (and ~60 others in the migrated QRDic
    rule set) author the ``@`` literally and rely on it being there;
    without this re-projection they never match.

    LLM-visible callers (chat dispatch, group batching, audit / UI
    surfaces, the DSL's ``%参数-1%`` / ``%参数N%`` resolvers) keep
    using :func:`plain_text` so AT user-ids never leak into chat
    history, audit logs, or rendered UIs.
    """
    parts: list[str] = []
    for seg in segments:
        if isinstance(seg, TextSegment):
            parts.append(seg.text)
        elif isinstance(seg, AtSegment):
            parts.append(f"@{seg.user_id}")
    return "".join(parts)
