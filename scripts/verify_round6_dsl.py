"""第六轮: 赛马 / 卧底 / 内联 $回调$ 链 / 带空格的 trigger.

目标:
1. 赛马打印位置 — 内联 ``$回调 赛马打印位置%一%$%甲%`` (FuncCallExpr) 把 N
   个 "。" 拼到 row 头, 模拟"赛道位置"图. 验证 inline $回调$ 取返回值
2. 赛马进行 — 3 个内联 $回调$ + ``$jump :跳转$`` 内部循环 (本身不带 jump,
   用 scheduler 触发 5 次)
3. 卧底 投票@xx 单次 — KV state 累计 (被投票/投票人/投票次数)
4. ``[内部]游戏判断 ([0-9]+)`` 带空格的 regex trigger — ``$回调 游戏判断
   12345$`` 应触发并 return 索引
5. ``[内部]说话词语(.*)`` — 找词语在描述列表中的索引 (返回 -1 即无)
6. ``[内部]卧底词条`` — 同步返回 JSON 数组中的随机一项 (上轮已过,这里加
   完整流程)
7. ``[内部]游戏删除 / 游戏清空`` — ``$删除$`` 文件路径
8. ``$回调 X arg$`` 多 token call (无空格 trigger) — 比如 ``$回调 赛马打
   印位置3$``
9. 黑杰克 加注([0-9]+) — 灵玉 + 奖池 + 加注量
10. ``[内部]X-Y`` literal trigger 带 ``$回调$`` 调用 (no regex)
11. 苏苏问答通识 — 没有 trigger 的 internal,纯 ``$回调$`` 调用
12. 赛马结算 a 胜 — 多 if 分支 + ``$回调 无人获胜结算$`` 兜底
"""

from __future__ import annotations

