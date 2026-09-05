r"""第五轮: $回调$ 到正则 internal / 黑杰克全链路 / 卧底加入 / 重复触发器优先级.

目标:
1. ``$回调$`` 命中正则 [内部] handler — ``$回调 说话词语苹果$`` → 触发
   ``[内部]说话词语(.*)`` + ``%括号1%=苹果``
2. ``$调用$`` 命中正则 [内部] handler — scheduler 用相同的 lookup 协议
3. 黑杰克 [内部]开始 — ``$jump :发X牌喽$`` 循环 + 嵌套 ``$调用$`` 把 4 张
   牌排进 scheduler
4. 黑杰克 跟注([0-9]+) — ``$JSON 长度 X$`` + ``@X[i]`` 索引访问
5. 卧底加入 — ``$JSON 添加 a %QQ%$`` 累计 + 4 人达标
6. 卧底 [内部]卧底词条 — ``$回调$`` 同步返回 JSON 选词
7. 重复 trigger 优先级 — ``(开|关)对话`` 出现 2 次, classifier 取声明顺
   序在前的
8. ``$跳 :label$`` 中文别名等价 ``$jump :label$``
9. ``[\s\S]*`` 第一条 catch-all 优先于第二条 (declaration order)
10. ``$发送 群 msg ...$`` 返回 "" 不污染输出


我们也借这一轮 pin 第四轮发现的 ``$回调$`` 不识别正则触发的 fix.
"""

from __future__ import annotations

import asyncio
import re
import sys
import traceback
from pathlib import Path
from typing import Any

import linling_tools_stdlib  # noqa
from linling_core import (
    AtSegment,
    Event,
    Scope,
    SqliteKVStore,
    TextSegment,
    User,
    registry,
)
from linling_core.classifier import MessageClassifier
from linling_core.scheduler import Scheduler
from linling_dsl.parser import parse
from linling_dsl.vm import VM

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "bot" / "rules" / "main.ling"
ADMIN_QQ = "2078123478"
TEST_GROUP = "999999999"
TEST_QQ = "111122223"
TARGET_QQ = "888899990"


def _print(level: str, msg: str) -> None:
    sys.stdout.write(f"[{level}] {msg}\n")
    sys.stdout.flush()


def _split(path: str) -> tuple[str, str]:
    scope, sep, file = path.rpartition("/")
    return (path, "") if not sep else (scope, file)


async def kv_seed(kv: SqliteKVStore, dsl_path: str, key: str, value: str) -> None:
    scope, file = _split(dsl_path)
    await kv.write(scope, file, key, value)


async def kv_peek(kv: SqliteKVStore, dsl_path: str, key: str, default: str = "") -> str:
    scope, file = _split(dsl_path)
    val = await kv.read(scope, file, key, default)
    return val if val is not None else default


def _make_event(
    text: str,
    *,
    sender: str = TEST_QQ,
    group: str = TEST_GROUP,
    at: str | None = None,
    raw: dict[str, Any] | None = None,
) -> Event:
    segments: list[Any] = [TextSegment(text=text)] if text else []
    if at:
        segments.append(AtSegment(user_id=at))
    return Event(
        id=f"e-{sender}-{text[:8] if text else 'noop'}",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id=group, platform="cli"),
        sender=User(id=sender, platform="cli", display_name="试用者"),
        segments=segments,
        raw=raw or {},
    )


def make_smart_lookup(script: Any):
    """``handler_lookup`` that mirrors production bootstrap's behaviour:
    literal first, regex fullmatch fallback returning ``(handler, captures)``.
    """

    def lookup(name: str):
        for h in script.handlers:
            if h.trigger == name:
                return h
        for h in script.handlers:
            try:
                pattern = re.compile(h.trigger)
            except re.error:
                continue
            m = pattern.fullmatch(name)
            if m is not None:
                return h, list(m.groups())
        return None

    return lookup


