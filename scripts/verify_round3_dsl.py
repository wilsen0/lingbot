"""第三轮: 跨块 jump / 内部 handler 链 / 全员守护链 / 多 capture group 高优先级.

目标:
1. ``[戳一戳]`` — Code 守卫 + 多档 ``$jump :形象标记$`` 跳出 if 嵌套
2. ``(.*)(背包|物品)(.*)`` — 3 capture groups + 同套形象标记 jump
3. ``兑换御妖符([0-9]+)`` — 现金式 [玉]<[括号1*100] 条件 + 1% 手续费扣减
4. ``兑换灵玉`` — 反向兑换, 用 %卡%*100 字面量当算术 (会保留字符串)
5. ``(查看消息|消息)`` — KV read → 输出 → 清空
6. ``解码/编码/Base64Decoder`` 等 codec 工具的 capture-group-as-tool-arg
7. ``加注([0-9]+)`` 黑杰克游戏中的下注流
8. ``(.*)国庆(.*)`` — 节日礼包 KV state + 限量逻辑
9. ``送花@.*`` + 送花([0-9]+) — at-mention vs 数字 两种调用形态
10. ``我的(卡|御妖)(.*)`` — OR 触发 + 第二捕获组吞掉余下的
11. ``[内部]开牌了`` 等 ``$调用$`` 链触发的 internal 推断
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

import linling_tools_stdlib  # noqa: F401
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
ROBOT_QQ = "5000000000"


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


def _build_vm(
    kv: SqliteKVStore,
    *,
    scheduler: Scheduler | None = None,
) -> VM:
    extras: dict[str, Any] = {
        "admin_users": (ADMIN_QQ,),
    }
    if scheduler is not None:
        extras["scheduler"] = scheduler
    return VM(tool_registry=registry, kv=kv, bot_id="susu_test", extras=extras)


def render(segs: list[Any]) -> str:
    return "".join(s.text for s in segs if isinstance(s, TextSegment))


def find(script: Any, trigger: str) -> Any | None:
    for h in script.handlers:
        if h.trigger == trigger:
            return h
    return None


# ---------------------------------------------------------------------------
# §1 [戳一戳] — Code guard + multi-jump out of nested if
# ---------------------------------------------------------------------------


async def case_chuoyichuo_jump_chain(script: Any) -> None:
    """戳一戳 一旦满足某个守护态, 必须 jump 到 :形象标记 而不是漏到 后续逻辑.

    我们种一个 ``个人守护=思思``, 期待 handler 输出: 思思.jpg + 后面的 stats 行.
    如果 jump 没工作, ``思思`` 那行后面会再跑别的 ``%Z%==`` 比较拿不到 hit.
    ``[戳一戳]`` 被 classifier 当字面量, 触发文 = 字符串 ``[戳一戳]``, OneBot 适配器
    把戳一戳事件改写成这条文本进 bus.
    """
    handler = find(script, "[戳一戳]")
    assert handler is not None, "找不到 [戳一戳] handler"

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        # %Code% 走 raw['operator_id']; handler 检查 ``%Code%!=%Robot%``,
        # 我们让 operator_id == bot_id 让守卫通过.
        ev = _make_event("[戳一戳]", raw={"operator_id": "susu_test"})

        # 种身份资料
        from datetime import datetime as _dt

        today_mmdd = _dt.now().strftime("%m%d")
        today_mmddhh = _dt.now().strftime("%m%d%H")
        today_dd = _dt.now().strftime("%d")
        await kv_seed(kv, "休闲系/珍品/个人守护", TEST_QQ, "思思")
        # 个人守护天/时 必须 == 今天, 否则前面有一段守卫会把守护清零.
        await kv_seed(kv, "休闲系/珍品/个人守护天", TEST_QQ, today_mmdd)
        await kv_seed(kv, "休闲系/珍品/个人守护时", TEST_QQ, today_mmddhh)
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "888")
        await kv_seed(kv, "啊/禁言系/妖力", TEST_QQ, "5")
        # ``如果:%群号%!=0&$读 啊/禁言系/禁言卡限制 %QQ% 0$!=%时间dd%``
        # 这一支会跳出去 ``$调用 0 温柔打卡$`` 然后早返回, 导致后面的
        # 守护-image 分支永远不到. 把 禁言卡限制 设成今天的 dd 让它
        # 进 fallthrough.
        await kv_seed(kv, "啊/禁言系/禁言卡限制", TEST_QQ, today_dd)

        scheduler = Scheduler()
        vm = _build_vm(kv, scheduler=scheduler)
        result = await vm.execute_handler(handler, ev, captures=[])
        text = render(result.segments)
        # image segment 应有 思思.jpg
        any_image_susu = any(
            "思思" in getattr(s, "url", "")
            for s in result.segments
            if hasattr(s, "url")
        )
        if not any_image_susu:
            urls = [getattr(s, "url", None) for s in result.segments if hasattr(s, "url")]
            raise AssertionError(
                f"应输出 思思 的图片 segment, 实际 url列表={urls}; text={text!r}"
            )
        # 同时还应输出 stats (灵玉 888, 妖力 5)
        if "888" not in text or "5" not in text:
            raise AssertionError(f"形象标记 后续 stats 行未输出: {text!r}")
    finally:
        await kv.close()
    _print(
        "OK",
        f"[戳一戳] 多 jump 跳到 :形象标记 成功; stats 文本={text.strip()!r}",
    )


# ---------------------------------------------------------------------------
# §2 (.*)(背包|物品)(.*) — 3 capture groups
# ---------------------------------------------------------------------------


async def case_beibao_capture(script: Any) -> None:
    classifier = MessageClassifier(script, command_prefixes=())
    cases = [
        ("背包", "(.*)(背包|物品)(.*)", ["", "背包", ""]),
        ("看看物品列表", "(.*)(背包|物品)(.*)", ["看看", "物品", "列表"]),
    ]
    misses: list[str] = []
    for txt, expected_trig, expected_caps in cases:
        ev = _make_event(txt)
        intent = classifier.classify(ev)
        m = intent.match
        if m is None or m.handler.trigger != expected_trig:
            misses.append(
                f"{txt!r}: 期望 {expected_trig!r}, "
                f"实得 {m.handler.trigger if m else (intent.kind, intent.reason)!r}"
            )
            continue
        if m.captures != expected_caps:
            misses.append(f"{txt!r}: 期望 captures={expected_caps}, 实得 {m.captures}")
    if misses:
        for line in misses:
            _print("FAIL", "  " + line)
        raise AssertionError("(.*)(背包|物品)(.*) 捕获失败")
    _print("OK", "(.*)(背包|物品)(.*) 三 capture group 命中")


# ---------------------------------------------------------------------------
# §3 兑换御妖符([0-9]+) — 1% surcharge
# ---------------------------------------------------------------------------


async def case_duihuan_yuyaofu(script: Any) -> None:
    handler = find(script, "兑换御妖符([0-9]+)")
    if handler is None:
        raise AssertionError("找不到 兑换御妖符([0-9]+)")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "10000")
        await kv_seed(kv, "啊/苏苏状态/心情值", TEST_QQ, "0")
        await kv_seed(kv, f"啊/{TEST_GROUP}/禁言卡", TEST_QQ, "2")
        scheduler = Scheduler()
        vm = _build_vm(kv, scheduler=scheduler)
        # %卡% / %玉% 算术: 需 玉 >= 9*100 = 900; 扣 9*101 = 909
        ev = _make_event("兑换御妖符9")
        await vm.execute_handler(handler, ev, captures=["9"])
        bal = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        cards = await kv_peek(kv, f"啊/{TEST_GROUP}/禁言卡", TEST_QQ, "0")
        if bal != "9091":  # 10000 - 909 = 9091
            raise AssertionError(f"灵玉余额预期 9091 (10000-9*101), 实得 {bal}")
        if cards != "11":  # 2 + 9
            raise AssertionError(f"禁言卡 预期 11, 实得 {cards}")
    finally:
        await kv.close()
    _print(
        "OK",
        f"兑换御妖符9: 灵玉 10000→9091 (扣 9*101); 卡 2→11 (+9)",
    )


# ---------------------------------------------------------------------------
# §4 兑换灵玉 — backward exchange w/ literal-string assignment
# ---------------------------------------------------------------------------


async def case_duihuan_lingyu(script: Any) -> None:
    """``兑换灵玉`` 全部输出走 ``±img=$图文 ...$±`` 进图片, 没有纯文本.

    断言:
    - 灵玉余额按 [%玉%+%卡%*100] 算术正确
    - 禁言卡 清零
    - 至少一个 ImageSegment 输出
    - ``换:%卡%*100`` 的赋值是字面量 (不算术), 后面 ``[%玉%+%卡%*100]`` 才是真算术
    """
    handler = find(script, "兑换灵玉")
    if handler is None:
        raise AssertionError("找不到 兑换灵玉")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "100")
        await kv_seed(kv, "啊/苏苏状态/心情值", TEST_QQ, "0")
        await kv_seed(kv, f"啊/{TEST_GROUP}/禁言卡", TEST_QQ, "5")
        scheduler = Scheduler()
        vm = _build_vm(kv, scheduler=scheduler)
        result = await vm.execute_handler(handler, _make_event("兑换灵玉"), captures=[])
        bal = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        cards = await kv_peek(kv, f"啊/{TEST_GROUP}/禁言卡", TEST_QQ, "0")

        # 100 + 5*100 = 600
        if bal != "600":
            raise AssertionError(f"灵玉余额预期 600 (100+5*100), 实得 {bal}")
        if cards != "0":
            raise AssertionError(f"禁言卡 应清零, 实得 {cards}")
        # 至少一个 ImageSegment
        from linling_core import ImageSegment

        if not any(isinstance(s, ImageSegment) for s in result.segments):
            raise AssertionError(
                f"兑换灵玉 应有 ImageSegment 输出, 实际 segments={result.segments}"
            )
    finally:
        await kv.close()
    _print(
        "OK",
        f"兑换灵玉: 灵玉 100→600 (算术正确); 卡 5→0; 输出含 ImageSegment",
    )


# ---------------------------------------------------------------------------
# §5 (查看消息|消息) — read → output → clear
# ---------------------------------------------------------------------------


async def case_chakanxiaoxi(script: Any) -> None:
    handler = find(script, "(查看消息|消息)")
    assert handler is not None
    classifier = MessageClassifier(script, command_prefixes=())
    for txt in ["查看消息", "消息"]:
        ev = _make_event(txt)
        intent = classifier.classify(ev)
        if intent.match is None or intent.match.handler.trigger != "(查看消息|消息)":
            raise AssertionError(
                f"{txt!r} 路由错误: "
                f"{intent.match.handler.trigger if intent.match else intent.reason}"
            )
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/主页系/最新消息", TEST_QQ, "你有 3 朵新玫瑰花")
        vm = _build_vm(kv)
        result = await vm.execute_handler(
            handler, _make_event("消息"), captures=["消息"]
        )
        text = render(result.segments)
        if "你有 3 朵新玫瑰花" not in text:
            raise AssertionError(f"输出应含消息内容, 实得 {text!r}")
        # 读完应清零
        cleared = await kv_peek(kv, "啊/主页系/最新消息", TEST_QQ, "MISS")
        if cleared not in ("0", ""):
            raise AssertionError(f"读完后消息应清零, 实得 {cleared!r}")
    finally:
        await kv.close()
    _print("OK", f"(查看消息|消息): 输出消息后清零; 输出={text.strip()!r}")


# ---------------------------------------------------------------------------
# §6 解码/编码/Base64Decoder — codec tools w/ capture group as arg
# ---------------------------------------------------------------------------


async def case_codec_tools(script: Any) -> None:
    cases = [
        ("URLEncoder", "编码(.*)", "编码hello world", "hello%20world"),
        ("URLDecoder", "解码(.*)", "解码hello%20world", "hello world"),
        ("Base64Decoder", "64解码(.*)", "64解码aGVsbG8=", "hello"),
        ("HexEncoder", "hex编码(.*)", "hex编码A", "41"),
        ("HexDecoder", "hex解码(.*)", "hex解码4142", "AB"),
    ]
    classifier = MessageClassifier(script, command_prefixes=())
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv)
        for name, trig, msg, expected in cases:
            handler = find(script, trig)
            if handler is None:
                raise AssertionError(f"找不到 {trig}")
            ev = _make_event(msg)
            intent = classifier.classify(ev)
            if intent.match is None or intent.match.handler.trigger != trig:
                raise AssertionError(
                    f"{msg!r} 路由错误: "
                    f"{intent.match.handler.trigger if intent.match else intent.reason}"
                )
            result = await vm.execute_handler(
                handler, ev, captures=intent.match.captures
            )
            text = render(result.segments).strip()
            if expected not in text:
                raise AssertionError(
                    f"{name}: 输入={msg!r} 期望含 {expected!r}, 实得 {text!r}"
                )
    finally:
        await kv.close()
    _print("OK", f"codec 工具: URL/Base64/Hex 编解码 全部正确")


# ---------------------------------------------------------------------------
# §7 加注([0-9]+) — bet handler classifier + capture
# ---------------------------------------------------------------------------


async def case_jiazhu_classifier(script: Any) -> None:
    classifier = MessageClassifier(script, command_prefixes=())
    ev = _make_event("加注200")
    intent = classifier.classify(ev)
    if intent.match is None or intent.match.handler.trigger != "加注([0-9]+)":
        raise AssertionError(
            f"加注 路由错误: "
            f"{intent.match.handler.trigger if intent.match else intent.reason}"
        )
    if intent.match.captures != ["200"]:
        raise AssertionError(f"捕获错误: {intent.match.captures}")
    _print("OK", "加注([0-9]+) 路由 + 捕获 一致")


# ---------------------------------------------------------------------------
# §8 (.*)国庆(.*) — KV state + balance check
# ---------------------------------------------------------------------------


async def case_guoqing_giftpack(script: Any) -> None:
    handler = find(script, "(.*)国庆(.*)")
    if handler is None:
        raise AssertionError("找不到 (.*)国庆(.*)")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/节日系/节日礼包", TEST_QQ, "0")
        await kv_seed(kv, "休闲系/珍品/气球", TEST_QQ, "0")
        # 剩余数量 < 1 才返回; 我们种 5
        await kv_seed(kv, "休闲系/珍品/气球", "剩余数量", "5")
        await kv_seed(kv, "小苏苏/密友", TEST_QQ, "0")
        vm = _build_vm(kv)
        result = await vm.execute_handler(
            handler, _make_event("国庆快乐"), captures=["", "快乐"]
        )
        text = render(result.segments)
        if "气球" not in text and "🎈" not in text:
            raise AssertionError(f"国庆 输出应有气球, 实得 {text!r}")
        # 节日礼包 写=1
        gift = await kv_peek(kv, "啊/节日系/节日礼包", TEST_QQ, "0")
        if gift != "1":
            raise AssertionError(f"节日礼包 应=1, 实得 {gift}")
        balloon = await kv_peek(kv, "休闲系/珍品/气球", TEST_QQ, "0")
        if balloon != "1":
            raise AssertionError(f"个人气球 应=1, 实得 {balloon}")
        remain = await kv_peek(kv, "休闲系/珍品/气球", "剩余数量", "0")
        if remain != "4":
            raise AssertionError(f"剩余数量 应=4, 实得 {remain}")
    finally:
        await kv.close()
    _print("OK", f"(.*)国庆(.*): 拿气球 + 节日礼包 标记; 剩余 5→4")


# ---------------------------------------------------------------------------
# §9 送花@.* — at-mention version
# ---------------------------------------------------------------------------


async def case_songhua_at(script: Any) -> None:
    handler = find(script, "送花@.*")
    if handler is None:
        raise AssertionError("找不到 送花@.*")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/活动系/玫瑰花", TEST_QQ, "5")
        await kv_seed(kv, "啊/活动系/玫瑰花", TARGET_QQ, "0")
        vm = _build_vm(kv)
        result = await vm.execute_handler(
            handler, _make_event("送花@", at=TARGET_QQ), captures=[]
        )
        text = render(result.segments)
        if "玫瑰花送给了ta" not in text and "您把玫瑰花送给了ta" not in text:
            raise AssertionError(f"送花@ 输出错: {text!r}")
        sender_flowers = await kv_peek(kv, "啊/活动系/玫瑰花", TEST_QQ, "0")
        target_flowers = await kv_peek(kv, "啊/活动系/玫瑰花", TARGET_QQ, "0")
        if sender_flowers != "4":
            raise AssertionError(f"sender 玫瑰花 应=4, 实得 {sender_flowers}")
        if target_flowers != "1":
            raise AssertionError(f"target 玫瑰花 应=1, 实得 {target_flowers}")
        # 主页消息 应有写入
        msg = await kv_peek(kv, "啊/主页系/最新消息", TARGET_QQ, "")
        if "玫瑰花" not in msg:
            raise AssertionError(f"主页消息 应含 '玫瑰花', 实得 {msg!r}")
    finally:
        await kv.close()
    _print("OK", f"送花@: 自己 5→4; 对方 0→1; 主页消息已写入")


# ---------------------------------------------------------------------------
# §10 我的(卡|御妖)(.*) — OR + greedy second capture
# ---------------------------------------------------------------------------


async def case_wode_ka_yuyao(script: Any) -> None:
    classifier = MessageClassifier(script, command_prefixes=())
    cases = [
        ("我的卡", ["卡", ""]),
        ("我的御妖", ["御妖", ""]),
        ("我的卡片", ["卡", "片"]),
    ]
    for txt, expected_caps in cases:
        ev = _make_event(txt)
        intent = classifier.classify(ev)
        m = intent.match
        if m is None or m.handler.trigger != "我的(卡|御妖)(.*)":
            raise AssertionError(
                f"{txt!r} 路由错误: "
                f"{m.handler.trigger if m else (intent.kind, intent.reason)}"
            )
        if m.captures != expected_caps:
            raise AssertionError(
                f"{txt!r} 捕获错误: 期望 {expected_caps}, 实得 {m.captures}"
            )
    _print("OK", "我的(卡|御妖)(.*) OR + 第二捕获组 一致")


# ---------------------------------------------------------------------------
# §11 (.*)吞(.*) — 反馈兜底 — 验证不会吃掉 ``反馈吞玉123``
# ---------------------------------------------------------------------------


async def case_tunyu_priority(script: Any) -> None:
    """``反馈吞玉([0-9]+)`` 应该比 ``(.*)吞(.*)`` 更早触发 (声明顺序在前)."""
    classifier = MessageClassifier(script, command_prefixes=())
    cases = [
        ("反馈吞玉123", "反馈吞玉([0-9]+)"),
        ("我吞了一颗珍珠", "(.*)吞(.*)"),
    ]
    for txt, expected in cases:
        ev = _make_event(txt)
        intent = classifier.classify(ev)
        m = intent.match
        if m is None or m.handler.trigger != expected:
            raise AssertionError(
                f"{txt!r}: 期望 {expected!r}, 实得 "
                f"{m.handler.trigger if m else (intent.kind, intent.reason)!r}"
            )
    _print("OK", "反馈吞玉 优先于 (.*)吞(.*) catch-all")


# ---------------------------------------------------------------------------
# §12 黑杰克: 加入对局 → 加入对局 (玩家 2) → 内部状态 OK
# ---------------------------------------------------------------------------


async def case_blackjack_join(script: Any) -> None:
    handler = find(script, "加入对局")
    if handler is None:
        raise AssertionError("找不到 加入对局")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        # 玩家 1 加入
        await kv_seed(kv, "啊/苏苏状态/心情值", TEST_QQ, "0")
        await kv_seed(kv, "啊/苏苏状态/心情值", TARGET_QQ, "0")
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "1000")
        await kv_seed(kv, "啊/灵玉系/灵玉", TARGET_QQ, "1000")
        await kv_seed(kv, "啊/娱乐系/黑杰克加入保护", "进行的群", "0")
        await kv_seed(kv, "啊/娱乐系/黑杰克", "玩家1", "0")
        await kv_seed(kv, "啊/娱乐系/黑杰克", "玩家2", "0")
        await kv_seed(kv, "啊/娱乐系/黑杰克", "是否开始", "0")
        await kv_seed(kv, "啊/娱乐系/黑杰克", f"奖池{TEST_GROUP}", "0")
        vm = _build_vm(kv)
        # 玩家 1 (TEST_QQ)
        await vm.execute_handler(handler, _make_event("加入对局"), captures=[])
        p1 = await kv_peek(kv, "啊/娱乐系/黑杰克", "玩家1", "")
        b1 = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "")
        if p1 != TEST_QQ:
            raise AssertionError(f"玩家1 应是 {TEST_QQ}, 实得 {p1!r}")
        if b1 != "900":
            raise AssertionError(f"玩家1 灵玉 1000→900, 实得 {b1!r}")
        # 玩家 2 (TARGET_QQ)
        await vm.execute_handler(
            handler, _make_event("加入对局", sender=TARGET_QQ), captures=[]
        )
        p2 = await kv_peek(kv, "啊/娱乐系/黑杰克", "玩家2", "")
        b2 = await kv_peek(kv, "啊/灵玉系/灵玉", TARGET_QQ, "")
        ready = await kv_peek(kv, "啊/娱乐系/黑杰克", f"是否准备{TEST_GROUP}", "")
        pool = await kv_peek(kv, "啊/娱乐系/黑杰克", f"奖池{TEST_GROUP}", "")
        if p2 != TARGET_QQ:
            raise AssertionError(f"玩家2 应是 {TARGET_QQ}, 实得 {p2!r}")
        if b2 != "900":
            raise AssertionError(f"玩家2 灵玉 1000→900, 实得 {b2!r}")
        if ready != "1":
            raise AssertionError(f"是否准备 应=1, 实得 {ready!r}")
        if pool != "200":
            raise AssertionError(f"奖池 应=200, 实得 {pool!r}")
    finally:
        await kv.close()
    _print(
        "OK",
        f"黑杰克 加入对局: 玩家1+玩家2 都扣 100; 奖池=200; 准备=1",
    )


# ---------------------------------------------------------------------------
# §13 ``%时间%`` 复杂格式串
# ---------------------------------------------------------------------------


async def case_time_formats() -> None:
    """``%时间MMdd%`` / ``%时间HHmm%`` / ``%时间HH:mm%`` 等输出与 datetime 一致.

    每个输出独占一行 — parser 的 assignment 启发式会把单行
    ``日:%时间MMdd% 时:...`` 整体当成一次赋值, 必须 break 出多行.
    """

    class _DT:
        @classmethod
        def now(cls):
            return datetime(2026, 5, 20, 14, 35, 7)

    body = """trig
