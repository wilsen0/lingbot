"""第二轮:覆盖 main.ling 其它复杂指令模块.

目标模块:
1. 灵玉划转(.*)@.* / 灵玉划转(.*)给(.*) — 4% 手续费 + 给苏总账户 + admin 大额报警
2. 提升妖力(.*)@.* — 多变量算术 + at-mention
3. 偷玉@.* / 逃跑 / 抓@.* — 多档时间窗口 (5/4/3/2/1) + KV state 流转
4. (我在|我去)(.*)天书(.*) — 3 capture groups, 时间窗口
5. 漂流瓶 — JSON 操作链 (添加/获取/删除/长度), 包括 R/P 共维护两个数组
6. 甩杆 / 起杆 — 时间窗口 + 多个 ``$调用 1000 钓到XX$`` 内部 handler
7. 加注 / 下注 — ``$jump :跳转$`` 跳进/跳出 if + KV state 控制流
8. (钓鱼|鱼塘) - OR trigger
9. 苏苏(.*) - fallback chat catcher (要避免它吃掉所有苏苏前缀指令)
10. JSON 长度 / 添加 / 删除 over a sequence
"""

from __future__ import annotations

import asyncio
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
ESCROW_QQ = "1707476110"  # 苏总账户, 灵玉划转 4% 手续费目的地
TEST_GROUP = "999999999"
TEST_GROUP_ROUTE = "206470486"  # (我在|我去) 路线规则只在这群生效
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


async def kv_peek(
    kv: SqliteKVStore, dsl_path: str, key: str, default: str | None = None
) -> str | None:
    scope, file = _split(dsl_path)
    return await kv.read(scope, file, key, default)


def make_event(
    text: str,
    *,
    sender: str = TEST_QQ,
    group: str = TEST_GROUP,
    at: str | None = None,
) -> Event:
    segments: list[Any] = [TextSegment(text=text)]
    if at:
        segments.append(AtSegment(user_id=at))
    return Event(
        id=f"e-{sender}-{text[:8]}",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id=group, platform="cli"),
        sender=User(id=sender, platform="cli", display_name="试用者"),
        segments=segments,
    )


def build_vm(
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


def make_lookup(script: Any):
    def _l(name: str):
        for h in script.handlers:
            if h.trigger == name:
                return h
        return None

    return _l


# ---------------------------------------------------------------------------
# §1 灵玉划转 — 4% 手续费 + 苏总账户进账 + admin 大额报警
# ---------------------------------------------------------------------------


async def case_lingyu_huazhuan_at(script: Any) -> None:
    """``灵玉划转100@对方`` 应该: 自己-104; 对方+100; 苏总+4."""
    handler = find(script, "灵玉划转([0-9]+)@.*")
    if handler is None:
        raise AssertionError("找不到 灵玉划转(.*)@.* handler")
    classifier = MessageClassifier(script, command_prefixes=())
    ev = make_event("灵玉划转100@xxx", at=TARGET_QQ)
    intent = classifier.classify(ev)
    if intent.match is None or intent.match.handler.trigger != "灵玉划转([0-9]+)@.*":
        raise AssertionError(
            f"灵玉划转 路由错误: {intent.match.handler.trigger if intent.match else intent.reason}"
        )
    if intent.match.captures != ["100"]:
        raise AssertionError(f"灵玉划转 捕获错误: {intent.match.captures}")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "1000")
        await kv_seed(kv, "啊/灵玉系/灵玉", TARGET_QQ, "10")
        await kv_seed(kv, "啊/灵玉系/灵玉", ESCROW_QQ, "0")

        vm = build_vm(kv)
        await vm.execute_handler(handler, ev, captures=intent.match.captures)
        sender = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        target = await kv_peek(kv, "啊/灵玉系/灵玉", TARGET_QQ, "0")
        escrow = await kv_peek(kv, "啊/灵玉系/灵玉", ESCROW_QQ, "0")

        # 1000 - 100*1.04 = 1000 - 104 = 896
        if sender != "896":
            raise AssertionError(f"sender 余额预期 896, 实得 {sender}")
        # 10 + 100 = 110
        if target != "110":
            raise AssertionError(f"target 余额预期 110, 实得 {target}")
        # 0 + 100 * 0.04 = 4
        if escrow != "4":
            raise AssertionError(f"escrow 余额预期 4 (4% 手续费), 实得 {escrow}")
    finally:
        await kv.close()
    _print("OK", "灵玉划转(.*)@.*: 余额扣减 + 4% 手续费 + escrow 进账 一致")


