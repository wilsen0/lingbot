from __future__ import annotations

from linling_core.onebot_codec import from_onebot_msg, to_onebot_msg
from linling_core.segments import (
    AtSegment,
    CardSegment,
    FaceSegment,
    ImageSegment,
    PokeSegment,
    ReplySegment,
    TextSegment,
    VoiceSegment,
    XmlSegment,
)


def test_decode_basic_text_and_at_and_image() -> None:
    msg = [
        {"type": "text", "data": {"text": "hi"}},
        {"type": "at", "data": {"qq": "12345"}},
        {
            "type": "image",
            "data": {"url": "https://x/a.png", "file_size": 1024},
        },
    ]
    segs = from_onebot_msg(msg)
    assert isinstance(segs[0], TextSegment) and segs[0].text == "hi"
    assert isinstance(segs[1], AtSegment) and segs[1].user_id == "12345"
    assert isinstance(segs[2], ImageSegment)
    assert segs[2].url == "https://x/a.png"
    # Non-first-class fields land in extras
    assert segs[2].extras == {"file_size": 1024}


def test_unknown_types_are_dropped() -> None:
    segs = from_onebot_msg([{"type": "something_new", "data": {}}])
    assert segs == []


def test_decode_reply_face_poke_voice_xml_json() -> None:
    msg = [
        {"type": "reply", "data": {"id": "m99"}},
        {"type": "face", "data": {"id": "7"}},
        {"type": "poke", "data": {"qq": "42"}},
        {"type": "record", "data": {"url": "https://x/a.silk", "duration": 3}},
        {"type": "xml", "data": {"data": "<xml/>"}},
        {"type": "json", "data": {"data": '{"k":1}'}},
    ]
    segs = from_onebot_msg(msg)
    assert isinstance(segs[0], ReplySegment) and segs[0].message_id == "m99"
    assert isinstance(segs[1], FaceSegment) and segs[1].face_id == "7"
    assert isinstance(segs[2], PokeSegment) and segs[2].target_user_id == "42"
    assert isinstance(segs[3], VoiceSegment) and segs[3].duration_s == 3
    assert isinstance(segs[4], XmlSegment) and segs[4].xml == "<xml/>"
    assert isinstance(segs[5], CardSegment) and segs[5].payload == '{"k":1}'


def test_encode_image_prefers_url_then_path_then_b64() -> None:
    assert to_onebot_msg([ImageSegment(url="https://x/a.png")])[0]["data"]["file"] == (
        "https://x/a.png"
    )
    assert to_onebot_msg([ImageSegment(path="/tmp/a.png")])[0]["data"]["file"] == (
        "file:///tmp/a.png"
    )
    assert to_onebot_msg([ImageSegment(b64="AAAA")])[0]["data"]["file"] == "base64://AAAA"


def test_encode_decode_roundtrip_text() -> None:
    msg = [{"type": "text", "data": {"text": "hello"}}]
    assert to_onebot_msg(from_onebot_msg(msg)) == msg


def test_encode_reply_and_at() -> None:
    out = to_onebot_msg([ReplySegment(message_id="m1"), AtSegment(user_id="9")])
    assert out == [
        {"type": "reply", "data": {"id": "m1"}},
        {"type": "at", "data": {"qq": "9"}},
    ]


def test_decode_is_permissive_on_missing_fields() -> None:
    msg = [{"type": "image", "data": {}}]
    segs = from_onebot_msg(msg)
    assert isinstance(segs[0], ImageSegment)
    assert segs[0].url is None


def test_decode_mface_and_bface_to_face_segment() -> None:
    # QQ market face (商城表情包/贴纸) and original face must decode to a real
    # segment instead of being dropped — otherwise a pure-sticker message
    # decodes to an empty segment list and looks like empty content downstream.
    msg = [
        {
            "type": "mface",
            "data": {
                "emoji_id": "123",
                "summary": "[doge]",
                "url": "https://x/doge.png",
                "emoji_package_id": 9,
            },
        },
        {"type": "bface", "data": {"id": "456", "name": "原创"}},
    ]
    segs = from_onebot_msg(msg)
    assert len(segs) == 2
    assert isinstance(segs[0], FaceSegment) and segs[0].face_id == "123"
    # Platform-specific fields preserved under extras, emoji_id promoted to face_id.
    assert "emoji_id" not in segs[0].extras
    assert segs[0].extras.get("summary") == "[doge]"
    assert isinstance(segs[1], FaceSegment) and segs[1].face_id == "456"
    assert segs[1].extras.get("name") == "原创"