import asyncio
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
MAIN_GROUP = "754800438"
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
    """Mirror production bootstrap's ``_lookup_handler``: literal first,
    then regex fullmatch returning ``(handler, captures)`` on hit.
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
        "main_group": MAIN_GROUP,
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
# §1 [内部]赛马打印位置(.*) — inline $回调$ + jump-loop emit "。" * N
# ---------------------------------------------------------------------------


async def case_saima_dayin_weizhi_inline(script: Any) -> None:
    """``$回调 赛马打印位置3$`` 应输出 3 个 "。" + 后续 ``%甲%`` 字符."""
    handler = find(script, "赛马打印位置(.*)")
    assert handler is not None and handler.is_internal

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        # 模拟: 单独执行 [内部]赛马打印位置(.*) with %括号1% = "3"
        # 内部 loop: i=0; "。" emit; i=1; "。" emit; i=2; "。" emit; i=3; jump out
        result = await vm.execute_handler(
            handler, _make_event("trig"), captures=["3"]
        )
        out = render(result.segments)
        # 应输出 3 个 "。"
        if out.count("。") != 3:
            raise AssertionError(
                f"[内部]赛马打印位置3 应输出 3 个 '。', 实得 {out.count('。')} 个; out={out!r}"
            )
    finally:
        await kv.close()
    _print("OK", f"[内部]赛马打印位置3: 输出 {out.count('。')} 个 '。' (loop 正确)")


# ---------------------------------------------------------------------------
# §2 内联 $回调$ 取返回值 — "$回调 X$%甲%" 模式
# ---------------------------------------------------------------------------


async def case_inline_callback_in_text(script: Any) -> None:
    """``$回调 赛马打印位置2$%甲%`` — inline FuncCallExpr 把 "。。" 与 "%甲%" 拼接."""
    handler = find(script, "赛马进行")
    if handler is None:
        raise AssertionError("找不到 赛马进行")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        # Seed 选手位置 / 图标
        await kv_seed(kv, f"赛马/{TEST_GROUP}/选手1", "位置", "0")
        await kv_seed(kv, f"赛马/{TEST_GROUP}/选手2", "位置", "0")
        await kv_seed(kv, f"赛马/{TEST_GROUP}/选手3", "位置", "0")
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        result = await vm.execute_handler(handler, _make_event("trig"))
        out = render(result.segments)
        # 应有: 比赛激烈进行中 + 3 行 (赛道+图标)
        if "比赛激烈进行中" not in out:
            raise AssertionError(f"赛马进行 输出错: {out!r}")
        # 应包含图标 (默认 🐴 / 🦓 / 🦄)
        if "🐴" not in out or "🦓" not in out or "🦄" not in out:
            raise AssertionError(f"赛马进行 图标 缺失: {out!r}")
    finally:
        await kv.close()
    _print(
        "OK",
        f"赛马进行 (内联 $回调$ + 图标拼接): 输出包含 🐴/🦓/🦄;\n  片段={out.strip()[:60]!r}",
    )


# ---------------------------------------------------------------------------
# §3 卧底 投票@xx 单次 — KV state 累加
# ---------------------------------------------------------------------------


async def case_woudi_toupiao(script: Any) -> None:
    handler = find(script, "投票1573789464@.*")
    if handler is None:
        raise AssertionError("找不到 投票@.*")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, f"{TEST_GROUP}/卧底/投票开始", "a", "1")
        await kv_seed(
            kv, f"游戏/{TEST_GROUP}.txt", "加入", f'["{TEST_QQ}","{TARGET_QQ}","333","444"]'
        )
        await kv_seed(kv, f"{TEST_GROUP}/卧底/被投票", TARGET_QQ, "0")
        await kv_seed(kv, f"{TEST_GROUP}/卧底/投票人", TEST_QQ, "0")
        await kv_seed(kv, f"{TEST_GROUP}/卧底/投票次数", "a", "0")

        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        ev = _make_event("投票@", at=TARGET_QQ)
        result = await vm.execute_handler(handler, ev, captures=[])
        text = render(result.segments)
        if "投了一票" not in text and "已记录" not in text:
            raise AssertionError(f"投票@ 应输出 '投了一票' / '已记录': {text!r}")
        被 = await kv_peek(kv, f"{TEST_GROUP}/卧底/被投票", TARGET_QQ, "")
        投 = await kv_peek(kv, f"{TEST_GROUP}/卧底/投票人", TEST_QQ, "")
        次 = await kv_peek(kv, f"{TEST_GROUP}/卧底/投票次数", "a", "")
        if 被 != "1" or 投 != "1" or 次 != "1":
            raise AssertionError(
                f"投票 KV state 错: 被={被} 投={投} 次={次} (期望 1/1/1)"
            )
    finally:
        await kv.close()
    _print("OK", f"卧底 投票@xx: 被投票=1, 投票人=1, 投票次数=1")


# ---------------------------------------------------------------------------
# §4 [内部]游戏判断 ([0-9]+) — trigger with literal space
# ---------------------------------------------------------------------------


async def case_youxipanduan_with_space(script: Any) -> None:
    """``$回调 游戏判断 12345$`` 应触发 ``[内部]游戏判断 ([0-9]+)`` 并返回索引."""
    handler = find(script, "游戏判断 ([0-9]+)")
    if handler is None:
        raise AssertionError("找不到 [内部]游戏判断 ([0-9]+)")
    if not handler.is_internal:
        raise AssertionError("[内部]游戏判断 应是 internal")

    # 从 main.ling 复用 lookup, 但要测的是 "$回调 游戏判断 12345$"
    # 这种调用. 我们写一个自包含小 script.
    src = """trig
s:$回调 游戏判断 222$
result=%s%

[内部]游戏判断 ([0-9]+)
M:["111","222","333"]
B:0
K:0
U:$JSON 长度 M$
:循环D
如果:%K%<%U%
T:$JSON 获取 M %K%$
如果:%T%==%括号1%
%K%
返回
如果尾
K:[%K%+1]
$跳 :循环D$
如果尾
-1
"""
    s2 = parse(src, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(s2))
        result = await vm.execute_handler(s2.handlers[0], _make_event("trig"))
        out = render(result.segments).strip()
        if "result=1" not in out:
            raise AssertionError(
                f"游戏判断 找 '222' 应返回索引 '1' (M=[111,222,333]), 实得 {out!r}"
            )
    finally:
        await kv.close()
    _print(
        "OK",
        f"$回调 游戏判断 222$: 命中带空格 trigger; 返回索引 1; 输出={out!r}",
    )


# ---------------------------------------------------------------------------
# §5 [内部]说话词语(.*) — find-index-of-value via regex trigger
# ---------------------------------------------------------------------------


async def case_shuohua_ciyu_index() -> None:
    """``嘎:$回调 说话词语苹果$`` 在描述列表里查 '苹果' 的索引."""
    src = """trig