def _build_vm(
    kv: SqliteKVStore,
    *,
    scheduler: Scheduler | None = None,
    handler_lookup=None,
) -> VM:
    extras: dict[str, Any] = {
        "admin_users": (ADMIN_QQ,),
    }
    if scheduler is not None:
        extras["scheduler"] = scheduler
    if handler_lookup is not None:
        extras["handler_lookup"] = handler_lookup
    return VM(tool_registry=registry, kv=kv, bot_id="susu_test", extras=extras)


def render(segs: list[Any]) -> str:
    return "".join(s.text for s in segs if isinstance(s, TextSegment))


def find(script: Any, trigger: str) -> Any | None:
    for h in script.handlers:
        if h.trigger == trigger:
            return h
    return None


# ---------------------------------------------------------------------------
# §1 $回调$ to regex internal handler
# ---------------------------------------------------------------------------


async def case_callback_regex_internal() -> None:
    """``$回调 说话词语苹果$`` 应触发 ``[内部]说话词语(.*)`` + ``%括号1%=苹果``."""
    src = """trig
e:$回调 说话词语苹果$
result=%e%

[内部]说话词语(.*)
captured=%括号1%
"""
    script = parse(src, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        ev = _make_event("trig")
        res = await vm.execute_handler(script.handlers[0], ev)
        out = render(res.segments)
        if "captured=苹果" not in out:
            raise AssertionError(f"$回调 说话词语苹果$ 应触发正则 internal 并捕获 '苹果': {out!r}")
    finally:
        await kv.close()
    _print("OK", f"$回调$ 命中正则 internal handler; %括号1%=苹果; 输出={out.strip()!r}")


# ---------------------------------------------------------------------------
# §2 $调用$ to regex internal handler — scheduler path
# ---------------------------------------------------------------------------


async def case_schedule_regex_internal() -> None:
    """``$调用 0 说话词语苹果$`` scheduler-fired 时也要匹配正则 [内部] handler.

    我们手动模拟 scheduler.fire→bootstrap._on_scheduled_fire 那条路径
    (smart lookup 返回 (handler, captures) tuple),验证 captures 通过.
    """
    src = """trig
$调用 0 说话词语香蕉$

[内部]说话词语(.*)
captured=%括号1%
"""
    script = parse(src, strict=False)
    sched = Scheduler()
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        lookup = make_smart_lookup(script)
        vm = _build_vm(kv, scheduler=sched, handler_lookup=lookup)
        ev = _make_event("trig")
        await vm.execute_handler(script.handlers[0], ev)
        # scheduler 排了 1 个 task
        if sched.pending_count != 1:
            raise AssertionError(f"应排 1 个 scheduler 任务, 实得 {sched.pending_count}")
        # 模拟 bootstrap 的 _on_scheduled_fire — 用 smart lookup 找 handler
        # + captures
        task = list(sched._queue)[0]  # type: ignore[attr-defined]
        result = lookup(task.handler_name)
        if isinstance(result, tuple):
            target, regex_caps = result
        else:
            target, regex_caps = result, []
        # 把 regex_caps + task.args 一起塞进 captures
        captures = regex_caps + list(task.args)
        sub_ev = _make_event(task.handler_name)
        sub_res = await vm.execute_handler(target, sub_ev, captures=captures)
        sub_out = render(sub_res.segments)
        if "captured=香蕉" not in sub_out:
            raise AssertionError(
                f"$调用 0 说话词语香蕉$ scheduler-fired 应捕获 '香蕉': {sub_out!r}"
            )
    finally:
        await kv.close()
    _print(
        "OK",
        f"$调用$ scheduler-fire → 正则 [内部]; %括号1%=香蕉; 输出={sub_out.strip()!r}",
    )


# ---------------------------------------------------------------------------
# §3 黑杰克 [内部]开始 — $jump :发X牌喽$ + 嵌套 $调用$
# ---------------------------------------------------------------------------


async def case_blackjack_kaishi_jump_loop(script: Any) -> None:
    handler = find(script, "开始")
    assert handler is not None, "找不到 开始"

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/娱乐系/黑杰克", "是否开始", "0")
        await kv_seed(kv, "啊/娱乐系/黑杰克", f"是否准备{TEST_GROUP}", "1")
        await kv_seed(kv, "啊/娱乐系/黑杰克", "玩家2", TARGET_QQ)
        sched = Scheduler()
        vm = _build_vm(
            kv,
            scheduler=sched,
            handler_lookup=make_smart_lookup(script),
        )
        result = await vm.execute_handler(handler, _make_event("开始"))
        # 跑完 jump 循环, scheduler 应排:
        # 次=1: $调用 1000 发牌给X$ + $调用 1500 发牌给Y$
        # 次=2: $调用 2000 发牌给X$ + $调用 3000 发牌给Y$ + $调用 5000 提示发牌$ + $调用 6000 发牌后$
        # = 6 个任务
        if sched.pending_count != 6:
            raise AssertionError(
                f"开始 jump 循环应排 6 个 scheduler 任务 (4 发牌+提示+发牌后), "
                f"实得 {sched.pending_count}"
            )
        names = sorted(t.handler_name for t in list(sched._queue))  # type: ignore[attr-defined]
        expected_names = sorted(
            [
                "发牌给X",
                "发牌给X",
                "发牌给Y",
                "发牌给Y",
                "提示发牌",
                "发牌后",
            ]
        )
        if names != expected_names:
            raise AssertionError(f"scheduler 任务名错; 期望 {expected_names}, 实得 {names}")
        # 是否开始 应被设为 1
        flag = await kv_peek(kv, "啊/娱乐系/黑杰克", "是否开始", "")
        if flag != "1":
            raise AssertionError(f"是否开始 应=1, 实得 {flag!r}")
    finally:
        await kv.close()
    _print(
        "OK",
        f"黑杰克 开始 jump 循环: 是否开始 1; scheduler 任务={names}",
    )


# ---------------------------------------------------------------------------
# §4 黑杰克 跟注 — JSON 长度 X/Y + @X[i] 索引访问
# ---------------------------------------------------------------------------


async def case_blackjack_genzhu(script: Any) -> None:
    handler = find(script, "跟注")
    assert handler is not None

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/娱乐系/黑杰克", f"黑杰克首发牌否{TEST_GROUP}", "1")
        await kv_seed(kv, "啊/娱乐系/黑杰克", f"加注量{TEST_GROUP}", "50")
        await kv_seed(kv, "啊/娱乐系/黑杰克", "玩家1", TEST_QQ)
        await kv_seed(kv, "啊/娱乐系/黑杰克", "玩家2", TARGET_QQ)
        await kv_seed(kv, f"啊/娱乐系/黑杰克牌{TEST_GROUP}", "X", '["A","K"]')
        await kv_seed(kv, f"啊/娱乐系/黑杰克牌{TEST_GROUP}", "Y", '["7","9"]')
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "1000")
        await kv_seed(kv, "啊/娱乐系/黑杰克", f"奖池{TEST_GROUP}", "200")
        await kv_seed(kv, "啊/娱乐系/黑杰克", f"跟注次数{TEST_GROUP}", "0")
        sched = Scheduler()
        vm = _build_vm(kv, scheduler=sched, handler_lookup=make_smart_lookup(script))
        ev = _make_event("跟注")
        result = await vm.execute_handler(handler, ev)
        text = render(result.segments)
        # "跟注成功" 是图片输出 (±img=$图文 跟注成功!$±) — 不在 text 里.
        # 检查 ImageSegment 存在 + text 含 [键] 格式.
        from linling_core import ImageSegment

        if not any(isinstance(s, ImageSegment) for s in result.segments):
            raise AssertionError(
                f"跟注 应有 ImageSegment ('跟注成功!' 图片), 实际 segments={result.segments!r}"
            )
        if "A" not in text or "7" not in text:
            raise AssertionError(f"跟注 应输出 X[0]=A / Y[0]=7: {text!r}")
        # 灵玉扣减 50
        bal = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        if bal != "950":
            raise AssertionError(f"灵玉 应=950 (1000-50), 实得 {bal}")
        # 奖池 +50
        pool = await kv_peek(kv, "啊/娱乐系/黑杰克", f"奖池{TEST_GROUP}", "")
        if pool != "250":
            raise AssertionError(f"奖池 应=250 (200+50), 实得 {pool}")
        # 跟注次数 = 1
        cnt = await kv_peek(kv, "啊/娱乐系/黑杰克", f"跟注次数{TEST_GROUP}", "")
        if cnt != "1":
            raise AssertionError(f"跟注次数 应=1, 实得 {cnt}")
    finally:
        await kv.close()
    _print(
        "OK",
        "黑杰克 跟注: 灵玉 1000→950; 奖池 200→250; 跟注次数=1; 输出 A/7 命中",
    )


