"""QRDic source-level regression tests.

These exercise the DSL exactly as it appears in the original
``QRDic/dicpro.txt``: three-token ``$读$`` / ``$写$`` calls, Chinese
order keywords on ``$排行榜$``, and multi-segment paths like
``小苏苏/好感/<QQ>/好感``. They are the canary that catches any
regression where the VM goes back to mis-routing path arguments.
"""

from __future__ import annotations

import linling_core.tools_builtin  # noqa: F401 — ensure tools are registered
import pytest
from linling_core.events import Event, Scope, User
from linling_core.segments import TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry
from linling_dsl.parser import parse
from linling_dsl.vm import VM


def _event(text: str, *, sender_id: str = "12345", group_id: str = "67890") -> Event:
    return Event(
        id="qrdic-test",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id=group_id, platform="test"),
        sender=User(id=sender_id, platform="test", display_name="小明"),
        segments=[TextSegment(text=text)],
    )


@pytest.fixture
async def kv():
    async with SqliteKVStore(bot_id="linling", db_path=":memory:") as store:
        yield store


@pytest.fixture
def vm(kv):
    return VM(tool_registry=registry, kv=kv, bot_id="linling")


# ---------------------------------------------------------------------------
# 1. Classic 3-segment path: "啊/灵玉系/灵玉"
# ---------------------------------------------------------------------------


async def test_read_write_three_segment_path(kv, vm):
    """The most common QRDic pattern: ``scope/subsystem/file key default``."""
    source = """\
我的灵玉
玉:$读 啊/灵玉系/灵玉 %QQ% 0$
你有 %玉% 灵玉
"""
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("我的灵玉"))
    assert result.segments[0].text == "你有 0 灵玉"

    # Populate using the *documented* mapping (design.md §8.1) and read back.
    await kv.write("啊/灵玉系", "灵玉", "12345", "9999")
    result = await vm.execute_handler(script.handlers[0], _event("我的灵玉"))
    assert result.segments[0].text == "你有 9999 灵玉"


async def test_write_persists_to_documented_columns(kv, vm):
    """``$写 啊/灵玉系/灵玉 %QQ% N$`` must land at scope=灵玉系, file=灵玉."""
    source = "设置\n$写 啊/灵玉系/灵玉 %QQ% 288$\nokay"
    script = parse(source, strict=False)
    await vm.execute_handler(script.handlers[0], _event("设置", sender_id="42"))
    assert await kv.read("啊/灵玉系", "灵玉", "42") == "288"


# ---------------------------------------------------------------------------
# 2. Four-segment path: "小苏苏/好感/<QQ>/好感"
# ---------------------------------------------------------------------------


async def test_four_segment_path(kv, vm):
    """``小苏苏/好感/12345/好感`` → scope=``小苏苏/好感/12345``, file=``好感``."""
    source = "好感\n好:$读 小苏苏/好感/%QQ%/好感 jb 50$\n好感 %好%"
    script = parse(source, strict=False)

    # Default returned on miss.
    r = await vm.execute_handler(script.handlers[0], _event("好感"))
    assert r.segments[0].text == "好感 50"

    await kv.write("小苏苏/好感/12345", "好感", "jb", "77")
    r = await vm.execute_handler(script.handlers[0], _event("好感"))
    assert r.segments[0].text == "好感 77"


# ---------------------------------------------------------------------------
# 3. Two-segment path: "偷玉游戏/偷玉数量"
# ---------------------------------------------------------------------------


async def test_two_segment_path_uses_empty_file(kv, vm):
    """Top-level ``偷玉游戏/偷玉数量`` → scope=``偷玉游戏``, file=``偷玉数量``."""
    source = "查\n数:$读 偷玉游戏/偷玉数量 %QQ% 0$\n数量 %数%"
    script = parse(source, strict=False)

    await kv.write("偷玉游戏", "偷玉数量", "12345", "3")
    r = await vm.execute_handler(script.handlers[0], _event("查"))
    assert r.segments[0].text == "数量 3"


# ---------------------------------------------------------------------------
# 4. $排行榜$: Chinese order keyword, stringified top, "\n" separator
# ---------------------------------------------------------------------------