%时间MMdd%
%时间HH%
%时间mm%
%时间HH:mm%
%时间MMddHH%
"""
    script = parse(body, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = _build_vm(kv)
        with patch("linling_dsl.vm.datetime", _DT):
            res = await vm.execute_handler(
                script.handlers[0], _make_event("trig")
            )
        out = render(res.segments)
        if "0520" not in out:
            raise AssertionError(f"%时间MMdd% 错: {out!r}")
        if "14:35" not in out:
            raise AssertionError(f"%时间HH:mm% 错: {out!r}")
        if "052014" not in out:
            raise AssertionError(f"%时间MMddHH% 错: {out!r}")
    finally:
        await kv.close()
    _print("OK", f"时间格式 全套: {out.strip()}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def main() -> int:
    failed: list[str] = []
    src = RULES_PATH.read_text(encoding="utf-8")
    script = parse(src, filename=str(RULES_PATH), strict=False)
    _print("OK", f"main.ling 解析成功, 共 {len(script.handlers)} 个 handler")

    cases = [
        ("[戳一戳] 多 jump 跳到形象标记", case_chuoyichuo_jump_chain),
        ("(.*)(背包|物品)(.*) 三 capture", case_beibao_capture),
        ("兑换御妖符([0-9]+)", case_duihuan_yuyaofu),
        ("兑换灵玉 (字面 + 算术)", case_duihuan_lingyu),
        ("(查看消息|消息) 读+清零", case_chakanxiaoxi),
        ("URL/Base64/Hex 编解码", case_codec_tools),
        ("加注([0-9]+) 路由+捕获", case_jiazhu_classifier),
        ("(.*)国庆(.*) 节日礼包", case_guoqing_giftpack),
        ("送花@.* at-mention", case_songhua_at),
        ("我的(卡|御妖)(.*) OR+捕获", case_wode_ka_yuyao),
        ("(.*)吞(.*) 反馈优先级", case_tunyu_priority),
        ("黑杰克 加入对局 双玩家", case_blackjack_join),
        ("%时间% 复杂格式", case_time_formats),
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