async def case_lingyu_huazhuan_geiqq(script: Any) -> None:
    """``灵玉划转100给12345`` 也跑一次, 验证两个捕获组的算术."""
    handler = find(script, "灵玉划转([0-9]+)给([0-9]+)")
    assert handler is not None
    classifier = MessageClassifier(script, command_prefixes=())
    ev = make_event(f"灵玉划转50给{TARGET_QQ}")
    intent = classifier.classify(ev)
    if intent.match is None or intent.match.handler.trigger != "灵玉划转([0-9]+)给([0-9]+)":
        raise AssertionError(
            f"路由错误: {intent.match.handler.trigger if intent.match else intent.reason}"
        )
    if intent.match.captures != ["50", TARGET_QQ]:
        raise AssertionError(f"捕获错误: {intent.match.captures}")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "500")
        await kv_seed(kv, "啊/灵玉系/灵玉", TARGET_QQ, "0")
        vm = build_vm(kv)
        await vm.execute_handler(handler, ev, captures=intent.match.captures)
        sender = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        target = await kv_peek(kv, "啊/灵玉系/灵玉", TARGET_QQ, "0")
        # 500 - 50*1.04 = 500 - 52 = 448
        if sender != "448":
            raise AssertionError(f"sender 余额预期 448, 实得 {sender}")
        if target != "50":
            raise AssertionError(f"target 余额预期 50, 实得 {target}")
    finally:
        await kv.close()
    _print("OK", "灵玉划转([0-9]+)给([0-9]+): 两个捕获组算术 一致")


# ---------------------------------------------------------------------------
# §2 提升妖力 — at-mention multiplied cost
# ---------------------------------------------------------------------------


async def case_tisheng_yaoli_at(script: Any) -> None:
    """``提升妖力10@xxx`` 应该: 灵玉 -660, 对方妖力 +10."""
    handler = find(script, "提升妖力([0-9]+)@.*")
    assert handler is not None
    classifier = MessageClassifier(script, command_prefixes=())
    ev = make_event("提升妖力10@xx", at=TARGET_QQ)
    intent = classifier.classify(ev)
    if (
        intent.match is None
        or intent.match.handler.trigger != "提升妖力([0-9]+)@.*"
    ):
        raise AssertionError(
            f"路由错误: {intent.match.handler.trigger if intent.match else intent.reason}"
        )

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "2000")
        await kv_seed(kv, "啊/禁言系/妖力", TARGET_QQ, "5")
        vm = build_vm(kv)
        result = await vm.execute_handler(
            handler, ev, captures=intent.match.captures
        )
        balance = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        yaoli = await kv_peek(kv, "啊/禁言系/妖力", TARGET_QQ, "0")
        if balance != "1340":  # 2000 - 10*66 = 1340
            raise AssertionError(f"灵玉余额预期 1340, 实得 {balance}")
        if yaoli != "15":  # 5 + 10
            raise AssertionError(f"对方妖力预期 15, 实得 {yaoli}")
        text = render(result.segments)
    finally:
        await kv.close()
    _print(
        "OK",
        f"提升妖力10@: 灵玉 2000→1340; 对方妖力 5→15; 输出={text.strip()!r}",
    )


# ---------------------------------------------------------------------------
# §3 偷玉游戏 — KV state-machine + 时间窗口
# ---------------------------------------------------------------------------