嘎:$回调 说话词语苹果$
got=%嘎%

[内部]说话词语(.*)
M:["香蕉","苹果","西瓜"]
B:0
K:0
U:$JSON 长度 M$
:循环D
如果:%K%<%U%
T:$JSON 获取 M %K%$
如果:%T%==%括号1%
%K%
返回
如果尾
K:[%K%+1]
$跳 :循环D$
如果尾
-1
"""
    script = parse(src, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        result = await vm.execute_handler(script.handlers[0], _make_event("trig"))
        out = render(result.segments).strip()
        if "got=1" not in out:
            raise AssertionError(
                f"说话词语 应返回 '苹果' 的索引 1, 实得 {out!r}"
            )
    finally:
        await kv.close()
    _print(
        "OK",
        f"$回调 说话词语苹果$: 索引 1; 输出={out!r}",
    )


# ---------------------------------------------------------------------------
# §6 [内部]说话词语(.*) — miss returns -1
# ---------------------------------------------------------------------------


async def case_shuohua_ciyu_miss() -> None:
    src = """trig
嘎:$回调 说话词语菠萝$
got=%嘎%

[内部]说话词语(.*)
M:["香蕉","苹果"]
B:0
K:0
U:$JSON 长度 M$
:循环D
如果:%K%<%U%
T:$JSON 获取 M %K%$
如果:%T%==%括号1%
%K%
返回
如果尾
K:[%K%+1]
$跳 :循环D$
如果尾
-1
"""
    script = parse(src, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        result = await vm.execute_handler(script.handlers[0], _make_event("trig"))
        out = render(result.segments).strip()
        if "got=-1" not in out:
            raise AssertionError(
                f"说话词语 没找到 '菠萝' 应返回 '-1', 实得 {out!r}"
            )
    finally:
        await kv.close()
    _print(
        "OK",
        f"$回调 说话词语菠萝$ (miss): 返回 -1; 输出={out!r}",
    )


# ---------------------------------------------------------------------------
# §7 卧底 [内部]游戏删除 — $删除$ 文件路径
# ---------------------------------------------------------------------------


async def case_woudi_youxi_shanchu(script: Any) -> None:
    """``[内部]游戏删除`` 调用 ``$删除$`` 把卧底 / 游戏 文件清空.

    路径形如 ``/storage/emulated/0/QR/QRDic/data/%群号%/卧底``,
    会被 ``$删除$`` shim 转成 KV scope 删除.
    """
    handler = find(script, "游戏删除")
    if handler is None:
        raise AssertionError("找不到 [内部]游戏删除")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        # 种点垃圾 KV
        await kv_seed(kv, f"{TEST_GROUP}/卧底/被投票", TEST_QQ, "1")
        await kv_seed(kv, f"游戏/{TEST_GROUP}.txt", "加入", "[]")
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        await vm.execute_handler(handler, _make_event("trig"))
        # 验证 KV 都被清空 (scope-level delete)
        v = await kv_peek(kv, f"{TEST_GROUP}/卧底/被投票", TEST_QQ, "MISS")
        if v != "MISS":
            raise AssertionError(
                f"游戏删除 应清空卧底 scope, 实得 {v!r}"
            )
    finally:
        await kv.close()
    _print(
        "OK",
        f"[内部]游戏删除: $删除$ 把 {TEST_GROUP}/卧底 scope 清空",
    )


# ---------------------------------------------------------------------------
# §8 黑杰克 加注([0-9]+) — 4-step KV 算术 + 校验
# ---------------------------------------------------------------------------


async def case_blackjack_jiazhu(script: Any) -> None:
    handler = find(script, "加注([0-9]+)")
    if handler is None:
        raise AssertionError("找不到 加注([0-9]+)")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        # 玩家 1 加注 50
        await kv_seed(kv, "啊/娱乐系/黑杰克", "玩家1", TEST_QQ)
        await kv_seed(kv, "啊/娱乐系/黑杰克", "玩家2", TARGET_QQ)
        await kv_seed(kv, "啊/娱乐系/黑杰克", f"黑杰克首发牌否{TEST_GROUP}", "1")
        await kv_seed(kv, "啊/娱乐系/黑杰克", f"加注量{TEST_GROUP}", "0")
        await kv_seed(kv, "啊/娱乐系/黑杰克", f"奖池{TEST_GROUP}", "100")
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "1000")
        sched = Scheduler()
        vm = _build_vm(kv, scheduler=sched, handler_lookup=make_smart_lookup(script))
        ev = _make_event("加注50")
        await vm.execute_handler(handler, ev, captures=["50"])
        bal = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "")
        pool = await kv_peek(kv, "啊/娱乐系/黑杰克", f"奖池{TEST_GROUP}", "")
        bet = await kv_peek(kv, "啊/娱乐系/黑杰克", f"加注量{TEST_GROUP}", "")
        if bal != "950":
            raise AssertionError(f"灵玉 应=950 (1000-50), 实得 {bal}")
        if pool != "150":
            raise AssertionError(f"奖池 应=150 (100+50), 实得 {pool}")
        if bet != "50":
            raise AssertionError(f"加注量 应=50, 实得 {bet}")
    finally:
        await kv.close()
    _print(
        "OK",
        f"加注50 黑杰克: 灵玉 1000→950; 奖池 100→150; 加注量=50",
    )


# ---------------------------------------------------------------------------
# §9 $调用$ scheduler-fired with space-trigger
# ---------------------------------------------------------------------------


async def case_schedule_with_space_trigger() -> None:
    """``$调用 0 游戏判断 555$`` 经 scheduler 派发应触发
    ``[内部]游戏判断 ([0-9]+)`` 并把 555 喂到 %括号1%.

    我们模拟 scheduler.fire → bootstrap._on_scheduled_fire 那条路径,
    用相同的 smart lookup + space-joined fallback.
    """
    src = """trig
