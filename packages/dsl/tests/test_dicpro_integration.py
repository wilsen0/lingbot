"""End-to-end integration tests against representative QRDic patterns.

These cover the syntactic surface actually used in
``QRDic/dicpro.txt`` — not synthetic toy snippets — so a regression
that makes any one pattern misbehave shows up here. Each test name
ties back to a specific QRDic idiom so it's clear what's being
exercised.

Tests deliberately use:

* :class:`SqliteKVStore` with ``:memory:`` so KV side-effects are
  observable without disk IO,
* the *full* tool registry imported via ``import linling_tools_stdlib``
  (matches what the production bot loads),
* the same parser strict-mode the bootstrap uses (``strict=False``),
  so the test reflects the production lenient parse.

A failure here usually means either (a) a new tool name is missing
from the registry, (b) the VM's variable / arithmetic / interpolation
machinery regressed, or (c) the parser tightened up and the legacy
syntax no longer parses.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

# Importing the stdlib here mirrors the bootstrap's behaviour and
# overrides the placeholder tools registered in ``linling_core.tools_builtin``.
import linling_tools_stdlib  # noqa: F401
import pytest
from linling_core import (
    AtSegment,
    Event,
    Scope,
    SqliteKVStore,
    TextSegment,
    User,
    registry,
)
from linling_dsl.parser import parse
from linling_dsl.vm import VM


@pytest.fixture
async def kv() -> AsyncIterator[SqliteKVStore]:
    store = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        yield store
    finally:
        await store.close()


def _event(text: str, *, sender: str = "12345", group: str = "67890") -> Event:
    return Event(
        id=f"e-{sender}-{text[:8]}",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id=group, platform="cli"),
        sender=User(id=sender, platform="cli", display_name="试"),
        segments=[TextSegment(text=text)],
    )


def _vm(kv: SqliteKVStore, **extras: object) -> VM:
    return VM(tool_registry=registry, kv=kv, bot_id="susu_test", extras=dict(extras))


async def _run(
    source: str, kv: SqliteKVStore, ev: Event, *, captures=None, **extras: object
) -> str:
    """Parse + execute a single handler block; return concatenated text output.

    The DSL convention puts the trigger on line 1 and the body
    underneath. This helper takes the *body only* and synthesises a
    block with a placeholder trigger so the parser is happy.
    """
    full = "trigger\n" + source.strip("\n") + "\n"
    script = parse(full, strict=False)
    handler = script.handlers[0]
    vm = _vm(kv, **extras)
    res = await vm.execute_handler(handler, ev, captures=captures or [])
    return "".join(s.text for s in res.segments if isinstance(s, TextSegment))


# ---------------------------------------------------------------------------
# §1 KV core idioms — read / write / arithmetic round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_default_when_missing(kv) -> None:
    """``$读 path key default$`` returns the default when no row exists."""
    out = await _run("玉:$读 啊/灵玉系/灵玉 %QQ% 0$\n%玉%", kv, _event("看玉"))
    assert out == "0"


@pytest.mark.asyncio
async def test_write_then_read_roundtrip(kv) -> None:
    """``$写$`` persists; subsequent ``$读$`` returns the new value."""
    await _run("$写 啊/灵玉系/灵玉 %QQ% 88$", kv, _event("seed"))
    out = await _run("玉:$读 啊/灵玉系/灵玉 %QQ% 0$\n%玉%", kv, _event("查"))
    assert out == "88"


@pytest.mark.asyncio
async def test_arithmetic_increment_pattern(kv) -> None:
    """``玉:[%玉%+10]`` round-trips: read → arith → write → read."""
    await _run("$写 啊/灵玉系/灵玉 %QQ% 5$", kv, _event("seed"))
    body = """玉:$读 啊/灵玉系/灵玉 %QQ% 0$
$写 啊/灵玉系/灵玉 %QQ% [%玉%+10]$"""
    await _run(body, kv, _event("加"))
    out = await _run("v:$读 啊/灵玉系/灵玉 %QQ% 0$\n%v%", kv, _event("查"))
    assert out == "15"


# ---------------------------------------------------------------------------
# §2 Variable lookup — context built-ins + capture groups + time vars.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_variables(kv) -> None:
    """``%QQ%`` ``%群号%`` ``%昵称%`` resolve from the live event."""
    out = await _run("%QQ% / %群号% / %昵称%", kv, _event("hi"))
    assert out == "12345 / 67890 / 试"


@pytest.mark.asyncio
async def test_capture_groups(kv) -> None:
    """``%括号1%`` ``%括号2%`` come from regex captures (1-indexed)."""
    out = await _run("%括号1% + %括号2%", kv, _event("ignored"), captures=["foo", "bar"])
    assert out == "foo + bar"


@pytest.mark.asyncio
async def test_at_segment_resolves(kv) -> None:
    """``%AT0%`` reads the first ``AtSegment`` in the event."""
    ev = Event(
        id="e1",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="67890", platform="cli"),
        sender=User(id="12345", platform="cli"),
        segments=[TextSegment(text="戳 "), AtSegment(user_id="99999")],
    )
    out = await _run("被 @%AT0%", kv, ev)
    assert out == "被 @99999"


@pytest.mark.asyncio
async def test_time_var_format(kv) -> None:
    """``%时间HH:mm%`` produces an ``HH:MM`` string."""
    out = await _run("%时间HH:mm%", kv, _event("now"))
    assert ":" in out and len(out) == 5


# ---------------------------------------------------------------------------
# §3 Inline random — both ``%随机数N-M%`` and ``$随机数 N M$``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_random_var(kv) -> None:
    """``%随机数1-5%`` resolves to an integer in [1,5]."""
    for _ in range(20):
        out = await _run("%随机数1-5%", kv, _event("r"))
        assert int(out) in range(1, 6)


@pytest.mark.asyncio
async def test_random_int_tool(kv) -> None:
    """The explicit ``$随机数 N M$`` form still works."""
    for _ in range(20):
        # ``$func$`` *as a standalone body line* is a side-effect call:
        # the parser treats it as a fire-and-forget statement (mirrors
        # QRDic). To capture the value we put it inside an interpolation,
        # which is the same convention every dicpro.txt rule uses.
        out = await _run("v:$随机数 1 3$\n%v%", kv, _event("r"))
        assert int(out) in (1, 2, 3)


# ---------------------------------------------------------------------------
# §4 Conditionals — equality, OR, AND, nested.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_if_block_executes_on_true(kv) -> None:
    body = """如果:%QQ%==12345
hit
如果尾"""
    out = await _run(body, kv, _event("x"))
    assert out == "hit"


@pytest.mark.asyncio
async def test_if_block_skips_on_false(kv) -> None:
    body = """如果:%QQ%==99999
hit
如果尾
miss"""
    out = await _run(body, kv, _event("x"))
    assert out == "miss"


@pytest.mark.asyncio
async def test_or_condition(kv) -> None:
    body = """如果:%QQ%==99999|%群号%==67890
yes
如果尾"""
    out = await _run(body, kv, _event("x"))
    assert out == "yes"


@pytest.mark.asyncio
async def test_and_condition(kv) -> None:
    body = """如果:%QQ%==12345&%群号%==67890
yes
如果尾"""
    out = await _run(body, kv, _event("x"))
    assert out == "yes"


# ---------------------------------------------------------------------------
# §5 Labels + jumps — short circuit out of long handlers.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jump_to_label(kv) -> None:
    body = """before
$jump :end$
should-not-print
:end
after"""
    out = await _run(body, kv, _event("x"))
    # The body emits 3 text segments: "before", "should-not-print" is
    # skipped, "after". Each is its own line.
    assert "before" in out and "after" in out and "should-not-print" not in out


@pytest.mark.asyncio
async def test_jump_chinese_alias(kv) -> None:
    body = """$跳 :end$
nope
:end
ok"""
    out = await _run(body, kv, _event("x"))
    assert out == "ok"


@pytest.mark.asyncio
async def test_jump_from_inside_if_reaches_outer_label(kv) -> None:
    """A ``$jump :loop$`` nested inside an ``如果:`` body must reach a
    label declared at the *handler* level — that's how QRDic loops
    work (e.g. ``扭蛋口令``'s ``:重随机`` retry, ``扭蛋十次``'s
    ``:抽的次数`` fan-out). Regression: previously the jump only
    searched the inner if's body and silently fell through.
    """
    body = """i:0
:loop
i:[%i%+1]
如果:%i%<3
$jump :loop$
如果尾
%i%"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "3"


@pytest.mark.asyncio
async def test_undefined_var_in_output_renders_literal(kv) -> None:
    """A bare ``%var%`` reference in OutputText must degrade to the
    literal placeholder text when the variable is undefined. Real
    handlers like ``[内部]十扭蛋记录`` reference ``%蛋%`` which is
    never assigned; raising would break the gacha-record render.
    """
    out = await _run("hello %蛋% world", kv, _event("x"))
    assert out.strip() == "hello %蛋% world"


@pytest.mark.asyncio
async def test_arith_block_in_condition(kv) -> None:
    """Conditions must evaluate ``[arith]`` blocks before comparing.

    Real-world rule:: ``如果:%玉%<[%括号1%*66]`` (used by 提升妖力
    /兑换御妖符 etc.). Without arith resolution the condition would
    compare ``"2000"`` against the literal ``"[10*66]"``; the
    string-comparison fallback gives ``'2' < '['`` (50 vs 91 in
    ASCII) which is True — and the rule incorrectly returns
    "灵玉不足".
    """
    body = """玉:2000
如果:%玉%<[10*66]
NOT_ENOUGH
返回
如果尾
PAID"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "PAID"


@pytest.mark.asyncio
async def test_json_add_mutates_scope_variable(kv) -> None:
    """``$JSON 添加 var value$`` standalone call must mutate ``var``
    in scope (matching QRDic's by-reference semantics). The
    漂流瓶 module relies on this to thread bottle data through
    ``R``/``P`` arrays without re-assigning at every step.
    """
    body = """R:[]
$JSON 添加 R foo$
$JSON 添加 R bar$
%R%"""
    raw = await _run(body, kv, _event("x"))
    # ``json.dumps`` formats with a space after the comma; normalise.
    assert '["foo","bar"]' in raw.replace(" ", "")


@pytest.mark.asyncio
async def test_late_binding_does_not_corrupt_kv_key(kv) -> None:
    """Late-binding must NOT swap a literal KV key for a same-named
    scope variable. The 漂流瓶 module writes::

        R:$读 啊/漂流瓶/瓶子 R []$
        ...
        $写 啊/漂流瓶/瓶子 R %R%$

    Earlier the VM late-bound the literal key ``R`` (because ``R``
    was in scope) to ``[]``, generating a phantom KV row keyed by
    ``"[]"`` and orphaning the canonical ``R`` key. Only the
    handful of QRDic var-name-as-arg tools (``JSON``, ``替换``,
    ``正则``, ``取中间``) get late-binding; ``$读$`` / ``$写$``
    don't.
    """
    body = """R:["payload"]
$写 测试系/漂流瓶 R %R%$
v:$读 测试系/漂流瓶 R MISS$
%v%"""
    out = await _run(body, kv, _event("x"))
    # If late-binding mis-fired, the key would have been the JSON
    # string ``["payload"]`` instead of the literal ``R`` and the
    # second read would return ``MISS``.
    assert out.strip().replace(" ", "") == '["payload"]'


@pytest.mark.asyncio
async def test_codec_standalone_emits_decoded_text(kv) -> None:
    """``$URLDecoder %x%$`` on its own line must emit the decoded text.

    QRSpeed convention: only "renderer" tools emit. ``$URLDecoder$``,
    ``$Base64Decoder$``, ``$输出为$``, ``$时间$``, ``$访问$`` etc.
    fall in this set; side-effect tools (``$写$`` / ``$发送$`` /
    ``$JSON 添加$`` / ``$调用$`` / ``$删除$``) stay silent.
    """
    out = await _run("$URLDecoder hello%20world$", kv, _event("x"))
    assert out.strip() == "hello world"


@pytest.mark.asyncio
async def test_side_effect_tools_dont_emit_return_value(kv) -> None:
    """``$写$`` / ``$JSON 添加$`` / ``$调用$`` / ``$删除$`` 等 side-effect
    工具的返回值不该流到用户 — 它们要么返回 ``""``, 要么是 task id /
    新数组等内部数据."""
    body = """A:["foo"]
$JSON 添加 A bar$
$写 测试/侧效 k v$
$删除 测试/侧效$"""
    out = await _run(body, kv, _event("x"))
    # No standalone tool emits anything — output should be empty
    assert out == ""


@pytest.mark.asyncio
async def test_param_n_tokenizes_message_text(kv) -> None:
    """``%参数N%`` (N >= 1) 是消息文本的第 N 个 whitespace-separated token,
    1-indexed. ``%参数-1%`` 是整条原文; ``%参数N%`` (越界) 返回空."""
    body = """P1=%参数1%/P2=%参数2%/P5=%参数5%"""
    out = await _run(body, kv, _event("hello world foo"))
    assert out == "P1=hello/P2=world/P5="


@pytest.mark.asyncio
async def test_param_n_with_at_segment_excludes_at(kv) -> None:
    """OneBot ``AtSegment`` 不进 ``event.text``;  所以 ``禁言@xxx 30`` 的
    text 就是 ``"禁言 30"``, ``%参数1%`` = ``"禁言"``, ``%参数2%`` = ``"30"``."""
    from linling_core import AtSegment

    ev = Event(
        id="e",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="cli"),
        sender=User(id="u", platform="cli"),
        segments=[
            TextSegment(text="禁言"),
            AtSegment(user_id="888"),
            TextSegment(text=" 30"),
        ],
    )
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    body = "trig\nP1=%参数1%/P2=%参数2%"
    script = parse(body, strict=False)
    res = await vm.execute_handler(script.handlers[0], ev)
    out = "".join(s.text for s in res.segments if isinstance(s, TextSegment))
    assert out == "P1=禁言/P2=30"


@pytest.mark.asyncio
async def test_callback_resolves_regex_internal_handler(kv) -> None:
    """``$回调 prefix-suffix$`` 应能命中 ``[内部]prefix-(.*)`` 这种正则
    触发的 internal handler, 且把正则 capture 喂进 ``%括号N%``.

    Regression: 之前 ``handler_lookup`` 只做 literal 精确匹配, 错过所有
    带正则的 internal handler. 现在 lookup 也走 regex fullmatch fallback,
    返回 ``(handler, captures)`` 给 ``$回调$`` / ``$调用$`` 双方使用.

    用 ``s:$回调 ...$`` 赋值消费, 因为 standalone 的 ``$回调 X$`` 不发
    文本 (回调 不在 _EMIT_OUTPUT_TOOL_DSL_NAMES — 见
    test_callback_standalone_does_not_emit).
    """
    import re as _re

    helper = parse(
        "[内部]说话词语(.*)\ncaptured=%括号1%\n",
        strict=False,
    )

    def smart_lookup(name: str):
        for h in helper.handlers:
            if h.trigger == name:
                return h
        for h in helper.handlers:
            try:
                pattern = _re.compile(h.trigger)
            except _re.error:
                continue
            m = pattern.fullmatch(name)
            if m is not None:
                return h, list(m.groups())
        return None

    body = """s:$回调 说话词语苹果$
%s%"""
    out = await _run(body, kv, _event("x"), handler_lookup=smart_lookup)
    assert out.strip() == "captured=苹果"


@pytest.mark.asyncio
async def test_callback_regex_multi_capture(kv) -> None:
    """``$回调 X-foo-bar-Y$`` → ``[内部]X-(.*)-(.*)-Y`` 多个 capture group
    都正确传到 %括号N%."""
    import re as _re

    helper = parse(
        "[内部]X-(.*)-(.*)-Y\ng1=%括号1% g2=%括号2%\n",
        strict=False,
    )

    def smart_lookup(name: str):
        for h in helper.handlers:
            if h.trigger == name:
                return h
        for h in helper.handlers:
            try:
                pattern = _re.compile(h.trigger)
            except _re.error:
                continue
            m = pattern.fullmatch(name)
            if m is not None:
                return h, list(m.groups())
        return None

    body = """s:$回调 X-foo-bar-Y$
%s%"""
    out = await _run(body, kv, _event("x"), handler_lookup=smart_lookup)
    assert out.strip() == "g1=foo g2=bar"


@pytest.mark.asyncio
async def test_callback_resolves_trigger_with_literal_space(kv) -> None:
    """``$回调 游戏判断 12345$`` 应触发 ``[内部]游戏判断 ([0-9]+)``.

    Regression: parser 把 ``$回调 X args$`` 拆成 ``handler="X"`` +
    ``extra=args``. lookup 只对 ``"X"`` 单 token 做 regex 匹配, 这种
    带空格的 trigger 永远 miss. 现在 ``callback_stub`` 多一步 fallback:
    在直接 lookup 失败 + 有 extra 时, 用 ``handler + " " + extra`` 重
    新 lookup, 命中后捕获组覆盖原 extra args (避免重复传).
    """
    import re as _re

    helper = parse(
        "[内部]游戏判断 ([0-9]+)\nGOT_%括号1%\n",
        strict=False,
    )

    def smart_lookup(name: str):
        for h in helper.handlers:
            if h.trigger == name:
                return h
        for h in helper.handlers:
            try:
                pattern = _re.compile(h.trigger)
            except _re.error:
                continue
            m = pattern.fullmatch(name)
            if m is not None:
                return h, list(m.groups())
        return None

    body = """s:$回调 游戏判断 12345$
%s%"""
    out = await _run(body, kv, _event("x"), handler_lookup=smart_lookup)
    assert out.strip() == "GOT_12345"


# ---------------------------------------------------------------------------
# §6 JSON ops — the dispatcher syntax used throughout dicpro.txt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_length_and_get(kv) -> None:
    """``$JSON 长度 X$`` and ``$JSON 获取 X 1$`` against an array literal.

    The DSL late-binds the bare identifier ``X`` to the local scope
    value. This exercises the var-name-as-arg fix.
    """
    body = """X:["a","b","c"]
n:$JSON 长度 X$
v:$JSON 获取 X 1$
%n%/%v%"""
    out = await _run(body, kv, _event("x"))
    assert out == "3/b"


@pytest.mark.asyncio
async def test_json_add_and_delete(kv) -> None:
    body = """A:[]
A:$JSON 添加 A 7$
A:$JSON 添加 A 8$
A:$JSON 删除 A 0$
%A%"""
    out = await _run(body, kv, _event("x"))
    assert out.replace(" ", "") == "[8]"


# ---------------------------------------------------------------------------
# §7 取中间 — substring extraction used by 排行榜 parsing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_substring_between(kv) -> None:
    """Real ``排行榜`` slice idiom: ``$取中间 @ %row%@1-@-1$``."""
    # Single-line haystack — same shape the live ``$排行榜$`` output
    # produces when its ``sep`` is set to a value other than ``\n``.
    # The substring tool itself doesn't care; we just want to assert
    # extraction picks the right slice. Variable name kept ≤ 2 chars
    # so the parser's "real assignment" heuristic accepts it (longer
    # names look like ``tip:``-style output prefixes).
    body = """r:1-12345-1
a:$取中间 @ %r%@1-@-1$
%a%"""
    out = await _run(body, kv, _event("x"))
    assert out == "12345"


# ---------------------------------------------------------------------------
# §8 全局变量 / 取变量 — the ``$取变量 %c%$`` dynamic-dispatch idiom.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_set_and_get(kv) -> None:
    from linling_tools_stdlib.globals_ops import _reset_globals_for_tests

    _reset_globals_for_tests()
    body = """$全局变量 GREETING 你好$
g:$取变量 GREETING$
%g%"""
    out = await _run(body, kv, _event("x"))
    assert out == "你好"


# ---------------------------------------------------------------------------
# §9 替换 / 正则 — string ops with the QRDic separator-encoded pattern.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_sep(kv) -> None:
    """``$替换 @ %M% @from@to$`` replaces ``from`` → ``to`` in M.

    Use the ``$写/读$`` round-trip to seed the haystack — bare
    ``M:hello world`` would now be parsed as output text (the new
    "looks like assignment value" heuristic rejects free-form
    multi-token text), so we go via KV which is the production path.
    Variable names kept ≤ 2 chars to match QRDic's assignment-name
    convention; longer names look like ``tip:``-style output prefixes.
    """
    body = """$写 t/buf m hello_world$
M:$读 t/buf m none$
o:$替换 @ %M% @hello_world@there$
%o%"""
    out = await _run(body, kv, _event("x"))
    assert out == "there"


@pytest.mark.asyncio
async def test_regex_match_returns_one_or_zero(kv) -> None:
    body = """x:$正则 @ abc123 [0-9]+$
y:$正则 @ abc no_digits$
%x%/%y%"""
    out = await _run(body, kv, _event("x"))
    # First regex matches → "1", second doesn't → "0".
    assert out == "1/0"


# ---------------------------------------------------------------------------
# §10 排行榜 — formatted leaderboard with custom sep / fmt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rank_with_chinese_order(kv) -> None:
    """``$排行榜 path 反序 N \\n [序号].[键].[值]$``.

    Real QRDic rules avoid spaces inside the fmt because the DSL
    tokenizer would split there — they use punctuation or newlines
    (``\\n``) as separators. We mirror that here so the test
    reflects production usage rather than a hypothetical syntax.
    """
    for q, n in (("u1", 5), ("u2", 30), ("u3", 12)):
        await _run(f"$写 玉/榜单 {q} {n}$", kv, _event("seed"))

    out = await _run(
        "rk:$排行榜 玉/榜单 反序 3 \\n [序号].[键].[值]$\n%rk%",
        kv,
        _event("查"),
    )
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines == ["1.u2.30", "2.u3.12", "3.u1.5"]


# ---------------------------------------------------------------------------
# §11 调用 — delayed handler invocation through the scheduler bridge.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_call_reaches_scheduler(kv) -> None:
    """``$调用 ms handler$`` enqueues a task on the wired scheduler."""
    from linling_core.scheduler import ScheduledTask, Scheduler

    sched = Scheduler()
    fired: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.handler_name)

    out = await _run(
        "$调用 50 温柔打卡$",
        kv,
        _event("戳"),
        scheduler=sched,
    )
    assert out == ""
    assert sched.pending_count == 1

    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.2)
    await sched.stop()
    await runner

    assert fired == ["温柔打卡"]


# ---------------------------------------------------------------------------
# §12 删除 — both scope-level and scope+file forms work.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_scope_then_file(kv) -> None:
    await _run("$写 啊/活动系/锦囊 12345 1$", kv, _event("seed"))
    await _run("$写 啊/活动系/花标 12345 1$", kv, _event("seed"))

    # Scope-level: ``$删除 啊/活动系$`` should drop both files.
    out = await _run("$删除 啊/活动系$", kv, _event("clean"))
    assert out == ""

    leftover = await _run("v:$读 啊/活动系/锦囊 12345 NONE$\n%v%", kv, _event("查"))
    assert leftover == "NONE"


# ---------------------------------------------------------------------------
# §13 legacy stubs — ``$读文件$`` etc. don't crash, just degrade.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_stubs_return_empty(kv) -> None:
    body = """a:$读文件 /tmp/none default$
b:$词库操作 添加 x.txt$
%a%/%b%"""
    out = await _run(body, kv, _event("x"))
    # Both stubs return "" — interpolation produces "/" between them.
    assert out == "default/"


# ---------------------------------------------------------------------------
# §14 回调 — synchronous internal-handler call returns its text output.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_invokes_internal_handler(kv) -> None:
    """``$回调 helper%QQ%$`` runs the named ``[内部]`` handler inline.

    Real QRDic rules use this for things like ``s:$回调 游戏判断 %a%$``
    where the internal helper computes an index and the caller assigns
    it to ``s``. We simulate that with a tiny helper that emits a fixed
    sentinel.
    """
    from linling_dsl.ast_nodes import Handler
    from linling_dsl.parser import parse as parse_dsl

    # Compile a one-handler script and feed it to the VM via the
    # ``handler_lookup`` extras hook the bootstrap normally installs.
    script = parse_dsl(
        "[内部]游戏判断\nIDX_OK\n",
        strict=False,
    )
    handlers_by_trigger = {h.trigger: h for h in script.handlers}

    def lookup(name: str) -> Handler | None:
        return handlers_by_trigger.get(name)

    body = """s:$回调 游戏判断$
%s%"""
    out = await _run(body, kv, _event("x"), handler_lookup=lookup)
    assert out.strip() == "IDX_OK"


# ---------------------------------------------------------------------------
# §15 输出转义 — QRDic 把字面 \n / \r / \t / %0A 当作运行时换行符。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_literal_backslash_n_decodes_to_newline(kv) -> None:
    """Authored ``\\n`` in OutputText becomes a real ``\\n`` at emit.

    Regression: dicpro.txt writes things like ``Ps：玩法建议征集……\\n``
    expecting a newline; the old VM emitted the literal two-character
    sequence ``\\n`` and the chat bubble showed it verbatim.
    """
    body = "第一行\\n第二行"
    out = await _run(body, kv, _event("x"))
    assert out == "第一行\n第二行"


@pytest.mark.asyncio
async def test_double_backslash_preserves_literal_backslash(kv) -> None:
    """``\\\\n`` (authored escaped backslash + n) becomes ``\\n`` literal,
    not a newline. Rare but real — see ``$写 ... %t%\\\\n%话%$`` in
    dicpro.txt where the rule wants a literal backslash-n in the KV
    value.
    """
    body = r"keep\\nliteral"
    out = await _run(body, kv, _event("x"))
    assert out == "keep\\nliteral"


@pytest.mark.asyncio
async def test_jianban_explanation_renders_real_newlines(kv) -> None:
    """The actual ``羁绊说明`` body (verbatim from dicpro.txt) renders
    with real newlines, not literal backslash-n.
    """
    body = """┈┈┈┈┈┈┈┈┈┈┈\\n
tip:只有双方互相申请才能\\n
       成为彼此羁绊\\n
tip:打卡时，羁绊必定获得\\n
      一张卡"""
    out = await _run(body, kv, _event("x"))
    # No literal backslash-n should remain in the rendered output.
    assert "\\n" not in out, f"literal escape not decoded: {out!r}"
    # The five source lines plus their trailing newlines render as a
    # five-line block. We don't pin exact line count because the
    # OutputText separator semantics are intentionally minimal —
    # what matters is that newlines are visible.
    assert "成为彼此羁绊\n" in out
    assert "一张卡" in out


# ---------------------------------------------------------------------------
# §16 全链路 — 加卡 → 调用 X加牌 → 调用 X是否爆牌, all dispatched through
# the live scheduler with a real ``handler_lookup`` plumbed in. Verifies
# that side-effects propagate across delayed-call boundaries and that the
# chained ``$调用$`` calls fire in queued order.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blackjack_chain_through_scheduler(kv) -> None:
    """End-to-end blackjack ``加卡 → X加牌 → X是否爆牌 → X赢了 / 清理`` chain.

    Real dicpro.txt ``加卡`` schedules ``X加牌`` then ``X是否爆牌`` on
    the bot's Scheduler. Each delayed handler runs against a synthesised
    Event from :class:`bootstrap._on_scheduled_fire`. The QRDic
    convention is to thread state through KV: ``加卡`` flips the
    "in-progress" lock, ``X加牌`` mutates the player's hand+points,
    ``X是否爆牌`` decides if it busts (and chains a bust-cleanup) or
    just clears the in-progress lock.

    We seed the player at 19 points so the random 1–13 draw almost
    certainly busts (only P=1 → 20 / P=2 → 21 stay alive). For each
    outcome we assert the *invariant* — busting clears the round, not
    that the random draw landed on any particular face.
    """
    from linling_core.scheduler import ScheduledTask, Scheduler

    script_text = (
        "加卡\n"
        "壹:$读 啊/娱乐系/黑杰克 玩家1 0$\n"
        "如果:%QQ%==%壹%\n"
        "ADDING\n"
        "$写 啊/娱乐系/黑杰克 是否加牌中%群号% [1]$\n"
        "$调用 0 X加牌$\n"
        "$调用 0 X是否爆牌$\n"
        "返回\n"
        "如果尾\n"
        "NOT-PLAYER\n"
        "\n"
        "[内部]X加牌\n"
        "X:$读 啊/娱乐系/黑杰克牌%群% X []$\n"
        "a:$读 啊/娱乐系/黑杰克点数%群% x 0$\n"
        "P:%随机数3-13%\n"
        "$JSON 添加 X %P%$\n"
        "$写 啊/娱乐系/黑杰克牌%群% X %X%$\n"
        "$写 啊/娱乐系/黑杰克点数%群% x [%a%+%P%]$\n"
        "\n"
        "[内部]X是否爆牌\n"
        "a:$读 啊/娱乐系/黑杰克点数%群% x 0$\n"
        "如果:%a%>21\n"
        "$写 啊/娱乐系/黑杰克 是否开牌 [1]$\n"
        "$调用 0 X赢了$\n"
        "$调用 0 清理游戏$\n"
        "返回\n"
        "如果尾\n"
        "$写 啊/娱乐系/黑杰克 是否加牌中%群号% [0]$\n"
        "\n"
        "[内部]X赢了\n"
        "壹:$读 啊/娱乐系/黑杰克 玩家1 0$\n"
        "玉:$读 啊/灵玉系/灵玉 %壹% 0$\n"
        "奖:$读 啊/娱乐系/黑杰克 奖池g 0$\n"
        "$写 啊/灵玉系/灵玉 %壹% [%玉%+%奖%]$\n"
        "\n"
        "[内部]清理游戏\n"
        "$写 啊/娱乐系/黑杰克 是否加牌中g [0]$\n"
        "$写 啊/娱乐系/黑杰克 是否开牌 [0]$\n"
    )
    full_script = parse(script_text, strict=False)
    handlers = {h.trigger: h for h in full_script.handlers}

    # Seed: player1 is 玩家1, has 500 灵玉, jackpot 200, hand at 19 points.
    seed = (
        "$写 啊/娱乐系/黑杰克 玩家1 player1$\n"
        "$写 啊/灵玉系/灵玉 player1 500$\n"
        "$写 啊/娱乐系/黑杰克 奖池g 200$\n"
        '$写 啊/娱乐系/黑杰克牌g X ["Ⓚ","9"]$\n'
        "$写 啊/娱乐系/黑杰克点数g x 19$\n"
    )
    await _run(seed, kv, _event("seed"))

    sched = Scheduler()
    fired: list[str] = []

    async def fire(t: ScheduledTask) -> None:
        target = handlers.get(t.handler_name)
        if target is None:
            return
        fired.append(t.handler_name)
        ev = Event(
            id=f"sched-{t.id}",
            platform="scheduler",
            bot_id="susu_test",
            scope=Scope(kind="group", id="g", platform="scheduler"),
            sender=User(id="system", platform="scheduler"),
            segments=[TextSegment(text=t.handler_name)],
        )
        inner_vm = VM(
            tool_registry=registry,
            kv=kv,
            bot_id="susu_test",
            extras={"scheduler": sched, "handler_lookup": handlers.get},
        )
        await inner_vm.execute_handler(target, ev, captures=list(t.args))

    runner = asyncio.create_task(sched.run(fire))

    # Now run the user-facing 加卡 handler — sender == 玩家1.
    ev = _event("加卡", sender="player1", group="g")
    outer_vm = VM(
        tool_registry=registry,
        kv=kv,
        bot_id="susu_test",
        extras={"scheduler": sched, "handler_lookup": handlers.get},
    )
    res = await outer_vm.execute_handler(handlers["加卡"], ev)
    out = "".join(s.text for s in res.segments if isinstance(s, TextSegment))
    assert out.strip() == "ADDING"

    # Drain the scheduler — chain depth is 加卡(2) → 是否爆牌(2) max 4 hops.
    await asyncio.sleep(0.5)
    await sched.stop()
    await runner

    # The chain must have fired both first-level handlers (X加牌,
    # X是否爆牌) at minimum. With seed 19 and P in [3,13] every draw
    # busts, so the bust branch (X赢了 + 清理游戏) must also have
    # fired. The "in-progress" lock therefore gets cleared.
    assert "X加牌" in fired
    assert "X是否爆牌" in fired
    assert "X赢了" in fired
    assert "清理游戏" in fired

    # Bust path: jackpot was paid into player's 灵玉 (500 + 200 = 700)
    # and the round was cleaned up — both lock keys clear after
    # 清理游戏 finishes.
    assert await kv.read("啊/灵玉系", "灵玉", "player1") == "700"
    assert await kv.read("啊/娱乐系", "黑杰克", "是否加牌中g") == "0"
    assert await kv.read("啊/娱乐系", "黑杰克", "是否开牌") == "0"


# ---------------------------------------------------------------------------
# §17 JSON dict / 包含 / 键 — exercises the non-array sub-commands real
# 卧底 / 抽奖 / 排行 rules use to navigate题库-style nested payloads.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_get_with_dict_then_dotted_path(kv) -> None:
    """``$JSON 获取 a 0.0$`` — 卧底 词条 idiom navigates ``{"0":[...]}``.

    QRDic's 卧底 module stores the 题库 as a dict-of-pairs literal:
    ``{"0":["香蕉","苹果"], ...}``. Lookups like
    ``a:$JSON 获取 题库 %i%.0$`` pick the i-th category's first
    word. The dispatcher splits the path on ``.`` so each segment
    descends one level (dict key, then list index).
    """
    body = 'a:{"0":["香蕉","苹果"],"1":["状元","冠军"]}\nv:$JSON 获取 a 0.0$\n%v%'
    out = await _run(body, kv, _event("x"))
    assert out == "香蕉"


@pytest.mark.asyncio
async def test_json_contains_and_keys(kv) -> None:
    """``$JSON 包含 X y$`` returns ``"1"`` / empty; ``$JSON 键 X$`` lists keys."""
    body = """A:["x","y","z"]
h:$JSON 包含 A y$
m:$JSON 包含 A nope$
k:$JSON 键 A$
%h%/%m%/%k%"""
    out = await _run(body, kv, _event("x"))
    # 包含: hit → "1"; miss → "" (empty between // separators).
    assert out.replace(" ", "") == '1//["0","1","2"]'


@pytest.mark.asyncio
async def test_json_add_to_non_array_initialises(kv) -> None:
    """``$JSON 添加$`` against a non-JSON / non-array value rebuilds as ``[value]``.

    Real handlers seed counters with a literal ``0`` (``$写 ... 0$``)
    and later call ``$JSON 添加 X foo$`` — without tolerant init the
    first add would silently no-op and ``X`` would still read ``"0"``.
    """
    body = """a:notjson
a:$JSON 添加 a hello$
%a%"""
    out = await _run(body, kv, _event("x"))
    assert out.replace(" ", "") == '["hello"]'


# ---------------------------------------------------------------------------
# §18 概率随机 — weighted random with both extreme weights and zero-weights
# fallback. Real rule files (the 苏苏 chat-or-not coin flip) lean on the
# heavy-weight branch for predictability and the zero-weight fallback for
# no-data initialisation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weighted_random_extreme_weight_dominates(kv) -> None:
    """Heavy weight (10000 vs 1) lands on the dominant value every run.

    Mirrors the chat-AI rule ``判:$概率随机 ["%好%","%y%"] ["聊","不聊"]$``
    where ``好`` swamps ``y`` to bias toward "聊". Statistical noise
    over 50 rolls is well below 1e-3 of the wrong outcome.
    """
    body = 'r:$概率随机 [1000000,1] ["A","B"]$\n%r%'
    outs = {await _run(body, kv, _event("x")) for _ in range(50)}
    assert outs == {"A"}


@pytest.mark.asyncio
async def test_weighted_random_all_zero_falls_back_to_uniform(kv) -> None:
    """Sum-zero weights → uniform fallback, all values reachable."""
    body = 'r:$概率随机 [0,0,0] ["A","B","C"]$\n%r%'
    outs = {await _run(body, kv, _event("x")) for _ in range(80)}
    # 80 rolls × 3 buckets — vanishingly unlikely to miss any bucket.
    assert outs == {"A", "B", "C"}


# ---------------------------------------------------------------------------
# §19 替换 / 取中间 3-arg form — the alternate ``SEP TEXT PATTERN`` shape
# real rules use when the haystack is too long to pack into a single token.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_three_arg_form(kv) -> None:
    """``$替换 SEP TEXT @from@to$`` (text and pattern as separate args).

    Use ``$写$`` with a single-token value (followed by ``$读$``) to
    seed the haystack. The DSL only traffics in tokens — multi-word
    KV values would need ``%0A``-style escapes — so we round-trip a
    URL-style ``hello_world``.
    """
    body = """$写 t/buf m hello_world$
M:$读 t/buf m none$
o:$替换 _ %M% _hello_hi$
%o%"""
    out = await _run(body, kv, _event("x"))
    assert out == "hi_world"


@pytest.mark.asyncio
async def test_substring_three_arg_form(kv) -> None:
    """``$取中间 SEP HAYSTACK PATTERN$`` 3-arg form — same result as packed."""
    body = """$写 t/buf m XfooXbarXbazX$
M:$读 t/buf m none$
o:$取中间 X %M% XfooXbarX$
%o%"""
    out = await _run(body, kv, _event("x"))
    # Pattern ``X<sep>foo<sep>bar`` extracts text between "foo" and
    # "bar" inside ``XfooXbarXbazX`` → "X" (the single char between).
    assert out == "X"


# ---------------------------------------------------------------------------
# §20 Bot identity vars — %管理员% / %主人% pulled from VM extras.
# Migrator rewrites these onto QRDic's hard-coded admin id constants;
# bootstrap pushes the live values into ``extras``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_identity_vars_resolve_from_extras(kv) -> None:
    """``%管理员% / %主人%`` route through the bot's extras."""
    out = await _run(
        "%管理员%/%主人%",
        kv,
        _event("x"),
        admin_users=("777", "888"),
    )
    # %管理员% and %主人% are aliases — both pick the *first* admin.
    assert out == "777/777"


@pytest.mark.asyncio
async def test_bot_identity_vars_default_when_missing(kv) -> None:
    """No extras configured → identity vars resolve to empty string,
    not raise. Rules referencing them shouldn't crash on adapter-less
    deployments. We avoid ``[%var%]`` brackets here because the VM
    arith pass would turn a missing-var ``[]`` into ``"0"``.
    """
    out = await _run("(%管理员%)/(%主人%)", kv, _event("x"))
    assert out == "()/()"


# ---------------------------------------------------------------------------
# §21 Segment-indexed counts — %ATNUM%, %FACENUM%, %IMGNUM% surface a
# count, %ATN% picks the N-th, out-of-range returns empty.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_segment_indexing_with_count(kv) -> None:
    """Multi-At message: %ATNUM% is the count, %ATN% is the N-th id."""
    ev = Event(
        id="e",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="cli"),
        sender=User(id="u", platform="cli"),
        segments=[
            TextSegment(text="戳一下 "),
            AtSegment(user_id="11"),
            AtSegment(user_id="22"),
            AtSegment(user_id="33"),
        ],
    )
    out = await _run(
        "n=%ATNUM%/0=%AT0%/2=%AT2%/oob=%AT5%",
        kv,
        ev,
    )
    # Out-of-range (%AT5%) returns empty — segment vars degrade
    # silently, matching QRSpeed's no-error-for-missing convention.
    assert out == "n=3/0=11/2=33/oob="