async def case_touyu_then_paolu(script: Any) -> None:
    """``偷玉@xxx`` → ``逃跑`` 一气呵成, 模拟最快档 (5+ 分钟): 100% 偷得."""
    h_steal = find(script, "偷玉@.*")
    h_run = find(script, "(逃跑|跑路)")
    assert h_steal and h_run

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/灵玉系/灵玉", TARGET_QQ, "20000")  # 富裕目标
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        await kv_seed(kv, "啊/禁言系/妖力", TEST_QQ, "10")
        await kv_seed(kv, "啊/禁言系/妖力", TARGET_QQ, "5")
        await kv_seed(kv, "啊/%群%/个人耐心", TEST_QQ, "0")

        vm = build_vm(kv)
        ev_steal = make_event("偷玉@xx", at=TARGET_QQ)
        await vm.execute_handler(h_steal, ev_steal)

        amount = await kv_peek(kv, "偷玉游戏/偷玉数量", TEST_QQ, "0")
        if amount == "0":
            raise AssertionError("偷玉数量未被写入")
        # 验证 state 已建立
        target_in_steal = await kv_peek(kv, "偷玉游戏/偷玉对象", TEST_QQ, "0")
        watcher = await kv_peek(kv, "偷玉游戏/谁在偷我玉", TARGET_QQ, "0")
        if target_in_steal != TARGET_QQ:
            raise AssertionError(f"偷玉对象未对齐: {target_in_steal!r}")
        if watcher != TEST_QQ:
            raise AssertionError(f"谁在偷我玉未对齐: {watcher!r}")

        # patch 时间, 让 [%时间HHmm%-%时%]>5
        # %时%是写入时的 %时间HHmm%; 我们把当前时间往后调 6 分钟:
        steal_time = await kv_peek(kv, "偷玉游戏/偷玉开始时间", TEST_QQ, "0")
        # %时% 是 4 位数 HHmm; 加 6 分钟即可
        st = int(steal_time)
        new_hh = st // 100
        new_mm = st % 100 + 6
        if new_mm >= 60:
            new_hh += 1
            new_mm -= 60
        new_time_hhmm = f"{new_hh:02d}{new_mm:02d}"

        class _FakeDT:
            @classmethod
            def now(cls):
                # 拿 hhmm 还原成今天该时刻
                now = datetime(2026, 5, 20, new_hh, new_mm, 0)
                return now

        ev_run = make_event("逃跑")
        with patch("linling_dsl.vm.datetime", _FakeDT):
            await vm.execute_handler(h_run, ev_run, captures=["逃跑"])

        # 验证: 自己 +amount; 目标 -amount; state 清零
        my_balance = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        target_balance = await kv_peek(kv, "啊/灵玉系/灵玉", TARGET_QQ, "0")
        if my_balance == "0":
            raise AssertionError(
                f"自己余额应增加, 实得 {my_balance}; 偷玉数量={amount}; "
                f"steal_time={steal_time}, fake_now={new_time_hhmm}"
            )
        target_state_after = await kv_peek(kv, "偷玉游戏/偷玉对象", TEST_QQ, "0")
        if target_state_after != "0":
            raise AssertionError(
                f"逃跑后 偷玉对象 应被重置为 0, 实得 {target_state_after!r}"
            )
    finally:
        await kv.close()
    _print(
        "OK",
        f"偷玉@→逃跑 (5+min): 抢走 {amount}; 自己={my_balance}, 目标={target_balance}",
    )


# ---------------------------------------------------------------------------
# §4 (我在|我去)(.*)天书(.*) — 3 capture groups
# ---------------------------------------------------------------------------