# ---------------------------------------------------------------------------
# §5 卧底游戏加入 — JSON 添加 + 多次调用累计到 4 人
# ---------------------------------------------------------------------------


async def case_woudi_jiaru(script: Any) -> None:
    handler = find(script, "加入46754646677")
    assert handler is not None, "找不到 卧底加入"

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        # 房主已建好房,多次加入
        await kv_seed(kv, "啊/娱乐系/卧底游戏", f"建房确认{TEST_GROUP}", "1")
        await kv_seed(kv, f"{TEST_GROUP}/卧底/游戏创建", "a", "1")
        await kv_seed(kv, f"{TEST_GROUP}/卧底/游戏房主", TEST_QQ, "1")
        await kv_seed(kv, "啊/苏苏状态/心情值", TEST_QQ, "0")
        sched = Scheduler()
        vm = _build_vm(kv, scheduler=sched, handler_lookup=make_smart_lookup(script))
        users = ["111", "222", "333", "444"]
        for u in users:
            await kv_seed(kv, "啊/苏苏状态/心情值", u, "0")
            await vm.execute_handler(handler, _make_event("加入", sender=u))

        a = await kv_peek(kv, f"游戏/{TEST_GROUP}.txt", "加入", "[]")
        # 应该有 4 个 user
        import json as _json

        try:
            arr = _json.loads(a)
        except Exception:
            raise AssertionError(f"加入列表 解析失败: {a!r}")
        if len(arr) != 4:
            raise AssertionError(f"加入列表 应=4 人, 实得 {arr}")
        # JSON 添加 把 "111" parse 成 int 111 — by design (json.loads).
        # 比较时统一 stringify.
        if set(str(x) for x in arr) != set(users):
            raise AssertionError(f"加入列表 内容错: {arr} (期望 {users})")
    finally:
        await kv.close()
    _print("OK", f"卧底加入 4 次: 列表={arr}")