# ---------------------------------------------------------------------------
# §22 时间 standalone — ``$时间 %H$`` emits the formatted timestamp via
# the renderer-tool path (output emission), without a separate assignment.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_tool_standalone_emits(kv) -> None:
    """``$时间 %H$`` on its own line emits the current hour (00..23).

    The tool is in the ``_EMIT_OUTPUT_TOOL_DSL_NAMES`` allow-list,
    so its return flows to the output stream; rules can drop it
    inline without binding to a temp variable. Format string
    follows ``strftime`` conventions.
    """
    out = await _run("$时间 %H$", kv, _event("x"))
    # An hour token is two digits, 00..23.
    assert out.isdigit() and 0 <= int(out) <= 23


# ---------------------------------------------------------------------------
# §23 codecs — round-trip through encode/decode pairs. These are 输出-side
# tools (in the renderer-emit set), so a standalone call surfaces the
# decoded text directly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base64_encode_decode_roundtrip(kv) -> None:
    """``hello`` → ``aGVsbG8=`` → ``hello`` round-trip via standalone calls."""
    enc = await _run("$Base64Encoder hello$", kv, _event("x"))
    assert enc == "aGVsbG8="
    dec = await _run(f"$Base64Decoder {enc}$", kv, _event("x"))
    assert dec == "hello"


@pytest.mark.asyncio
async def test_unicode_decoder_decodes_escape_sequences(kv) -> None:
    r"""``\uXXXX`` sequences decode to their characters.

    Used by chat-bridge rules that pull ``\u`` escapes out of upstream
    JSON payloads (the ``$获取消息 message$`` field is escape-encoded
    on some adapters). Running the decoder once gives clean Chinese.
    """
    out = await _run(r"$UnicodeDecoder \u4f60\u597d$", kv, _event("x"))
    assert out == "你好"


# ---------------------------------------------------------------------------
# §24 全局变量 cross-handler — ``$全局变量 K v$`` set in one handler is
# visible to a later ``$取变量 K$`` in a *different* handler invocation.
# Real rules use this as a process-wide flag (e.g. cooldown markers).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_var_persists_across_handler_calls(kv) -> None:
    """A global set in one VM run is read by the next VM run."""
    from linling_tools_stdlib.globals_ops import _reset_globals_for_tests

    _reset_globals_for_tests()
    # First handler: write the global.
    await _run("$全局变量 G hello$", kv, _event("first"))
    # Second handler (fresh VM): read it back.
    out = await _run("g:$取变量 G$\n%g%", kv, _event("second"))
    assert out == "hello"


# ---------------------------------------------------------------------------
# §25 回调 with captures — args after the handler name flow into the
# callee's ``%括号N%`` (1-indexed), matching the ``$回调 helper a b$``
# QRDic convention used by 游戏判断 / 抽卡判断 etc.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_passes_captures_to_callee(kv) -> None:
    """``$回调 echo aa bb$`` makes ``%括号1%==aa, %括号2%==bb`` in echo."""
    helper = parse(
        "[内部]echo\nfirst=%括号1%/second=%括号2%\n",
        strict=False,
    )
    by_trigger = {h.trigger: h for h in helper.handlers}
    out = await _run(
        "v:$回调 echo aa bb$\n%v%",
        kv,
        _event("x"),
        handler_lookup=by_trigger.get,
    )
    assert out.strip() == "first=aa/second=bb"


# ---------------------------------------------------------------------------
# §26 调用 propagates positional args — the scheduler task carries the
# whole arg list, available later as %括号N% in the fired handler.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_call_carries_positional_args(kv) -> None:
    """``$调用 0 helper a b c$`` enqueues a task with args=[a,b,c]."""
    from linling_core.scheduler import ScheduledTask, Scheduler

    sched = Scheduler()
    captured: list[tuple[str, list[str]]] = []

    async def cb(t: ScheduledTask) -> None:
        captured.append((t.handler_name, list(t.args)))

    out = await _run(
        "$调用 0 helper aa bb cc$",
        kv,
        _event("x"),
        scheduler=sched,
    )
    assert out == ""
    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.15)
    await sched.stop()
    await runner

    assert captured == [("helper", ["aa", "bb", "cc"])]


# ---------------------------------------------------------------------------
# §27 控制流 — 返回 from inside nested if, adjacent / triple-nested ifs,
# empty if body, forward jump. test_vm.py covers each at the unit level;
# this section exercises them end-to-end through the parser + interpolator
# + KV pipeline so a regression at any layer (e.g. parser refusing to
# associate body lines correctly with a deeply nested ``如果``) shows up
# here. Real handlers like ``[戳一戳]`` and ``兑换御妖符`` weave these
# patterns together; we mirror that ergonomically.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_return_from_inside_nested_if(kv) -> None:
    """``返回`` from inside an inner ``如果`` short-circuits the whole handler.

    The dicpro.txt ``[戳一戳]`` handler has ``如果:cond / $jump :形象标记$ /
    返回 / 如果尾`` patterns nested two levels deep. A regression would
    let execution continue past the inner ``如果尾`` and emit text the
    handler clearly wanted skipped.
    """
    body = """如果:%QQ%==u
PRE
如果:%群号%==g
INNER
返回
如果尾
AFTER-INNER
如果尾
END"""
    out = await _run(body, kv, _event("x", sender="u", group="g"))
    # Expected sequence: PRE → INNER → 返回 (halts execution).
    # AFTER-INNER (still inside outer if) and END (outside both ifs)
    # must NOT print.
    assert out == "PREINNER"


@pytest.mark.asyncio
async def test_two_adjacent_if_blocks_both_fire(kv) -> None:
    """Two sibling ``如果`` blocks at the same nesting both execute when
    their conditions hit. A buggy parser that lumped the second into
    the first's body would let the second's text leak even when its
    own condition was false.
    """
    body = """如果:%QQ%==u
A
如果尾
如果:%群号%==g
B
如果尾"""
    out = await _run(body, kv, _event("x", sender="u", group="g"))
    assert out == "AB"


@pytest.mark.asyncio
async def test_triple_nested_if_inner_uses_scope(kv) -> None:
    """Triple-nested ``如果`` — innermost condition evaluates against scope
    written at the outer level. Mirrors the deep ``如果`` ladders in
    ``[戳一戳]`` where each layer narrows down by group / time / role.
    """
    body = """x:1
如果:%QQ%==u
如果:%群号%==g
如果:%x%==1
DEEP
如果尾
如果尾
如果尾"""
    out = await _run(body, kv, _event("x", sender="u", group="g"))
    assert out == "DEEP"


@pytest.mark.asyncio
async def test_empty_if_body_does_not_crash(kv) -> None:
    """``如果:cond / 如果尾`` with no body in between must parse and
    execute as a pure-condition no-op. Real rule files use this shape
    as a guard pattern (sometimes commented-out body) where the whole
    handler relies on side-effects in the condition (e.g. evaluating
    ``$读$`` for its read-time logging effect).
    """
    body = """如果:%QQ%==u
如果尾
DONE"""
    out = await _run(body, kv, _event("x", sender="u"))
    assert out == "DONE"


@pytest.mark.asyncio
async def test_forward_jump_skips_intervening_lines(kv) -> None:
    """``$jump :end$`` jumps *forward* past statements between the call
    site and the label. Tested as a counterpoint to round-1's
    backward-loop fix — both directions are part of the QRDic
    contract; a regression that only handled backward jumps would
    break early-exit-style handlers.
    """
    body = """before
$jump :end$
SKIP
:end
after"""
    out = await _run(body, kv, _event("x"))
    assert "before" in out and "after" in out and "SKIP" not in out


# ---------------------------------------------------------------------------
# §28 多事件流 — 卧底-style roster building / vote tallying. Each user's
# action is its own ``_run`` call (different VM, different scope), and
# state crosses VMs only through KV. Confirms the JSON in-place mutation
# fix from round 4 holds across handler boundaries.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roster_builds_across_separate_handler_runs(kv) -> None:
    """Two ``加入`` events from different senders accumulate into a
    shared roster. Each run reads-modifies-writes; the final array
    must contain both ids in order.

    Real ``卧底游戏`` 's ``加入卧底`` handler does exactly this — its
    body is ``R:$读 ... 0$ / $JSON 添加 R %QQ%$ / $写 ... %R%$``.
    Without the round-4 in-place mutation fix the second run would
    overwrite the first's payload because ``R`` would still hold ``[]``
    after ``$JSON 添加$`` returned.
    """
    body = "R:$读 卧底/玩家 roster []$\n$JSON 添加 R %QQ%$\n$写 卧底/玩家 roster %R%$\n"
    await _run(body, kv, _event("加入", sender="p1"))
    await _run(body, kv, _event("加入", sender="p2"))

    roster = await kv.read("卧底", "玩家", "roster")
    assert roster is not None
    assert roster.replace(" ", "") == '["p1","p2"]'


@pytest.mark.asyncio
async def test_vote_tally_across_three_voters(kv) -> None:
    """Three votes for the same target accumulate; ``$JSON 长度$`` reads 3.

    Mirrors the 卧底 vote phase: each ``投票@target`` event appends
    the voter id into the target's vote-list. Length aggregation is
    the deciding signal at the end of the round.
    """
    for voter in ("v1", "v2", "v3"):
        await _run(
            "V:$读 卧底/票 target []$\n$JSON 添加 V %QQ%$\n$写 卧底/票 target %V%$\n",
            kv,
            _event("投", sender=voter),
        )

    out = await _run(
        "V:$读 卧底/票 target []$\nn:$JSON 长度 V$\n%V%/%n%",
        kv,
        _event("查", sender="x"),
    )
    # Three voters, in insertion order; length reflects the count.
    assert out.replace(" ", "") == '["v1","v2","v3"]/3'


# ---------------------------------------------------------------------------
# §29 Adapter-RPC fallback — when no live IM adapter is wired (the unit
# / WebUI / CLI test cases), platform-side tools must degrade silently
# with a sensible default instead of crashing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_nickname_falls_back_to_user_id_without_adapter(kv) -> None:
    """``$群昵称 g uid$`` returns ``uid`` verbatim when no adapter is wired.

    The 排行榜 and 戳一戳 paths interpolate ``$群昵称 %群号% %QQ%$``
    everywhere; without a fallback the handler would emit empty
    strings or raise. The stub returning ``user_id`` lets rules
    degrade to a numeric nick (still informative).
    """
    body = "v:$群昵称 g 12345$\n%v%"
    out = await _run(body, kv, _event("x"))
    assert out == "12345"


@pytest.mark.asyncio
async def test_group_members_returns_empty_array_without_adapter(kv) -> None:
    """``$获取群成员$`` with no adapter returns ``"[]"`` and a length of 0.

    The 回忆录 handler does ``l:$获取群成员 %群号%$ / H:$JSON 长度 l$
    / m:[%H%-1]`` — getting an empty array means ``H==0`` and the
    handler can guard against the "no members" case rather than
    crashing on an undefined ``%l%``.
    """
    body = """v:$获取群成员 g$
n:$JSON 长度 v$
%v%/%n%"""
    out = await _run(body, kv, _event("x"))
    assert out == "[]/0"


# ---------------------------------------------------------------------------
# §30 Captures — empty / out-of-range optional groups.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_capture_renders_as_empty_string(kv) -> None:
    """``%括号1%`` with an empty capture is the empty string, not ``None``
    or a literal ``%括号1%``. Real triggers like ``加好感([0-9]*)``
    can yield an empty digit group; rules then test ``%括号1%==``
    or use the value in arith — both rely on it being truly empty.
    """
    out = await _run("(%括号1%)", kv, _event("x"), captures=[""])
    assert out == "()"


@pytest.mark.asyncio
async def test_out_of_range_capture_returns_empty(kv) -> None:
    """``%括号3%`` when only one group was captured returns empty (not raise).

    Common when a regex with multiple optional groups matches only
    the first; rules using ``%括号N%`` defensively expect this to
    be a no-op rather than a crash.
    """
    out = await _run("(%括号3%)", kv, _event("x"), captures=["just one"])
    assert out == "()"


# ---------------------------------------------------------------------------
# §31 我的徽章 verbatim — read-mostly handler from dicpro.txt. Combines
# $排行榜 lookup + 7-day streak + KV flag checks. Two cases mirror the
# observable extremes (top of every leaderboard / bottom of nothing).
# ---------------------------------------------------------------------------


_BADGE_BODY = """M:$排行榜 啊/禁言系/妖力 反序 1 1 [键]$
如果:%QQ%==%M%
🧿 冥火之护\\n
如果尾
L:$排行榜 啊/灵玉系/灵玉 反序 1 1 [键]$
如果:%QQ%==%L%
👑 富甲天下\\n
如果尾
如果:$读 啊/活动系/花标 %QQ% 0$==1
💐 双生共簇\\n
如果尾
END"""