$调用 0 游戏判断 555$

[内部]游戏判断 ([0-9]+)
M:["111","555","777"]
B:0
K:0
U:$JSON 长度 M$
:loop
如果:%K%<%U%
T:$JSON 获取 M %K%$
如果:%T%==%括号1%
FOUND_AT_%K%
返回
如果尾
K:[%K%+1]
$跳 :loop$
如果尾
NOT_FOUND
"""
    script = parse(src, strict=False)
    sched = Scheduler()
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        lookup = make_smart_lookup(script)
        vm = _build_vm(kv, scheduler=sched, handler_lookup=lookup)
        ev = _make_event("trig")
        await vm.execute_handler(script.handlers[0], ev)
        if sched.pending_count != 1:
            raise AssertionError(f"应排 1 个任务, 实得 {sched.pending_count}")
        task = list(sched._queue)[0]  # type: ignore[attr-defined]
        if task.handler_name != "游戏判断":
            raise AssertionError(f"task.handler_name 应=游戏判断, 实得 {task.handler_name!r}")
        if task.args != ["555"]:
            raise AssertionError(f"task.args 应=['555'], 实得 {task.args!r}")
        # 模拟 _on_scheduled_fire 的 space-joined fallback
        result = lookup(task.handler_name)
        consumed = False
        if result is None and task.args:
            joined = task.handler_name + " " + " ".join(str(a) for a in task.args)
            result = lookup(joined)
            if result is not None:
                consumed = True
        if isinstance(result, tuple):
            target, regex_caps = result
        else:
            target, regex_caps = result, []
        captures = regex_caps if consumed else regex_caps + list(task.args)
        sub_ev = _make_event(task.handler_name)
        sub_res = await vm.execute_handler(target, sub_ev, captures=captures)
        sub_out = render(sub_res.segments).strip()
        if "FOUND_AT_1" not in sub_out:
            raise AssertionError(
                f"$调用 0 游戏判断 555$ scheduler-fire 应触发 [内部]游戏判断 ([0-9]+) "
                f"并 FOUND_AT_1, 实得 {sub_out!r}"
            )
    finally:
        await kv.close()
    _print(
        "OK",
        f"$调用 0 游戏判断 555$ scheduler-fire: space-joined 命中正则 trigger",
    )


# ---------------------------------------------------------------------------
# §10 $回调$ literal trigger (无 regex)
# ---------------------------------------------------------------------------


async def case_callback_literal_trigger() -> None:
    src = """trig
s:$回调 plain_handler$
out=%s%