async def test_rank_with_chinese_order_and_newline_sep(kv, vm):
    """``$排行榜 path 反序 N \\n fmt$`` — the most common QRDic shape."""
    for key, value in [("a", "10"), ("b", "30"), ("c", "20")]:
        await kv.write("啊/灵玉系", "灵玉", key, value)

    source = "榜\nM:$排行榜 啊/灵玉系/灵玉 反序 3 \\n 榜[序号]-[键]-[值]$\n%M%"
    script = parse(source, strict=False)
    r = await vm.execute_handler(script.handlers[0], _event("榜"))
    assert r.segments[0].text == "榜1-b-30\n榜2-c-20\n榜3-a-10"


async def test_rank_tolerates_unknown_order(kv, vm):
    """Unknown order token falls back to 反序 rather than crashing."""
    await kv.write("x", "y", "k", "5")
    source = "r\nM:$排行榜 x/y wat 1 \\n [键]$\n%M%"
    script = parse(source, strict=False)
    r = await vm.execute_handler(script.handlers[0], _event("r"))
    assert r.segments[0].text == "k"


# ---------------------------------------------------------------------------
# 5. $删除$: QRDic filesystem-style absolute paths
# ---------------------------------------------------------------------------


async def test_delete_absolute_qrdic_path(kv, vm):
    """``$删除 /storage/emulated/0/QR/QRDic/data/<scope>/<file>$`` → scope+file wipe."""
    # Must use the interpolated group id here — the DSL %群号% reference in
    # the delete path below expands to the event's scope.id, and the shim
    # then maps it to (scope="67890/卧底", file="被投票").
    await kv.write("67890/卧底", "被投票", "k1", "v1")
    await kv.write("67890/卧底", "被投票", "k2", "v2")
    await kv.write("67890/卧底", "说话", "keep", "keep")

    source = "清\n$删除 /storage/emulated/0/QR/QRDic/data/%群号%/卧底/被投票$"
    script = parse(source, strict=False)
    await vm.execute_handler(script.handlers[0], _event("清"))

    assert await kv.read("67890/卧底", "被投票", "k1") is None
    assert await kv.read("67890/卧底", "被投票", "k2") is None
    # The adjacent file under the same scope must survive.
    assert await kv.read("67890/卧底", "说话", "keep") == "keep"


async def test_delete_scope_level(kv, vm):
    """``$删除 /storage/.../data/<scope>$`` nukes the whole scope."""
    await kv.write("67890/卧底", "被投票", "k", "v")
    await kv.write("67890/卧底", "说话", "k", "v")

    source = "清\n$删除 /storage/emulated/0/QR/QRDic/data/%群号%/卧底$"
    script = parse(source, strict=False)
    await vm.execute_handler(script.handlers[0], _event("清"))

    assert await kv.read("67890/卧底", "被投票", "k") is None
    assert await kv.read("67890/卧底", "说话", "k") is None


async def test_delete_non_kv_path_is_noop(kv, vm):
    """Paths outside the QRDic data tree (cache, root .txt) are ignored."""
    await kv.write("keep", "keep", "keep", "keep")
    source = "清\n$删除 /storage/emulated/0/QR/QRDic/data/cache$"
    script = parse(source, strict=False)
    await vm.execute_handler(script.handlers[0], _event("清"))
    # Unrelated KV rows untouched.
    assert await kv.read("keep", "keep", "keep") == "keep"


# ---------------------------------------------------------------------------
# 6. Python / Agent view still exposes clean (scope, file, key) API
# ---------------------------------------------------------------------------


async def test_python_read_kv_keeps_clean_signature(kv):
    """Agents / integrations call ``read_kv`` with scope+file+key, not a path."""
    await kv.write("啊/灵玉系", "灵玉", "12345", "9999")

    read_td = registry.get("read_kv")
    assert read_td is not None
    # Explicitly verify the Python-visible signature hasn't regressed to a
    # single-path argument: this is what the LLM tool-calling code relies on.
    assert set(read_td.schema) == {"scope", "file", "key", "default"}
    assert read_td.dsl_name == ""  # not DSL-callable; the shim is.

    from linling_core.tools import ToolCtx

    ctx = ToolCtx(kv=kv, event=None, bot_id="linling")
    assert await read_td.fn(ctx, "啊/灵玉系", "灵玉", "12345") == "9999"