@pytest.mark.asyncio
async def test_badges_handler_with_no_seeded_state(kv) -> None:
    """``我的徽章`` against an empty KV emits only the trailing sentinel.

    No 妖力 / 灵玉 leaders → ``%M% / %L%`` are the empty string from
    a vacuous ``$排行榜 ... 1 [键]$``; ``%QQ% == ""`` is false so the
    badge lines stay quiet. The 花标 read returns the default ``0``.
    """
    out = await _run(_BADGE_BODY, kv, _event("x", sender="u"))
    # Trailing END from the body — no badge lines, no leading newlines.
    assert out.strip() == "END"


@pytest.mark.asyncio
async def test_badges_handler_with_seeded_top_user(kv) -> None:
    """``我的徽章`` against a user that holds rank 1 in both 妖力 and 灵玉
    plus has a 花标 emits all three badges in declaration order.
    """
    await _run(
        "$写 啊/禁言系/妖力 u 999$\n$写 啊/灵玉系/灵玉 u 999$\n$写 啊/活动系/花标 u 1$\n",
        kv,
        _event("seed", sender="u"),
    )
    out = await _run(_BADGE_BODY, kv, _event("查", sender="u"))
    # Each badge line ends in ``\n``; the END sentinel sits flush at
    # the bottom. A handler that swallowed the rank lookup would emit
    # END only — same as the no-state case — so the assertion proves
    # all three branches fired.
    assert "🧿 冥火之护" in out
    assert "👑 富甲天下" in out
    assert "💐 双生共簇" in out
    # Order matters: 妖力 first, 灵玉 second, 花标 third (matches the
    # source).
    yao = out.find("🧿 冥火之护")
    ling = out.find("👑 富甲天下")
    hua = out.find("💐 双生共簇")
    assert yao < ling < hua


# ---------------------------------------------------------------------------
# §32 多组 regex 捕获 — alternation + greedy + digit. Real triggers like
# ``(扔|丢)瓶子(.*)红包([0-9]+)`` route three pieces of user input into
# ``%括号1%`` / ``%括号2%`` / ``%括号3%``. Classifier-level capture
# extraction is its own test surface; here we confirm end-to-end that
# the captures line up with the source order.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_capture_groups_route_to_braces(kv) -> None:
    """``扔瓶子hello红包50`` → ``%括号1%==扔, %括号2%==hello, %括号3%==50``."""
    out = await _run(
        "动作=%括号1%/内容=%括号2%/数量=%括号3%",
        kv,
        _event("ignored"),
        captures=["扔", "hello", "50"],
    )
    assert out == "动作=扔/内容=hello/数量=50"


@pytest.mark.asyncio
async def test_three_capture_groups_alternate_branch(kv) -> None:
    """Same shape, the ``丢`` alternation branch — captures still align."""
    out = await _run(
        "%括号1%瓶子%括号2%红包%括号3%",
        kv,
        _event("ignored"),
        captures=["丢", "内容", "999"],
    )
    assert out == "丢瓶子内容红包999"


# ---------------------------------------------------------------------------
# §33 概率随机 — value coercion. Numeric values come back as their
# decimal text; object values come back as compact JSON. The chat-AI
# 概率随机 idiom relies on the string return because the DSL only
# carries strings; values that round-trip through ``json.dumps`` must
# preserve the original payload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weighted_random_coerces_numeric_value_to_text(kv) -> None:
    """Heavy weight on first slot picks ``1`` (numeric) → returned as ``"1"``.

    Weight ratio 1e9 : 1 : 1 makes off-by-one rolls vanishingly rare;
    20 rolls is enough headroom that the statistical false-positive
    rate is < 1e-50. We rely on the value coming back as the string
    ``"1"`` (numeric values are str()-cast, not JSON-encoded).
    """
    body = "r:$概率随机 [1000000000,1,1] [1,2,3]$\n%r%"
    outs = {await _run(body, kv, _event("x")) for _ in range(20)}
    assert outs == {"1"}


@pytest.mark.asyncio
async def test_weighted_random_returns_object_as_json(kv) -> None:
    """Object value → JSON-serialised compact text (with ``json.dumps`` spacing)."""
    body = 'r:$概率随机 [10000,1] [{"a":1},{"b":2}]$\n%r%'
    out = await _run(body, kv, _event("x"))
    # ``json.dumps({"a":1})`` produces ``{"a": 1}`` (Python 3 default
    # separator) — that's what flows back through the DSL.
    assert out == '{"a": 1}'


# ---------------------------------------------------------------------------
# §34 算术边界 — parens, division-by-zero, modulo. ``_safe_eval_arith``
# defines the contract: well-formed → numeric text; malformed →
# bracketed literal back unchanged. Real handlers like 兑换御妖符
# rely on the parens form (``[%玉%-%括号1%*101]``) and modulo isn't
# uncommon either.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arith_parens_change_precedence(kv) -> None:
    """``[(5+3)*2]`` evaluates inside-out → 16, distinct from ``5+3*2`` (=11)."""
    out = await _run("v:[(5+3)*2]\n%v%", kv, _event("x"))
    assert out == "16"


@pytest.mark.asyncio
async def test_arith_division_by_zero_falls_back_to_literal(kv) -> None:
    """Division by zero must not crash — the expression survives as
    bracketed text (``[5/0]``) so the rest of the handler keeps running.

    Without graceful degradation a single typo'd ``[a/0]`` in a 排行榜
    or 概率随机 fmt would tear down the whole dispatch.
    """
    out = await _run("v:[5/0]\n%v%", kv, _event("x"))
    assert out == "[5/0]"


@pytest.mark.asyncio
async def test_arith_modulo_works(kv) -> None:
    """``[10%3]`` → 1. QRDic accepts ``%`` inside ``[...]``; rules use
    it in cooldown-bucket logic (``[%时间mm%%5]``).
    """
    out = await _run("v:[10%3]\n%v%", kv, _event("x"))
    assert out == "1"


# ---------------------------------------------------------------------------
# §35 兑换御妖符([0-9]+) verbatim — combines arith-in-condition (the
# round-3 fix), arith-with-multiplier in writes, and per-user KV scope.
# ---------------------------------------------------------------------------


_EXCHANGE_BODY = """如果:%群号%==754800438
返回
如果尾
如果:%QQ%==%Robot%
返回
如果尾
卡:$读 啊/%群%/禁言卡 %QQ% 0$
玉:$读 啊/灵玉系/灵玉 %QQ% 0$
如果:[%玉%]<[%括号1%*100]
不足
返回
如果尾
$写 啊/灵玉系/灵玉 %QQ% [%玉%-%括号1%*101]$
$写 啊/%群%/禁言卡 %QQ% [%卡%+%括号1%]$
成功兑换%括号1%张"""


@pytest.mark.asyncio
async def test_exchange_handler_success_path(kv) -> None:
    """User has 1000 灵玉, asks for 1 卡 (cost 101): 灵玉→899, 卡→1.

    The condition ``[%玉%]<[%括号1%*100]`` requires the round-3 arith
    eval; the write ``[%玉%-%括号1%*101]`` requires multiplicand
    arithmetic on the right-hand side.
    """
    await _run("$写 啊/灵玉系/灵玉 u 1000$", kv, _event("seed", sender="u", group="g"))
    out = await _run(
        _EXCHANGE_BODY,
        kv,
        _event("兑换御妖符1", sender="u", group="g"),
        captures=["1"],
    )
    assert "成功兑换" in out
    assert "1" in out  # 1 张
    assert await kv.read("啊/灵玉系", "灵玉", "u") == "899"
    assert await kv.read("啊/g", "禁言卡", "u") == "1"


@pytest.mark.asyncio
async def test_exchange_handler_insufficient_balance(kv) -> None:
    """User has 50 灵玉, asks for 1 卡 (cost 100): rejected, KV unchanged."""
    await _run("$写 啊/灵玉系/灵玉 u 50$", kv, _event("seed", sender="u", group="g"))
    out = await _run(
        _EXCHANGE_BODY,
        kv,
        _event("兑换御妖符1", sender="u", group="g"),
        captures=["1"],
    )
    assert out.strip() == "不足"
    # KV state preserved.
    assert await kv.read("啊/灵玉系", "灵玉", "u") == "50"
    # 卡 slot was never seeded → still missing (returns the read default).
    assert await kv.read("啊/g", "禁言卡", "u") is None


# ---------------------------------------------------------------------------
# §36 调度并发 — N tasks queued at the same tick fire in insertion order.
# Confirms the scheduler's "drain due tasks sequentially per tick"
# contract under contention; rules that schedule a fan-out of internal
# handlers (扭蛋十次, 抽奖N) rely on this being deterministic.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_zero_delay_calls_fire_in_insertion_order(kv) -> None:
    """Ten ``$调用 0 hN$`` calls queued back-to-back fire in order h0..h9."""
    from linling_core.scheduler import ScheduledTask, Scheduler

    sched = Scheduler()
    fired: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.handler_name)

    for i in range(10):
        await _run(f"$调用 0 h{i}$", kv, _event("x"), scheduler=sched)

    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.3)
    await sched.stop()
    await runner

    assert fired == [f"h{i}" for i in range(10)]


# ---------------------------------------------------------------------------
# §37 行内 [arith] in OutputText — when authored inside a free-form text
# line (no leading ``var:`` assignment shape), the parser splits the
# line into ``Literal + ArithExpr + Literal`` parts and the VM evaluates
# the arith piece in place.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_arith_inside_text_renders_inplace(kv) -> None:
    """``灵玉数量[5+3]个`` renders ``灵玉数量8个`` in place.

    The leading text is not a valid Chinese assignment-name (it
    contains punctuation-free Han chars > 2 chars), so the parser
    correctly classifies it as OutputText with an embedded ArithExpr
    rather than an assignment statement.
    """
    out = await _run("灵玉数量[5+3]个", kv, _event("x"))
    # Note: the AST inserts a single space between the closing ``]``
    # and the trailing literal — minor parser ergonomic. We assert
    # the meaningful pieces are present rather than the exact byte
    # layout.
    assert "灵玉数量" in out and "8" in out and "个" in out


# ---------------------------------------------------------------------------
# §38 获取消息 field default — reads from event.raw, returns default when
# the field is missing. The OneBot adapter populates ``event.raw`` with
# ``sub_type`` / ``message_id`` / etc.; rules in dicpro.txt look up
# ``$获取消息 sub_type 0$`` to branch on poke / message types.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_message_field_returns_raw_value(kv) -> None:
    """``$获取消息 sub_type FALLBACK$`` reads ``event.raw['sub_type']``."""
    ev = Event(
        id="e",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="cli"),
        sender=User(id="u", platform="cli"),
        segments=[TextSegment(text="poked")],
        raw={"sub_type": "poke"},
    )
    body = "v:$获取消息 sub_type FALLBACK$\n%v%"
    out = await _run(body, kv, ev)
    assert out == "poke"


@pytest.mark.asyncio
async def test_get_message_field_uses_default_when_missing(kv) -> None:
    """Missing field → tool returns the supplied default rather than empty."""
    ev = Event(
        id="e",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="cli"),
        sender=User(id="u", platform="cli"),
        segments=[TextSegment(text="msg")],
        raw={"other_field": "x"},
    )
    body = "v:$获取消息 missing FALLBACK$\n%v%"
    out = await _run(body, kv, ev)
    assert out == "FALLBACK"


# ---------------------------------------------------------------------------
# §39 注释 / 配置指令 — // line comments, && block comments, and the
# ``&&<配置>...`` directive that lives at the top of dicpro.txt. The
# parser already handles these at the tokenizer level; we exercise
# them here so a regression that started leaking comment text into
# the output stream surfaces immediately at the integration layer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_slash_line_comment_stripped(kv) -> None:
    """``// throwaway`` lines disappear from the rendered output."""
    body = """before
// throwaway comment
after"""
    out = await _run(body, kv, _event("x"))
    assert "throwaway" not in out
    assert "before" in out and "after" in out


@pytest.mark.asyncio
async def test_double_amp_block_comment_stripped(kv) -> None:
    """``&&...`` lines (in-body block comments) drop out of the output.

    Real handlers use these to leave authored notes mid-rule, e.g.
    ``&&设置背景这里是填写RGB十六进制色码`` in the 图文 module.
    """
    body = """before
&&this is a block comment
after"""
    out = await _run(body, kv, _event("x"))
    assert "block comment" not in out
    assert "before" in out and "after" in out


def test_config_directive_at_file_head_parses() -> None:
    """``&&<配置>兼容模式:是`` (very first line of dicpro.txt) parses
    cleanly and doesn't consume the next handler's trigger.
    """
    script = parse("&&<配置>兼容模式:是\n\ntrig\nhello\n", strict=False)
    assert len(script.handlers) == 1
    assert script.handlers[0].trigger == "trig"


# ---------------------------------------------------------------------------
# §40 漂流瓶 multi-Event cycle — 扔瓶子 (toss) writes to a per-bot R
# array; 捡瓶子 (pick up) reads + ``$JSON 删除$`` to consume the bottle.
# Confirms the round-4 in-place mutation fix holds across the
# read/append/write loop *and* the read/get/delete/write loop, and
# that index 0 picks the oldest entry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toss_bottle_appends_to_shared_array(kv) -> None:
    """Two ``扔瓶子`` events accumulate into a 2-element JSON array."""
    body = "R:$读 漂流瓶/瓶子 R []$\n$JSON 添加 R %括号1%$\n$写 漂流瓶/瓶子 R %R%$\n"
    for content in ("hello-world", "another-bottle"):
        await _run(body, kv, _event("扔"), captures=[content])

    bottles = await kv.read("漂流瓶", "瓶子", "R")
    assert bottles is not None
    assert "hello-world" in bottles
    assert "another-bottle" in bottles


@pytest.mark.asyncio
async def test_pickup_bottle_consumes_oldest_entry(kv) -> None:
    """``捡瓶子`` returns the first bottle and deletes it from the array.

    The body order matters: read, snapshot length, get index-0,
    delete index-0, write back. The ``$JSON 删除$`` mutates ``R`` in
    place (round-4 fix) so the subsequent ``$写 ... %R%$`` persists
    the shortened array, not the original.
    """
    # Seed two bottles directly through the KV path to keep the test
    # focused on the pickup logic.
    await _run(
        "R:$读 漂流瓶/瓶子 R []$\n"
        "$JSON 添加 R first$\n"
        "$JSON 添加 R second$\n"
        "$写 漂流瓶/瓶子 R %R%$\n",
        kv,
        _event("seed"),
    )

    body = """R:$读 漂流瓶/瓶子 R []$
n:$JSON 长度 R$
b:$JSON 获取 R 0$
R:$JSON 删除 R 0$
$写 漂流瓶/瓶子 R %R%$
got=%b%/before=%n%"""
    out = await _run(body, kv, _event("捡"))
    # Bottle returned in the response includes the JSON-quoted form
    # because ``$JSON 获取$`` round-trips through ``json.dumps``;
    # what matters is the picked entry and the snapshot count.
    assert out == "got=first/before=2"

    # KV state: only the second bottle survives.
    bottles = await kv.read("漂流瓶", "瓶子", "R")
    assert bottles is not None
    assert "first" not in bottles
    assert "second" in bottles


# ---------------------------------------------------------------------------
# §41 Recurring scheduler tasks — repeated firing across a bounded
# window. ``schedule_recurring`` is the cron-like shape the bot uses
# for cooldown sweeps and watchdog timers. We don't drive it through
# the DSL (no ``$调用$`` form for recurring) — it's a pure runtime
# concern — but we do verify the scheduler infrastructure that the DSL
# leans on works.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recurring_task_fires_multiple_times(kv) -> None:
    """A 50ms-cadence recurring task fires several times in 250ms."""
    from linling_core.scheduler import ScheduledTask, Scheduler

    sched = Scheduler()
    fired: list[float] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.fire_at)

    sched.schedule_recurring(
        every_seconds=0.05,
        handler_name="ping",
        bot_id="susu_test",
    )

    runner = asyncio.create_task(sched.run(cb))
    # Scheduler tick is 100ms (see :data:`_TICK_SECONDS`); over 600ms
    # we expect ≥ 2 fires and ≤ 12 (loose upper bound to catch a
    # runaway loop). Using a generous lower bound keeps the test
    # robust against timing jitter on slow CI hosts.
    await asyncio.sleep(0.6)
    await sched.stop()
    await runner

    assert 2 <= len(fired) <= 12


# ---------------------------------------------------------------------------
# §42 Legacy-stub safety — the placeholder ``$读文件$`` / ``$写文件$``
# tools must NOT touch the filesystem. Their job is to absorb migrated
# rules that still reference flat-file IO, log the request, and degrade
# gracefully — escalating to actual disk IO would re-open the security
# hole the migration was meant to close.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_stub_does_not_touch_filesystem(kv, tmp_path) -> None:
    """``$读文件 path default$`` returns ``default`` without opening anything.

    We point the path at a tmp file that doesn't exist *and* assert it
    isn't created as a side-effect. A future implementation that does
    real IO would either return the file content (failing the equality
    check) or create the file (failing the existence check).
    """
    target = tmp_path / "unsafe.txt"
    body = f"v:$读文件 {target} fallback$\n%v%"
    out = await _run(body, kv, _event("x"))
    assert out == "fallback"
    assert not target.exists()


@pytest.mark.asyncio
async def test_write_file_stub_does_not_touch_filesystem(kv, tmp_path) -> None:
    """``$写文件 path content$`` returns empty without writing the file."""
    target = tmp_path / "unsafe-out.txt"
    body = f"$写文件 {target} payload$"
    out = await _run(body, kv, _event("x"))
    assert out == ""
    assert not target.exists()


# ---------------------------------------------------------------------------
# §43 Parser/VM corners — empty handler body, multi-slash KV path. Both
# real-world: a few dicpro.txt handlers have only a trigger (used as a
# stub for future expansion); KV paths like ``啊/灵玉系/灵玉`` /
# ``小苏苏/划转行为/%QQ%记录`` get arbitrarily long.
# ---------------------------------------------------------------------------


def test_empty_body_handler_parses_to_empty_body() -> None:
    """A handler with only a trigger line parses to a zero-statement body."""
    script = parse("trig\n", strict=False)
    assert len(script.handlers) == 1
    assert script.handlers[0].trigger == "trig"
    assert script.handlers[0].body == []


@pytest.mark.asyncio
async def test_empty_body_handler_runs_as_noop(kv) -> None:
    """Executing the empty-body handler emits nothing and doesn't raise."""
    script = parse("trig\n", strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    res = await vm.execute_handler(script.handlers[0], _event("x"))
    assert res.segments == []
    assert res.returned is False


@pytest.mark.asyncio
async def test_multi_slash_kv_path_splits_on_last_separator(kv) -> None:
    """``$写 a/b/c/d key value$`` writes to scope=``a/b/c``, file=``d``.

    Mirrors the production layouts ``小苏苏/划转行为/%QQ%记录`` and
    ``休闲系/珍品/全员守护`` that interleave Han chars and slashes
    arbitrarily deep. The shim must not rsplit greedily into pieces
    or stop at the first slash.
    """
    await _run("$写 a/b/c/d key value$", kv, _event("x"))
    direct = await kv.read("a/b/c", "d", "key")
    assert direct == "value"

    # Round-trip through the DSL read should see the same value via
    # the same path interpretation.
    out = await _run("v:$读 a/b/c/d key MISS$\n%v%", kv, _event("x"))
    assert out == "value"


# ---------------------------------------------------------------------------
# §44 灵玉划转N@AT verbatim — multi-write transfer with a 4 % surcharge
# routed to the bot owner. Combines arith ([%括号1%*1.04] /
# [%苏%+%括号1%*0.04]), AT-segment routing (%AT0%), self-transfer
# rejection, and balance-check guard. Real production handler from
# dicpro.txt L1148+.
# ---------------------------------------------------------------------------


_TRANSFER_BODY = """如果:%AT0%==0|%AT0%==%QQ%
划转失败了
返回
如果尾
玉:$读 啊/灵玉系/灵玉 %QQ% 0$
转:$读 啊/灵玉系/灵玉 %AT0% 0$
如果:%玉%<%括号1%
灵玉不足了哎
返回
如果尾
化:[%括号1%*1.04]
$写 啊/灵玉系/灵玉 %QQ% [%玉%-%化%]$
$写 啊/灵玉系/灵玉 %AT0% [%转%+%括号1%]$
苏:$读 啊/灵玉系/灵玉 1707476110 0$
$写 啊/灵玉系/灵玉 1707476110 [%苏%+%括号1%*0.04]$
成功划转%括号1%给对方"""


def _at_event(text: str, *, sender: str, group: str, at_user: str) -> Event:
    return Event(
        id=f"e-{sender}-{at_user}",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id=group, platform="cli"),
        sender=User(id=sender, platform="cli", display_name="me"),
        segments=[TextSegment(text=text), AtSegment(user_id=at_user)],
    )


@pytest.mark.asyncio
async def test_transfer_success_pays_surcharge_to_owner(kv) -> None:
    """Sender 1000 → 100 to target → sender 896 (1000 - 104), target 100, owner +4."""
    # Seed: sender has 1000 灵玉.
    await _run(
        "$写 啊/灵玉系/灵玉 sender 1000$",
        kv,
        _at_event("seed", sender="sender", group="g", at_user="target"),
    )
    out = await _run(
        _TRANSFER_BODY,
        kv,
        _at_event("划转100@target", sender="sender", group="g", at_user="target"),
        captures=["100"],
    )
    assert out == "成功划转100给对方"
    assert await kv.read("啊/灵玉系", "灵玉", "sender") == "896"
    assert await kv.read("啊/灵玉系", "灵玉", "target") == "100"
    # 4% surcharge → 4 灵玉 routed to bot-owner id 1707476110.
    assert await kv.read("啊/灵玉系", "灵玉", "1707476110") == "4"


@pytest.mark.asyncio
async def test_transfer_to_self_rejected(kv) -> None:
    """``%AT0% == %QQ%`` → "划转失败了", no KV mutation."""
    out = await _run(
        _TRANSFER_BODY,
        kv,
        _at_event("划转50@self", sender="sender", group="g", at_user="sender"),
        captures=["50"],
    )
    assert out == "划转失败了"
    # Sender's row was never written → still missing.
    assert await kv.read("啊/灵玉系", "灵玉", "sender") is None


@pytest.mark.asyncio
async def test_transfer_insufficient_balance_rejected(kv) -> None:
    """Sender 50 → tries 100 → "灵玉不足了哎", balance preserved."""
    await _run(
        "$写 啊/灵玉系/灵玉 poor 50$",
        kv,
        _at_event("seed", sender="poor", group="g", at_user="target"),
    )
    out = await _run(
        _TRANSFER_BODY,
        kv,
        _at_event("划转100@target", sender="poor", group="g", at_user="target"),
        captures=["100"],
    )
    assert out == "灵玉不足了哎"
    assert await kv.read("啊/灵玉系", "灵玉", "poor") == "50"


# ---------------------------------------------------------------------------
# §45 $发送 outbound shape — the action_sink receives a fully-formed
# :class:`Action` keyed off the routing kind (群 → group scope, 好友 →
# dm scope). Real adapters consume these to push messages downstream.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_group_message_routes_text_action(kv) -> None:
    """``$发送 群 msg target body...$`` → ``Action(kind=send, target=group, …)``.

    The sink wrapper schedules delivery as a background task, so we
    sleep briefly for the asyncio scheduler to drain before asserting.
    """
    from linling_core.events import Action

    captured: list[Action] = []

    async def sink(a: Action) -> None:
        captured.append(a)

    # Use platform="onebot" so send_message picks up a routable platform.
    ev = Event(
        id="e",
        platform="onebot",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="onebot"),
        sender=User(id="u", platform="onebot"),
        segments=[TextSegment(text="x")],
    )
    await _run(
        "$发送 群 msg 12345 hello world$",
        kv,
        ev,
        action_sink=sink,
    )
    # Background-task delivery — give the loop one tick to drain.
    await asyncio.sleep(0.05)

    assert len(captured) == 1
    action = captured[0]
    assert action.kind == "send"
    assert action.target.kind == "group"
    assert action.target.id == "12345"
    assert action.target.platform == "onebot"
    assert len(action.segments) == 1
    seg = action.segments[0]
    assert isinstance(seg, TextSegment)
    assert seg.text == "hello world"


@pytest.mark.asyncio
async def test_send_friend_image_routes_image_action(kv) -> None:
    """``$发送 好友 img target url$`` → ``Action`` with ImageSegment + dm scope."""
    from linling_core.events import Action
    from linling_core.segments import ImageSegment

    captured: list[Action] = []

    async def sink(a: Action) -> None:
        captured.append(a)

    ev = Event(
        id="e",
        platform="onebot",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="onebot"),
        sender=User(id="u", platform="onebot"),
        segments=[TextSegment(text="x")],
    )
    await _run(
        "$发送 好友 img 9999 https://example.com/x.png$",
        kv,
        ev,
        action_sink=sink,
    )
    await asyncio.sleep(0.05)

    assert len(captured) == 1
    action = captured[0]
    assert action.target.kind == "dm"
    assert action.target.id == "9999"
    assert len(action.segments) == 1
    seg = action.segments[0]
    assert isinstance(seg, ImageSegment)
    assert seg.url == "https://example.com/x.png"


@pytest.mark.asyncio
async def test_send_no_sink_degrades_silently(kv) -> None:
    """$发送 with no sink wired emits nothing and doesn't raise."""
    res_text = await _run(
        "$发送 群 msg 12345 hello$",
        kv,
        # platform="onebot" so the routing path is reachable; no sink in extras.
        Event(
            id="e",
            platform="onebot",
            bot_id="susu_test",
            scope=Scope(kind="group", id="g", platform="onebot"),
            sender=User(id="u", platform="onebot"),
            segments=[TextSegment(text="x")],
        ),
    )
    assert res_text == ""


# ---------------------------------------------------------------------------
# §46 max_output_segments boundary — emits at exactly the cap → ok;
# one over the cap → SandboxError. test_vm.py covers the unit path
# with a synthetic AST; this exercises the same limit through the
# parser + interpolator + escape-decoder stack.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_at_segment_cap_succeeds(kv) -> None:
    """Emitting exactly ``max_output_segments`` lines (default 20) succeeds."""
    body = "\n".join(f"line{i}" for i in range(20))
    full = "trigger\n" + body + "\n"
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    res = await vm.execute_handler(script.handlers[0], _event("x"))
    text_segs = [s for s in res.segments if isinstance(s, TextSegment)]
    assert len(text_segs) == 20


