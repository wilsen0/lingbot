"""逐条验证复杂 DSL 指令是否能完整翻译并跑通.

聚焦目标:
1. 扭蛋 / 扭蛋十次 / 扭蛋五十次 等嵌套 ``$调用$`` 流程
2. 扭蛋口令 这种带 ``$jump$`` 的复杂控制流
3. ``苏苏(.*)`` 系列正则触发 + ``%括号1%`` 替换
4. ``(小苏苏|菜单)`` 等带 OR 选择的触发器
5. 苏苏早安 / 苏苏晚安 / 苏苏xx笨蛋 等带捕获组的触发器
6. ``[内部]扭蛋记录`` 这一行 ``±img=$图文 %录%$±%蛋%`` 行内 sigil 解析
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import linling_tools_stdlib  # noqa: F401  注册标准工具
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
from linling_dsl.parser import ParseError, parse
from linling_dsl.vm import VM


REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "bot" / "rules" / "main.ling"
ADMIN_QQ = "2078123478"
MAIN_GROUP = "754800438"
TEST_GROUP = "999999999"  # 非主群, 不会被前置守卫 ``%群号%==%主群%`` 拦掉
TEST_QQ = "111122223"


def _print(level: str, msg: str) -> None:
    sys.stdout.write(f"[{level}] {msg}\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 解析阶段
# ---------------------------------------------------------------------------


def parse_rules() -> Any:
    src = RULES_PATH.read_text(encoding="utf-8")
    try:
        script = parse(src, filename=str(RULES_PATH), strict=False)
    except ParseError as exc:
        _print("FAIL", f"parser strict=False 仍然报错: {exc}")
        raise
    _print("OK", f"main.ling 解析成功, 共 {len(script.handlers)} 个 handler")
    return script


def find_handler(script: Any, trigger: str) -> Any | None:
    for h in script.handlers:
        if h.trigger == trigger:
            return h
    return None


# ---------------------------------------------------------------------------
# 公用 fixture
# ---------------------------------------------------------------------------


def make_event(text: str, *, sender: str = TEST_QQ, group: str = TEST_GROUP) -> Event:
    return Event(
        id=f"e-{sender}-{text[:8]}",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id=group, platform="cli"),
        sender=User(id=sender, platform="cli", display_name="试用者"),
        segments=[TextSegment(text=text)],
    )


def make_event_with_at(text: str, at_user: str, *, sender: str = TEST_QQ) -> Event:
    return Event(
        id=f"e-{sender}-{text[:8]}-at",
        platform="cli",
        bot_id="susu_test",
        scope=Scope(kind="group", id=TEST_GROUP, platform="cli"),
        sender=User(id=sender, platform="cli", display_name="试用者"),
        segments=[TextSegment(text=text), AtSegment(user_id=at_user)],
    )


def build_vm(
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


def render_segments(segs: list[Any]) -> str:
    parts = []
    for s in segs:
        if isinstance(s, TextSegment):
            parts.append(s.text)
        else:
            parts.append(f"<{type(s).__name__}>")
    return "".join(parts)


def script_lookup(script: Any):
    def _l(name: str):
        for h in script.handlers:
            if h.trigger == name:
                return h
        return None

    return _l


def _split(path: str) -> tuple[str, str]:
    """Mirror linling_core.tools_builtin._split_path so tests prep KV correctly."""
    scope, sep, file = path.rpartition("/")
    if not sep:
        return path, ""
    return scope, file


async def kv_seed(kv: SqliteKVStore, dsl_path: str, key: str, value: str) -> None:
    """Write to KV using DSL ``$写 dsl_path key value$`` semantics."""
    scope, file = _split(dsl_path)
    await kv.write(scope, file, key, value)


async def kv_peek(
    kv: SqliteKVStore, dsl_path: str, key: str, default: str | None = None
) -> str | None:
    scope, file = _split(dsl_path)
    return await kv.read(scope, file, key, default)


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


async def case_classifier_routes(script: Any) -> None:
    """验证 classifier 能把目标命令路由到正确 handler."""
    classifier = MessageClassifier(script, command_prefixes=())

    cases = [
        ("扭蛋", "扭蛋"),
        ("扭蛋十次", "扭蛋十次"),
        ("扭蛋五十次", "扭蛋五十次"),
        ("扭蛋口令", "扭蛋口令"),
        ("小苏苏", "(小苏苏|菜单)"),
        ("菜单", "(小苏苏|菜单)"),
        ("苏苏好厉害", "苏苏(.*)"),
        ("苏苏早安今天天气真好", "苏苏早安(.*)"),
        ("苏苏晚安啦", "苏苏晚安(.*)"),
        ("苏苏笨笨笨蛋", "苏苏(.*)笨蛋"),
    ]
    misses: list[str] = []
    for text, expected in cases:
        ev = make_event(text)
        intent = classifier.classify(ev)
        m = intent.match
        if m is None or m.handler.trigger != expected:
            actual = m.handler.trigger if m else f"<no match: {intent.kind}/{intent.reason}>"
            misses.append(f"{text!r}: 期望 {expected!r}, 实得 {actual!r}")
    if misses:
        for m in misses:
            _print("FAIL", "  " + m)
        raise AssertionError("classifier 路由不一致")
    _print("OK", "classifier 把扭蛋/苏苏系命令路由到正确 handler")


async def case_run_single_gacha(script: Any) -> None:
    """跑一次 ``扭蛋`` (50 灵玉单抽), 验证扣玉与至少 1 个输出."""
    handler = find_handler(script, "扭蛋")
    assert handler is not None, "未找到扭蛋 handler"
    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "200")
        vm = build_vm(kv)
        ev = make_event("扭蛋")
        result = await vm.execute_handler(handler, ev, captures=[])
        text = render_segments(result.segments)
        balance = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        if balance != "150":
            raise AssertionError(f"灵玉余额预期 150, 实得 {balance!r}; 输出: {text!r}")
        if not result.segments:
            raise AssertionError(f"扭蛋无任何输出: 输出={result.segments!r}")
    finally:
        await kv.close()
    _print("OK", f"扭蛋单抽 跑通; 灵玉 200→150; 输出={text!r}")


async def case_run_gacha_ten_times(script: Any) -> None:
    """``扭蛋十次`` 在 VM 内调用 ``$调用 [200*i] 单十次扭蛋$`` 10 次, 然后 ``$调用 2300 十扭蛋记录$``.

    Note: parser strips the ``[内部]`` prefix from trigger names, 故在 lookup 时直接 ``单十次扭蛋``.
    """
    handler_ten = find_handler(script, "扭蛋十次")
    handler_unit = find_handler(script, "单十次扭蛋")
    handler_record = find_handler(script, "十扭蛋记录")
    if not (handler_ten and handler_unit and handler_record):
        raise AssertionError(
            f"扭蛋十次 / 单十次扭蛋 / 十扭蛋记录 至少有一个没找到; "
            f"扭蛋类相关触发器: {[h.trigger for h in script.handlers if '扭蛋' in h.trigger]}"
        )
    if not (handler_unit.is_internal and handler_record.is_internal):
        raise AssertionError(
            f"单十次扭蛋 / 十扭蛋记录 应是 internal handler, "
            f"实际 is_internal={handler_unit.is_internal}/{handler_record.is_internal}"
        )

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "1000")
        scheduler = Scheduler()
        lookup = script_lookup(script)
        vm = build_vm(kv, scheduler=scheduler, handler_lookup=lookup)
        ev = make_event("扭蛋十次")
        await vm.execute_handler(handler_ten, ev, captures=[])

        balance = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        if balance != "512":
            raise AssertionError(f"扭蛋十次后灵玉应是 1000-488=512, 实得 {balance!r}")
        if scheduler.pending_count != 11:
            raise AssertionError(
                f"扭蛋十次应排 11 个 scheduler 任务 (10 单抽+1 记录), "
                f"实得 {scheduler.pending_count}"
            )

        # 不依赖 scheduler 真 fire (那要 ~4 秒). 直接顺序调 10 次单抽 + 1 次记录,
        # 验证内部 handler 自身的扭蛋记录累加 / 灵玉/蛋壳/珍品逻辑.
        for _ in range(10):
            await vm.execute_handler(handler_unit, ev, captures=[])
        rec_result = await vm.execute_handler(handler_record, ev, captures=[])
        rec_text = render_segments(rec_result.segments)
        record = await kv_peek(kv, "休闲系/珍品/扭蛋记录", TEST_QQ, "")
        if "扭哇扭哇" not in (record or ""):
            raise AssertionError(
                f"扭蛋记录应被记录 handler 重置为含 '扭哇扭哇', 实得 {record!r}"
            )
        if not rec_text:
            raise AssertionError(
                f"十扭蛋记录 handler 应有输出, 实际 segments={rec_result.segments}"
            )
    finally:
        await kv.close()
    _print(
        "OK",
        f"扭蛋十次 全链跑通; pending=11; record 输出={rec_text.strip()!r}",
    )


async def case_run_gacha_fifty_times(script: Any) -> None:
    handler_50 = find_handler(script, "扭蛋五十次")
    handler_unit = find_handler(script, "单五十次扭蛋")
    handler_record = find_handler(script, "五十扭蛋记录")
    handler_tip = find_handler(script, "扭蛋提示")
    if not all([handler_50, handler_unit, handler_record, handler_tip]):
        raise AssertionError(
            f"扭蛋五十次 / 单五十次扭蛋 / 五十扭蛋记录 / 扭蛋提示 至少有一个没找到; "
            f"扭蛋类相关触发器: {[h.trigger for h in script.handlers if '扭蛋' in h.trigger]}"
        )

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/灵玉系/灵玉", TEST_QQ, "5000")
        # 让 ``T:$读 啊/禁言系/小小号 T []$`` 不命中 ``正则:%T%==.*%QQ%.*``,
        # 所以默认 [] 就不会匹配, 保持流转.
        scheduler = Scheduler()
        lookup = script_lookup(script)
        vm = build_vm(kv, scheduler=scheduler, handler_lookup=lookup)
        ev = make_event("扭蛋五十次")
        await vm.execute_handler(handler_50, ev, captures=[])

        balance = await kv_peek(kv, "啊/灵玉系/灵玉", TEST_QQ, "0")
        if balance != "2612":
            raise AssertionError(
                f"扭蛋五十次后灵玉应是 5000-2388=2612, 实得 {balance!r}"
            )
        # 52 = 50 单抽 + 1 提示 + 1 五十扭蛋记录
        if scheduler.pending_count != 52:
            raise AssertionError(
                f"扭蛋五十次应排 52 个 scheduler 任务 (50 单抽+1 提示+1 记录), "
                f"实得 {scheduler.pending_count}"
            )

        # 直接顺序跑 50 次单抽 + 1 次记录, 验证不爆错
        for _ in range(50):
            await vm.execute_handler(handler_unit, ev, captures=[])
        rec_result = await vm.execute_handler(handler_record, ev, captures=[])
        rec_text = render_segments(rec_result.segments)
        if not rec_text:
            raise AssertionError("五十扭蛋记录 handler 无输出")
    finally:
        await kv.close()
    _print(
        "OK",
        f"扭蛋五十次 全链跑通; pending=52; record 输出={rec_text.strip()!r}",
    )


async def case_run_kouling_jump(script: Any) -> None:
    """``扭蛋口令`` 用 ``$jump :重随机$`` 死循环抽 S/B/C 直到三者互不相等."""
    handler = find_handler(script, "扭蛋口令")
    assert handler is not None

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "休闲系/扭蛋/扭蛋指令", "五十", "GACHA50OK")
        await kv_seed(kv, "休闲系/扭蛋/扭蛋指令", "十", "GACHA10OK")
        await kv_seed(kv, "休闲系/扭蛋/扭蛋指令", "单", "GACHA1OK")
        vm = build_vm(kv)
        ev = make_event("扭蛋口令")
        result = await vm.execute_handler(handler, ev, captures=[])
        text = render_segments(result.segments)
        if not any(token in text for token in ("GACHA50OK", "GACHA10OK", "GACHA1OK")):
            raise AssertionError(f"扭蛋口令没有输出任何已收集的口令: {text!r}")
    finally:
        await kv.close()
    _print("OK", f"扭蛋口令 jump 收敛; 输出 {text.strip()!r}")


async def case_susu_capture_group(script: Any) -> None:
    """``苏苏(.*)`` 应该把后面的内容塞到 %括号1%."""
    classifier = MessageClassifier(script, command_prefixes=())
    ev = make_event("苏苏你今天好可爱")
    intent = classifier.classify(ev)
    m = intent.match
    if m is None or m.handler.trigger != "苏苏(.*)":
        raise AssertionError(
            f"苏苏(.*) 没匹配上, 实际命中: "
            f"{m.handler.trigger if m else (intent.kind, intent.reason)}"
        )
    if m.captures != ["你今天好可爱"]:
        raise AssertionError(f"苏苏(.*) 捕获错误: {m.captures}")
    _print("OK", f"苏苏(.*) 捕获组正确: {m.captures}")


async def case_susu_baidan_with_capture(script: Any) -> None:
    """``苏苏(.*)笨蛋`` 这种带模板的捕获组."""
    classifier = MessageClassifier(script, command_prefixes=())
    ev = make_event("苏苏你这个笨蛋")
    intent = classifier.classify(ev)
    m = intent.match
    if m is None or m.handler.trigger != "苏苏(.*)笨蛋":
        raise AssertionError(
            f"苏苏(.*)笨蛋 没匹配上: "
            f"{m.handler.trigger if m else (intent.kind, intent.reason)}"
        )
    if m.captures != ["你这个"]:
        raise AssertionError(f"苏苏(.*)笨蛋 捕获错误: {m.captures}")
    _print("OK", f"苏苏(.*)笨蛋 捕获组正确: {m.captures}")


async def case_xiaosusu_or_menu(script: Any) -> None:
    """``(小苏苏|菜单)`` 的 OR 触发."""
    classifier = MessageClassifier(script, command_prefixes=())
    for txt in ["小苏苏", "菜单"]:
        ev = make_event(txt)
        intent = classifier.classify(ev)
        m = intent.match
        if m is None or m.handler.trigger != "(小苏苏|菜单)":
            raise AssertionError(
                f"{txt} 没匹配 (小苏苏|菜单): "
                f"{m.handler.trigger if m else (intent.kind, intent.reason)}"
            )
    _print("OK", "(小苏苏|菜单) 双分支均能触发")


async def case_susu_zaoan_run(script: Any) -> None:
    """``苏苏早安(.*)`` 的实际执行 — 第一次心情值减 3 + 道安限制写入.

    handler 里 ``如果:%时间HH%>10|%时间HH%<5`` 会在非早 5-10 点直接早返回,
    所以 patch ``datetime.now()`` 到一个早 9 点的时间.
    """
    handler = find_handler(script, "苏苏早安(.*)")
    assert handler is not None

    kv = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        await kv_seed(kv, "啊/苏苏状态/心情值", TEST_QQ, "5")
        scheduler = Scheduler()
        vm = build_vm(kv, scheduler=scheduler, handler_lookup=script_lookup(script))
        ev = make_event("苏苏早安今天天气真好")
        intent = MessageClassifier(script, command_prefixes=()).classify(ev)
        assert intent.match is not None

        fake_now = datetime(2026, 5, 20, 9, 0, 0)

        class _FakeDT:
            @classmethod
            def now(cls):
                return fake_now

        with patch("linling_dsl.vm.datetime", _FakeDT):
            result = await vm.execute_handler(
                handler, ev, captures=intent.match.captures
            )
        text = render_segments(result.segments)
        if "早上好呀" not in text:
            raise AssertionError(f"苏苏早安 输出缺少 '早上好呀': {text!r}")
        new_mood = await kv_peek(kv, "啊/苏苏状态/心情值", TEST_QQ, "")
        if new_mood != "2":
            raise AssertionError(f"心情值应被减 3, 期望 2, 实得 {new_mood!r}")
    finally:
        await kv.close()
    _print(
        "OK",
        f"苏苏早安(.*) 跑通; 心情 5→{new_mood}; 输出={text.strip()!r}",
    )


async def case_record_inline_image(script: Any) -> None:
    """``[内部]十扭蛋记录`` 体内有一行 ``±img=$图文 %录%$±%蛋%`` — parser 把它整行当 OutputText."""
    handler = find_handler(script, "十扭蛋记录")
    if handler is None or not handler.is_internal:
        raise AssertionError(
            f"找不到 [内部]十扭蛋记录 (trigger=十扭蛋记录, is_internal=True); "
            f"实际 handler={handler!r}"
        )
    body_types = [type(s).__name__ for s in handler.body]
    if "OutputText" not in body_types:
        raise AssertionError(
            f"[内部]十扭蛋记录 应至少包含一条 OutputText "
            f"(从 ±img=$图文 %录%$±%蛋% 行), 实际 body 类型: {body_types}"
        )
    _print("OK", f"[内部]十扭蛋记录 解析包含: {body_types}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def main() -> int:
    failed: list[str] = []
    script = parse_rules()

    cases = [
        ("classifier 路由", case_classifier_routes),
        ("扭蛋 单抽", case_run_single_gacha),
        ("扭蛋十次 + scheduler", case_run_gacha_ten_times),
        ("扭蛋五十次", case_run_gacha_fifty_times),
        ("扭蛋口令 ($jump 收敛)", case_run_kouling_jump),
        ("苏苏(.*) 捕获", case_susu_capture_group),
        ("苏苏(.*)笨蛋 捕获", case_susu_baidan_with_capture),
        ("(小苏苏|菜单) 分支", case_xiaosusu_or_menu),
        ("苏苏早安(.*) 跑通", case_susu_zaoan_run),
        ("[内部]十扭蛋记录 行内 ±img±", case_record_inline_image),
    ]

    for name, fn in cases:
        try:
            await fn(script)
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