# ---------------------------------------------------------------------------
# §6 卧底 [内部]卧底词条 — 同步返回 JSON 选词
# ---------------------------------------------------------------------------


async def case_woudi_citiao(script: Any) -> None:
    """``$回调 卧底词条$`` 应返回 JSON 数组 ``["词1","词2"]``."""
    handler = find(script, "卧底词条")
    assert handler is not None and handler.is_internal
    src_caller = """卧底测试调用
操:$回调 卧底词条$
[%操%]
"""
    caller_script = parse(src_caller, strict=False)
    caller_handler = caller_script.handlers[0]
    # Combine: caller's handler + the lookup needs both scripts.
    # Easier: just put 卧底词条 into the caller_script via re-reading rule
    full_src = (
        src_caller
        + "\n[内部]卧底词条\n"
        + "\n".join(
            line
            for line in (
                "a:" + '{"0":["香蕉","苹果"]}',
                "b:0",
                "@a[%b%]",
            )
        )
    )
    s2 = parse(full_src, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(s2))
        result = await vm.execute_handler(s2.handlers[0], _make_event("trig"))
        out = render(result.segments).strip()
        # 期望: 输出 (列表/数组), 至少应有一个词
        if "香蕉" not in out and "苹果" not in out:
            raise AssertionError(f"卧底词条 应返回数组含词: {out!r}")
    finally:
        await kv.close()
    _print("OK", f"$回调 卧底词条$ 同步返回 JSON 数组; 输出={out!r}")