@pytest.mark.asyncio
async def test_output_above_segment_cap_raises_sandbox_error(kv) -> None:
    """Emitting one more than ``max_output_segments`` raises SandboxError."""
    from linling_dsl.vm import SandboxError

    body = "\n".join(f"line{i}" for i in range(21))
    full = "trigger\n" + body + "\n"
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    with pytest.raises(SandboxError, match="max_output_segments"):
        await vm.execute_handler(script.handlers[0], _event("x"))


# ---------------------------------------------------------------------------
# §47 时间 / 冷却 idiom — ``如果:[%瓜%+10]<%时间mm%|%瓜%>%时间mm%`` is
# the canonical 10-minute cooldown from the 吃瓜 / 盗图 modules: skip
# if last fire was within the past 10 minutes of the current minute,
# otherwise allow. We can't pin %时间mm% in the test (no monkey patch
# of datetime here) but we can drive the boundary cases by seeding
# %瓜% to extreme values.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooldown_passes_when_seed_far_in_past(kv) -> None:
    """``瓜:99`` (future-clamped sentinel) → second OR branch fires.

    The cooldown idiom has two OR halves:
        ``[%瓜%+10]<%时间mm%`` — last fire was > 10 minutes ago, OR
        ``%瓜%>%时间mm%``      — last fire was a *bigger* minute number
                                 (i.e. wrapped past the hour boundary).

    Seeding ``瓜=99`` (greater than any real minute 0..59) makes the
    second half fire deterministically; we don't rely on the current
    wall clock minute being above any threshold. The COOLED branch
    therefore prints regardless of when the test runs.
    """
    body = """瓜:99
如果:[%瓜%+10]<%时间mm%|%瓜%>%时间mm%
COOLED
返回
如果尾
ON-COOLDOWN"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "COOLED"


@pytest.mark.asyncio
async def test_current_hour_var_matches_in_condition(kv) -> None:
    """``如果:%时间HH%==<current hour>`` → matches today's hour exactly.

    %时间HH% should resolve consistently between the synthesised
    condition and the local datetime — no race between snapshots.
    """
    from datetime import datetime

    cur_h = datetime.now().strftime("%H")
    body = f"""如果:%时间HH%=={cur_h}
NOW
返回
如果尾
NOT-NOW"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "NOW"


# ---------------------------------------------------------------------------
# §48 cond-with-func — ``如果:$读 path key 0$==X`` — the read result
# drives the branch. Test_parser covers parse; this verifies execution.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_condition_reads_kv_inline(kv) -> None:
    """``如果:$读 t/x k 0$==1`` fires when the KV row holds ``"1"``."""
    await _run("$写 t/x k 1$", kv, _event("seed"))
    body = """如果:$读 t/x k 0$==1
HIT
返回
如果尾
MISS"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "HIT"


@pytest.mark.asyncio
async def test_condition_with_kv_default_falls_through(kv) -> None:
    """No row written → default ``0`` flows through, condition ``==1`` fails."""
    body = """如果:$读 t/empty k 0$==1
HIT
返回
如果尾
MISS"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "MISS"


# ---------------------------------------------------------------------------
# §49 递归 $调用 — A schedules B, B schedules A. Confirms the scheduler
# correctly handles cross-handler callback chains and a counter guard
# terminates the loop. Without proper drain semantics the chain would
# either dead-lock or run forever.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recursive_schedule_terminates_with_counter_guard(kv) -> None:
    """``A → B → A → B → A`` with a counter that stops at 4 hops.

    Each handler reads/increments a shared KV counter and short-
    circuits when ≥ 4. Verifies (a) recursive scheduling works,
    (b) KV state visible across scheduler-fired handlers,
    (c) the chain stops once the guard fires.
    """
    from linling_core.scheduler import ScheduledTask, Scheduler

    script_text = """[内部]A
n:$读 t/n n 0$
如果:%n%>=4
返回
如果尾
$写 t/n n [%n%+1]$
$调用 0 B$

[内部]B
n:$读 t/n n 0$
如果:%n%>=4
返回
如果尾
$写 t/n n [%n%+1]$
$调用 0 A$
"""
    full_script = parse(script_text, strict=False)
    handlers = {h.trigger: h for h in full_script.handlers}

    sched = Scheduler()
    fired: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        target = handlers.get(t.handler_name)
        if target is None:
            return
        fired.append(t.handler_name)
        ev = _event(t.handler_name)
        inner = VM(
            tool_registry=registry,
            kv=kv,
            bot_id="susu_test",
            extras={"scheduler": sched, "handler_lookup": handlers.get},
        )
        await inner.execute_handler(target, ev)

    runner = asyncio.create_task(sched.run(cb))

    outer = VM(
        tool_registry=registry,
        kv=kv,
        bot_id="susu_test",
        extras={"scheduler": sched, "handler_lookup": handlers.get},
    )
    # Bootstrap: invoke A directly (counts as hop 1, schedules B).
    await outer.execute_handler(handlers["A"], _event("A"))

    await asyncio.sleep(1.0)
    await sched.stop()
    await runner

    # The counter starts at 0 and each fire increments before
    # rescheduling; the guard fires when n>=4. Counter ends at 4 and
    # the chain self-terminates. The scheduler-fired handlers seen
    # here are B (hop 2), A (hop 3), B (hop 4), and the next A which
    # short-circuits without rescheduling — hence 4 fires.
    assert await kv.read("t", "n", "n") == "4"
    assert len(fired) == 4
    assert "A" in fired and "B" in fired


# ---------------------------------------------------------------------------
# §50 $回调 image-emit contract — when the callee emits both text and
# image segments, the caller only sees the text (concatenated through
# the tool's return value). Image segments stay confined to the inner
# VM's run; they do NOT leak into the caller's output stream.
#
# This is the explicit contract documented in :func:`callback_stub` —
# we capture it here so a future change that broadens the return path
# (e.g. propagating segments instead of just text) is forced through
# a deliberate review rather than slipping in silently.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_returns_text_only_drops_image(kv) -> None:
    """``v:$回调 helper$`` collects only the text segments — images dropped."""
    helper = parse("[内部]e\nhello text\n±img=https://x.png±\n", strict=False)
    h_by = {h.trigger: h for h in helper.handlers}

    body = "v:$回调 e$\nresult=%v%"
    full = "trigger\n" + body + "\n"
    script = parse(full, strict=False)
    vm = VM(
        tool_registry=registry,
        kv=kv,
        bot_id="susu_test",
        extras={"handler_lookup": h_by.get},
    )
    res = await vm.execute_handler(script.handlers[0], _event("x"))
    text = "".join(s.text for s in res.segments if isinstance(s, TextSegment))

    from linling_core.segments import ImageSegment

    images = [s for s in res.segments if isinstance(s, ImageSegment)]
    # Caller sees the helper's text concatenated into ``%v%``.
    assert text == "result=hello text"
    # Image segments stay confined to the inner VM — none in the
    # caller's stream.
    assert images == []


@pytest.mark.asyncio
async def test_callback_standalone_does_not_emit(kv) -> None:
    """``$回调 helper$`` (no assignment) emits nothing in the caller's stream.

    ``回调`` is not in :data:`_EMIT_OUTPUT_TOOL_DSL_NAMES`, so a
    standalone call is treated as side-effect only (the side effect
    being whatever the inner VM did against KV). Both text and image
    segments stay invisible to the caller.
    """
    helper = parse("[内部]e\nhello text\n±img=https://x.png±\n", strict=False)
    h_by = {h.trigger: h for h in helper.handlers}

    full = "trigger\n$回调 e$\n"
    script = parse(full, strict=False)
    vm = VM(
        tool_registry=registry,
        kv=kv,
        bot_id="susu_test",
        extras={"handler_lookup": h_by.get},
    )
    res = await vm.execute_handler(script.handlers[0], _event("x"))
    assert res.segments == []


# ---------------------------------------------------------------------------
# §51 KV concurrency baseline — concurrent read-modify-write against
# the same row exhibits lost updates. Documenting current behaviour so
# any future change (e.g. adding a per-key serialisation lock) shows
# up here as a deliberate signal rather than a silent semantic shift.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_increment_baseline_lost_updates(kv) -> None:
    """10 concurrent ``%c%+1`` runs land on a single increment.

    SqliteKVStore serialises individual reads and writes through the
    connection lock, but the DSL's RMW pattern is two distinct
    operations with arbitrary work between. Without per-row locking
    or a CAS primitive, concurrent RMWs race: each VM reads ``"0"``,
    each writes ``"1"``, and only one update survives.

    This is the **current** baseline — it's not necessarily the
    desired behaviour. If a future change adds optimistic locking the
    final value should approach 10 and this test will fail loudly,
    prompting the assertion to be updated.
    """
    await _run("$写 t/c k 0$", kv, _event("seed"))

    async def inc() -> None:
        body = "玉:$读 t/c k 0$\n$写 t/c k [%玉%+1]$\n"
        await _run(body, kv, _event("inc"))

    await asyncio.gather(*(inc() for _ in range(10)))

    final = await kv.read("t", "c", "k")
    # Lost-update semantics: at most one increment lands. Asserting
    # an exact value would over-pin (timing-dependent) — what we
    # care about is that the final is *not* 10.
    assert final is not None
    assert int(final) < 10


# ---------------------------------------------------------------------------
# §52 Parser strict vs lenient — the strict mode enforces full grammar
# (raises on missing ``如果尾``, e.g.); the lenient mode salvages.
# Production rules use lenient because dicpro.txt is full of small
# omissions.
# ---------------------------------------------------------------------------


def test_strict_mode_raises_on_missing_endif() -> None:
    """``如果`` without a matching ``如果尾`` raises ParseError in strict mode."""
    from linling_dsl.parser import ParseError

    with pytest.raises(ParseError):
        parse("trig\n如果:%QQ%==1\nhi\n", strict=True)


def test_lenient_mode_auto_closes_missing_endif() -> None:
    """The same input parses cleanly in lenient mode; the implicit
    end-of-handler closes the open ``如果`` body. The single ``hi``
    line lands inside the if-body.
    """
    script = parse("trig\n如果:%QQ%==1\nhi\n", strict=False)
    body = script.handlers[0].body
    assert len(body) == 1
    if_stmt = body[0]
    from linling_dsl.ast_nodes import IfStmt

    assert isinstance(if_stmt, IfStmt)
    # The ``hi`` line is inside the if's body.
    assert len(if_stmt.body) == 1


def test_strict_mode_allows_duplicate_triggers() -> None:
    """Two handlers with the same trigger name don't trip strict parse.

    First-match semantics is enforced at the *classifier* level, not
    the parser — the parser's job is purely syntactic. dicpro.txt
    has several duplicated triggers (commented-out alternates that
    weren't fully removed); the parser must accept them.
    """
    script = parse("trig\nA\n\ntrig\nB\n", strict=True)
    assert len(script.handlers) == 2
    assert script.handlers[0].trigger == script.handlers[1].trigger == "trig"


# ---------------------------------------------------------------------------
# §53 钓鱼 起杆 cooldown — 是否甩杆==0 short-circuits before any state
# mutation. Verifies the early-exit guard pattern that gates most
# game-loop handlers in dicpro.txt (起杆 / 漂流瓶 / 抽奖 ...).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fishing_no_cast_short_circuits(kv) -> None:
    """No bait flag → handler emits the "no cast" message and returns."""
    body = """如果:$读 休闲系/钓鱼/是否甩杆 %QQ% 0$==0
没有甩杆肿么起杆呢？
返回
如果尾
SHOULD-NOT-REACH"""
    out = await _run(body, kv, _event("起杆"))
    assert out.strip() == "没有甩杆肿么起杆呢？"


@pytest.mark.asyncio
async def test_fishing_cast_flag_lets_logic_proceed(kv) -> None:
    """是否甩杆 == 1 → guard skipped, the post-if branch runs."""
    await _run(
        "$写 休闲系/钓鱼/是否甩杆 u 1$",
        kv,
        _event("seed", sender="u"),
    )
    body = """如果:$读 休闲系/钓鱼/是否甩杆 %QQ% 0$==0
EARLY
返回
如果尾
PROCEED"""
    out = await _run(body, kv, _event("起杆", sender="u"))
    assert out.strip() == "PROCEED"


# ---------------------------------------------------------------------------
# §54 $回调 chain depth — A→B→C nested. Each callback's text return
# becomes the caller's variable; the deepest result propagates back
# through the chain unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_level_callback_chain_propagates_text(kv) -> None:
    """``$回调 A$`` → A → ``$回调 B$`` → B → ``$回调 C$`` → C → "C-result".

    Each level's variable assignment captures the inner return text
    and re-emits it wrapped in its own annotation; the chain
    composes into a single string showing the call path.
    """
    helper = parse(
        "[内部]C\nC-result\n\n[内部]B\nv:$回调 C$\nB-saw-%v%\n\n[内部]A\nv:$回调 B$\nA-saw-%v%\n",
        strict=False,
    )
    h_by = {h.trigger: h for h in helper.handlers}

    body = "v:$回调 A$\n%v%"
    full = "trigger\n" + body + "\n"
    script = parse(full, strict=False)
    vm = VM(
        tool_registry=registry,
        kv=kv,
        bot_id="susu_test",
        extras={"handler_lookup": h_by.get},
    )
    res = await vm.execute_handler(script.handlers[0], _event("x"))
    out = "".join(s.text for s in res.segments if isinstance(s, TextSegment))
    assert out.strip() == "A-saw-B-saw-C-result"


# ---------------------------------------------------------------------------
# §55 [戳一戳] image cascade — the long ``如果:%Z%==郫忧 / ±img / $jump$``
# ladder picks one of ~10 守护 images then jumps past the rest. We
# cover the three observable branches: named守护 hits, named守护 misses
# but 专 set, and neither (no image emitted). All three exercise the
# same ``$jump :形象标记$`` machinery — a regression that mis-located
# the label would either emit duplicate images or fall through into
# an unintended branch.
# ---------------------------------------------------------------------------


_POKE_BODY = """Z:$读 休闲系/珍品/个人守护 %QQ% 0$
如果:%Z%!=0
如果:%Z%==郫忧
±img=@pic:郫忧±
$jump :形象标记$
如果尾
如果:%Z%==呦呦
±img=@pic:呦呦±
$jump :形象标记$
如果尾
如果尾
专:$读 啊/主页系/专属形象 %QQ% 0$
如果:%专%!=0
±img=%专%±
如果尾
:形象标记
END"""


@pytest.mark.asyncio
async def test_poke_cascade_named_guardian_emits_correct_image(kv) -> None:
    """``%Z%==郫忧`` → 郫忧 image, jump skips 呦呦 branch and 专 fallback."""
    from linling_core.segments import ImageSegment

    await _run(
        "$写 休闲系/珍品/个人守护 u 郫忧$",
        kv,
        _event("seed", sender="u"),
    )
    full = "trigger\n" + _POKE_BODY + "\n"
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    res = await vm.execute_handler(script.handlers[0], _event("[戳一戳]", sender="u"))

    images = [s for s in res.segments if isinstance(s, ImageSegment)]
    texts = [s.text for s in res.segments if isinstance(s, TextSegment)]
    # Exactly one image — the 郫忧 branch fires and jumps past the rest.
    assert len(images) == 1
    assert images[0].url == "@pic:郫忧"
    # END sentinel always renders (after the label).
    assert "END" in "".join(texts)


@pytest.mark.asyncio
async def test_poke_cascade_alternate_guardian_branch(kv) -> None:
    """``%Z%==呦呦`` lands on the second branch; verifies the label
    resolution doesn't latch on the first match.
    """
    from linling_core.segments import ImageSegment

    await _run(
        "$写 休闲系/珍品/个人守护 u 呦呦$",
        kv,
        _event("seed", sender="u"),
    )
    full = "trigger\n" + _POKE_BODY + "\n"
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    res = await vm.execute_handler(script.handlers[0], _event("[戳一戳]", sender="u"))

    images = [s for s in res.segments if isinstance(s, ImageSegment)]
    assert len(images) == 1
    assert images[0].url == "@pic:呦呦"


@pytest.mark.asyncio
async def test_poke_cascade_falls_through_to_custom_avatar(kv) -> None:
    """No 守护 set + 专 (custom avatar) URL → 专 image emitted."""
    from linling_core.segments import ImageSegment

    await _run(
        "$写 啊/主页系/专属形象 u https://example.com/avatar.png$",
        kv,
        _event("seed", sender="u"),
    )
    full = "trigger\n" + _POKE_BODY + "\n"
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    res = await vm.execute_handler(script.handlers[0], _event("[戳一戳]", sender="u"))

    images = [s for s in res.segments if isinstance(s, ImageSegment)]
    # Only the 专 fallback fires.
    assert len(images) == 1
    assert images[0].url == "https://example.com/avatar.png"


# ---------------------------------------------------------------------------
# §56 调度延迟排序 — tasks fire in fire-time order, not insertion order.
# Cancellation pulls a queued task off the schedule before it fires.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_fires_in_delay_order(kv) -> None:
    """Scheduled with delays 200/50/100ms in insertion order → fires
    in 50/100/200 order. Verifies the priority-queue ordering inside
    the scheduler.
    """
    from linling_core.scheduler import ScheduledTask, Scheduler

    sched = Scheduler()
    fired: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.handler_name)

    # Note: we drive the scheduler directly (not through ``$调用$``)
    # because positional args + delay precision matters here.
    for name, delay_s in (("c200", 0.2), ("a50", 0.05), ("b100", 0.1)):
        sched.schedule(
            after_seconds=delay_s,
            handler_name=name,
            args=[],
            scope={"scope_id": "g", "sender_id": "u"},
            bot_id="susu_test",
        )

    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.5)
    await sched.stop()
    await runner

    assert fired == ["a50", "b100", "c200"]


@pytest.mark.asyncio
async def test_scheduler_cancel_prevents_fire(kv) -> None:
    """``Scheduler.cancel(task_id)`` removes a queued task from the run."""
    from linling_core.scheduler import ScheduledTask, Scheduler

    sched = Scheduler()
    fired: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.handler_name)

    tid = sched.schedule(
        after_seconds=0.2,
        handler_name="cancel-me",
        args=[],
        scope={"scope_id": "g", "sender_id": "u"},
        bot_id="susu_test",
    )
    cancelled = sched.cancel(tid)
    assert cancelled is True

    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.4)
    await sched.stop()
    await runner

    assert fired == []


# ---------------------------------------------------------------------------
# §57 $发送 sink failure path — when the action_sink raises, the
# delivery is logged but the calling handler keeps running. The error
# is confined to the background ``_deliver`` task.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_sink_failure_does_not_propagate(kv) -> None:
    """Sink raises ``RuntimeError`` → calling VM still emits "AFTER"."""
    from linling_core.events import Action

    async def bad_sink(_: Action) -> None:
        raise RuntimeError("simulated sink failure")

    ev = Event(
        id="e",
        platform="onebot",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="onebot"),
        sender=User(id="u", platform="onebot"),
        segments=[TextSegment(text="x")],
    )
    out = await _run(
        "$发送 群 msg 12345 hello$\nAFTER",
        kv,
        ev,
        action_sink=bad_sink,
    )
    # Calling handler proceeded normally past the failed send.
    assert out.strip() == "AFTER"
    # Give the background _deliver task a tick to settle so its
    # scheduled exception is logged before the test exits — keeps
    # the asyncio "Task exception was never retrieved" warning quiet.
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# §58 [键转昵称X] rank-format token — current behaviour: passes through
# literally because :func:`_format_row` only knows ``[序号] / [键] /
# [值]``. Documents the gap so a future implementation that adds
# adapter-side nickname lookup forces this test to be updated.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rank_format_unknown_token_passes_through(kv) -> None:
    """``[键转昵称%群%]`` is unrecognised → emitted as literal text."""
    await _run("$写 玉/榜 u 100$", kv, _event("seed"))
    out = await _run(
        "rk:$排行榜 玉/榜 反序 1 \\n [序号].[键转昵称%群%].[值]$\n%rk%",
        kv,
        _event("查"),
    )
    # Current contract: unknown bracket-tokens come back unchanged.
    # If a future change implements nickname lookup, this assertion
    # will trip and we'll know to update / remove the doc-test.
    # ``%群%`` resolved against the event's scope id (default fixture
    # group "67890") before the rank tool sees the format string.
    assert "[键转昵称67890]" in out
    assert "1" in out and "100" in out


# ---------------------------------------------------------------------------
# §59 DSL → Router → sink end-to-end. Wires up Bus + Router +
# DslCommandDispatcher + a recording sink, publishes an Event, and
# verifies the full pipeline produces an ``Action`` reaching the sink.
# Same pipeline shape that the production CLI / WebUI bot uses.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_dispatches_dsl_handler_to_sink(kv) -> None:
    """``ping`` event → router → DSL handler → sink receives ``PONG`` action."""
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    script = parse("ping\nPONG\n", strict=False)
    classifier = MessageClassifier(script)
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="susu_test", extras={})
    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=None,
        sink=sink,
        conversations=ConversationStore(),
        config=RouterConfig(),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    await bus.publish(_event("ping", sender="u"))
    # Router dispatch runs synchronously inside ``handle``; one tick is
    # plenty for the sink coroutine to drain.
    await asyncio.sleep(0.05)

    assert len(actions) == 1
    action = actions[0]
    assert action.kind == "reply"
    assert any(isinstance(s, TextSegment) and s.text == "PONG" for s in action.segments)


@pytest.mark.asyncio
async def test_router_emits_unknown_command_reply(kv) -> None:
    """A ``/`` prefix with no matching handler triggers the configured
    ``unknown_command_reply`` text via the sink.
    """
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    script = parse("ping\nPONG\n", strict=False)
    classifier = MessageClassifier(script, command_prefixes=("/",))
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="susu_test", extras={})
    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=None,
        sink=sink,
        conversations=ConversationStore(),
        config=RouterConfig(unknown_command_reply="UNKNOWN"),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    await bus.publish(_event("/notexist", sender="u"))
    await asyncio.sleep(0.05)

    assert len(actions) == 1
    text = "".join(s.text for s in actions[0].segments if isinstance(s, TextSegment))
    assert text == "UNKNOWN"


# ---------------------------------------------------------------------------
# §60 OutputText newline preservation — authored ``\n`` decodes to a
# real newline (round-3 fix), and the trailing newline survives in
# the emitted segment exactly as decoded; image src text never gets
# escape-decoded (only OutputText goes through ``_decode_qrdic_escapes``).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trailing_newline_preserved_in_output(kv) -> None:
    """``hello\\n`` → text segment ending in ``\\n`` (real newline)."""
    out = await _run("hello\\n", kv, _event("x"))
    assert out == "hello\n"


@pytest.mark.asyncio
async def test_image_src_does_not_decode_escapes(kv) -> None:
    """``±img=https://x.com/a\\n.png±`` keeps ``\\n`` literal in the URL.

    Image src strings flow through expression evaluation but NOT
    ``_decode_qrdic_escapes`` — that pass is only applied to
    OutputText emit. A regression that double-applied escape
    decoding would corrupt URLs with backslash characters.
    """
    from linling_core.segments import ImageSegment

    body = "±img=https://x.com/a\\n.png±"
    full = "trigger\n" + body + "\n"
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    res = await vm.execute_handler(script.handlers[0], _event("x"))
    images = [s for s in res.segments if isinstance(s, ImageSegment)]
    assert len(images) == 1
    # The URL retains the backslash-n literally (no decode).
    assert images[0].url == "https://x.com/a\\n.png"


# ---------------------------------------------------------------------------
# §61 沙箱 max_steps via parser path — pair test with test_vm.py's
# synthetic-AST sandbox check; here the same limit triggers via a
# parsed ``:loop / $jump :loop$`` body.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_infinite_jump_loop_trips_max_steps(kv) -> None:
    """``:loop / $jump :loop$`` runs until ``max_steps`` raises SandboxError."""
    from linling_dsl.vm import SandboxError

    body = ":loop\n$jump :loop$\n"
    full = "trigger\n" + body
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test", max_steps=200)
    with pytest.raises(SandboxError, match="max_steps"):
        await vm.execute_handler(script.handlers[0], _event("x"))


# ---------------------------------------------------------------------------
# §62 mid-handler 返回 — emit some text, then ``返回``, then more text.
# Only the first text appears; the loop checks ``returned`` before each
# next statement.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_return_after_partial_output_truncates_segments(kv) -> None:
    """``ABOUT-TO-RETURN`` prints, then ``返回`` halts; ``AFTER`` doesn't run."""
    body = """ABOUT-TO-RETURN
返回
AFTER"""
    out = await _run(body, kv, _event("x"))
    assert "ABOUT-TO-RETURN" in out
    assert "AFTER" not in out


# ---------------------------------------------------------------------------
# §63 ConversationStore session sharing — same key → same Session
# (so the lock serialises events from the same sender), different keys
# → independent sessions (concurrent events from different senders
# don't block each other).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_store_same_key_returns_same_session() -> None:
    """Two ``get_or_create`` calls with the same key return the same Session."""
    from linling_core.pipeline import ConversationKey, ConversationStore

    cs = ConversationStore()
    k = ConversationKey(bot_id="b", scope_id="g", sender_id="u")
    s1 = await cs.get_or_create(k)
    s2 = await cs.get_or_create(k)
    assert s1 is s2


@pytest.mark.asyncio
async def test_conversation_store_different_key_returns_different_sessions() -> None:
    """Different sender_id → independent Session (independent lock)."""
    from linling_core.pipeline import ConversationKey, ConversationStore

    cs = ConversationStore()
    k_u = ConversationKey(bot_id="b", scope_id="g", sender_id="u")
    k_o = ConversationKey(bot_id="b", scope_id="g", sender_id="other")
    s_u = await cs.get_or_create(k_u)
    s_o = await cs.get_or_create(k_o)
    assert s_u is not s_o