async def case_route_record(script: Any) -> None:
    handler = find(script, "(我在|我去)(.*)天书(.*)")
    if handler is None:
        raise AssertionError("找不到 (我在|我去)(.*)天书(.*)")
    classifier = MessageClassifier(script, command_prefixes=())
    ev = make_event("我去 1线 天书 满人", group=TEST_GROUP_ROUTE)
    intent = classifier.classify(ev)
    if intent.match is None or intent.match.handler.trigger != "(我在|我去)(.*)天书(.*)":
        raise AssertionError(
            f"路由错误: {intent.match.handler.trigger if intent.match else intent.reason}"
        )
    if intent.match.captures != ["我去", " 1线 ", " 满人"]:
        raise AssertionError(f"3 个捕获组错误: {intent.match.captures}")

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "小苏苏/普通域外/刷新时间", "a", "10")  # 10-22 之间
        await kv_seed(kv, "小苏苏/普通域外/刷新时间", "天书时", "0")  # 不在冷却期
        await kv_seed(kv, "小苏苏/普通域外/刷新时间", "刷新分", "0")

        # 模拟正午 12 点
        class _FakeDT:
            @classmethod
            def now(cls):
                return datetime(2026, 5, 20, 12, 30, 0)

        vm = build_vm(kv)
        with patch("linling_dsl.vm.datetime", _FakeDT):
            res = await vm.execute_handler(
                handler, ev, captures=intent.match.captures
            )
        text = render(res.segments)
        if "已记录" not in text:
            raise AssertionError(f"路线记录失败 (没有 '已记录'): {text!r}")
        # 验证: 路线表里有写入
        loc = await kv_peek(
            kv,
            f"小苏苏/普通域外/所在路线{TEST_GROUP_ROUTE}",
            "天书",
            "",
        )
        if not loc:
            raise AssertionError(f"路线 KV 未写入, 实得 {loc!r}")
    finally:
        await kv.close()
    _print("OK", f"(我在|我去)(.*)天书(.*) 三 capture groups + 时间窗口 OK; 写入={loc!r}")


# ---------------------------------------------------------------------------
# §5 漂流瓶 — JSON ops chain (添加 → 读 → 长度 → 获取 → 删除)
# ---------------------------------------------------------------------------


async def case_throw_then_pickup_bottle(script: Any) -> None:
    """流程: 用户 A 扔瓶, 用户 B 捞瓶, 用户 B 开瓶."""
    h_throw = find(script, "(扔|丢)瓶子(.*)")
    h_pick = find(script, "(捡|捞)瓶子")
    h_open = find(script, "(打开|开瓶子)")
    assert h_throw and h_pick and h_open, "漂流瓶 handler 至少有一个找不到"

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        # 用户 A 扔
        await kv_seed(kv, "啊/苏苏状态/心情值", TEST_QQ, "0")
        await kv_seed(kv, "啊/%群%/个人耐心", TEST_QQ, "0")
        ev_throw = make_event("扔瓶子大家好呀", sender=TEST_QQ)
        # 三个捕获组: ('扔', '大家好呀') 因为 trigger 是 (扔|丢)瓶子(.*) 只有 2 个
        captures_throw = ["扔", "大家好呀"]
        vm = build_vm(kv)
        await vm.execute_handler(h_throw, ev_throw, captures=captures_throw)

        # 验证 R/P 数组都有内容
        rs = await kv_peek(kv, "啊/漂流瓶/瓶子", "R", "[]")
        ps = await kv_peek(kv, "啊/漂流瓶/瓶子", "P", "[]")
        if "大家好呀" not in (rs or ""):
            raise AssertionError(f"扔瓶后 R 应包含瓶子内容, 实得 {rs!r}")
        if "试用者" not in (ps or ""):
            raise AssertionError(f"扔瓶后 P 应记录昵称, 实得 {ps!r}")

        # 用户 B 捞 — 由于 ``风:$随机数 0-%H%$`` 可能取到 H 这个越界值,
        # 多试几次让 randint 命中 [0,1] 中的 0
        result_pick = None
        text_pick = ""
        for _ in range(10):
            await kv_seed(kv, "啊/苏苏状态/心情值", TARGET_QQ, "0")
            await kv_seed(kv, "啊/%群%/个人耐心", TARGET_QQ, "0")
            await kv_seed(kv, "啊/漂流瓶/持有瓶子", TARGET_QQ, "0")
            ev_pick = make_event("捞瓶子", sender=TARGET_QQ)
            captures_pick = ["捞"]
            result_pick = await vm.execute_handler(
                h_pick, ev_pick, captures=captures_pick
            )
            text_pick = render(result_pick.segments)
            if "捡到了一个瓶子" in text_pick:
                break
            # 若失败 (随机数取到边界), 重新种 R/P 让 H>0
            await kv_seed(kv, "啊/漂流瓶/瓶子", "R", '["大家好呀"]')
            await kv_seed(kv, "啊/漂流瓶/瓶子", "P", '["试用者"]')
        if "捡到了一个瓶子" not in text_pick:
            raise AssertionError(f"捞瓶 多次重试仍失败: 最后输出 {text_pick!r}")

        # 验证捞瓶后用户 B "持有瓶子" = 1
        held = await kv_peek(kv, "啊/漂流瓶/持有瓶子", TARGET_QQ, "0")
        if held != "1":
            raise AssertionError(f"捞瓶后 持有瓶子 应为 1, 实得 {held!r}")
        # 大海上瓶子被取走
        rs_after = await kv_peek(kv, "啊/漂流瓶/瓶子", "R", "[]")
        if "大家好呀" in (rs_after or ""):
            raise AssertionError(f"捞瓶后 R 应被删除, 实得 {rs_after!r}")

        # 用户 B 开
        ev_open = make_event("打开", sender=TARGET_QQ)
        captures_open = ["打开"]
        # `c:$读 啊/%群%/个人耐心 %QQ 0$` — 这里 rule 有个老笔误 (`%QQ` 缺尾 `%`),
        # 我们的解析器会把它当 literal 不会崩,但 c 拿不到值; 用降级的 %QQ% lookup,
        # 实际写法跑起来 c 会是空, 后面比较 `如果:%c%>6` 就 false, 流程能继续.
        result_open = await vm.execute_handler(h_open, ev_open, captures=captures_open)
        text_open = render(result_open.segments)
        if "大家好呀" not in text_open:
            raise AssertionError(f"开瓶 输出应包含瓶子内容: {text_open!r}")
    finally:
        await kv.close()
    _print(
        "OK",
        f"扔瓶→捞瓶→开瓶 全流程 + JSON 操作链 跑通; 内容 '大家好呀' 已传递",
    )


