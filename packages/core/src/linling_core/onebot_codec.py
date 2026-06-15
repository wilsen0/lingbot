"""Codec helpers for OneBot v11 message arrays.

Placed in core so that any adapter (not just the official OneBot one)
that speaks a compatible dialect can reuse the mapping. The transform is
lossy on purpose — platform-specific fields we do not model are dropped
to ``extras`` rather than the top-level segment schema.

Reference: https://github.com/botuniverse/onebot-11
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from linling_core.segments import (
    AtSegment,
    CardSegment,
    FaceSegment,
    FileSegment,
    ImageSegment,
    PokeSegment,
    ReplySegment,
    Segment,
    TextSegment,
    VideoSegment,
    VoiceSegment,
    XmlSegment,
)


def from_onebot_msg(message: list[dict[str, Any]]) -> list[Segment]:
    """Translate a OneBot v11 ``message`` array into our ``Segment`` list."""
    out: list[Segment] = []
    for item in message:
        seg = _from_one(item)
        if seg is not None:
            out.append(seg)
    return out


def to_onebot_msg(segments: list[Segment]) -> list[dict[str, Any]]:
    """Translate our ``Segment`` list into a OneBot v11 ``message`` array."""
    out: list[dict[str, Any]] = []
    for s in segments:
        item = _to_one(s)
        if item is not None:
            out.append(item)
    return out


# --- decode --------------------------------------------------------------


def _dec_text(d: dict[str, Any]) -> Segment:
    return TextSegment(text=str(d.get("text", "")))


def _dec_image(d: dict[str, Any]) -> Segment:
    return ImageSegment(
        url=_opt_str(d, "url") or _opt_str(d, "file"),
        alt=_opt_str(d, "alt"),
        extras={k: v for k, v in d.items() if k not in ("url", "file", "alt")},
    )


def _dec_at(d: dict[str, Any]) -> Segment:
    return AtSegment(user_id=str(d.get("qq", "all")))


def _dec_reply(d: dict[str, Any]) -> Segment:
    return ReplySegment(message_id=str(d.get("id", "")))


def _dec_face(d: dict[str, Any]) -> Segment:
    return FaceSegment(face_id=str(d.get("id", "")))


def _dec_poke(d: dict[str, Any]) -> Segment:
    return PokeSegment(target_user_id=_opt_str(d, "qq"))


def _dec_file(d: dict[str, Any]) -> Segment:
    return FileSegment(
        url=_opt_str(d, "url"),
        name=_opt_str(d, "name"),
        size=_opt_int(d, "size"),
    )


def _dec_record(d: dict[str, Any]) -> Segment:
    return VoiceSegment(
        url=_opt_str(d, "url") or _opt_str(d, "file"),
        duration_s=_opt_int(d, "duration"),
    )


def _dec_video(d: dict[str, Any]) -> Segment:
    return VideoSegment(url=_opt_str(d, "url") or _opt_str(d, "file"))


def _dec_xml(d: dict[str, Any]) -> Segment:
    return XmlSegment(xml=str(d.get("data", "")))


def _dec_json(d: dict[str, Any]) -> Segment:
    return CardSegment(payload=str(d.get("data", "")))


def _dec_mface(d: dict[str, Any]) -> Segment:
    """QQ market face (商城表情包 / 贴纸).

    Modeled as a :class:`FaceSegment` — a sticker is conceptually a face —
    with the platform-specific fields (``summary``/``url``/``emoji_package_id``
    etc.) preserved under ``extras``. Previously this type had no decoder and
    was dropped entirely, which made a pure-sticker message decode to an empty
    segment list and appear as genuinely-empty text downstream. Keeping it as
    a real segment lets the agent layer tell "user sent a sticker" apart from
    "user sent nothing".
    """
    return FaceSegment(
        face_id=str(d.get("emoji_id", "")),
        extras={k: v for k, v in d.items() if k != "emoji_id"},
    )


def _dec_bface(d: dict[str, Any]) -> Segment:
    """QQ original/creative face (原创表情). Same rationale as :func:`_dec_mface`."""
    return FaceSegment(face_id=str(d.get("id", "")), extras=dict(d))


_DECODERS: dict[str, Callable[[dict[str, Any]], Segment]] = {
    "text": _dec_text,
    "image": _dec_image,
    "at": _dec_at,
    "reply": _dec_reply,
    "face": _dec_face,
    "poke": _dec_poke,
    "file": _dec_file,
    "record": _dec_record,
    "video": _dec_video,
    "xml": _dec_xml,
    "json": _dec_json,
    "mface": _dec_mface,
    "bface": _dec_bface,
}


def _from_one(item: dict[str, Any]) -> Segment | None:
    kind = item.get("type")
    data: dict[str, Any] = item.get("data") or {}
    dec = _DECODERS.get(str(kind))
    if dec is None:
        # Unknown kinds are dropped for forward compatibility with various
        # OneBot v11 implementations.
        return None
    return dec(data)


# --- encode --------------------------------------------------------------


def _enc_text(s: TextSegment) -> dict[str, Any]:
    return {"type": "text", "data": {"text": s.text}}


def _enc_image(s: ImageSegment) -> dict[str, Any]:
    d: dict[str, Any] = {}
    if s.url:
        d["file"] = s.url
    elif s.path:
        d["file"] = f"file://{s.path}"
    elif s.b64:
        d["file"] = f"base64://{s.b64}"
    if s.alt:
        d["alt"] = s.alt
    return {"type": "image", "data": d}


def _enc_at(s: AtSegment) -> dict[str, Any]:
    return {"type": "at", "data": {"qq": s.user_id}}


def _enc_reply(s: ReplySegment) -> dict[str, Any]:
    return {"type": "reply", "data": {"id": s.message_id}}


def _enc_face(s: FaceSegment) -> dict[str, Any]:
    return {"type": "face", "data": {"id": s.face_id}}


def _enc_poke(s: PokeSegment) -> dict[str, Any]:
    data = {"qq": s.target_user_id} if s.target_user_id else {}
    return {"type": "poke", "data": data}


def _enc_file(s: FileSegment) -> dict[str, Any]:
    return {"type": "file", "data": _nonempty({"url": s.url, "name": s.name})}


def _enc_voice(s: VoiceSegment) -> dict[str, Any]:
    return {"type": "record", "data": _nonempty({"file": s.url or s.path})}


def _enc_video(s: VideoSegment) -> dict[str, Any]:
    return {"type": "video", "data": _nonempty({"file": s.url or s.path})}


def _enc_xml(s: XmlSegment) -> dict[str, Any]:
    return {"type": "xml", "data": {"data": s.xml}}


def _enc_card(s: CardSegment) -> dict[str, Any]:
    return {"type": "json", "data": {"data": s.payload}}


_ENCODERS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "text": _enc_text,
    "image": _enc_image,
    "at": _enc_at,
    "reply": _enc_reply,
    "face": _enc_face,
    "poke": _enc_poke,
    "file": _enc_file,
    "voice": _enc_voice,
    "video": _enc_video,
    "xml": _enc_xml,
    "card": _enc_card,
}


def _to_one(seg: Segment) -> dict[str, Any] | None:
    enc = _ENCODERS.get(seg.kind)
    return None if enc is None else enc(seg)


# --- utilities -----------------------------------------------------------


def _opt_str(d: dict[str, Any], k: str) -> str | None:
    v = d.get(k)
    return None if v is None else str(v)


def _opt_int(d: dict[str, Any], k: str) -> int | None:
    v = d.get(k)
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _nonempty(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != ""}