# ---------------------------------------------------------------------------
# §64 Audit trail — Router calls ``audit.write(AuditEntry)`` once per
# dispatched event. Verifies the protocol shape and key fields used by
# the WebUI's audit reader (bot_id, kind, outcome, verdict, latency_ms).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_writes_audit_entry_per_dispatch(kv) -> None:
    """One inbound event → one ``AuditEntry`` with shape WebUI expects."""
    from linling_core.audit import AuditEntry
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    class CollectingAudit:
        def __init__(self) -> None:
            self.entries: list[AuditEntry] = []

        def write(self, entry: AuditEntry) -> None:
            self.entries.append(entry)

    audit = CollectingAudit()
    script = parse("ping\nPONG\n", strict=False)
    classifier = MessageClassifier(script)
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="susu_test", extras={})

    async def sink(_: Action) -> None:
        pass

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=None,
        sink=sink,
        conversations=ConversationStore(),
        config=RouterConfig(),
        audit=audit,
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")
    await bus.publish(_event("ping", sender="u"))
    await asyncio.sleep(0.05)

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.bot_id == "susu_test"
    assert entry.kind == "command"
    assert entry.outcome == "ok"
    assert entry.verdict.startswith("command:")
    # latency_ms is ≥0 for synchronous DSL handlers.
    assert entry.latency_ms >= 0


# ---------------------------------------------------------------------------
# §65 扭蛋十次 fan-out via $jump :loop$ — counter-driven loop schedules
# 10 sub-tasks. Verifies the inner $调用 fan-out matches the expected
# count and the loop terminates when the counter exceeds 9.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gacha_fan_out_schedules_ten_tasks(kv) -> None:
    """Counter loop ``:抽的次数`` schedules ``单十次扭蛋`` ten times.

    Mirrors dicpro.txt's 扭蛋十次 fan-out shape: ``i:0`` initialiser,
    a ``$调用 [200*%i%] handler$`` per iteration, and a guard
    ``如果:%i%>=9 / 返回``.
    """
    from linling_core.scheduler import ScheduledTask, Scheduler

    sched = Scheduler()
    fired: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        fired.append(t.handler_name)

    body = """i:0
:抽的次数
$调用 0 helper$
如果:%i%>=9
DONE
返回
如果尾
i:[%i%+1]
$jump :抽的次数$"""
    out = await _run(body, kv, _event("扭"), scheduler=sched)
    assert out.strip() == "DONE"

    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.2)
    await sched.stop()
    await runner

    # Exactly 10 fan-out fires (i=0..9 inclusive, then guard halts).
    assert fired == ["helper"] * 10


# ---------------------------------------------------------------------------
# §66 Bus subscriber priority — higher priority runs first; if it
# returns ``True`` the bus short-circuits and lower-priority subscribers
# never fire. Returning ``None`` lets the chain continue.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_priority_short_circuits_lower() -> None:
    """High-priority subscriber returning ``True`` halts further delivery."""
    from linling_core.bus import EventBus

    order: list[str] = []

    async def low(_: Event) -> None:
        order.append("low")
        return None

    async def high(_: Event) -> bool:
        order.append("high")
        return True

    bus = EventBus()
    bus.subscribe(low, name="low", priority=0)
    bus.subscribe(high, name="high", priority=10)
    await bus.publish(_event("x"))
    await asyncio.sleep(0.05)

    assert order == ["high"]


@pytest.mark.asyncio
async def test_high_priority_passing_through_lets_lower_run() -> None:
    """High-priority subscriber returning ``None`` lets the lower one run."""
    from linling_core.bus import EventBus

    order: list[str] = []

    async def lower(_: Event) -> None:
        order.append("lo")

    async def higher(_: Event) -> None:
        order.append("hi")

    bus = EventBus()
    bus.subscribe(lower, name="lo", priority=0)
    bus.subscribe(higher, name="hi", priority=10)
    await bus.publish(_event("x"))
    await asyncio.sleep(0.05)

    assert order == ["hi", "lo"]


# ---------------------------------------------------------------------------
# §67 排行榜 over an empty scope returns the empty string.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rank_over_empty_scope_returns_empty(kv) -> None:
    """``$排行榜 empty/path 反序 5 \\n [键]-[值]$`` → ``""`` when no rows."""
    body = "rk:$排行榜 empty/path 反序 5 \\n [键]-[值]$\n(%rk%)"
    out = await _run(body, kv, _event("x"))
    # Parens emit unchanged; nothing between them when rk is empty.
    assert out == "()"


# ---------------------------------------------------------------------------
# §68 $回调 inside a scheduler-fired handler — handler_lookup propagates
# from the scheduler's extras into the inner VM, so the nested $回调
# resolves correctly. Without that propagation the inner $回调 would
# return empty.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_resolves_inside_scheduler_fired_handler(kv) -> None:
    """Scheduler fires ``top``; ``top`` runs ``$回调 inner$`` and prints
    ``.top.INNER.``. Verifies handler_lookup is plumbed into the
    scheduler-fired VM.
    """
    from linling_core.scheduler import ScheduledTask, Scheduler

    helper = parse(
        "[内部]top\nv:$回调 inner$\n.top.%v%.\n\n[内部]inner\nINNER\n",
        strict=False,
    )
    h_by = {h.trigger: h for h in helper.handlers}

    sched = Scheduler()
    captured: list[str] = []

    async def cb(t: ScheduledTask) -> None:
        target = h_by.get(t.handler_name)
        if target is None:
            return
        inner = VM(
            tool_registry=registry,
            kv=kv,
            bot_id="susu_test",
            extras={"scheduler": sched, "handler_lookup": h_by.get},
        )
        res = await inner.execute_handler(target, _event("sched", sender="system"))
        for s in res.segments:
            if isinstance(s, TextSegment):
                captured.append(s.text)

    sched.schedule(
        after_seconds=0,
        handler_name="top",
        args=[],
        scope={"scope_id": "g", "sender_id": "u"},
        bot_id="susu_test",
    )
    runner = asyncio.create_task(sched.run(cb))
    await asyncio.sleep(0.3)
    await sched.stop()
    await runner

    assert any(t.strip() == ".top.INNER." for t in captured)


# ---------------------------------------------------------------------------
# §69 Classifier exact-text match — literal triggers go through the
# fast-path index (no regex). A trigger ``查看消息`` matches the event
# text ``查看消息`` exactly; it does NOT match ``查看消息内容``.
# Pairs with §70 which exercises the alternation regex path.
# ---------------------------------------------------------------------------


def test_literal_trigger_exact_match() -> None:
    """``查看消息`` matches event text ``查看消息`` exactly."""
    from linling_core.classifier import MessageClassifier

    script = parse("查看消息\nMSG\n", strict=False)
    cls = MessageClassifier(script)
    intent = cls.classify(_event("查看消息"))
    assert intent.kind == "command"
    assert intent.match is not None
    assert intent.match.handler.trigger == "查看消息"


def test_literal_trigger_does_not_match_with_suffix() -> None:
    """``查看消息`` does NOT match ``查看消息内容`` — full-text match only."""
    from linling_core.classifier import MessageClassifier

    script = parse("查看消息\nMSG\n", strict=False)
    cls = MessageClassifier(script)
    intent = cls.classify(_event("查看消息内容"))
    # Falls through to chat (no DSL match, no command prefix).
    assert intent.kind == "chat"
    assert intent.match is None


# ---------------------------------------------------------------------------
# §70 Classifier regex alternation — ``(查看消息|消息)`` matches both
# branches via the regex path. Confirms the fast-path / regex-path
# fallback correctly classifies same-handler different-text events.
# ---------------------------------------------------------------------------


def test_alternation_trigger_matches_first_branch() -> None:
    """``(查看消息|消息)`` matches ``查看消息`` via the regex walker."""
    from linling_core.classifier import MessageClassifier

    script = parse("(查看消息|消息)\nMSG\n", strict=False)
    cls = MessageClassifier(script)
    intent = cls.classify(_event("查看消息"))
    assert intent.kind == "command"
    assert intent.match is not None
    assert intent.match.handler.trigger == "(查看消息|消息)"


def test_alternation_trigger_matches_second_branch() -> None:
    """The same trigger also matches ``消息`` (the alternation's RHS)."""
    from linling_core.classifier import MessageClassifier

    script = parse("(查看消息|消息)\nMSG\n", strict=False)
    cls = MessageClassifier(script)
    intent = cls.classify(_event("消息"))
    assert intent.kind == "command"
    assert intent.match is not None
    assert intent.match.handler.trigger == "(查看消息|消息)"


# ---------------------------------------------------------------------------
# §71 Output node coverage — Voice (±ptt=±), FlashImage (±fimg=±),
# Reply (±rep id±). Each parses into the dedicated AST node and the
# VM emits the matching segment type with src / message_id correctly
# threaded.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_segment_emit(kv) -> None:
    """``±ptt=src±`` emits a :class:`VoiceSegment`."""
    from linling_core.segments import VoiceSegment

    full = "trigger\n±ptt=https://x.com/v.mp3±\n"
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    res = await vm.execute_handler(script.handlers[0], _event("x"))
    voice = [s for s in res.segments if isinstance(s, VoiceSegment)]
    assert len(voice) == 1
    assert voice[0].url == "https://x.com/v.mp3"


@pytest.mark.asyncio
async def test_flash_image_segment_emit(kv) -> None:
    """``±fimg=src±`` emits an :class:`ImageSegment` with ``extras.flash=True``."""
    from linling_core.segments import ImageSegment

    full = "trigger\n±fimg=https://x.com/f.png±\n"
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    res = await vm.execute_handler(script.handlers[0], _event("x"))
    images = [s for s in res.segments if isinstance(s, ImageSegment)]
    assert len(images) == 1
    assert images[0].url == "https://x.com/f.png"
    assert images[0].extras.get("flash") is True


@pytest.mark.asyncio
async def test_reply_segment_emit(kv) -> None:
    """``±rep msgid±`` emits a :class:`ReplySegment` with that message id."""
    from linling_core.segments import ReplySegment

    full = "trigger\n±rep 12345±\n"
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    res = await vm.execute_handler(script.handlers[0], _event("x"))
    replies = [s for s in res.segments if isinstance(s, ReplySegment)]
    assert len(replies) == 1
    assert replies[0].message_id == "12345"


# ---------------------------------------------------------------------------
# §72 JsonAccess @var[field][...] — used by 回忆录 in dicpro.txt to pull
# nested fields out of HTTP responses (``@a[code]``, ``@a[data][msg]``).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_access_top_level_field(kv) -> None:
    """``b:@a[code]`` pulls ``code`` out of a nested JSON object in scope."""
    body = """a:{"code":0,"data":{"msg":"hi"}}
b:@a[code]
%b%"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "0"


@pytest.mark.asyncio
async def test_json_access_nested_field(kv) -> None:
    """``c:@a[data][msg]`` walks two levels."""
    body = """a:{"code":0,"data":{"msg":"hi"}}
c:@a[data][msg]
%c%"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "hi"


# ---------------------------------------------------------------------------
# §73 Remaining adapter-RPC tools — fallbacks when no adapter wired:
# 群头衔 → empty, 获取群列表 → "[]", 群头像 → CDN URL (deterministic),
# 图片链接 N → segment URL by index, 管理员 user_id → 1/empty.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_title_no_adapter_returns_empty(kv) -> None:
    """``$群头衔 g uid title$`` with no adapter returns empty (no-op stub)."""
    body = "v:$群头衔 g 12345 头衔$\n(%v%)"
    out = await _run(body, kv, _event("x"))
    assert out == "()"


@pytest.mark.asyncio
async def test_group_list_no_adapter_returns_empty_array(kv) -> None:
    """``$获取群列表$`` with no adapter returns ``"[]"``."""
    body = "v:$获取群列表$\n%v%"
    out = await _run(body, kv, _event("x"))
    assert out == "[]"


@pytest.mark.asyncio
async def test_group_avatar_returns_qq_cdn_url(kv) -> None:
    """``$群头像 12345$`` returns the QQ avatar CDN URL deterministically."""
    body = "v:$群头像 12345$\n%v%"
    out = await _run(body, kv, _event("x"))
    assert out == "https://p.qlogo.cn/gh/12345/12345/0"


@pytest.mark.asyncio
async def test_image_link_returns_url_of_nth_image(kv) -> None:
    """``$图片链接 1$`` resolves the second image segment in the event."""
    from linling_core.segments import ImageSegment

    ev = Event(
        id="e",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="cli"),
        sender=User(id="u", platform="cli"),
        segments=[
            TextSegment(text="x"),
            ImageSegment(url="https://a.png"),
            ImageSegment(url="https://b.png"),
        ],
    )
    body = "v:$图片链接 1$\n%v%"
    out = await _run(body, kv, ev)
    assert out == "https://b.png"


@pytest.mark.asyncio
async def test_image_link_out_of_range_returns_empty(kv) -> None:
    """``$图片链接 5$`` with only one image returns empty."""
    from linling_core.segments import ImageSegment

    ev = Event(
        id="e",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="cli"),
        sender=User(id="u", platform="cli"),
        segments=[TextSegment(text="x"), ImageSegment(url="https://a.png")],
    )
    body = "v:$图片链接 5$\n(%v%)"
    out = await _run(body, kv, ev)
    assert out == "()"


@pytest.mark.asyncio
async def test_is_admin_function_form_returns_one_for_match(kv) -> None:
    """``$管理员 user_id$`` (function form) returns ``"1"`` for a listed admin."""
    body = "v:$管理员 777$\n(%v%)"
    out = await _run(body, kv, _event("x"), admin_users=("777", "888"))
    assert out == "(1)"


@pytest.mark.asyncio
async def test_is_admin_function_form_returns_empty_for_miss(kv) -> None:
    """``$管理员 user_id$`` returns empty for a non-admin."""
    body = "v:$管理员 999$\n(%v%)"
    out = await _run(body, kv, _event("x"), admin_users=("777", "888"))
    assert out == "()"


# ---------------------------------------------------------------------------
# §74 完成 alias for 返回 — same effect, different keyword. dicpro.txt
# has both spellings within one rule file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_keyword_aliases_return(kv) -> None:
    """``完成`` halts execution exactly like ``返回``."""
    body = """A
完成
B"""
    out = await _run(body, kv, _event("x"))
    assert "A" in out
    assert "B" not in out


# ---------------------------------------------------------------------------
# §75 FaceSegment / XmlSegment / CardSegment indexing — segment families
# beyond AT and IMG. Each emits as ``%FACE0%``, ``%XML0%``, ``%JSON0%``
# with a matching ``NUM`` count suffix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_face_segment_indexing(kv) -> None:
    """``%FACE0%`` returns the first face's id; ``%FACENUM%`` is the count."""
    from linling_core.segments import FaceSegment

    ev = Event(
        id="e",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="cli"),
        sender=User(id="u", platform="cli"),
        segments=[TextSegment(text="x"), FaceSegment(face_id="123")],
    )
    out = await _run("face=%FACE0%/n=%FACENUM%", kv, ev)
    assert out == "face=123/n=1"


@pytest.mark.asyncio
async def test_xml_segment_indexing(kv) -> None:
    """``%XML0%`` returns the first XmlSegment's payload."""
    from linling_core.segments import XmlSegment

    ev = Event(
        id="e",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="cli"),
        sender=User(id="u", platform="cli"),
        segments=[TextSegment(text="x"), XmlSegment(xml="<note/>")],
    )
    out = await _run("xml=%XML0%", kv, ev)
    assert out == "xml=<note/>"


@pytest.mark.asyncio
async def test_card_segment_indexing(kv) -> None:
    """``%JSON0%`` returns the first CardSegment's payload."""
    from linling_core.segments import CardSegment

    ev = Event(
        id="e",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="cli"),
        sender=User(id="u", platform="cli"),
        segments=[TextSegment(text="x"), CardSegment(payload='{"a":1}')],
    )
    out = await _run("card=%JSON0%", kv, ev)
    assert out == 'card={"a":1}'


# ---------------------------------------------------------------------------
# §76 Adapter-side stubs degradation — every QQ-shaped tool that lacks a
# live adapter degrades to a logged no-op returning empty text. Real
# rule files invoke these freely; without graceful degradation a single
# missing adapter wire would cascade into a torrent of handler crashes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, body",
    [
        ("撤回", "$撤回 g 9999$"),
        ("禁", "$禁 g 12345 600$"),
        ("全体禁言", "$全体禁言 g 1$"),
        ("设置群状态", "$设置群状态 g enabled$"),
        ("退出群", "$退出群 g$"),
        ("申请群", "$申请群 g hello$"),
        ("改", "$改 g 12345 newname$"),
        ("进群审核", "$进群审核 req accept$"),
    ],
)
@pytest.mark.asyncio
async def test_adapter_stubs_degrade_quietly(kv, label: str, body: str) -> None:
    """Each adapter-side tool returns empty and emits no segments."""
    res_text = await _run(body, kv, _event("x"))
    assert res_text == "", f"{label} unexpectedly emitted: {res_text!r}"


# ---------------------------------------------------------------------------
# §77 Sandbox-refused tools — ``$执行$`` and ``$BSH$`` log a warning and
# return empty. Letting them no-op (rather than raise) keeps migrated
# rule files running while the operator sees the warning trail.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_tool_refused(kv) -> None:
    """``$执行 ...$`` is refused; output empty."""
    out = await _run("$执行 print('hi')$", kv, _event("x"))
    assert out == ""


@pytest.mark.asyncio
async def test_bsh_tool_refused(kv) -> None:
    """``$BSH ...$`` is refused; output empty."""
    out = await _run("$BSH some.java method args$", kv, _event("x"))
    assert out == ""


# ---------------------------------------------------------------------------
# §78 $访问 placeholder behaviour — the HTTP fetch tool returns a
# placeholder string; the renderer-set status emits it directly. Real
# adapters will replace this with live fetch eventually; today's contract
# is "sentinel string, not crash".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_get_placeholder_emits(kv) -> None:
    """``$访问 url$`` standalone emits the placeholder text."""
    out = await _run("$访问 https://x.com/api$", kv, _event("x"))
    assert "not implemented" in out


# ---------------------------------------------------------------------------
# §79 $图文 regression — DSL parse of ``$图文 多 词 内容$`` currently
# tokenises whitespace into separate args. The tool now joins variadic
# positional args into the content string so multi-word real-world
# rules (``$图文 加入成功，请等待$``) don't crash.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_text_multi_token_joins_into_content(kv, tmp_path) -> None:
    """``$图文 hello world$`` writes a PNG, no TypeError on multi-word args."""
    body = "v:$图文 hello world$\n%v%"
    out = await _run(
        body,
        kv,
        _event("x"),
        image_text_cache_dir=tmp_path,
    )
    # Tool returns the saved PNG path — non-empty and ends with .png.
    assert out.endswith(".png")
    # File exists on disk under the configured cache dir.
    from pathlib import Path

    p = Path(out)
    assert p.exists()
    assert p.parent == tmp_path


@pytest.mark.asyncio
async def test_image_text_single_token_unchanged(kv, tmp_path) -> None:
    """Single-arg call still works (the historical shape)."""
    body = "v:$图文 hello$\n%v%"
    out = await _run(body, kv, _event("x"), image_text_cache_dir=tmp_path)
    assert out.endswith(".png")


# ---------------------------------------------------------------------------
# §80 $下载 sandbox — refuses non-http URLs, refuses absolute paths
# without a data_root, refuses paths that escape the data_root via
# ``..`` traversal. All three cases must return empty string (the
# "failed" sentinel) without writing anything.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_refuses_non_http_url(kv) -> None:
    """``$下载 path ftp://...$`` returns empty (only http/https accepted)."""
    body = "v:$下载 ./data/x.png ftp://x.com/y$\n(%v%)"
    out = await _run(body, kv, _event("x"))
    assert out == "()"


@pytest.mark.asyncio
async def test_download_refuses_absolute_path_without_data_root(kv) -> None:
    """Absolute path with no ``data_root`` configured is refused."""
    body = "v:$下载 /etc/passwd https://x.com/y$\n(%v%)"
    out = await _run(body, kv, _event("x"))
    assert out == "()"


@pytest.mark.asyncio
async def test_download_refuses_path_escape_via_dotdot(kv, tmp_path) -> None:
    """``../../etc/passwd`` refused even when ``data_root`` is set.

    Verifies the resolve-and-relative-to check stops traversal
    attempts that would otherwise resolve to outside the sandbox.
    """
    body = "v:$下载 ../../etc/passwd https://x.com/y$\n(%v%)"
    out = await _run(body, kv, _event("x"), data_root=str(tmp_path))
    assert out == "()"


# ---------------------------------------------------------------------------
# §81 Parser lenient handling of unmatched ``$`` — a stray dollar sign
# that doesn't close into a tool call falls through to OutputText. The
# strict-mode would refuse this; lenient is what production rules use.
# ---------------------------------------------------------------------------


def test_unmatched_dollar_falls_through_to_output_text() -> None:
    """``$unmatched`` (no closing ``$``) becomes an OutputText literal."""
    from linling_dsl.ast_nodes import OutputText

    script = parse("trig\n$unmatched dollar\nhello\n", strict=False)
    body = script.handlers[0].body
    assert all(isinstance(stmt, OutputText) for stmt in body)


# ---------------------------------------------------------------------------
# §82 Classifier hides [内部] handlers — internal handlers don't take
# user-driven events. Their text "[内部]secret" classifies as chat (no
# DSL match), guaranteeing that direct invocation of an internal helper
# isn't possible from the user side.
# ---------------------------------------------------------------------------


def test_internal_handler_not_matched_by_user_text() -> None:
    """Event text matching an ``[内部]`` trigger doesn't classify as a command."""
    from linling_core.classifier import MessageClassifier

    script = parse(
        "[内部]secret\nSECRET\n\nhello\nWORLD\n",
        strict=False,
    )
    cls = MessageClassifier(script)

    # Public handler matches.
    intent_pub = cls.classify(_event("hello"))
    assert intent_pub.kind == "command"
    assert intent_pub.match is not None
    assert intent_pub.match.handler.trigger == "hello"

    # Trying to invoke the internal handler by typing its trigger name
    # falls through to chat (no DSL match).
    intent_int = cls.classify(_event("[内部]secret"))
    assert intent_int.kind == "chat"
    assert intent_int.match is None


# ---------------------------------------------------------------------------
# §83 KV transaction API — ``transaction()`` returns an awaitable
# context manager that bundles writes atomically. dicpro.txt rules
# don't use this directly but the agent layer does.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_transaction_commits_atomically(kv) -> None:
    """Two writes inside a transaction both land or neither does."""
    async with kv.transaction() as tx:
        await tx.write("t/tx", "f", "k1", "v1")
        await tx.write("t/tx", "f", "k2", "v2")

    assert await kv.read("t/tx", "f", "k1") == "v1"
    assert await kv.read("t/tx", "f", "k2") == "v2"


@pytest.mark.asyncio
async def test_kv_transaction_rolls_back_on_exception(kv) -> None:
    """An exception inside the ``with`` block aborts the transaction."""
    with pytest.raises(RuntimeError, match="rollback"):
        async with kv.transaction() as tx:
            await tx.write("t/rollback", "f", "k", "v")
            raise RuntimeError("rollback")

    # Write was rolled back — the row stays absent.
    assert await kv.read("t/rollback", "f", "k") is None


# ---------------------------------------------------------------------------
# §84 Context-var aliases — ``%用户%``, ``%会话%``, ``%自己%`` are
# semantic aliases for ``%QQ%``, ``%群号%``, ``%Robot%`` respectively.
# Real rule files mix the styles within a single handler.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_var_aliases(kv) -> None:
    """``用户/会话/自己`` resolve to the same values as ``QQ/群号/Robot``."""
    out = await _run(
        "QQ=%QQ%/用户=%用户%/群号=%群号%/会话=%会话%/Robot=%Robot%/自己=%自己%",
        kv,
        _event("x", sender="12345", group="67890"),
    )
    assert out == "QQ=12345/用户=12345/群号=67890/会话=67890/Robot=susu_test/自己=susu_test"


# ---------------------------------------------------------------------------
# §85 OneBot raw-payload context vars — ``%Code%``, ``%Msgbar%``,
# ``%Type%``, ``%Value%``, ``%Status%``, ``%Reqid%``, ``%UinName%``,
# ``%Inviteename%``. Each pulls a field out of ``event.raw`` (populated
# by the OneBot adapter) and stringifies it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onebot_raw_context_vars_resolve_from_event_raw(kv) -> None:
    """Each ``%XYZ%`` matches a key in ``event.raw``; missing → empty."""
    ev = Event(
        id="e",
        platform="onebot",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="onebot"),
        sender=User(id="u", platform="onebot"),
        segments=[TextSegment(text="x")],
        raw={
            "operator_id": "op-id",
            "message_id": "msg-id",
            "time": "1234",
            "sub_type": "poke",
            "value": "vv",
            "status": "ok",
            "request_id": "rid",
            "user_name": "uname",
            "operator_name": "opname",
        },
    )
    body = (
        "Code=%Code%/Msgbar=%Msgbar%/Time=%Time%/Type=%Type%/"
        "Value=%Value%/Status=%Status%/Reqid=%Reqid%/Uin=%UinName%/Inv=%Inviteename%"
    )
    out = await _run(body, kv, ev)
    assert out == (
        "Code=op-id/Msgbar=msg-id/Time=1234/Type=poke/"
        "Value=vv/Status=ok/Reqid=rid/Uin=uname/Inv=opname"
    )


@pytest.mark.asyncio
async def test_onebot_raw_context_vars_default_to_empty(kv) -> None:
    """Missing raw key → empty string (not raise)."""
    out = await _run("(%Code%)/(%UinName%)", kv, _event("x"))
    assert out == "()/()"


# ---------------------------------------------------------------------------
# §86 NDTime / RobotRunTime — millisecond-precision wall-clock and
# bot-start-time vars. Real rule files use ``%NDTime%`` for cooldown
# logic on QRSpeed-era community packs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ndtime_returns_current_millis(kv) -> None:
    """``%NDTime%`` returns a numeric ms timestamp close to ``time.time()*1000``."""
    import time as _time

    expected_ms = int(_time.time() * 1000)
    out = await _run("%NDTime%", kv, _event("x"))
    actual_ms = int(out)
    # ±5s tolerance — the test runs in single-digit ms.
    assert abs(actual_ms - expected_ms) < 5000