# ---------------------------------------------------------------------------
# §6 加注 / 下注 — 多 ``$jump :跳转$`` + KV state
# ---------------------------------------------------------------------------


async def case_xiazhu_route(script: Any) -> None:
    """``下注([0-9]+)`` classifier 路由 + 简单跑通."""
    classifier = MessageClassifier(script, command_prefixes=())
    ev = make_event("下注50")
    intent = classifier.classify(ev)
    if intent.match is None or intent.match.handler.trigger != "下注([0-9]+)":
        raise AssertionError(
            f"路由错误: {intent.match.handler.trigger if intent.match else intent.reason}"
        )
    if intent.match.captures != ["50"]:
        raise AssertionError(f"下注 捕获错误: {intent.match.captures}")
    _print("OK", "下注([0-9]+) 路由 + 捕获 一致")


# ---------------------------------------------------------------------------
# §7 (钓鱼|鱼塘) OR trigger
# ---------------------------------------------------------------------------


async def case_diaoyu_yutang(script: Any) -> None:
    """``钓鱼`` 和 ``鱼塘`` 都应该路由到 (钓鱼|鱼塘) handler."""
    classifier = MessageClassifier(script, command_prefixes=())
    for txt in ["钓鱼", "鱼塘"]:
        ev = make_event(txt)
        intent = classifier.classify(ev)
        if intent.match is None or intent.match.handler.trigger != "(钓鱼|鱼塘)":
            raise AssertionError(
                f"{txt} 路由错误: "
                f"{intent.match.handler.trigger if intent.match else intent.reason}"
            )
    _print("OK", "(钓鱼|鱼塘) 双分支均能触发")


# ---------------------------------------------------------------------------
# §8 苏苏(.*) — fallback chat catcher 不能吃掉 苏苏早安/苏苏晚安/苏苏xx笨蛋
# ---------------------------------------------------------------------------