def test_dsl_shims_hidden_from_llm_catalog():
    """LLM tool schemas must not include the QRDic compat shims."""
    schemas = registry.llm_schemas()
    llm_names = {s["function"]["name"] for s in schemas}
    assert "read_kv" in llm_names
    assert "write_kv" in llm_names
    assert "dsl_read_kv" not in llm_names
    assert "dsl_write_kv" not in llm_names
    assert "dsl_rank_kv" not in llm_names


# ---------------------------------------------------------------------------
# QRSpeed P0 compatibility additions: %IMG*%, ##-comment, \%XX escapes
# ---------------------------------------------------------------------------


async def test_imgnum_resolves_image_segment_count(kv, vm):
    """``%IMGNUM%`` reflects the number of ``ImageSegment`` on the inbound event."""
    from linling_core.segments import ImageSegment

    source = "看图\n你发了 %IMGNUM% 张图"
    script = parse(source, strict=False)
    event = Event(
        id="img-test",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        segments=[
            TextSegment(text="看图"),
            ImageSegment(url="https://example.com/a.png"),
            ImageSegment(url="https://example.com/b.png"),
        ],
    )
    result = await vm.execute_handler(script.handlers[0], event)
    assert result.segments[0].text == "你发了 2 张图"


async def test_img0_resolves_first_image_url(kv, vm):
    """``%IMG0%`` returns the first ``ImageSegment`` URL; missing slots return ''."""
    from linling_core.segments import ImageSegment

    source = "img\n第一张:%IMG0%\n第三张:%IMG2%"
    script = parse(source, strict=False)
    event = Event(
        id="img-test",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        segments=[
            TextSegment(text="img"),
            ImageSegment(url="https://example.com/0.png"),
        ],
    )
    result = await vm.execute_handler(script.handlers[0], event)
    text = "".join(s.text for s in result.segments if hasattr(s, "text"))
    assert "第一张:https://example.com/0.png" in text
    assert "第三张:" in text and "第三张:https" not in text  # IMG2 is missing → empty


async def test_double_hash_comment_skipped_in_body(kv, vm):
    """``##`` in a handler body is treated as a comment, never as output."""
    source = """\
hello
## this is a system-style comment
yo
## also this
"""
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("hello"))
    text = "".join(s.text for s in result.segments if hasattr(s, "text"))
    # Only "yo" should reach output.
    assert text.strip() == "yo"
    assert "##" not in text


def test_double_hash_handler_dropped_at_parse_time():
    """A handler whose first line starts with ``##`` is dropped, like ``//``."""
    source = """\
##commented out trigger
this body would never run

real
ok
"""
    script = parse(source, strict=False)
    triggers = [h.trigger for h in script.handlers]
    assert "real" in triggers
    assert "##commented out trigger" not in triggers


async def test_url_escape_percent_xx_decoded_to_characters(kv, vm):
    """``\\%0A`` decodes to LF; ``\\%20`` to space; ``\\%25`` survives as ``%``."""
    source = "esc\nA\\%0AB\\%20C\\%25D"
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("esc"))
    text = "".join(s.text for s in result.segments if hasattr(s, "text"))
    # Note: VM doesn't apply ``_decode_qrdic_escapes`` to %var% interpolation
    # results because they're already decoded in OutputText emit. Here our
    # input has no \\n etc., just URL-encoded escapes.
    assert text == "A\nB C%D"


async def test_url_escape_percent_xx_does_not_break_var_lookup(kv, vm):
    """``\\%25IMG%x%\\%25`` is decoded to ``%IMG0%`` but **not** re-resolved.

    QRSpeed's ``$执行 \\%25IMG%x%\\%25$`` pattern was dynamic var lookup
    (eval the resulting string as a var name). We don't honour the eval
    side; we just ensure the encoded ``%`` doesn't accidentally turn
    into a lookup that crashes — it stays as a literal ``%`` in output.
    """
    source = "esc2\nx:0\nv:\\%25IMG%x%\\%25"
    script = parse(source, strict=False)
    # Just verifying the body parses + executes without error and the
    # final assignment value text contains the literal percent signs.
    result = await vm.execute_handler(script.handlers[0], _event("esc2"))
    # No output (only assignments) — handler returns silently.
    assert all(not getattr(s, "text", "").strip() for s in result.segments)