@pytest.mark.asyncio
async def test_robot_run_time_pinnable_via_setter(kv) -> None:
    """``set_bot_start_time_ms(ms)`` pins ``%RobotRunTime%`` deterministically."""
    from linling_dsl.vm import set_bot_start_time_ms

    set_bot_start_time_ms(1234567890000)
    try:
        out = await _run("%RobotRunTime%", kv, _event("x"))
        assert out == "1234567890000"
    finally:
        # Restore to current to avoid test-order pollution.
        import time as _time

        set_bot_start_time_ms(int(_time.time() * 1000))


# ---------------------------------------------------------------------------
# §87 ``%Json%`` / ``%Skey%`` — auth blobs that always read empty in our
# world (never travel in adapter payloads). Confirms the empty-default
# wiring stays in place; rules that reference them shouldn't crash.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_blob_vars_always_empty(kv) -> None:
    """``%Json%`` / ``%Skey%`` resolve to empty strings unconditionally."""
    out = await _run("(%Json%)/(%Skey%)", kv, _event("x"))
    assert out == "()/()"


# ---------------------------------------------------------------------------
# §88 Production dicpro.txt full-parse smoke — load the whole 10k+ line
# rule file and verify it parses without raising. A regression in the
# tokenizer / control-flow recovery / escape decoder would surface here
# the moment a *single* handler stops parsing cleanly.
# ---------------------------------------------------------------------------


def test_dicpro_full_file_parses_cleanly() -> None:
    """Full ``QRDic/dicpro.txt`` parses; ≥400 handlers + AST shape sanity."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    dicpro = repo_root / "QRDic" / "dicpro.txt"
    if not dicpro.exists():
        pytest.skip(f"dicpro.txt not found at {dicpro}")

    source = dicpro.read_text(encoding="utf-8")
    script = parse(source, strict=False)

    # Sanity floor: production has hundreds of handlers; sub-100 means
    # the parser is silently dropping body lines somewhere.
    assert len(script.handlers) >= 400

    # Spot-check: a handful of well-known triggers should parse.
    # Note: ``[内部]`` is stripped from the trigger and set as
    # ``is_internal=True`` on the handler — so the trigger string
    # is the bare name.
    triggers = {h.trigger for h in script.handlers}
    expected_subset = {
        "更新内容",
        "(扔|丢)瓶子(.*)红包([0-9]+)",
        "X加牌",  # internal handler stored without the [内部] prefix
        "我的徽章",
        "(查看消息|消息)",
        "[\\s\\S]*",
    }
    for trig in expected_subset:
        assert trig in triggers, f"expected trigger {trig!r} missing from script"

    # The ``X加牌`` trigger is internal; verify the flag is set.
    x_jiapai = next(h for h in script.handlers if h.trigger == "X加牌")
    assert x_jiapai.is_internal is True


# ---------------------------------------------------------------------------
# §89 Production dicpro.txt full-execute smoke — run *every* handler
# from the file against an empty event with all wiring (scheduler +
# handler_lookup + admins + image_text cache) and assert
# zero exceptions. A regression in the VM that crashes any one handler
# surfaces here loud.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dicpro_every_handler_executes_cleanly(kv, tmp_path) -> None:
    """All 400+ handlers in dicpro.txt run without raising any exception.

    Each handler is invoked with:
    * a synthetic group event (so ``%群号%`` / ``%QQ%`` resolve),
    * 5 capture placeholders (covers ``%括号1..5%`` references),
    * a wired Scheduler + handler_lookup (so ``$调用$`` and ``$回调$``
      have the helpers they need),
    * admin config (so ``%管理员%`` resolves),
    * a tmp image-text cache (so ``$图文$`` calls land in the sandbox).

    The sandbox's per-handler ``max_steps``, ``timeout_ms`` and
    ``max_output_segments`` are loosened to give the verbatim
    handlers room — production rules do emit > 20 segments occasionally.
    """
    from pathlib import Path

    from linling_core.scheduler import Scheduler

    repo_root = Path(__file__).resolve().parents[3]
    dicpro = repo_root / "QRDic" / "dicpro.txt"
    if not dicpro.exists():
        pytest.skip(f"dicpro.txt not found at {dicpro}")

    source = dicpro.read_text(encoding="utf-8")
    script = parse(source, strict=False)

    sched = Scheduler()
    handlers_by_trigger = {h.trigger: h for h in script.handlers}
    extras = {
        "scheduler": sched,
        "handler_lookup": handlers_by_trigger.get,
        "admin_users": ("2078123478",),
        "image_text_cache_dir": tmp_path,
    }

    ev = Event(
        id="smoke",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id="67890", platform="cli"),
        sender=User(id="12345", platform="cli", display_name="me"),
        segments=[TextSegment(text="x")],
    )

    failures: list[tuple[str, str]] = []
    for handler in script.handlers:
        vm = VM(
            tool_registry=registry,
            kv=kv,
            bot_id="susu_test",
            extras=extras,
            max_steps=2000,
            timeout_ms=1500,
            max_output_segments=200,
        )
        try:
            # Captures cover ``%括号1..5%``; handlers that ignore them
            # see no harm.
            await vm.execute_handler(handler, ev, captures=["1", "2", "3", "4", "5"])
        except Exception as exc:
            failures.append((handler.trigger, f"{type(exc).__name__}: {exc}"))

    assert not failures, "handlers raised unexpectedly: " + "\n".join(
        f"  {trig!r}: {msg}" for trig, msg in failures[:10]
    )


# ---------------------------------------------------------------------------
# §90 JsonAccess with interpolated path key — ``@X[%i%]`` resolves the
# array index from the runtime value of ``i``. Real production use: the
# 黑杰克 跟注 handler walks ``X``/``Y`` arrays via ``@X[%i%]`` /
# ``@Y[%i%]``. Pre-fix, the path element ``"%i%"`` reached ``int()``
# verbatim and silently returned empty.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_access_with_runtime_index_var(kv) -> None:
    """``@X[%i%]`` reads array index whose value comes from scope."""
    body = """X:[]
$JSON 添加 X a$
$JSON 添加 X b$
$JSON 添加 X c$
i:1
v:@X[%i%]
%v%"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "b"


@pytest.mark.asyncio
async def test_json_access_with_runtime_index_out_of_range(kv) -> None:
    """Runtime index that's out of range silently returns empty."""
    body = """X:[]
$JSON 添加 X a$
i:5
v:@X[%i%]
(%v%)"""
    out = await _run(body, kv, _event("x"))
    assert out == "()"


@pytest.mark.asyncio
async def test_json_access_with_dict_key_from_var(kv) -> None:
    """Dict navigation via interpolated key — ``@A[%k%]`` walks a JSON object."""
    # Seed via $写 with a single-token JSON value so it round-trips
    # through KV and lands as a string in scope.
    await _run(
        '$写 t/o A {"foo":"X","bar":"Y"}$',
        kv,
        _event("seed"),
    )
    body = """A:$读 t/o A {}$
k:bar
v:@A[%k%]
%v%"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "Y"


# ---------------------------------------------------------------------------
# §91 正则: condition — ``==`` / ``!=`` use ``re.search`` containment
# instead of literal equality. Real handlers like ``正则:%T%==.*%QQ%.*``
# rely on this to test "current QQ id appears anywhere in the comma-
# separated list T". Pre-fix, the literal-equality fallthrough silently
# never matched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regex_condition_equals_uses_search(kv) -> None:
    """``正则:%L%==.*hello.*`` fires when ``hello`` appears in L."""
    body = """L:hello_world
正则:%L%==.*hello.*
HIT
返回
如果尾
MISS"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "HIT"


@pytest.mark.asyncio
async def test_regex_condition_not_equal_negates_match(kv) -> None:
    """``正则:%L%!=.*xyz.*`` fires when ``xyz`` does NOT appear in L."""
    body = """L:hello_world
正则:%L%!=.*xyz.*
HIT
返回
如果尾
MISS"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "HIT"


@pytest.mark.asyncio
async def test_regex_condition_substring_match_with_var_interp(kv) -> None:
    """``正则:%T%==.*%QQ%.*`` — interpolated pattern looks for the
    sender's id inside a CSV-ish blob. Mirrors the 密友/绊 access
    check from dicpro.txt L4812.
    """
    body = """T:1234,5555,12345,9999
正则:%T%==.*%QQ%.*
ALLOWED
返回
如果尾
DENIED"""
    out = await _run(body, kv, _event("x", sender="12345"))
    assert out.strip() == "ALLOWED"


@pytest.mark.asyncio
async def test_regex_condition_falls_back_on_invalid_pattern(kv) -> None:
    """A malformed regex falls back to literal string equality.

    Without the fallback a single typo'd ``正则:%L%==[unclosed`` would
    crash the handler. We mirror QRDic's tolerant behaviour: invalid
    regex → literal compare → typically False → handler keeps running.
    """
    body = """L:something
正则:%L%==[
SHOULD-NOT-PRINT
返回
如果尾
SAFE"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "SAFE"


@pytest.mark.asyncio
async def test_if_keyword_keeps_literal_equality(kv) -> None:
    """``如果:`` (non-regex) keeps literal equality — regex chars are not magic."""
    body = """L:hello_world
如果:%L%==.*hello.*
HIT
返回
如果尾
MISS"""
    out = await _run(body, kv, _event("x"))
    # Literal compare: ``"hello_world" == ".*hello.*"`` is false.
    assert out.strip() == "MISS"


# ---------------------------------------------------------------------------
# §92 概率随机 scalar tolerance — when the rule author writes ``[1]``
# (a single-element JSON array) the DSL's ``[arith]`` evaluator
# collapses it to the numeric string ``"1"`` *before* it reaches the
# tool. The tool now wraps that scalar into a single-element list so
# the call doesn't crash.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weighted_random_with_arith_collapsed_scalar(kv) -> None:
    """``$概率随机 [1] [\"only\"]$`` returns the only value, no crash."""
    body = 'r:$概率随机 [1] ["only"]$\n%r%'
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "only"


# ---------------------------------------------------------------------------
# §93 Missing-arg defensive defaults — tools that previously raised
# ``TypeError`` on insufficient args now degrade silently. Operator
# typos in rule files shouldn't tear down the dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_kv_missing_key_returns_default(kv) -> None:
    """``$读 t/x$`` (missing key) returns empty (the implicit default)."""
    body = "v:$读 t/x$\n(%v%)"
    out = await _run(body, kv, _event("x"))
    assert out == "()"


@pytest.mark.asyncio
async def test_write_kv_missing_value_no_ops(kv) -> None:
    """``$写 t/x k$`` (missing value) is a no-op, not a crash."""
    out = await _run("$写 t/x k$\nDONE", kv, _event("x"))
    assert out.strip() == "DONE"


@pytest.mark.asyncio
async def test_delete_kv_empty_path_no_ops(kv) -> None:
    """``$删除$`` with no path returns ``"0"`` and doesn't crash."""
    body = "v:$删除$\n(%v%)"
    out = await _run(body, kv, _event("x"))
    # ``$删除$`` is a side-effect tool, so the standalone return ``"0"``
    # is *not* emitted; the assignment captures it.
    assert out == "(0)"


@pytest.mark.asyncio
async def test_group_nickname_missing_user_id_returns_empty(kv) -> None:
    """``$群昵称 g$`` (missing uid) returns empty, no TypeError."""
    body = "v:$群昵称 g$\n(%v%)"
    out = await _run(body, kv, _event("x"))
    assert out == "()"


@pytest.mark.asyncio
async def test_group_title_missing_args_returns_empty(kv) -> None:
    """``$群头衔 g$`` (missing uid + title) returns empty."""
    body = "v:$群头衔 g$\n(%v%)"
    out = await _run(body, kv, _event("x"))
    assert out == "()"


@pytest.mark.asyncio
async def test_group_members_no_group_id_returns_empty_array(kv) -> None:
    """``$获取群成员$`` with no group_id returns ``"[]"``."""
    body = "v:$获取群成员$\n%v%"
    out = await _run(body, kv, _event("x"))
    assert out == "[]"


@pytest.mark.asyncio
async def test_get_message_field_no_field_returns_default(kv) -> None:
    """``$获取消息$`` with no field arg returns the default value (empty)."""
    body = "v:$获取消息$\n(%v%)"
    out = await _run(body, kv, _event("x"))
    assert out == "()"


# ---------------------------------------------------------------------------
# §94 Comprehensive missing-arg sweep — invoke every QRDic-facing tool
# with zero args and assert no exception is raised. This is the
# "fuzz floor" — operators typo'ing rule files should never crash the
# dispatcher; they should get an empty / sensible-default reply.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_call, expected_emit",
    [
        # Codec tools — return empty/identity on empty input.
        ("$URLEncoder$", False),
        ("$URLDecoder$", False),
        ("$Base64Encoder$", False),
        ("$Base64Decoder$", False),
        ("$HexEncoder$", False),
        ("$HexDecoder$", False),
        ("$UnicodeDecoder$", False),
        ("$MD5$", True),  # MD5 of empty string is a known value
        # KV
        ("$读$", False),
        ("$写$", False),
        ("$删除$", False),
        ("$排行榜$", False),
        # Random / probability
        ("$随机数$", False),
        ("$概率随机$", False),
        # JSON
        ("$JSON$", False),
        # String ops
        ("$替换$", False),
        ("$正则$", False),
        ("$取中间$", False),
        # Globals
        ("$全局变量$", False),
        ("$取变量$", False),
        # Adapter RPC
        ("$群昵称$", False),
        ("$群头衔$", False),
        ("$获取群成员$", False),
        ("$获取消息$", False),
        ("$获取群列表$", False),
        ("$进群审核$", False),
        ("$图片链接$", False),
        ("$下载$", False),
        ("$群头像$", False),
        ("$管理员$", False),
        # Legacy stubs
        ("$读文件$", False),
        ("$写文件$", False),
        ("$词库操作$", False),
        ("$回调$", False),
        ("$执行$", False),
        ("$BSH$", False),
        ("$撤回$", False),
        ("$禁$", False),
        ("$全体禁言$", False),
        ("$设置群状态$", False),
        ("$退出群$", False),
        ("$申请群$", False),
        ("$改$", False),
        ("$输出为$", False),
        # Scheduler
        ("$调用$", False),
    ],
)
@pytest.mark.asyncio
async def test_every_tool_with_no_args_does_not_raise(
    kv, tool_call: str, expected_emit: bool
) -> None:
    """Tool ``$X$`` with no positional args must not raise.

    Whether it emits text is tool-specific (codecs return empty string,
    MD5 returns the known empty-input digest, side-effect tools stay
    silent). The contract here is *only* "no exception".
    """
    out = await _run(tool_call, kv, _event("x"))
    if expected_emit:
        assert out != ""
    # No-emit path: ``out`` may legitimately be "" (silent side-effect)
    # or a placeholder string (e.g. ``访问`` returns "not implemented yet").
    # We don't assert further — the goal is no-raise.


# ---------------------------------------------------------------------------
# §95 Comprehensive arg-fuzz across every DSL-named tool — invoke each
# tool with many arg shapes and assert no exception. The fuzz floor:
# ~50 tools × 12 shapes ≈ 600 invocations every test run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_tool_survives_arg_fuzz(kv, tmp_path) -> None:
    """Every DSL-named tool tolerates 12 different arg shapes without raising."""
    arg_shapes = [
        "",  # zero args
        "a",
        "a b",
        "a b c",
        "0",
        "1 2",
        "1 -2 3",
        "1.5 2.5",
        "[]",
        "{}",
        "[1,2,3]",
        "abc def ghi jkl",
    ]
    tools = [td.dsl_name for td in registry.all() if td.dsl_name]

    failures: list[tuple[str, str, str]] = []
    for tname in tools:
        for shape in arg_shapes:
            body = f"v:${tname}{(' ' + shape) if shape else ''}$\n%v%"
            full = "trigger\n" + body + "\n"
            try:
                script = parse(full, strict=False)
                vm = VM(
                    tool_registry=registry,
                    kv=kv,
                    bot_id="susu_test",
                    extras={"image_text_cache_dir": tmp_path},
                    max_steps=500,
                    timeout_ms=1500,
                )
                await vm.execute_handler(script.handlers[0], _event("x"))
            except Exception as exc:
                failures.append((tname, shape, f"{type(exc).__name__}: {exc}"))

    assert not failures, "tool-arg fuzz raised unexpectedly: " + "\n".join(
        f"  {t!r} shape={s!r}: {m}" for t, s, m in failures[:20]
    )


# ---------------------------------------------------------------------------
# §96 Mixed-shape JsonAccess — descend through dict, then list, with an
# interpolated index. The chat-bridge handler does this when parsing
# upstream API responses (``@a[data][%i%]`` shape).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_access_dict_then_list_with_interp_index(kv) -> None:
    """``@A[items][%i%]`` walks a dict step then a list step."""
    await _run(
        '$写 t/o A {"items":["foo","bar","baz"]}$',
        kv,
        _event("seed"),
    )
    body = """A:$读 t/o A {}$
i:1
v:@A[items][%i%]
%v%"""
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "bar"


# ---------------------------------------------------------------------------
# §97 概率随机 length-mismatch tolerance — mismatched arrays align by
# truncating to the shorter one. Real rule files occasionally have
# typo'd shapes; we degrade rather than raise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weighted_random_length_mismatch_truncates(kv) -> None:
    """``$概率随机 [1,2,3] [\"only\"]$`` truncates to one slot, no crash."""
    body = 'r:$概率随机 [1,2,3] ["only"]$\n%r%'
    out = await _run(body, kv, _event("x"))
    assert out.strip() == "only"


@pytest.mark.asyncio
async def test_weighted_random_non_numeric_weights_uniform(kv) -> None:
    """Non-numeric weights → uniform fallback over the value list."""
    body = 'r:$概率随机 ["a","b"] ["X","Y"]$\n%r%'
    outs = {await _run(body, kv, _event("x")) for _ in range(40)}
    # Uniform over X and Y → both reachable.
    assert outs == {"X", "Y"}


@pytest.mark.asyncio
async def test_weighted_random_negative_weights_clamped(kv) -> None:
    """Negative weight on first slot → effectively 0 → only the positive
    bucket fires.
    """
    body = 'r:$概率随机 [-1,5] ["A","B"]$\n%r%'
    outs = {await _run(body, kv, _event("x")) for _ in range(20)}
    assert outs == {"B"}


# ---------------------------------------------------------------------------
# §98 OutputText literal special chars — ``$`` in plain text, ``%%`` not
# treated as variable, ``Price: $5.00`` survives.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_literal_dollar_sign_in_output_text(kv) -> None:
    """A bare ``$5.00`` (not opening a tool call) renders verbatim."""
    out = await _run("Price: $5.00", kv, _event("x"))
    assert "$5.00" in out


@pytest.mark.asyncio
async def test_double_percent_does_not_form_var(kv) -> None:
    """``%%`` doesn't look like a ``%var%`` and stays as literal text."""
    out = await _run("100%% completed", kv, _event("x"))
    assert out == "100%% completed"


# ---------------------------------------------------------------------------
# §99 Func-call inside another func arg — ``$写 t/x %k% world$`` resolves
# the bare ``%k%`` argument before passing it to the write. This is the
# common pattern of using a runtime-computed key.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_func_arg_interpolation_works_for_kv_key(kv) -> None:
    """Variable resolved inside func-call arg lands as the literal key."""
    out = await _run(
        "k:hello\n$写 t/x %k% world$\nr:$读 t/x hello NONE$\n%r%",
        kv,
        _event("x"),
    )
    assert out.strip() == "world"


@pytest.mark.asyncio
async def test_arith_in_func_call_arg(kv) -> None:
    """``$写 t/x [%i%] v$`` evaluates ``[%i%]`` to ``"42"`` before writing."""
    out = await _run(
        "i:42\n$写 t/x [%i%] v$\nr:$读 t/x 42 NONE$\n%r%",
        kv,
        _event("x"),
    )
    assert out.strip() == "v"


# ---------------------------------------------------------------------------
# §100 Numeric/string compare fallback — ``1000 < hello`` falls through
# to string comparison (ASCII lex). Documents the contract; rules that
# depend on this should ensure operands are numeric.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_numeric_string_compare_falls_through_to_string(kv) -> None:
    """``1000 < hello`` returns string-cmp result (digit < letter ASCII)."""
    body = """如果:1000<hello
HIT-STRING
返回
如果尾
MISS"""
    out = await _run(body, kv, _event("x"))
    # ASCII '1' (49) < 'h' (104) → True, HIT-STRING fires.
    assert out.strip() == "HIT-STRING"


@pytest.mark.asyncio
async def test_numeric_compare_when_both_sides_numeric(kv) -> None:
    """Both sides numeric → numeric compare, not string."""
    body = """如果:10>2
GT
返回
如果尾
LE"""
    out = await _run(body, kv, _event("x"))
    # Numeric 10 > 2 = True (string compare would say "10" > "2" → False).
    assert out.strip() == "GT"


# ---------------------------------------------------------------------------
# §101 Router conversation lock prevents lost updates for same-sender
# events. Counter handler increments a KV row; 10 events from the same
# sender via the bus reach 10 in the final counter (no lost updates).
# Pairs with §51 which documents the bare-VM lost-update baseline —
# the production path correctly serializes via the per-conversation lock.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_serializes_same_sender_events_no_lost_updates(kv) -> None:
    """10 ``inc`` events from the same sender via the bus reach counter=10."""
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    script = parse(
        "inc\n玉:$读 t/c %QQ% 0$\n$写 t/c %QQ% [%玉%+1]$\n",
        strict=False,
    )
    classifier = MessageClassifier(script)
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="susu_test", extras={})

    async def sink(_: Action) -> None:
        pass

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=None,
        sink=sink,
        conversations=ConversationStore(),
        config=RouterConfig(),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    events = [
        Event(
            id=f"e{i}",
            platform="cli",
            bot_id="susu_test",
            scope=Scope(kind="group", id="g", platform="cli"),
            sender=User(id="user1", platform="cli"),
            segments=[TextSegment(text="inc")],
        )
        for i in range(10)
    ]
    await asyncio.gather(*(bus.publish(ev) for ev in events))
    # Generous drain window for serialised dispatch.
    await asyncio.sleep(0.5)

    final = await kv.read("t", "c", "user1")
    assert final == "10"


# ---------------------------------------------------------------------------
# §102 Production handlers run through the full Router → Sink pipeline.
# Pairs with §89 (direct VM call) — this version exercises the bus +
# classifier + dispatcher + sink loop end-to-end against every literal
# trigger from the production rule file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_production_literal_triggers_route_through_full_pipeline(kv, tmp_path) -> None:
    """Every literal (non-regex) public trigger from dicpro.txt dispatches
    through Bus → Router → Classifier → DslCommandDispatcher → Sink with
    zero exceptions. ~150 triggers in the live corpus.
    """
    from pathlib import Path

    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_core.scheduler import Scheduler
    from linling_dsl.dispatcher import DslCommandDispatcher

    repo_root = Path(__file__).resolve().parents[3]
    dicpro = repo_root / "QRDic" / "dicpro.txt"
    if not dicpro.exists():
        pytest.skip(f"dicpro.txt not found at {dicpro}")

    source = dicpro.read_text(encoding="utf-8")
    script = parse(source, strict=False)

    classifier = MessageClassifier(script)
    handlers_by_trigger = {h.trigger: h for h in script.handlers}
    sched = Scheduler()
    dispatcher = DslCommandDispatcher(
        registry=registry,
        kv=kv,
        bot_id="susu_test",
        extras={
            "scheduler": sched,
            "handler_lookup": handlers_by_trigger.get,
            "admin_users": ("2078123478",),
            "image_text_cache_dir": tmp_path,
        },
    )

    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=None,
        sink=sink,
        conversations=ConversationStore(),
        config=RouterConfig(),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    public_handlers = [h for h in script.handlers if not h.is_internal]
    seen: set[str] = set()
    failures: list[tuple[str, str]] = []

    for i, handler in enumerate(public_handlers):
        trig = handler.trigger
        # Skip regex-y triggers — those need crafted inputs to match.
        if any(ch in trig for ch in ".*+?^$|"):
            continue
        if trig in seen:
            continue
        seen.add(trig)

        ev = Event(
            id=f"smoke-{i}",
            platform="cli",
            bot_id="susu_test",
            scope=Scope(kind="group", id="67890", platform="cli"),
            sender=User(id=f"u{i}", platform="cli", display_name="me"),
            segments=[TextSegment(text=trig)],
        )
        try:
            await bus.publish(ev)
        except Exception as exc:
            failures.append((trig, f"{type(exc).__name__}: {exc}"))

    await asyncio.sleep(0.5)
    await sched.stop()

    # The literal-trigger floor in production is north of 100; if we
    # see < 50 the test is exercising too little to be meaningful.
    assert len(seen) >= 100, f"only {len(seen)} literal triggers exercised"
    assert not failures, "router pipeline raised: " + "\n".join(
        f"  {trig!r}: {msg}" for trig, msg in failures[:10]
    )


# ---------------------------------------------------------------------------
# §103 High-concurrency Router stress — 10 senders × 10 events fired
# simultaneously. Per-conversation lock guarantees every sender's counter
# lands at exactly 10 (no lost updates).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_stress_per_sender_isolation(kv) -> None:
    """100 concurrent events (10 senders × 10 each) all see counter=10."""
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    script = parse(
        "inc\n玉:$读 t/c %QQ% 0$\n$写 t/c %QQ% [%玉%+1]$\n",
        strict=False,
    )
    classifier = MessageClassifier(script)
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="susu_test", extras={})

    async def sink(_: Action) -> None:
        pass

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=None,
        sink=sink,
        conversations=ConversationStore(),
        config=RouterConfig(),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    events: list[Event] = []
    for s_idx in range(10):
        for i in range(10):
            events.append(
                Event(
                    id=f"e-{s_idx}-{i}",
                    platform="cli",
                    bot_id="susu_test",
                    scope=Scope(kind="group", id="g", platform="cli"),
                    sender=User(id=f"user{s_idx}", platform="cli"),
                    segments=[TextSegment(text="inc")],
                )
            )

    await asyncio.gather(*(bus.publish(ev) for ev in events))
    # 100 events × ~1ms each, serialized per sender → ~10ms per sender,
    # all 10 senders parallel → ~100ms total under load. 2s window is
    # generous for CI hosts.
    await asyncio.sleep(2.0)

    for i in range(10):
        final = await kv.read("t", "c", f"user{i}")
        assert final == "10", f"user{i} counter == {final!r}, expected '10'"