[内部]plain_handler
HELLO
"""
    script = parse(src, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        result = await vm.execute_handler(script.handlers[0], _make_event("trig"))
        out = render(result.segments).strip()
        if "out=HELLO" not in out:
            raise AssertionError(f"$回调 plain_handler$: {out!r}")
    finally:
        await kv.close()
    _print("OK", f"$回调 plain_handler$ (literal): out=HELLO")


# ---------------------------------------------------------------------------
# §11 苏苏问答通识 — internal handler called from catch-all
# ---------------------------------------------------------------------------


async def case_susu_wenda_tongshi(script: Any) -> None:
    handler = find(script, "苏苏问答通识")
    if handler is None:
        raise AssertionError("找不到 [内部]苏苏问答通识")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        # 没种问答, D 和 A 都是 [], handler 应早返回
        vm = _build_vm(kv, handler_lookup=make_smart_lookup(script))
        result = await vm.execute_handler(handler, _make_event("apple banana"))
        # 应无输出 (空 [])
        out = render(result.segments)
        if out.strip():
            raise AssertionError(
                f"[内部]苏苏问答通识 在 D=A=[] 时应无输出, 实得 {out!r}"
            )
    finally:
        await kv.close()
    _print("OK", "[内部]苏苏问答通识: D=A=[] 静默")


# ---------------------------------------------------------------------------
# §12 赛马结算 — 选手1 胜利分支
# ---------------------------------------------------------------------------


async def case_saima_jiesuan_yi_wins(script: Any) -> None:
    """选手1 位置最高 + 有人下注选手1 — 应给那人加 奖池*0.96 灵玉."""
    handler = find(script, "赛马结算")
    if handler is None:
        raise AssertionError("找不到 [内部]赛马结算")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, f"赛马/{TEST_GROUP}/选手1", "位置", "10")
        await kv_seed(kv, f"赛马/{TEST_GROUP}/选手2", "位置", "5")
        await kv_seed(kv, f"赛马/{TEST_GROUP}/选手3", "位置", "3")
        await kv_seed(kv, "啊/灵玉系/下注人", "下注选手1", TEST_QQ)
        await kv_seed(kv, "啊/灵玉系/赛马奖池", TEST_GROUP, "1000")
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "100")
        sched = Scheduler()
        vm = _build_vm(kv, scheduler=sched, handler_lookup=make_smart_lookup(script))
        result = await vm.execute_handler(handler, _make_event("trig"))
        out = render(result.segments)
        if "选手1" not in out or "恭喜" not in out:
            raise AssertionError(f"赛马结算 选手1 胜利文案缺失: {out!r}")
        # 灵玉 100 + 1000*0.96 = 1060
        bal = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "")
        if bal != "1060":
            raise AssertionError(f"灵玉 应=1060 (100+1000*0.96), 实得 {bal}")
        # 奖池清零
        pool = await kv_peek(kv, "啊/灵玉系/赛马奖池", TEST_GROUP, "")
        if pool != "0":
            raise AssertionError(f"奖池 应清零, 实得 {pool}")
    finally:
        await kv.close()
    _print(
        "OK",
        f"赛马结算 选手1胜: 灵玉 100→1060 (奖池1000*0.96); 奖池 0",
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def main() -> int:
    failed: list[str] = []
    src = RULES_PATH.read_text(encoding="utf-8")
    script = parse(src, filename=str(RULES_PATH), strict=False)
    _print("OK", f"main.ling 解析成功, 共 {len(script.handlers)} 个 handler")

    cases = [
        ("[内部]赛马打印位置3 (jump-loop)", case_saima_dayin_weizhi_inline),
        ("赛马进行 (内联 $回调$)", case_inline_callback_in_text),
        ("卧底 投票@xx 单次", case_woudi_toupiao),
        ("[内部]游戏判断 ([0-9]+) 带空格 trigger", case_youxipanduan_with_space),
        ("[内部]说话词语(.*) 索引命中", case_shuohua_ciyu_index),
        ("[内部]说话词语(.*) 未命中 → -1", case_shuohua_ciyu_miss),
        ("[内部]游戏删除 ($删除$)", case_woudi_youxi_shanchu),
        ("黑杰克 加注50", case_blackjack_jiazhu),
        ("$调用$ scheduler 带空格 trigger", case_schedule_with_space_trigger),
        ("$回调 literal trigger", case_callback_literal_trigger),
        ("[内部]苏苏问答通识", case_susu_wenda_tongshi),
        ("[内部]赛马结算 选手1胜", case_saima_jiesuan_yi_wins),
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