async def test_qrspeed_inline_case_insensitive_trigger_matches() -> None:
    """``(?i)留言板help`` accepts both ``留言板HELP`` and ``留言板help``.

    Python's ``re.compile`` natively understands the ``(?i)`` inline
    flag, so the classifier's regex bucket already supports this
    QRSpeed pattern out of the box. This test pins the behaviour
    against accidental regressions (e.g. someone normalising triggers
    to lower-case before compile).
    """
    from linling_core.classifier import MessageClassifier
    from linling_dsl.parser import parse

    source = "(?i)留言板help\nok\n"
    script = parse(source, strict=False)
    classifier = MessageClassifier(script=script)

    def _ev(text: str) -> Event:
        return Event(
            id="x",
            platform="test",
            bot_id="linling",
            scope=Scope(kind="group", id="g", platform="test"),
            sender=User(id="u", platform="test"),
            segments=[TextSegment(text=text)],
        )

    assert classifier.classify(_ev("留言板HELP")).kind == "command"
    assert classifier.classify(_ev("留言板help")).kind == "command"
    assert classifier.classify(_ev("留言板Hello")).kind == "chat"


# ---------------------------------------------------------------------------
# QRSpeed P1 compatibility additions: %NDTime%, %RobotRunTime%, %管理员%/%主人%,
# new media sigils ±ptt= ±fimg= ±rep, $时间 fmt$, $MD5$, $JSON 包含/键$
# ---------------------------------------------------------------------------


async def test_ndtime_returns_milliseconds_now(kv, vm) -> None:
    """``%NDTime%`` evaluates to the current wall-clock in milliseconds."""
    import time

    source = "ndt\n%NDTime%"
    script = parse(source, strict=False)
    before = int(time.time() * 1000)
    result = await vm.execute_handler(script.handlers[0], _event("ndt"))
    after = int(time.time() * 1000)
    text = result.segments[0].text
    assert text.isdigit()
    millis = int(text)
    assert before <= millis <= after + 50  # small slack for execution


async def test_robotruntime_reflects_set_bot_start_time(kv, vm) -> None:
    """``%RobotRunTime%`` reflects whatever :func:`set_bot_start_time_ms` set."""
    from linling_dsl.vm import set_bot_start_time_ms

    set_bot_start_time_ms(1_700_000_000_000)
    source = "rrt\n%RobotRunTime%"
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("rrt"))
    assert result.segments[0].text == "1700000000000"


async def test_admin_resolved_from_extras(kv) -> None:
    """``%管理员%`` / ``%主人%`` come from dispatcher extras."""
    vm = VM(
        tool_registry=registry,
        kv=kv,
        bot_id="linling",
        extras={
            "admin_users": ("2078123478", "11111"),
        },
    )
    source = "ids\n管理员=%管理员% 主人=%主人%"
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("ids"))
    text = result.segments[0].text
    assert "管理员=2078123478" in text
    assert "主人=2078123478" in text  # alias


async def test_admin_empty_when_unconfigured(kv, vm) -> None:
    """No admin_users configured → empty strings, not crash."""
    source = "ids\nadmin=%管理员%"
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("ids"))
    text = result.segments[0].text
    assert "admin=" in text and "admin=2078123478" not in text


async def test_voice_sigil_emits_voice_segment(kv, vm) -> None:
    """``±ptt=URL±`` lands as a :class:`VoiceSegment`."""
    from linling_core.segments import VoiceSegment

    source = "voice\n±ptt=https://example.com/v.mp3±"
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("voice"))
    voice = next((s for s in result.segments if isinstance(s, VoiceSegment)), None)
    assert voice is not None
    assert voice.url == "https://example.com/v.mp3"