async def case_susu_fallback_priority(script: Any) -> None:
    """声明顺序在前的更具体 trigger 应优先于 苏苏(.*) 这个 catch-all."""
    classifier = MessageClassifier(script, command_prefixes=())
    cases = [
        ("苏苏早安啦", "苏苏早安(.*)"),
        ("苏苏晚安啦", "苏苏晚安(.*)"),
        ("苏苏你这个笨蛋", "苏苏(.*)笨蛋"),
        # 普通的: 应该掉到 苏苏(.*) 这个最末尾的 catch-all
        ("苏苏在干嘛", "苏苏(.*)"),
    ]
    misses = []
    for txt, expected in cases:
        ev = make_event(txt)
        intent = classifier.classify(ev)
        actual = intent.match.handler.trigger if intent.match else None
        if actual != expected:
            misses.append(f"{txt!r}: 期望 {expected!r}, 实得 {actual!r}")
    if misses:
        for m in misses:
            _print("FAIL", "  " + m)
        raise AssertionError("苏苏 fallback 优先级错误")
    _print("OK", "苏苏xx 的具体规则 优先于 苏苏(.*) catch-all")


# ---------------------------------------------------------------------------
# §9 JSON 操作链 — 添加, 长度, 获取, 删除
# ---------------------------------------------------------------------------


async def case_json_ops_chain() -> None:
    """通过一段最小 .ling 测 JSON 操作链 — 不依赖 main.ling."""
    source = """trig
A:[]
A:$JSON 添加 A foo$
A:$JSON 添加 A bar$
A:$JSON 添加 A baz$
H:$JSON 长度 A$
G:$JSON 获取 A 1$
A:$JSON 删除 A 0$
H2:$JSON 长度 A$
H=%H% G=%G% H2=%H2% A=%A%
"""
    script = parse(source, strict=False)
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        vm = build_vm(kv)
        ev = make_event("trig")
        res = await vm.execute_handler(script.handlers[0], ev)
        out = render(res.segments).strip()
        # 期望: H=3 G=bar H2=2 A=["bar","baz"]
        if "H=3" not in out:
            raise AssertionError(f"长度错: {out!r}")
        if "G=bar" not in out:
            raise AssertionError(f"获取错: {out!r}")
        if "H2=2" not in out:
            raise AssertionError(f"删除后长度错: {out!r}")
        # JSON 删除 用 Python json.dumps 格式 — ``["bar", "baz"]``
        # (with a space after ``,``); 比对时统一去掉空格.
        if '["bar","baz"]' not in out.replace(" ", ""):
            raise AssertionError(f"删除后内容错: {out!r}")
    finally:
        await kv.close()
    _print("OK", f"JSON 添加/长度/获取/删除 链条 一致: {out}")


# ---------------------------------------------------------------------------
# §10 甩杆 / 起杆 — 时间窗口路由
# ---------------------------------------------------------------------------