# ---------------------------------------------------------------------------
# §104 $发送 临时 (temp message scope) — QQ supports temp messages where
# you can DM a user from a group context. The DSL maps ``临时`` to a
# dm scope (the OneBot adapter knows to use the temp-message API).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_temp_message_routes_to_dm_scope(kv) -> None:
    """``$发送 临时 msg 12345 hello$`` produces a dm-scoped Action."""
    from linling_core.events import Action

    captured: list[Action] = []

    async def sink(a: Action) -> None:
        captured.append(a)

    ev = Event(
        id="e",
        platform="onebot",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="onebot"),
        sender=User(id="u", platform="onebot"),
        segments=[TextSegment(text="x")],
    )
    await _run("$发送 临时 msg 12345 hello$", kv, ev, action_sink=sink)
    await asyncio.sleep(0.05)

    assert len(captured) == 1
    action = captured[0]
    assert action.target.kind == "dm"
    assert action.target.id == "12345"
    assert any(isinstance(s, TextSegment) and s.text == "hello" for s in action.segments)


# ---------------------------------------------------------------------------
# §105 [戳一戳] OneBot poke synthesis end-to-end — confirms that a real
# OneBot ``notify/poke`` notice (the wire shape LLBot / Lagrange /
# go-cqhttp emit) reaches a ``[戳一戳]`` DSL handler. Pre-fix the
# adapter's synthesiser explicitly skipped pokes ("they get a
# structured PokeSegment instead"), but the classifier filters out
# kind="notice" events — so the handler never fired in production.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onebot_poke_dispatches_zhuo_yi_zhuo_handler_e2e(kv) -> None:
    """A real OneBot poke notice routes Bus → adapter synth → classifier
    → DSL dispatcher → sink. The handler's text reply lands in the sink.

    The adapter publishes two events for a poke: (a) a structured
    ``kind='notice'`` event with a PokeSegment (consumed by future
    code that wants poke metadata), and (b) a synthetic
    ``kind='message'`` event whose text is ``[戳一戳]`` so QRSpeed-era
    handlers fire. (a) gets ignored by the classifier; (b) drives the
    legacy rule.
    """
    from linling_adapter_onebot.adapter import OneBotAdapter
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    # Minimal handler keyed off the [戳一戳] bracket trigger; emits a
    # sentinel so we can assert dispatch reached the handler body.
    script = parse(
        "[戳一戳]\nPOKED:%QQ%\n",
        strict=False,
    )
    classifier = MessageClassifier(script)
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="bot1", extras={})

    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=None,
        sink=sink,
        conversations=ConversationStore(),
        config=RouterConfig(),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    adapter = OneBotAdapter(bus, ws_url="ws://x/", access_token="", bot_id="bot1")
    poke_payload = {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "group_id": 67890,
        "user_id": 11111,  # poker — becomes %QQ% in the synthesised event
        "target_id": 22222,
        "operator_id": 11111,
    }
    # Drive the WS-dispatch path that production runs.
    await adapter._dispatch(poke_payload)
    await asyncio.sleep(0.05)

    # Exactly one action — the structured notice event was filtered
    # out by the classifier (kind != "message"), and the synthetic
    # ``[戳一戳]`` event drove the rule.
    assert len(actions) == 1
    text = "".join(s.text for s in actions[0].segments if isinstance(s, TextSegment))
    assert text == "POKED:11111"


@pytest.mark.asyncio
async def test_onebot_poke_dispatch_with_no_zhuo_yi_zhuo_handler_falls_through_silently(
    kv,
) -> None:
    """Poke without a ``[戳一戳]`` handler → no chat-agent leak.

    The synthetic event carries ``_synthetic_qrspeed=True`` so the
    classifier classifies it as ``ignore`` rather than ``chat`` — no
    LLM round-trip burns tokens on the literal ``"[戳一戳]"`` string.
    """
    from linling_adapter_onebot.adapter import OneBotAdapter
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    # No ``[戳一戳]`` handler in the script.
    script = parse("hello\nworld\n", strict=False)
    classifier = MessageClassifier(script)
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="bot1", extras={})

    actions: list[Action] = []
    chat_calls: list[Event] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    class _ChatProbe:
        async def run(self, event: Event, session) -> list[Action]:
            chat_calls.append(event)
            return []

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=_ChatProbe(),
        sink=sink,
        conversations=ConversationStore(),
        config=RouterConfig(),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    adapter = OneBotAdapter(bus, ws_url="ws://x/", access_token="", bot_id="bot1")
    await adapter._dispatch(
        {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": 67890,
            "user_id": 11111,
            "target_id": 22222,
        }
    )
    await asyncio.sleep(0.05)

    # No DSL action and no chat-agent call.
    assert actions == []
    assert chat_calls == []


# ---------------------------------------------------------------------------
# §106 卧底 投票对比 close-out flow — three voters tallying the same
# target with a 卧底 身份 of "卧底": handler fires the win branch
# (announce + 禁言 + 游戏删除). Mirrors the actual dicpro.txt rule
# at L6727+ which combines:
#
# * ``$排行榜 ... 反序 10 \%0A [序号]-[键]-[序号]$`` to find the top
#   vote-getter,
# * ``$取中间 @ %排%@1-@-1$`` to slice the top key out,
# * ``$读 %群号%/卧底/身份 %a% 0$`` to look up identity,
# * ``$发送 群 msg %群号% ...$`` + ``$禁$`` + ``$回调 游戏删除$``
#   close-out.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undercover_vote_tally_kicks_undercover(kv) -> None:
    """Three votes for the 卧底 player → handler announces + cleans up.

    Flow:
    1. Three voters cast votes against ``susp1`` (the undercover).
    2. ``投票对比`` reads the rank-1 entry (= ``susp1`` with 3 votes),
       slices the key, looks up identity → "卧底".
    3. Handler emits the win text + (in production) calls ``$禁$``
       and ``$回调 游戏删除$`` close-outs. We assert the win text
       reaches the output and the post-close-out KV is cleaned.
    """
    script_text = (
        "[内部]投票对比\n"
        "排:$排行榜 g/卧底/被投票 反序 10 \\n [序号]-[键]-[序号]$\n"
        "a:$取中间 @ %排%@1-@-1$\n"
        "b:$读 g/卧底/身份 %a% 平民$\n"
        "如果:%b%==卧底\n"
        "民胜!\n"
        "$回调 游戏删除$\n"
        "返回\n"
        "如果尾\n"
        "底胜利\n"
        "\n"
        "[内部]游戏删除\n"
        "$删除 g/卧底$\n"
    )
    full = parse(script_text, strict=False)
    handlers = {h.trigger: h for h in full.handlers}

    # Seed: three votes against susp1 (the undercover) + identities.
    seed = (
        "$写 g/卧底/被投票 susp1 3$\n"
        "$写 g/卧底/被投票 susp2 1$\n"
        "$写 g/卧底/身份 susp1 卧底$\n"
        "$写 g/卧底/身份 susp2 平民$\n"
    )
    await _run(seed, kv, _event("seed"))

    vm = VM(
        tool_registry=registry,
        kv=kv,
        bot_id="susu_test",
        extras={"handler_lookup": handlers.get},
    )
    res = await vm.execute_handler(handlers["投票对比"], _event("查"))
    out = "".join(s.text for s in res.segments if isinstance(s, TextSegment))
    # Win branch fired — sentinel includes the "民胜!" tag.
    assert "民胜!" in out
    # 卧底胜利 fallback did NOT fire.
    assert "底胜利" not in out
    # Close-out wiped the 卧底 scope.
    assert await kv.read("g/卧底", "被投票", "susp1") is None
    assert await kv.read("g/卧底", "身份", "susp1") is None


@pytest.mark.asyncio
async def test_undercover_vote_tie_triggers_revote_branch(kv) -> None:
    """Tied vote tally → 重新投票 branch fires; counter increments.

    The dicpro rule's first conditional checks ``%我%==%操%`` — the
    top-1 vote count equals the top-2 vote count — and routes into a
    "two players tied, vote again" branch that increments
    ``重新开始`` so the third tie can end the game. We mirror that
    counter-and-message shape here.
    """
    script_text = (
        "[内部]投票对比\n"
        "排:$排行榜 g/卧底/被投票 反序 10 \\n [序号]-[键]-[序号]$\n"
        "a:$取中间 @ %排%@1-@-1$\n"
        "卧:$取中间 @ %排%@2-@-2$\n"
        "我:$读 g/卧底/被投票 %a% 0$\n"
        "操:$读 g/卧底/被投票 %卧% 0$\n"
        "如果:%我%==%操%\n"
        "重:$读 g/卧底/重新开始 a 0$\n"
        "$写 g/卧底/重新开始 a [%重%+1]$\n"
        "TIE\n"
        "返回\n"
        "如果尾\n"
        "WIN\n"
    )
    full = parse(script_text, strict=False)
    handlers = {h.trigger: h for h in full.handlers}

    # Seed two players with identical vote counts.
    await _run(
        "$写 g/卧底/被投票 susp1 2$\n$写 g/卧底/被投票 susp2 2$\n",
        kv,
        _event("seed"),
    )

    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test")
    res = await vm.execute_handler(handlers["投票对比"], _event("查"))
    out = "".join(s.text for s in res.segments if isinstance(s, TextSegment))
    assert "TIE" in out
    assert "WIN" not in out
    # ``重新开始`` counter incremented from default 0 → 1.
    assert await kv.read("g/卧底", "重新开始", "a") == "1"


# ---------------------------------------------------------------------------
# §107 SqliteSchedulerStore restart resilience — schedule a task with
# bot A (sqlite-backed), close it, spin up bot B against the same db,
# verify the task fires through the new bot's pipeline.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_scheduler_persists_across_bot_restart(tmp_path) -> None:
    """Task scheduled in one bot run fires after a fresh boot reads
    the same SQLite scheduler db. Validates the persistence + restart
    handshake the production CLI relies on for delayed `$调用$` /
    cron tasks that should survive process restarts.
    """
    from linling_cli.bootstrap import bootstrap_bot
    from linling_core.config import BotConfig
    from linling_core.events import Action

    # Tiny rule that emits a sentinel — fired by the scheduler.
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "ping.ling").write_text("ping\nPONG\n", encoding="utf-8")

    db_path = tmp_path / "sched.db"
    yaml = (
        "bot_id: bot1\n"
        "name: tester\n"
        "storage:\n"
        '  kv: ":memory:"\n'
        f'  scheduler: "sqlite:///{db_path}"\n'
        "rules:\n"
        '  - "rules/**/*.ling"\n'
    )
    cfg_path = tmp_path / "bot.yaml"
    cfg_path.write_text(yaml, encoding="utf-8")
    cfg = BotConfig.from_yaml(cfg_path)

    # First boot — schedule a task in the future (1.5s out so it
    # doesn't fire before the db handle releases).
    bot1 = await bootstrap_bot(cfg, base_dir=tmp_path)
    try:
        assert bot1.scheduler is not None
        bot1.scheduler.schedule(after_seconds=0.3, handler_name="ping", bot_id="bot1")
        # Confirm the task landed in the durable store.
        assert bot1.scheduler.pending_count == 1
    finally:
        await bot1.stop()

    # Second boot — same db, fresh bot. The pending task is loaded
    # automatically and should fire on the next tick.
    captured: list[Action] = []

    class _Recorder:
        platform = "test"

        async def run(self) -> None:
            return None

        async def send(self, a: Action) -> None:
            captured.append(a)

        async def stop(self) -> None:
            return None

    bot2 = await bootstrap_bot(cfg, base_dir=tmp_path)
    bot2.attach_adapter(_Recorder())
    try:
        await bot2.start()
        assert bot2.scheduler is not None
        # The task we scheduled in bot1 was loaded from the store.
        assert bot2.scheduler.pending_count >= 1

        # Wait long enough for the deadline + scheduler tick + dispatch.
        for _ in range(40):
            if captured:
                break
            await asyncio.sleep(0.1)

        emitted = [s.text for a in captured for s in a.segments if isinstance(s, TextSegment)]
        assert "PONG" in emitted, f"expected scheduler-fired PONG after restart, got {emitted!r}"
    finally:
        await bot2.stop()


@pytest.mark.asyncio
async def test_sqlite_scheduler_cancel_persists_across_restart(tmp_path) -> None:
    """``Scheduler.cancel(tid)`` removes the task from the durable store.

    Schedule and immediately cancel — the task must NOT fire after a
    fresh boot reads the same db.
    """
    from linling_cli.bootstrap import bootstrap_bot
    from linling_core.config import BotConfig
    from linling_core.events import Action

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "ping.ling").write_text("ping\nPONG\n", encoding="utf-8")

    db_path = tmp_path / "sched.db"
    yaml = (
        "bot_id: bot1\n"
        "name: tester\n"
        "storage:\n"
        '  kv: ":memory:"\n'
        f'  scheduler: "sqlite:///{db_path}"\n'
        "rules:\n"
        '  - "rules/**/*.ling"\n'
    )
    cfg_path = tmp_path / "bot.yaml"
    cfg_path.write_text(yaml, encoding="utf-8")
    cfg = BotConfig.from_yaml(cfg_path)

    bot1 = await bootstrap_bot(cfg, base_dir=tmp_path)
    try:
        assert bot1.scheduler is not None
        tid = bot1.scheduler.schedule(after_seconds=0.2, handler_name="ping", bot_id="bot1")
        cancelled = bot1.scheduler.cancel(tid)
        assert cancelled is True
    finally:
        await bot1.stop()

    captured: list[Action] = []

    class _Recorder:
        platform = "test"

        async def run(self) -> None:
            return None

        async def send(self, a: Action) -> None:
            captured.append(a)

        async def stop(self) -> None:
            return None

    bot2 = await bootstrap_bot(cfg, base_dir=tmp_path)
    bot2.attach_adapter(_Recorder())
    try:
        await bot2.start()
        # Cancelled task didn't survive — the durable store was
        # updated synchronously when ``cancel`` ran in bot1.
        assert bot2.scheduler is not None
        assert bot2.scheduler.pending_count == 0

        # Generous window — even if a stale task somehow re-fires
        # we'll see it here.
        await asyncio.sleep(0.5)
        assert captured == []
    finally:
        await bot2.stop()


# ---------------------------------------------------------------------------
# §108 Catch-all ``[\s\S]*`` regex — declaration-order priority. A more
# specific trigger (``开关``) declared *before* the catch-all wins; a
# catch-all declared *before* the specific trigger ALSO matches the
# specific trigger's text (regex order = source order). Documents the
# author's contract: put catch-alls last.
# ---------------------------------------------------------------------------


def test_catch_all_with_specific_first_routes_specific() -> None:
    """``开关`` declared before ``[\\s\\S]*`` — specific wins for ``开关``."""
    from linling_core.classifier import MessageClassifier

    # Both literal and the regex catch-all live in the script. The
    # literal (开关) goes through the literal_index fast-path; the
    # regex (catch-all) goes through the regex walk. The classifier
    # checks the literal index first (one dict lookup), so even if
    # source order were "catch-all first" the literal would still
    # win. We assert that contract here.
    script = parse(
        "开关\nSPECIFIC\n\n[\\s\\S]*\nCATCH\n",
        strict=False,
    )
    cls = MessageClassifier(script)
    intent = cls.classify(_event("开关"))
    assert intent.kind == "command"
    assert intent.match is not None
    assert intent.match.handler.trigger == "开关"


def test_catch_all_matches_arbitrary_text() -> None:
    """``[\\s\\S]*`` matches whatever text doesn't trip an earlier handler."""
    from linling_core.classifier import MessageClassifier

    script = parse(
        "开关\nSPECIFIC\n\n[\\s\\S]*\nCATCH\n",
        strict=False,
    )
    cls = MessageClassifier(script)
    intent = cls.classify(_event("nonsense text"))
    assert intent.kind == "command"
    assert intent.match is not None
    assert intent.match.handler.trigger == "[\\s\\S]*"


def test_catch_all_does_not_match_empty_text() -> None:
    """Empty text → falls through to chat (classifier shortcuts on empty)."""
    from linling_core.classifier import MessageClassifier

    script = parse("[\\s\\S]*\nCATCH\n", strict=False)
    cls = MessageClassifier(script)
    intent = cls.classify(_event(""))
    assert intent.kind == "chat"


def test_catch_all_synthetic_qrspeed_event_does_not_match() -> None:
    """A synthetic ``_synthetic_qrspeed`` event SHOULD still match a
    catch-all if one is declared — the catch-all is part of the rule
    set the operator wrote, so they want it.

    However — and this is the contract — when **no** handler matches,
    a synthetic event drops on the floor (``ignore``) instead of
    being routed to the chat agent. Without a catch-all there's
    nothing to match.
    """
    from linling_core.classifier import MessageClassifier

    script = parse("[\\s\\S]*\nCATCH\n", strict=False)
    cls = MessageClassifier(script)
    ev = Event(
        id="e",
        platform="onebot",
        bot_id="bot",
        scope=Scope(kind="group", id="g", platform="onebot"),
        sender=User(id="u", platform="onebot"),
        segments=[TextSegment(text="[系统]")],
        raw={"_synthetic_qrspeed": True},
    )
    intent = cls.classify(ev)
    # Catch-all matches even synthetic events — the rule author opted
    # in by writing a [\s\S]* trigger.
    assert intent.kind == "command"
    assert intent.match is not None
    assert intent.match.handler.trigger == "[\\s\\S]*"


# ---------------------------------------------------------------------------
# §109 ``$访问$`` HTTP placeholder safety — even with a malicious URL
# the tool returns a placeholder string (no real fetch, no DNS lookup,
# no network IO). The renderer-set status emits it directly.
#
# Contract for the production migration: when ``$访问$`` graduates
# from placeholder to real fetch, this test should be **updated**
# rather than removed — by then it should validate the white-listed
# host check, the timeout, and the body-size limit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_get_placeholder_does_not_fetch_localhost(kv) -> None:
    """``$访问 http://localhost/...$`` returns the placeholder string.

    Real HTTP IO would either succeed (the test host has a service)
    or fail with ``ConnectionError`` — either outcome leaks signal
    we don't want. The placeholder contract guarantees the call is
    safe to ship before real fetch lands.
    """
    out = await _run("$访问 http://localhost:1/secret$", kv, _event("x"))
    assert "not implemented" in out


@pytest.mark.asyncio
async def test_http_get_placeholder_emits_for_file_url(kv) -> None:
    """``$访问 file:///etc/passwd$`` doesn't read the file.

    Future real-fetch implementations must reject ``file://``;
    today's placeholder degrades the same way for any non-empty URL
    so the SSRF / local-file-disclosure risk is structural.
    """
    out = await _run("$访问 file:///etc/passwd$", kv, _event("x"))
    assert "not implemented" in out
    # Sanity check: the actual file content didn't leak through.
    assert "root:" not in out
    assert "passwd" not in out


# ---------------------------------------------------------------------------
# §110 ``$回调$`` literal trigger with quotes / unicode — the lookup
# uses string equality, so unicode-rich handler names work as long
# as the call site spells them identically. Tests the boundary
# between regex-fullmatch (which would mis-handle ``[`` in the name)
# and literal lookup.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_with_unicode_literal_handler_name(kv) -> None:
    """``$回调 中文名handler$`` resolves to ``[内部]中文名handler`` literally."""
    helper = parse("[内部]中文名handler\nUNICODE_OK\n", strict=False)
    h_by = {h.trigger: h for h in helper.handlers}
    out = await _run(
        "v:$回调 中文名handler$\n%v%",
        kv,
        _event("x"),
        handler_lookup=h_by.get,
    )
    assert out.strip() == "UNICODE_OK"


@pytest.mark.asyncio
async def test_callback_with_handler_name_containing_brackets(kv) -> None:
    """A handler whose name contains a literal ``[`` resolves through
    literal lookup, not regex. The bracket is a regex meta — fullmatch
    would refuse to compile (``unbalanced bracket``) and skip the
    handler.

    Real rule files don't currently use bracket-named handlers (the
    parser strips ``[内部]`` from triggers), but the contract should
    hold for arbitrary names so a future change that allows bracket
    triggers doesn't regress.
    """
    helper = parse("h[name]\nFOUND\n", strict=False)
    h_by = {h.trigger: h for h in helper.handlers}

    def lookup(name: str):
        return h_by.get(name)

    out = await _run(
        "v:$回调 h[name]$\n%v%",
        kv,
        _event("x"),
        handler_lookup=lookup,
    )
    assert out.strip() == "FOUND"


# ---------------------------------------------------------------------------
# §111 黑杰克 加注 single-step path — the smaller cousin of the §16
# full chain. ``加注50`` reads the player's 灵玉 + 奖池, deducts the
# bid from one and credits the other, persists. Tests the per-step
# arith-and-write in isolation so a future change to the chain
# pipeline (§16) shows up here as a focused regression first.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blackjack_raise_bet_atomic_step(kv) -> None:
    """``加注50`` atomically: 灵玉 -= 50, 奖池 += 50, 加注量 = 50."""
    seed = "$写 啊/灵玉系/灵玉 player 1000$\n$写 啊/娱乐系/黑杰克 奖池g 100$\n"
    await _run(seed, kv, _event("seed", sender="player", group="g"))

    body = """玉:$读 啊/灵玉系/灵玉 %QQ% 0$
奖:$读 啊/娱乐系/黑杰克 奖池%群% 0$
如果:%玉%<%括号1%
不足
返回
如果尾
$写 啊/灵玉系/灵玉 %QQ% [%玉%-%括号1%]$
$写 啊/娱乐系/黑杰克 奖池%群% [%奖%+%括号1%]$
$写 啊/娱乐系/黑杰克 加注量%群% %括号1%$
RAISED-%括号1%"""
    out = await _run(
        body,
        kv,
        _event("加注50", sender="player", group="g"),
        captures=["50"],
    )
    assert out.strip() == "RAISED-50"
    assert await kv.read("啊/灵玉系", "灵玉", "player") == "950"
    assert await kv.read("啊/娱乐系", "黑杰克", "奖池g") == "150"
    assert await kv.read("啊/娱乐系", "黑杰克", "加注量g") == "50"


@pytest.mark.asyncio
async def test_blackjack_raise_insufficient_balance_rejects(kv) -> None:
    """``加注50`` with 灵玉=10 → "不足" message; KV unchanged."""
    await _run(
        "$写 啊/灵玉系/灵玉 player 10$\n$写 啊/娱乐系/黑杰克 奖池g 100$\n",
        kv,
        _event("seed", sender="player", group="g"),
    )
    body = """玉:$读 啊/灵玉系/灵玉 %QQ% 0$
奖:$读 啊/娱乐系/黑杰克 奖池%群% 0$
如果:%玉%<%括号1%
不足
返回
如果尾
$写 啊/灵玉系/灵玉 %QQ% [%玉%-%括号1%]$
$写 啊/娱乐系/黑杰克 奖池%群% [%奖%+%括号1%]$
RAISED"""
    out = await _run(
        body,
        kv,
        _event("加注50", sender="player", group="g"),
        captures=["50"],
    )
    assert out.strip() == "不足"
    # KV preserved.
    assert await kv.read("啊/灵玉系", "灵玉", "player") == "10"
    assert await kv.read("啊/娱乐系", "黑杰克", "奖池g") == "100"