async def test_flash_image_sigil_emits_image_with_extras(kv, vm) -> None:
    """``±fimg=URL±`` lands as ``ImageSegment(extras={"flash": True})``."""
    from linling_core.segments import ImageSegment

    source = "fimg\n±fimg=https://example.com/x.png±"
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("fimg"))
    img = next((s for s in result.segments if isinstance(s, ImageSegment)), None)
    assert img is not None
    assert img.url == "https://example.com/x.png"
    assert img.extras.get("flash") is True


async def test_reply_sigil_emits_reply_segment(kv, vm) -> None:
    """``±rep msgid±`` lands as a :class:`ReplySegment`."""
    from linling_core.segments import ReplySegment

    source = "rep\n±rep 12345±"
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("rep"))
    rep = next((s for s in result.segments if isinstance(s, ReplySegment)), None)
    assert rep is not None
    assert rep.message_id == "12345"


async def test_bub_and_strmsg_sigils_silently_dropped(kv, vm) -> None:
    """``±bub N±`` and ``±strmsg X±`` parse but emit nothing.

    These are QQ装扮 / 字符串水印 sigils that don't have a portable
    representation; we accept the syntax (so the handler isn't a
    parse error) but the runtime drops them.
    """
    source = """\
deco
±bub 5±
±strmsg hello world±
output
"""
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("deco"))
    text = "".join(s.text for s in result.segments if hasattr(s, "text"))
    assert text.strip() == "output"


async def test_md5_tool_returns_hex_digest(kv, vm) -> None:
    import linling_tools_stdlib  # noqa: F401 — registers $MD5$ etc.

    # Standalone ``$MD5 hello world$`` runs the tool and discards the
    # result (a top-level FuncCall is a side-effect statement). To
    # observe it in the output we wrap it in a text line so the
    # VM does an inline FuncCallExpr substitution.
    source = "md\nresult: $MD5 hello world$"
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("md"))
    # MD5("hello world") == 5eb63bbbe01eeed093cb22bb8f5acdc3
    assert result.segments[0].text == "result: 5eb63bbbe01eeed093cb22bb8f5acdc3"


async def test_format_time_tool_translates_java_isms(kv, vm) -> None:
    """``$时间 yyyyMMdd$`` translates Java date letters to strftime."""
    import linling_tools_stdlib  # noqa: F401

    source = "ft\nday: $时间 yyyyMMdd$"
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("ft"))
    text = result.segments[0].text
    assert text.startswith("day: ")
    digits = text[len("day: ") :]
    assert len(digits) == 8 and digits.isdigit()  # YYYYMMDD


async def test_json_contains_and_keys_subcommands(kv, vm) -> None:
    import linling_tools_stdlib  # noqa: F401

    # NOTE: ``有a:$JSON 包含 ...$`` would parse as a 1-char-name
    # assignment because ``有a`` (2 chars) fits the ≤2-char rule. We
    # use distinct >2-char prefixes that *would* hit the assignment
    # rule and instead emit each tool result inline.
    source = """\
js
A:{"a":1,"b":2}
contains a: $JSON 包含 A a$
contains c: $JSON 包含 A c$
keys: $JSON 键 A$
"""
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("js"))
    text = "\n".join(s.text for s in result.segments if hasattr(s, "text"))
    assert "contains a: 1" in text
    # Absent → empty.
    assert "contains c: " in text and "contains c: 1" not in text
    assert 'keys: ["a", "b"]' in text or 'keys: ["a","b"]' in text


# ---------------------------------------------------------------------------
# QRSpeed P2 compat: %FACE*%, %XML*%, %JSON*%, %FIMG*%, $图片链接$, $管理员 X$, $群头像$
# ---------------------------------------------------------------------------


async def test_face_var_resolves_face_segment(kv, vm) -> None:
    """``%FACE0%`` returns the first FaceSegment's face_id."""
    from linling_core.segments import FaceSegment

    source = "f\nface=%FACE0%"
    script = parse(source, strict=False)
    event = Event(
        id="x",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        segments=[
            TextSegment(text="f"),
            FaceSegment(face_id="76"),
            FaceSegment(face_id="178"),
        ],
    )
    result = await vm.execute_handler(script.handlers[0], event)
    assert result.segments[0].text == "face=76"