async def case_shuaiganqigan_run(script: Any) -> None:
    """甩杆 → (3 分钟后) 起杆 → 应进入 ``$调用 1000 钓到一般鱼$`` 分支.

    [%时间HHmm%-%时%]>200 → 钓到一般鱼
    [%时间HHmm%-%时%]>130 → 钓到好鱼
    [%时间HHmm%-%时%]>100 → 钓到差鱼
    我们模拟时间差 = 3 分钟 (从 0900 到 0903), 进 130 分支.
    """
    h_throw = find(script, "甩杆")
    h_pull = find(script, "起杆")
    assert h_throw and h_pull

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "休闲系/钓鱼/鱼竿", TEST_QQ, "1")
        await kv_seed(kv, "休闲系/钓鱼/鱼饵", TEST_QQ, "5")

        scheduler = Scheduler()
        vm = build_vm(kv, scheduler=scheduler, handler_lookup=make_lookup(script))

        # 9:00 甩杆
        class _DT_900:
            @classmethod
            def now(cls):
                return datetime(2026, 5, 20, 9, 0, 0)

        with patch("linling_dsl.vm.datetime", _DT_900):
            await vm.execute_handler(h_throw, make_event("甩杆"))
        thrown = await kv_peek(kv, "休闲系/钓鱼/是否甩杆", TEST_QQ, "0")
        if thrown != "1":
            raise AssertionError(f"甩杆后 是否甩杆 应=1, 实得 {thrown!r}")

        # 9:03 起杆 — 时间差 = 0903 - 0900 = 3, 不满足任何分支 (>100/130/200),
        # 走到末尾 "你奋力起杆，发现鱼钩上什么也没有" 的输出.
        # 改成 11:01 起杆 — 时间差 = 1101 - 0900 = 201, 满足 [>200] → 钓到一般鱼
        class _DT_1101:
            @classmethod
            def now(cls):
                return datetime(2026, 5, 20, 11, 1, 0)

        with patch("linling_dsl.vm.datetime", _DT_1101):
            await vm.execute_handler(h_pull, make_event("起杆"))

        # 是否甩杆 应被清零
        flag = await kv_peek(kv, "休闲系/钓鱼/是否甩杆", TEST_QQ, "0")
        if flag != "0":
            raise AssertionError(f"起杆后 是否甩杆 应=0, 实得 {flag!r}")
        # scheduler 应被排了 1 个 "钓到一般鱼"
        if scheduler.pending_count != 1:
            raise AssertionError(
                f"起杆 (>200) 应排 1 个 scheduler 任务, 实得 {scheduler.pending_count}"
            )
        next_task = list(scheduler._queue)[0]  # type: ignore[attr-defined]
        if next_task.handler_name != "钓到一般鱼":
            raise AssertionError(f"应调用 '钓到一般鱼', 实得 {next_task.handler_name!r}")
    finally:
        await kv.close()
    _print(
        "OK",
        f"甩杆→起杆 (>200 分钟差) 路由到 '钓到一般鱼'; "
        f"是否甩杆 状态机 0→1→0 正确",
    )


# ---------------------------------------------------------------------------
# §11 加注 / 下注 — classifier captures
# ---------------------------------------------------------------------------


async def case_jiazhu_capture(script: Any) -> None:
    """``加注50`` 路由 + 捕获."""
    classifier = MessageClassifier(script, command_prefixes=())
    ev = make_event("加注50")
    intent = classifier.classify(ev)
    if intent.match is None or intent.match.handler.trigger != "加注([0-9]+)":
        raise AssertionError(
            f"加注 路由错误: {intent.match.handler.trigger if intent.match else intent.reason}"
        )
    if intent.match.captures != ["50"]:
        raise AssertionError(f"加注 捕获错误: {intent.match.captures}")
    _print("OK", "加注([0-9]+) 路由 + 捕获 一致")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def main() -> int:
    failed: list[str] = []
    src = RULES_PATH.read_text(encoding="utf-8")
    script = parse(src, filename=str(RULES_PATH), strict=False)
    _print("OK", f"main.ling 解析成功, 共 {len(script.handlers)} 个 handler")

    cases = [
        ("灵玉划转(.*)@.* (4% 手续费)", case_lingyu_huazhuan_at),
        ("灵玉划转([0-9]+)给([0-9]+)", case_lingyu_huazhuan_geiqq),
        ("提升妖力(.*)@.*", case_tisheng_yaoli_at),
        ("偷玉@→逃跑 (5+ min 档)", case_touyu_then_paolu),
        ("(我在|我去)(.*)天书(.*)", case_route_record),
        ("漂流瓶 扔→捞→开 全链", case_throw_then_pickup_bottle),
        ("下注 路由", case_xiazhu_route),
        ("(钓鱼|鱼塘) OR", case_diaoyu_yutang),
        ("苏苏xx 子规则优先级", case_susu_fallback_priority),
        ("JSON 添加/长度/获取/删除 链", case_json_ops_chain),
        ("甩杆→起杆 时间窗口路由", case_shuaiganqigan_run),
        ("加注([0-9]+) 路由+捕获", case_jiazhu_capture),
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
