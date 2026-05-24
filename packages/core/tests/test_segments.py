from __future__ import annotations

import pytest
from linling_core import plain_text
from linling_core.segments import (
    AtSegment,
    ImageSegment,
    Segment,
    TextSegment,
    at,
    image,
    match_text,
    text,
)
from pydantic import TypeAdapter, ValidationError

SegmentAdapter = TypeAdapter(Segment)


def test_text_helper_builds_text_segment() -> None:
    seg = text("hello")
    assert isinstance(seg, TextSegment)
    assert seg.text == "hello"
    assert seg.kind == "text"


def test_segments_are_frozen() -> None:
    seg = text("immutable")
    with pytest.raises(ValidationError):
        seg.text = "mutated"  # type: ignore[misc]


def test_image_helper_requires_exactly_one_source() -> None:
    image(url="https://example.com/a.png")
    image(path="/tmp/a.png")
    image(b64="AAAA")

    with pytest.raises(ValueError, match="exactly one"):
        image()
    with pytest.raises(ValueError, match="exactly one"):
        image(url="x", path="y")


def test_discriminated_union_roundtrip() -> None:
    segs: list[Segment] = [
        text("a"),
        at("123"),
        image(url="https://x/y.png", alt="pic"),
    ]
    dumped = [SegmentAdapter.dump_python(s) for s in segs]
    loaded = [SegmentAdapter.validate_python(d) for d in dumped]
    assert [s.kind for s in loaded] == ["text", "at", "image"]
    assert isinstance(loaded[1], AtSegment)
    assert isinstance(loaded[2], ImageSegment)


def test_discriminated_union_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        SegmentAdapter.validate_python({"kind": "wat", "text": "hi"})


def test_plain_text_concatenates_only_text_segments() -> None:
    segs: list[Segment] = [text("hello "), at("99"), text("world")]
    assert plain_text(segs) == "hello world"


def test_extras_bag_allows_opaque_payload() -> None:
    seg = ImageSegment(url="https://x/y.png", extras={"file_size": 1024})
    assert seg.extras["file_size"] == 1024


def test_match_text_reprojects_at_user_ids() -> None:
    """``match_text`` re-projects AT mentions as ``@<user_id>`` for triggers."""
    segs: list[Segment] = [text("赠送大飞龙"), at("99999")]
    assert match_text(segs) == "赠送大飞龙@99999"


def test_match_text_preserves_segment_order() -> None:
    """AT projections happen in segment order, mixed with text fragments."""
    segs: list[Segment] = [
        text("苏苏加好感"),
        at("12345"),
        text(" 50"),
    ]
    assert match_text(segs) == "苏苏加好感@12345 50"


def test_match_text_drops_non_text_non_at_segments() -> None:
    """Image / face / etc. segments are still ignored — only text + at count."""
    segs: list[Segment] = [
        text("hi "),
        image(url="https://x/y.png"),
        at("777"),
        text("!"),
    ]
    assert match_text(segs) == "hi @777!"


def test_match_text_falls_back_to_plain_when_no_at() -> None:
    """No AT segments → match_text matches plain_text exactly."""
    segs: list[Segment] = [text("打卡")]
    assert match_text(segs) == plain_text(segs)