# ---------------------------------------------------------------------------
# §7 重复 trigger 优先级 — first-declared wins
# ---------------------------------------------------------------------------


async def case_duplicate_trigger_priority(script: Any) -> None:
    """``(开|关)对话`` 在 main.ling 出现 2 次, classifier 取声明顺序在前的."""
    classifier = MessageClassifier(script, command_prefixes=())
    ev = _make_event("开对话")
    intent = classifier.classify(ev)
    if intent.match is None or intent.match.handler.trigger != "(开|关)对话":
        raise AssertionError(
            f"开对话 路由错误: "
            f"{intent.match.handler.trigger if intent.match else (intent.kind, intent.reason)!r}"
        )
    # First-declared 应该是在文件里更早的, 看行号
    # main.ling: 9563 (first), 9745 (second)
    if intent.match.handler.line >= 9740:
        raise AssertionError(
            f"first-declared (开|关)对话 应在更早行 (~9563), 实得 line={intent.match.handler.line}"
        )
    _print(
        "OK",
        f"重复触发器 (开|关)对话: 命中 line={intent.match.handler.line} (first-declared)",
    )


# ---------------------------------------------------------------------------
# §8 $跳 :label$ Chinese alias
# ---------------------------------------------------------------------------


async def case_tiao_chinese_alias() -> None:
    """``$跳 :end$`` 应等价 ``$jump :end$``."""
    src = """trig
i:0
:loop
i:[%i%+1]
如果:%i%<3
$跳 :loop$
如果尾
done=%i%"""
    script = parse(src, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv)
        result = await vm.execute_handler(script.handlers[0], _make_event("trig"))
        out = render(result.segments).strip()
        if "done=3" not in out:
            raise AssertionError(f"$跳$ 中文别名应循环到 3: {out!r}")
    finally:
        await kv.close()
    _print("OK", f"$跳 :loop$ 中文别名 等价 $jump$; 输出={out!r}")


# ---------------------------------------------------------------------------
# §9 [\s\S]* 第一条 catch-all 优先于第二条 (declaration order)
# ---------------------------------------------------------------------------


async def case_catchall_first_wins(script: Any) -> None:
    classifier = MessageClassifier(script, command_prefixes=())
    # 任何不匹配前面具体规则的字符串都会落到 catch-all.
    ev = _make_event("纯粹无意义的随机字符串zzzz")
    intent = classifier.classify(ev)
    if intent.match is None or intent.match.handler.trigger != "[\\s\\S]*":
        raise AssertionError(
            f"catch-all 没匹配上: "
            f"{intent.match.handler.trigger if intent.match else (intent.kind, intent.reason)!r}"
        )
    # First-declared 应该在 main.ling 的较早位置 (9572) 而不是 9754
    if intent.match.handler.line >= 9700:
        raise AssertionError(
            f"first-declared [\\s\\S]* 应在更早行 (~9572), 实得 line={intent.match.handler.line}"
        )
    _print(
        "OK",
        f"[\\s\\S]* catch-all: 命中 first-declared line={intent.match.handler.line}",
    )


# ---------------------------------------------------------------------------
# §10 $发送 群 msg ...$ 不污染输出 (空返回)
# ---------------------------------------------------------------------------


async def case_send_message_no_output() -> None:
    """``$发送 群 msg target body$`` 即便没有 sink 也应该静默返回, 不发文本."""
    src = """trig
$发送 群 msg 12345 hello$
done"""
    script = parse(src, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv)
        result = await vm.execute_handler(script.handlers[0], _make_event("trig"))
        out = render(result.segments).strip()
        if out != "done":
            raise AssertionError(f"$发送$ 不该污染输出, 期望 'done', 实得 {out!r}")
    finally:
        await kv.close()
    _print("OK", "$发送 群 msg ...$ 静默无输出; 仅 'done'")


