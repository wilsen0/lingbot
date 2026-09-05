"""Shared rendering of non-text message segments into LLM-visible markers.

Both the DM chat dispatcher and the group-batch dispatcher need to project
image / face / voice / … segments into short text markers (``[图片]`` /
``[表情]`` / …) so a text-based or vision LLM sees a stable placeholder
instead of raw segment data. This module owns that mapping so the two
paths can't drift.
"""

from __future__ import annotations

from linling_core.segments import (
    CardSegment,
    FaceSegment,
    FileSegment,
    ImageSegment,
    Segment,
    TextSegment,
    VideoSegment,
    VoiceSegment,
    XmlSegment,
)

# Non-text segments → short LLM-visible markers, in segment order. We render
# these into the buffered message text so a pure-sticker / image / voice
# message no longer appears as empty content (which made the LLM ask "what did
# you send?"). ``AtSegment`` / ``ReplySegment`` / ``PokeSegment`` are modeled
# separately (mentions_me / at_targets / reply_to_me) and deliberately not
# inlined here — they must not show up as stray text in the visible text.
NON_TEXT_MARKERS: tuple[tuple[type[Segment], str], ...] = (
    (ImageSegment, "[图片]"),
    (FaceSegment, "[表情]"),  # QQ basic face + mface/bface 商城表情包/贴纸
    (VoiceSegment, "[语音]"),
    (VideoSegment, "[视频]"),
    (FileSegment, "[文件]"),
    (CardSegment, "[卡片]"),
    (XmlSegment, "[卡片]"),
)


def llm_visible_text(segments: list[Segment]) -> str:
    """Render segments as the text the LLM should see.

    ``TextSegment``s pass through verbatim; each known non-text segment becomes
    its marker (``[图片]`` / ``[表情]`` / …). Unknown segment kinds contribute
    nothing. Leading/trailing whitespace is stripped, so a pure non-text message
    yields exactly its marker (e.g. ``"[表情]"``) rather than an empty string.
    """
    parts: list[str] = []
    for seg in segments:
        if isinstance(seg, TextSegment):
            parts.append(seg.text)
            continue
        for kind, marker in NON_TEXT_MARKERS:
            if isinstance(seg, kind):
                parts.append(marker)
                break
    return "".join(parts).strip()


def has_non_text_segment(segments: list[Segment]) -> bool:
    """True iff ``segments`` carry any non-text content we render as a marker."""
    for seg in segments:
        if isinstance(seg, TextSegment):
            continue
        if any(isinstance(seg, kind) for kind, _ in NON_TEXT_MARKERS):
            return True
    return False