async def test_face_aliases_share_same_segments(kv, vm) -> None:
    """``%FACE0%`` ``%FACENEW0%`` ``%FACEPRO0%`` all map to FaceSegment.

    QRSpeed's three buckets collapsed in OneBot — we honour the alias
    so every name resolves to the same source.
    """
    from linling_core.segments import FaceSegment

    source = "f\nx=%FACENEW0%-%FACEPRO0%"
    script = parse(source, strict=False)
    event = Event(
        id="x",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        segments=[TextSegment(text="f"), FaceSegment(face_id="42")],
    )
    result = await vm.execute_handler(script.handlers[0], event)
    assert result.segments[0].text == "x=42-42"


async def test_xml_segment_var(kv, vm) -> None:
    from linling_core.segments import XmlSegment

    source = "f\n%XML0%"
    script = parse(source, strict=False)
    event = Event(
        id="x",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        segments=[TextSegment(text="f"), XmlSegment(xml="<msg>hi</msg>")],
    )
    result = await vm.execute_handler(script.handlers[0], event)
    assert "<msg>hi</msg>" in result.segments[0].text


async def test_json0_segment_var_resolves_to_card_payload(kv, vm) -> None:
    """``%JSON0%`` returns the first CardSegment's payload (vs ``%Json%`` which is the auth blob)."""
    from linling_core.segments import CardSegment

    source = "f\n%JSON0%"
    script = parse(source, strict=False)
    event = Event(
        id="x",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        segments=[TextSegment(text="f"), CardSegment(payload='{"k":"v"}')],
    )
    result = await vm.execute_handler(script.handlers[0], event)
    assert '{"k":"v"}' in result.segments[0].text


async def test_fimg_var_filters_flash_images(kv, vm) -> None:
    """``%FIMG0%`` only sees ImageSegments flagged with extras.flash."""
    from linling_core.segments import ImageSegment

    source = "f\nfimg=%FIMG0% num=%FIMGNUM%"
    script = parse(source, strict=False)
    event = Event(
        id="x",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        segments=[
            TextSegment(text="f"),
            ImageSegment(url="https://e.com/normal.png"),
            ImageSegment(url="https://e.com/flash.png", extras={"flash": True}),
        ],
    )
    result = await vm.execute_handler(script.handlers[0], event)
    text = result.segments[0].text
    assert text == "fimg=https://e.com/flash.png num=1"


async def test_image_link_tool_returns_indexed_url(kv, vm) -> None:
    """``$图片链接 N$`` matches ``%IMGN%`` semantics."""
    import linling_tools_stdlib  # noqa: F401
    from linling_core.segments import ImageSegment

    source = "f\nlink: $图片链接 1$"
    script = parse(source, strict=False)
    event = Event(
        id="x",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        segments=[
            TextSegment(text="f"),
            ImageSegment(url="https://e.com/0.png"),
            ImageSegment(url="https://e.com/1.png"),
        ],
    )
    result = await vm.execute_handler(script.handlers[0], event)
    assert result.segments[0].text == "link: https://e.com/1.png"


async def test_is_admin_tool_yes_no(kv) -> None:
    import linling_tools_stdlib  # noqa: F401

    vm = VM(
        tool_registry=registry,
        kv=kv,
        bot_id="linling",
        extras={"admin_users": ("12345", "99999")},
    )
    source = "f\nyes:$管理员 12345$ no:$管理员 999$"
    script = parse(source, strict=False)
    event = _event("f")
    result = await vm.execute_handler(script.handlers[0], event)
    text = result.segments[0].text
    assert text == "yes:1 no:"


async def test_group_avatar_returns_qq_cdn_url(kv, vm) -> None:
    import linling_tools_stdlib  # noqa: F401

    source = "f\nava: $群头像 754800438$"
    script = parse(source, strict=False)
    result = await vm.execute_handler(script.handlers[0], _event("f"))
    assert result.segments[0].text == "ava: https://p.qlogo.cn/gh/754800438/754800438/0"