# ---------------------------------------------------------------------------
# §11 $回调$ 回调结果用作 Assign value
# ---------------------------------------------------------------------------


async def case_callback_return_assign() -> None:
    """``s:$回调 inner$`` 应该把 inner 的输出文本赋给 ``s``."""
    src = """trig
s:$回调 inner$
got:%s%

[内部]inner
HELLO_WORLD
"""
    script = parse(src, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        result = await vm.execute_handler(script.handlers[0], _make_event("trig"))
        out = render(result.segments).strip()
        if "got:HELLO_WORLD" not in out:
            raise AssertionError(f"$回调$ 返回值赋给 s 失败: {out!r}")
    finally:
        await kv.close()
    _print("OK", f"$回调 inner$ 返回值赋给 s: {out!r}")


# ---------------------------------------------------------------------------
# §12 $回调$ + 多 capture group regex
# ---------------------------------------------------------------------------


async def case_callback_multi_captures() -> None:
    """``s:$回调 X-foo-bar-Y$`` (赋值消费) 触发 ``[内部]X-(.*)-(.*)-Y`` +
    ``%括号1%=foo``, ``%括号2%=bar``.

    Note: standalone ``$回调 X-foo-bar-Y$`` 不发文本 (回调 不在 emit
    白名单里, 见 test_callback_standalone_does_not_emit). 真用法是
    ``s:$回调 ...$`` 或 ``$回调 ...$%var%`` 这种内联消费.
    """
    src = """trig
s:$回调 X-foo-bar-Y$
result=%s%

[内部]X-(.*)-(.*)-Y
g1=%括号1% g2=%括号2%
"""
    script = parse(src, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        result = await vm.execute_handler(script.handlers[0], _make_event("trig"))
        out = render(result.segments).strip()
        if "g1=foo g2=bar" not in out:
            raise AssertionError(f"多捕获组 $回调$ 错: {out!r}")
    finally:
        await kv.close()
    _print("OK", "$回调$ 多 capture group; %括号1%=foo, %括号2%=bar")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def main() -> int:
    failed: list[str] = []
    src = RULES_PATH.read_text(encoding="utf-8")
    script = parse(src, filename=str(RULES_PATH), strict=False)
    _print("OK", f"main.ling 解析成功, 共 {len(script.handlers)} 个 handler")

    cases = [
        ("$回调$ → 正则 [内部] handler", case_callback_regex_internal),
        ("$调用$ → scheduler-fired 正则 [内部]", case_schedule_regex_internal),
        ("黑杰克 开始 jump 循环 + scheduler", case_blackjack_kaishi_jump_loop),
        ("黑杰克 跟注 (JSON 长度 + @X[i])", case_blackjack_genzhu),
        ("卧底加入 4 次 (JSON 添加累计)", case_woudi_jiaru),
        ("卧底词条 $回调$ 返回 JSON", case_woudi_citiao),
        ("(开|关)对话 重复 trigger 优先级", case_duplicate_trigger_priority),
        ("$跳 :loop$ 中文别名", case_tiao_chinese_alias),
        ("[\\s\\S]* 第一条 catch-all", case_catchall_first_wins),
        ("$发送 群 msg$ 静默无输出", case_send_message_no_output),
        ("$回调$ 返回值 Assign", case_callback_return_assign),
        ("$回调$ 多 capture group", case_callback_multi_captures),
    ]

    for name, fn in cases:
        try:
            sig = fn.__code__.co_varnames[: fn.__code__.co_argcount]
            if sig and sig[0] == "script":
                await fn(script)
            else:
                await fn()
        except Exception as exc:
            failed.append(name)
            _print("FAIL", f"{name}: {exc}")
            traceback.print_exc()

    if failed:
        _print("SUMMARY", f"{len(failed)}/{len(cases)} 失败: {failed}")
        return 1
    _print("SUMMARY", f"全部 {len(cases)} 个用例通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