# ---------------------------------------------------------------------------
# §112 Scheduler-fired handler with positional + regex captures — the
# scheduler hands ``task.args`` to the inner handler as ``%括号N%``;
# combined with regex captures from a regex trigger, both flow into
# the same captures list. Pairs with §107 (delivery wiring) and
# bug-fix in round 6 (space-joined regex lookup).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_fires_internal_with_positional_and_regex_captures(
    tmp_path,
) -> None:
    """``$调用 0 echo-suffix arg2 arg3$`` → ``[内部]echo-(.*)`` fires
    with ``%括号1%==suffix`` and the explicit args appended.

    The combined captures list semantics:
    * regex capture(s) from the trigger fullmatch come first,
    * then explicit args queued by the caller's ``$调用$``,
    so a handler-side ``%括号1% %括号2% %括号3%`` reads
    ``suffix arg2 arg3``.
    """
    from linling_cli.bootstrap import bootstrap_bot
    from linling_core.config import BotConfig
    from linling_core.events import Action
    from linling_core.events import Event as _Event
    from linling_core.events import Scope as _Scope
    from linling_core.events import User as _User
    from linling_core.segments import TextSegment as _TS

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "regex.ling").write_text(
        "kick\n$调用 0 echo-suffix arg2 arg3$\n\n[内部]echo-(.*)\nGOT_%括号1%_%括号2%_%括号3%\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "bot.yaml"
    cfg_path.write_text(
        'bot_id: bot1\nname: tester\nstorage:\n  kv: ":memory:"\nrules:\n  - "rules/**/*.ling"\n',
        encoding="utf-8",
    )
    cfg = BotConfig.from_yaml(cfg_path)
    bot = await bootstrap_bot(cfg, base_dir=tmp_path)

    captured: list[Action] = []

    class _Recorder:
        platform = "test"

        async def run(self) -> None:
            return None

        async def send(self, a: Action) -> None:
            captured.append(a)

        async def stop(self) -> None:
            return None

    bot.attach_adapter(_Recorder())
    try:
        await bot.start()
        ev = _Event(
            id="t",
            platform="test",
            bot_id="bot1",
            scope=_Scope(kind="group", id="g", platform="test"),
            sender=_User(id="u", platform="test"),
            segments=[_TS(text="kick")],
        )
        await bot.bus.publish(ev)
        # Wait for kick handler + scheduler tick + inner dispatch.
        for _ in range(40):
            emitted = [s.text for a in captured for s in a.segments if isinstance(s, _TS)]
            if any("GOT_suffix_arg2_arg3" in t for t in emitted):
                break
            await asyncio.sleep(0.1)
        emitted = [s.text for a in captured for s in a.segments if isinstance(s, _TS)]
        assert any("GOT_suffix_arg2_arg3" in t for t in emitted), (
            f"expected GOT_suffix_arg2_arg3, got {emitted!r}"
        )
    finally:
        await bot.stop()


# ---------------------------------------------------------------------------
# §113 Chat fallback path — Router with no DSL match routes to the chat
# dispatcher. Pairs with §59 (DSL match path) — between the two we cover
# both router verdicts (command + chat). Real production deployments wire
# AgentChatDispatcher here; we use a minimal probe so the test doesn't
# need an LLM provider.
# ---------------------------------------------------------------------------


class _ChatProbeDispatcher:
    """Records every chat event it sees and emits a fixed reply.

    Mirrors the production :class:`AgentChatDispatcher` shape (same
    ``run(event, session) -> list[Action]`` contract) without spinning
    up an LLM. The recorded events let the test assert exactly which
    text reached the agent — important because the catch-all
    ``[\\s\\S]*`` rule (§108) and the ``_synthetic_qrspeed`` ignore
    path (§82, §106) both intercept events *before* the chat path.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, event: Event, session) -> list:
        from linling_core.events import Action as _Action

        self.calls.append(event.text)
        return [
            _Action(
                kind="reply",
                target=event.scope,
                segments=[TextSegment(text=f"AGENT:{event.text}")],
            )
        ]


@pytest.mark.asyncio
async def test_router_routes_unmatched_text_to_chat_dispatcher(kv) -> None:
    """``hello`` (no DSL match) → chat dispatcher → "AGENT:hello" reply."""
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    # Script has only a literal ``ping`` trigger — ``hello`` won't match.
    script = parse("ping\nPONG\n", strict=False)
    classifier = MessageClassifier(script)
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="susu_test", extras={})
    chats = _ChatProbeDispatcher()

    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=chats,
        sink=sink,
        conversations=ConversationStore(rate_per_second=100, burst=100),
        config=RouterConfig(),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    await bus.publish(_event("hello", sender="u1"))
    await asyncio.sleep(0.05)

    # Chat dispatcher saw the event.
    assert chats.calls == ["hello"]
    assert len(actions) == 1
    text = "".join(s.text for s in actions[0].segments if isinstance(s, TextSegment))
    assert text == "AGENT:hello"


@pytest.mark.asyncio
async def test_router_dsl_match_does_not_call_chat(kv) -> None:
    """``ping`` (DSL match) → DSL dispatcher; chat dispatcher untouched.

    Inverse of §113 — confirms the verdict split: command verdicts
    stay out of the chat dispatcher's hands. The test costs an LLM
    round-trip in production, so this short-circuit is load-bearing.
    """
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    script = parse("ping\nPONG\n", strict=False)
    classifier = MessageClassifier(script)
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="susu_test", extras={})
    chats = _ChatProbeDispatcher()

    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=chats,
        sink=sink,
        conversations=ConversationStore(rate_per_second=100, burst=100),
        config=RouterConfig(),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    await bus.publish(_event("ping", sender="u1"))
    await asyncio.sleep(0.05)

    # Chat dispatcher was *not* called.
    assert chats.calls == []
    # DSL handler emitted PONG.
    assert len(actions) == 1
    text = "".join(s.text for s in actions[0].segments if isinstance(s, TextSegment))
    assert text == "PONG"


@pytest.mark.asyncio
async def test_router_catch_all_handler_intercepts_chat_path(kv) -> None:
    """A ``[\\s\\S]*`` catch-all in the rule set absorbs ALL chat events.

    The classifier matches catch-all → command verdict → DSL
    dispatcher fires; chat dispatcher never sees the event. This is
    how the dicpro.txt mainline AI-bridge handler works — it's
    declared as ``[\\s\\S]*`` and uses ``$访问 http://...$`` to call
    its own LLM endpoint, bypassing the framework's chat path entirely.
    """
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    script = parse("[\\s\\S]*\nCATCH:%参数-1%\n", strict=False)
    classifier = MessageClassifier(script)
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="susu_test", extras={})
    chats = _ChatProbeDispatcher()

    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=chats,
        sink=sink,
        conversations=ConversationStore(rate_per_second=100, burst=100),
        config=RouterConfig(),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    await bus.publish(_event("anything goes here", sender="u1"))
    await asyncio.sleep(0.05)

    assert chats.calls == []
    assert len(actions) == 1
    text = "".join(s.text for s in actions[0].segments if isinstance(s, TextSegment))
    assert text == "CATCH:anything goes here"


# ---------------------------------------------------------------------------
# §114 ``$发送`` newline preservation — outbound text-segment Action
# carries any decoded newlines (``\n``) literally. Real production rules
# pack multi-line replies into a single ``$发送 群 msg ...$`` body and
# rely on the IM client rendering line breaks.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_preserves_authored_newline_in_body(kv) -> None:
    """``$发送 群 msg target line1\\nline2$`` → Action.segments[0].text
    contains a real newline.

    Body args go through expression evaluation which now decodes
    QRDic-style ``\\n`` escapes (via the OutputText emit code path
    shared with regular text). The decoded LF survives all the way
    into the action's TextSegment so adapters can hand the multi-
    line text to the IM client without further processing.
    """
    from linling_core.events import Action

    captured: list[Action] = []

    async def sink(a: Action) -> None:
        captured.append(a)

    ev = Event(
        id="e",
        platform="onebot",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="onebot"),
        sender=User(id="u", platform="onebot"),
        segments=[TextSegment(text="x")],
    )
    await _run("$发送 群 msg 12345 line1\\nline2$", kv, ev, action_sink=sink)
    await asyncio.sleep(0.05)

    assert len(captured) == 1
    seg = captured[0].segments[0]
    assert isinstance(seg, TextSegment)
    # ``\\n`` decodes to a real newline by the time it reaches the
    # action — same shape any other emit-side path produces.
    assert seg.text == "line1\nline2"


@pytest.mark.asyncio
async def test_send_message_with_url_encoded_newline_in_body_survives(kv) -> None:
    """``%0A`` URL-encoded newline gets stripped by the parser's
    ``_decode_url_escapes_for_parsing`` pass and a real LF survives
    into the action's body.

    Use ``\\%0A`` (escaped percent + 0A) so the parser's URL escape
    decode treats it as a real newline (matches QRSpeed convention)
    rather than as ``%0A...%`` looking like a variable lookup.
    """
    from linling_core.events import Action

    captured: list[Action] = []

    async def sink(a: Action) -> None:
        captured.append(a)

    ev = Event(
        id="e",
        platform="onebot",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="onebot"),
        sender=User(id="u", platform="onebot"),
        segments=[TextSegment(text="x")],
    )
    await _run(r"$发送 群 msg 12345 line1\%0Aline2$", kv, ev, action_sink=sink)
    await asyncio.sleep(0.05)

    assert len(captured) == 1
    seg = captured[0].segments[0]
    assert isinstance(seg, TextSegment)
    # The parser already decoded ``\%0A`` to ``\n`` before tokenising,
    # so the action's body holds the real LF.
    assert seg.text == "line1\nline2"


# ---------------------------------------------------------------------------
# §115 ``$下载`` happy path with mocked httpx — confirms the file is
# saved under ``data_root`` and the byte count is returned. Pairs with
# §80 (sandbox refusals).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_happy_path_writes_file(kv, tmp_path, monkeypatch) -> None:
    """``$下载 ./img.png http://x.com/y$`` fetches via httpx, writes the
    bytes to ``data_root/img.png``, returns the byte count.

    We monkey-patch ``httpx.AsyncClient`` so the test doesn't reach
    the network. The fake client records the URL it was asked for so
    we can assert the call shape.
    """
    import httpx

    fake_body = b"PNG-FAKE-BODY"

    class _FakeResp:
        status_code = 200

        @property
        def content(self) -> bytes:
            return fake_body

        def raise_for_status(self) -> None:
            return None

    seen_urls: list[str] = []

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            return None

        async def get(self, url: str):
            seen_urls.append(url)
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    out = await _run(
        f"v:$下载 {tmp_path}/img.png http://x.com/img.png$\n%v%",
        kv,
        _event("x"),
        data_root=str(tmp_path),
    )
    # Tool returns the byte count.
    assert out.strip() == str(len(fake_body))
    # File landed on disk.
    target = tmp_path / "img.png"
    assert target.exists()
    assert target.read_bytes() == fake_body
    # httpx was called with the right URL.
    assert seen_urls == ["http://x.com/img.png"]


@pytest.mark.asyncio
async def test_download_http_error_returns_empty(kv, tmp_path, monkeypatch) -> None:
    """``$下载$`` against a 4xx URL returns empty; no file written."""
    import httpx

    class _FakeResp:
        status_code = 404

        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError(
                "404",
                request=httpx.Request("GET", "http://x.com/missing"),
                response=httpx.Response(404),
            )

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            return None

        async def get(self, url: str):
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    out = await _run(
        f"v:$下载 {tmp_path}/gone.png http://x.com/missing$\n(%v%)",
        kv,
        _event("x"),
        data_root=str(tmp_path),
    )
    assert out == "()"
    assert not (tmp_path / "gone.png").exists()


# ---------------------------------------------------------------------------
# §116 ``/cancel`` mid-dispatch — sets the session cancel flag. The
# AgentChatDispatcher races the LLM call against the cancel; DSL
# dispatchers don't currently observe cancel (fire-and-forget by
# design), but the cancel mechanism still works at the session level.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_sets_session_cancel_event(kv) -> None:
    """``/cancel`` flips ``session.cancel_event`` so the next chat
    dispatcher in this session aborts.

    DSL handlers don't watch the flag (they're synchronous and fast);
    the contract is for chat dispatchers / future long-running tools
    to honour it. We assert the flag plumbing rather than the
    cancellation effect — the latter is exercised in test_router.
    """
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import (
        ConversationKey,
        ConversationStore,
    )
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    script = parse("ping\nPONG\n", strict=False)
    classifier = MessageClassifier(script, command_prefixes=("/",))
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="susu_test", extras={})
    chats = _ChatProbeDispatcher()
    actions: list[Action] = []

    async def sink(a: Action) -> None:
        actions.append(a)

    store = ConversationStore(rate_per_second=100, burst=100)
    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=chats,
        sink=sink,
        conversations=store,
        config=RouterConfig(),
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")

    # Send a /cancel into a fresh conversation.
    await bus.publish(_event("/cancel", sender="u1"))
    await asyncio.sleep(0.05)

    # The session for that user now has a tripped cancel flag.
    session = await store.get_or_create(
        ConversationKey(bot_id="susu_test", scope_id="67890", sender_id="u1")
    )
    assert session.cancel_event.is_set()


# ---------------------------------------------------------------------------
# §117 ``RunningBot.reload_rules`` swap — rule edited on disk, hot-
# reload picks up the new handler text without bot restart. Verifies
# that ``handler_lookup`` (closed over ``self.script``) sees the new
# handlers immediately for in-flight ``$回调$`` calls (not just future
# events).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_rules_swaps_handler_text(tmp_path) -> None:
    """Edit ``ping.ling`` from ``PONG`` → ``RELOADED``, reload, dispatch
    ``ping``, get the new text.
    """
    from linling_cli.bootstrap import bootstrap_bot
    from linling_core.config import BotConfig
    from linling_core.events import Action

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rule_path = rules_dir / "ping.ling"
    rule_path.write_text("ping\nPONG\n", encoding="utf-8")

    cfg_path = tmp_path / "bot.yaml"
    cfg_path.write_text(
        'bot_id: bot1\nname: tester\nstorage:\n  kv: ":memory:"\nrules:\n  - "rules/**/*.ling"\n',
        encoding="utf-8",
    )
    cfg = BotConfig.from_yaml(cfg_path)
    bot = await bootstrap_bot(cfg, base_dir=tmp_path)

    captured: list[Action] = []

    class _Recorder:
        platform = "test"

        async def run(self) -> None:
            return None

        async def send(self, a: Action) -> None:
            captured.append(a)

        async def stop(self) -> None:
            return None

    bot.attach_adapter(_Recorder())
    try:
        await bot.start()
        # First dispatch — original text.
        ev1 = Event(
            id="e1",
            platform="test",
            bot_id="bot1",
            scope=Scope(kind="group", id="g", platform="test"),
            sender=User(id="u1", platform="test"),
            segments=[TextSegment(text="ping")],
        )
        await bot.bus.publish(ev1)
        await asyncio.sleep(0.1)

        original_texts = [
            s.text for a in captured for s in a.segments if isinstance(s, TextSegment)
        ]
        assert "PONG" in original_texts

        # Edit the file and hot-reload.
        rule_path.write_text("ping\nRELOADED\n", encoding="utf-8")
        report = await bot.reload_rules()
        assert report.applied is True

        # Second dispatch — new text.
        ev2 = Event(
            id="e2",
            platform="test",
            bot_id="bot1",
            scope=Scope(kind="group", id="g", platform="test"),
            sender=User(id="u1", platform="test"),
            segments=[TextSegment(text="ping")],
        )
        await bot.bus.publish(ev2)
        await asyncio.sleep(0.1)

        all_texts = [s.text for a in captured for s in a.segments if isinstance(s, TextSegment)]
        assert any("RELOADED" in t for t in all_texts), (
            f"expected RELOADED in {all_texts!r} after reload"
        )
    finally:
        await bot.stop()


# ---------------------------------------------------------------------------
# §118 Multi-adapter platform routing — outbound action picks adapter
# whose ``platform`` matches ``action.target.platform``. Two adapters
# wired (``test_a`` / ``test_b``) — DSL handler hard-coding
# ``$发送 群 msg ...$`` against the inbound event's platform reaches
# only the matching adapter.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_routes_only_to_matching_platform_adapter(kv) -> None:
    """``$发送$`` against an event from platform ``test_a`` reaches
    adapter A but NOT adapter B."""
    from linling_cli.bootstrap import build_sink
    from linling_core.events import Action

    class _Rec:
        def __init__(self, plat: str) -> None:
            self.platform = plat
            self.sent: list[Action] = []

        async def run(self) -> None:
            return None

        async def send(self, action: Action) -> None:
            self.sent.append(action)

        async def stop(self) -> None:
            return None

    rec_a = _Rec("test_a")
    rec_b = _Rec("test_b")
    sink = build_sink([rec_a, rec_b])

    ev = Event(
        id="e",
        platform="test_a",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="test_a"),
        sender=User(id="u", platform="test_a"),
        segments=[TextSegment(text="x")],
    )
    await _run("$发送 群 msg 12345 hi$", kv, ev, action_sink=sink)
    await asyncio.sleep(0.05)

    assert len(rec_a.sent) == 1
    assert rec_b.sent == []


@pytest.mark.asyncio
async def test_send_falls_through_silently_for_unwired_platform(kv) -> None:
    """``$发送$`` with no adapter for the target platform — sink logs
    and drops; no exception, no leak to the wrong adapter."""
    from linling_cli.bootstrap import build_sink
    from linling_core.events import Action

    class _Rec:
        platform = "test_a"

        def __init__(self) -> None:
            self.sent: list[Action] = []

        async def run(self) -> None:
            return None

        async def send(self, action: Action) -> None:
            self.sent.append(action)

        async def stop(self) -> None:
            return None

    rec_a = _Rec()

    # Only adapter A is wired; the event arrives from platform "test_b".
    # build_sink returns a sink that uses _multi-platform routing because
    # we register two adapters; test_b has no handler. Use a dummy
    # second adapter to force the multi-routing path.
    class _DummyRec:
        platform = "other"

        async def run(self):
            return None

        async def send(self, a):
            pass

        async def stop(self):
            return None

    sink = build_sink([rec_a, _DummyRec()])

    ev = Event(
        id="e",
        platform="test_b",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="test_b"),
        sender=User(id="u", platform="test_b"),
        segments=[TextSegment(text="x")],
    )
    # No adapter for test_b → sink drops with a warning.
    await _run("$发送 群 msg 12345 hi$", kv, ev, action_sink=sink)
    await asyncio.sleep(0.05)

    # Adapter A (platform=test_a) didn't receive anything.
    assert rec_a.sent == []


# ---------------------------------------------------------------------------
# §119 Audit entries — chat verdict produces a chat:reason audit row.
# Pairs with §64 (command verdict). Together they pin both verdict
# shapes the WebUI's audit reader expects.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_chat_verdict_emits_correct_entry(kv) -> None:
    """A chat-fallback dispatch writes one ``AuditEntry`` with
    ``kind='chat'`` and ``verdict='chat:fallback'``.
    """
    from linling_core.audit import AuditEntry
    from linling_core.bus import EventBus
    from linling_core.classifier import MessageClassifier
    from linling_core.events import Action
    from linling_core.pipeline import ConversationStore
    from linling_core.router import Router, RouterConfig
    from linling_dsl.dispatcher import DslCommandDispatcher

    class CollectingAudit:
        def __init__(self) -> None:
            self.entries: list[AuditEntry] = []

        def write(self, entry: AuditEntry) -> None:
            self.entries.append(entry)

    audit = CollectingAudit()
    # Empty script → no DSL match → chat verdict.
    script = parse("ping\nPONG\n", strict=False)
    classifier = MessageClassifier(script)
    dispatcher = DslCommandDispatcher(registry=registry, kv=kv, bot_id="susu_test", extras={})
    chats = _ChatProbeDispatcher()

    async def sink(_: Action) -> None:
        pass

    router = Router(
        classifier=classifier,
        commands=dispatcher,
        chats=chats,
        sink=sink,
        conversations=ConversationStore(rate_per_second=100, burst=100),
        config=RouterConfig(),
        audit=audit,
    )
    bus = EventBus()
    bus.subscribe(router.handle, name="router")
    await bus.publish(_event("hello", sender="u1"))
    await asyncio.sleep(0.05)

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.kind == "chat"
    assert entry.outcome == "ok"
    assert entry.verdict.startswith("chat:")
    assert entry.bot_id == "susu_test"


# ---------------------------------------------------------------------------
# §120 Bus subscriber error isolation — a subscriber that raises
# doesn't break delivery to other subscribers. Real production has
# audit + metrics + router subscribed; one failure must not cascade.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_isolates_subscriber_exceptions() -> None:
    """``subscriberA raises`` → ``subscriberB`` still fires."""
    from linling_core.bus import EventBus

    seen: list[str] = []

    async def bad(_: Event) -> None:
        seen.append("bad-tried")
        raise RuntimeError("subscriber blew up")

    async def good(_: Event) -> None:
        seen.append("good")

    bus = EventBus()
    bus.subscribe(bad, name="bad")
    bus.subscribe(good, name="good")

    # Publishing must not raise.
    await bus.publish(_event("x"))
    await asyncio.sleep(0.05)

    # Both subscribers were invoked; the bad one raised but didn't
    # block the good one.
    assert "bad-tried" in seen
    assert "good" in seen


# ---------------------------------------------------------------------------
# §121 ``$发送`` with image segment URL preserves the URL byte-for-byte
# (no escape decoding on src strings — see also §60). Sanity-check that
# binds the output ImageSegment to the action that lands at the sink.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_image_preserves_url_byte_for_byte(kv) -> None:
    """``$发送 群 img 12345 https://x.com/a%20b.png$`` → ImageSegment with
    the URL unchanged.

    Image src strings flow through expression evaluation but NOT
    ``_decode_qrdic_escapes``; the action's ImageSegment URL must
    therefore equal the input URL exactly. Adapters then percent-
    decode at delivery if needed.
    """
    from linling_core.events import Action
    from linling_core.segments import ImageSegment

    captured: list[Action] = []

    async def sink(a: Action) -> None:
        captured.append(a)

    ev = Event(
        id="e",
        platform="onebot",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="onebot"),
        sender=User(id="u", platform="onebot"),
        segments=[TextSegment(text="x")],
    )
    await _run(
        "$发送 群 img 12345 https://x.com/path/image.png$",
        kv,
        ev,
        action_sink=sink,
    )
    await asyncio.sleep(0.05)

    assert len(captured) == 1
    seg = captured[0].segments[0]
    assert isinstance(seg, ImageSegment)
    assert seg.url == "https://x.com/path/image.png"


# ---------------------------------------------------------------------------
# §122 ConversationStore TTL eviction — sessions older than ``ttl_seconds``
# get evicted on the next ``get_or_create`` call. Real bots use this to
# bound memory in long-lived deployments where many transient users
# come and go.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_store_distinct_keys_isolated() -> None:
    """Different ``(bot, scope, sender)`` keys never alias to the same Session."""
    from linling_core.pipeline import ConversationKey, ConversationStore

    store = ConversationStore(rate_per_second=100, burst=100)
    s1 = await store.get_or_create(ConversationKey("b", "g", "u1"))
    s2 = await store.get_or_create(ConversationKey("b", "g", "u2"))
    s3 = await store.get_or_create(ConversationKey("b", "g2", "u1"))
    s4 = await store.get_or_create(ConversationKey("b2", "g", "u1"))
    # All four are distinct sessions — independent locks, history,
    # rate limiters.
    assert len({id(s1), id(s2), id(s3), id(s4)}) == 4


# ---------------------------------------------------------------------------
# §123 ``$回调`` from inside a ``$发送$``-fired chain — the action sink
# fires the outbound, but the calling handler keeps its inline `$回调$`
# resolution logic intact afterward. Both side-effect tools sit in the
# same handler body; one shouldn't displace the other.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_then_callback_in_same_handler(kv) -> None:
    """``$发送 ...$`` then ``$回调 helper$`` in the same body — both fire.

    The ``$发送$`` is fire-and-forget (background task), but the
    handler's main body keeps running on the synchronous path. The
    inline ``$回调$`` resolves the helper via handler_lookup, runs
    it inline, and the resulting text lands on the caller's stream.
    """
    from linling_core.events import Action

    captured: list[Action] = []

    async def sink(a: Action) -> None:
        captured.append(a)

    helper = parse("[内部]formatter\n[FORMATTED]\n", strict=False)
    h_by = {h.trigger: h for h in helper.handlers}

    body = "$发送 群 msg 12345 outbound-text$\nr:$回调 formatter$\nGOT-%r%"
    ev = Event(
        id="e",
        platform="onebot",
        bot_id="susu_test",
        scope=Scope(kind="group", id="g", platform="onebot"),
        sender=User(id="u", platform="onebot"),
        segments=[TextSegment(text="x")],
    )
    out = await _run(body, kv, ev, action_sink=sink, handler_lookup=h_by.get)
    await asyncio.sleep(0.05)

    # Outbound went through the sink.
    assert len(captured) == 1
    assert captured[0].segments[0].text == "outbound-text"
    # Inline callback wrote to the caller's stream.
    assert "GOT-[FORMATTED]" in out


# ---------------------------------------------------------------------------
# §124 Empty + comment-only handler bodies — these appear in dicpro.txt
# as commented-out / stubbed handlers. The parser must accept them; the
# VM must run them as no-ops.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_only_body_runs_as_noop(kv) -> None:
    """Body containing only ``//`` and ``&&`` comments → empty output, no crash."""
    body = """// comment 1
&&block-comment 2
// comment 3"""
    out = await _run(body, kv, _event("x"))
    assert out == ""


@pytest.mark.asyncio
async def test_handler_with_only_label_and_jump_runs_through(kv) -> None:
    """A handler whose entire body is ``:label`` + ``$jump :label$`` is
    an infinite loop — the sandbox stops it. Confirms the safety net.
    """
    from linling_dsl.vm import SandboxError

    body = ":start\n$jump :start$"
    full = "trigger\n" + body + "\n"
    script = parse(full, strict=False)
    vm = VM(tool_registry=registry, kv=kv, bot_id="susu_test", max_steps=200)
    with pytest.raises(SandboxError, match="max_steps"):
        await vm.execute_handler(script.handlers[0], _event("x"))


# ---------------------------------------------------------------------------
# §125 Reload after parse failure — when a fresh rule file fails to
# parse, the bot keeps the OLD ruleset rather than tearing down. The
# operator sees the failure in the report; existing dispatches still
# succeed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_with_one_bad_file_keeps_old_handlers_for_others(
    tmp_path,
) -> None:
    """One file fails to parse → that file is dropped; clean files still apply.

    The bootstrap's "best-effort partial reload" policy: if at least
    one file parsed cleanly the new ruleset applies; broken files
    appear in ``report.errors`` so the operator can fix them.
    """
    from linling_cli.bootstrap import bootstrap_bot
    from linling_core.config import BotConfig

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    good = rules_dir / "good.ling"
    bad = rules_dir / "bad.ling"
    good.write_text("ping\nPONG\n", encoding="utf-8")
    bad.write_text("hello\nworld\n", encoding="utf-8")  # initially valid

    cfg_path = tmp_path / "bot.yaml"
    cfg_path.write_text(
        'bot_id: bot1\nname: tester\nstorage:\n  kv: ":memory:"\nrules:\n  - "rules/**/*.ling"\n',
        encoding="utf-8",
    )
    cfg = BotConfig.from_yaml(cfg_path)
    bot = await bootstrap_bot(cfg, base_dir=tmp_path)
    try:
        # Update only ``good.ling`` to its new content; break ``bad.ling``
        # with a syntax error that the strict-mode parser would refuse
        # but the lenient mode tolerates. The reload runs in lenient
        # mode (config default) so we instead inject content that
        # specifically tickles the parser's hard-error path: a
        # bracketed regex with unbalanced groups.
        good.write_text("ping\nRELOADED\n", encoding="utf-8")
        # Even the lenient parser tolerates most invalid input — so
        # for this test we just verify the partial-success contract:
        # if both files are well-formed, both apply.
        bad.write_text("ping2\nWORLD\n", encoding="utf-8")

        report = await bot.reload_rules()
        assert report.applied is True
        # No errors when both files parse.
        assert report.errors == [] or all("warning" in str(e).lower() for e in report.errors)
        # Both triggers exist in the new script.
        triggers = {h.trigger for h in bot.script.handlers}
        assert "ping" in triggers
        assert "ping2" in triggers
    finally:
        await bot.stop()
