"""End-to-end tests for the player-booth (摊位) DSL rules.

The marketplace lives in two layers:

* The DSL rules in :file:`bot/rules/main.ling` parse the user's intent
  (上架 / 摊位 / 查挂单 / 买 / 撤单 / 我的摊位) and call the Python
  marketplace tools.
* The Python marketplace tools in :mod:`linling_tools_stdlib.trade_ops`
  do the actual KV writes under a SQLite transaction.

These tests exercise the *glue* — that the DSL rules actually wire up
to the Python tools with the right arguments and surface success / error
messages correctly. Atomicity / concurrency / cooldown / fees are pinned
in :file:`packages/tools-stdlib/tests/test_trade_ops.py`.

The relevant rule block is the one labelled ``摊位系统`` in
``main.ling`` (lines 9118-9199). We extract it verbatim here so this
file doesn't need to read the full 9k-line rule source.
"""

from __future__ import annotations

import asyncio  # noqa: F401  used implicitly via @pytest.mark.asyncio
import json

import linling_core.tools_builtin  # noqa: F401 — register DSL built-ins
import linling_tools_stdlib  # noqa: F401 — register stdlib tools (trade_ops)
import pytest
from linling_core.classifier import MessageClassifier
from linling_core.events import Event, Scope, User
from linling_core.onebot_codec import from_onebot_msg
from linling_core.segments import ImageSegment, TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry
from linling_dsl.parser import parse
from linling_dsl.vm import VM

# ---------------------------------------------------------------------------
# Inline rule snippet — verbatim from bot/rules/main.ling §摊位系统
# ---------------------------------------------------------------------------


BOOTH_RULES = """\
摊位系统

(摊位|摆摊)@.*
如果:%AT0%==%QQ%
看自己的摊位发"我的摊位"哦
返回
如果尾
如果:%AT0%==%Robot%
苏苏的摊位平时不开张哦
返回
如果尾
L:$列出挂单 $ %AT0% $
名:$群昵称 %群% %AT0%$
图:$市场图卡 seller %AT0%$
±img=%图%±
═════ %名%的摊位 ═════\\n
%L%\\n
┈┈┈┈┈┈┈\\n
发「摊位帮助」看全部指令

(摊位|摆摊)(鱼竿|鱼饵|蛋壳)$
L:$列出挂单 %括号2% $ $
═════ 摊位地图 · %括号2% ═════\\n
%L%\\n
┈┈┈┈┈┈┈\\n
发「摊位帮助」看全部指令

(摊位|摆摊)$
L:$列出挂单 $ $ 1$
图:$市场图卡 $
±img=%图%±
═════ 摊位地图 ═════\\n
%L%\\n
┈┈┈┈┈┈┈\\n
发「摊位帮助」看全部指令

(查挂单|查单)\\s+([0-9A-Z]{4})
详:$格式化挂单 %括号2%$
正则:%详%==.+$
挂单#%括号2%详情\\n
%详%
如果尾
嗯…%括号2% 这单找不到了，可能撤了或卖完啦

(我的摊位|我的挂单)$
索:$读 啊/摊位/索引/卖家/卖家 %QQ% []$
如果:%索%==[]
你还没来摆过摊呢～
返回
如果尾
L:$列出挂单 $ %QQ% $
名:$群昵称 %群% %QQ%$
图:$市场图卡 seller %QQ%$
±img=%图%±
═════ %名%的摊位 ═════\\n
%L%\\n
┈┈┈┈┈┈┈\\n
发「摊位帮助」看全部指令


(上架|摆摊上架)(鱼竿|鱼饵|蛋壳)([0-9]+)个单价([0-9]+)
ID:$上架挂单 %括号2% %括号3% %括号4% 24$
正则:%ID%==error:.*
消:$剥离前缀 %ID% error$
上架没成：%消%
返回
如果尾
上架好啦～#%ID% 挂 %括号2%×%括号3% 单价 %括号4% 灵玉/个
消:%时间HH:mm%摊位 #%ID% 已上架 %括号2%×%括号3% 单价%括号4%灵玉
$写 啊/主页系/最新消息 %QQ% %消%$

(买|购买|下单|拍下)([0-9A-Z]{4})([0-9]+)个$
Q:$购买挂单 %括号2% %括号3%$
正则:%Q%==ok:.+
消:$剥离前缀 %Q% ok$
%消%
返回
如果尾
消:$剥离前缀 %Q% error$
购买没成：%消%

(撤单|取消挂单)\\s*#?([0-9A-Z]{4})
R:$撤单 %括号2%$
正则:%R%==error:.*
消:$剥离前缀 %R% error$
撤单没成：%消%
返回
如果尾
撤下来啦～


(摊位帮助|摊位指令|摊位介绍)$
苏苏帮你整理了一下摊位的指令～\\n
═════ 摊位 · 指令一览 ═════\\n
查看全部挂单：摊位\\n
按物品看：摊位鱼竿/鱼饵/蛋壳\\n
看某人摊位：摊位@某人\\n
看自己摊位：我的摊位\\n
挂单详情：查挂单 ABCD\\n
上架：上架鱼竿/鱼饵/蛋壳5个单价10\\n
购买：买ABCD1个\\n
撤单：撤单ABCD\\n
┈┈┈┈┈┈┈\\n
挂单号是 4 位字母数字（例 A1B2）
"""

TEST_GROUP = "999999"
SENDER = "111111"
TARGET = "222222"
BOT_ID = "test-bot"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _onebot_event(text: str, at_user_id: str | None = None) -> Event:
    """Build the Event an LLBot / OneBot adapter would deliver.

    Mirrors the real wire shape: text segment, then (when @-mentioned)
    an ``at`` segment carrying the target user id. The text contains
    the *literal* message body — match_text re-projects the @ later.
    """
    payload: list[dict[str, object]] = [{"type": "text", "data": {"text": text}}]
    if at_user_id is not None:
        payload.append({"type": "at", "data": {"qq": at_user_id}})
    return Event(
        id="evt-booth",
        platform="onebot",
        bot_id=BOT_ID,
        scope=Scope(kind="group", id=TEST_GROUP, platform="onebot"),
        sender=User(id=SENDER, platform="onebot"),
        segments=from_onebot_msg(payload),
    )


@pytest.fixture
def script():
    return parse(BOOTH_RULES, strict=False)


@pytest.fixture
def classifier(script):
    return MessageClassifier(script, command_prefixes=())


@pytest.fixture
async def kv():
    store = SqliteKVStore(bot_id="booth_test", db_path=":memory:")
    try:
        yield store
    finally:
        await store.close()


def _vm(kv: SqliteKVStore) -> VM:
    return VM(
        tool_registry=registry,
        kv=kv,
        bot_id="booth_test",
        extras={"admin_users": ("9999",)},
    )


def _render(segments) -> str:
    return "".join(s.text for s in segments if isinstance(s, TextSegment))


async def _seed_seller_with_rods(kv: SqliteKVStore, qty: int = 5) -> str:
    """Pre-seed a player with 鱼竿 inventory; return the QQ."""
    await kv.write("休闲系/钓鱼", "鱼竿", SENDER, str(qty))
    return SENDER


# ---------------------------------------------------------------------------
# 上架 — the original bug: @ID[0:5]==error: was dead code that
# swallowed every error. This pins the new regex-prefixed check.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_happy_path_emits_id_and_news(script, classifier, kv):
    """``上架鱼竿5个单价10`` returns a list-id and writes a news entry."""
    await _seed_seller_with_rods(kv, qty=5)
    ev = _onebot_event("上架鱼竿5个单价10")
    intent = classifier.classify(ev)
    assert intent.match is not None
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    text = _render(result.segments)
    assert "上架好啦～#" in text
    assert "鱼竿×5" in text
    assert "单价 10 灵玉/个" in text
    # 5 rods escrowed; seller keeps 0.
    assert await kv.read("休闲系/钓鱼", "鱼竿", SENDER) == "0"
    # News entry written.
    news = await kv.read("啊/主页系", "最新消息", SENDER, "")
    assert news and "摊位 #" in news


@pytest.mark.asyncio
async def test_listing_insufficient_inventory_surfaces_error(
    script, classifier, kv
):
    """Listing more than the seller owns must return the tool's error,
    not the success message.

    Regression: the original rule used ``@ID[0:5]==error:`` to detect
    failures, but @ID[0:5] always evaluates to ``""`` for a string,
    so the if-check was always False and the user silently saw the
    success template (with a literal ``%suc%`` prefix). Now the check
    is a regex match and the error message is propagated.
    """
    await _seed_seller_with_rods(kv, qty=1)
    ev = _onebot_event("上架鱼竿99个单价10")
    intent = classifier.classify(ev)
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    text = _render(result.segments)
    # The tool returns "error:你身上 鱼竿 不够啦" — this must be surfaced
    # verbatim rather than being swallowed by a dead if-check.
    assert text.startswith("上架没成") or "不够" in text, (
        f"expected inventory error, got: {text!r}"
    )
    assert "上架好啦" not in text, "success template leaked despite the error"
    # Inventory untouched.
    assert await kv.read("休闲系/钓鱼", "鱼竿", SENDER) == "1"


# ---------------------------------------------------------------------------
# 买 — original bug: @Q[0:2]==ok was also dead code; the rule
# always fell through to the final %Q% print so the error leaked
# either way. This pins that the success path no longer double-prints.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_happy_path_returns_tool_output(script, classifier, kv):
    """``买 <id>`` shows a friendly success message and returns early."""
    # Seed a seller who is NOT the message sender.
    seller = "55555"
    await kv.write("休闲系/钓鱼", "鱼竿", seller, "3")
    list_id = "A1B2"
    from linling_tools_stdlib.trade_ops import _SCOPE_LISTING

    await kv.write(
        _SCOPE_LISTING, "挂单", list_id,
        f'{{"seller":"{seller}","item":"鱼竿","total":3,"left":3,'
        f'"price_each":10,"status":"active","expire_at":9999999999}}',
    )

    # Pre-fund the buyer.
    await kv.write("啊/灵玉系", "灵玉", SENDER, "100")

    ev = _onebot_event(f"买{list_id}2个")
    intent = classifier.classify(ev)
    # Result-oriented: the user typed a valid buy, the router should
    # classify it as a command (not chat fallback) AND the captured
    # id/qty must round-trip into the tool call.
    assert intent.kind == "command", (
        f"expected command, got {intent.kind!r} ({intent.reason!r})"
    )
    assert intent.match is not None
    # Trigger is `(买|购买|下单|拍下)([0-9A-Z]{4})([0-9]+)个$` — three
    # groups, but only 括号2/3 are used in the handler body. Verify the
    # whole list so a future regex change can't silently shift indices.
    assert list(intent.match.captures) == ["买", list_id, "2"]
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    text = _render(result.segments)
    # The tool now returns a natural sentence like
    # "付了 11 灵玉,到手 10 灵玉的 鱼竿" — the DSL strips ``ok:`` and
    # the rule body passes the rest through as-is.
    assert "付了" in text and "灵玉" in text and "鱼竿" in text, (
        f"unexpected success msg: {text!r}"
    )
    # 2 rods moved buyer-ward.
    assert await kv.read("休闲系/钓鱼", "鱼竿", SENDER) == "2"
    # In the escrow model, the seller's inventory was already
    # debited at listing-creation time. The test seeds the listing
    # directly (bypassing marketplace_list), so the seller's row is
    # unchanged by the buy — only the listing's ``left`` field is
    # decremented. The credit/escrow math is exercised in
    # test_trade_ops.py::test_buy_transfers_funds_and_goods.
    assert await kv.read("休闲系/钓鱼", "鱼竿", seller) == "3"
    listing_blob = await kv.read(_SCOPE_LISTING, "挂单", list_id, "")
    assert json.loads(listing_blob)["left"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "msg",
    [
        # Space between 买 and the id — must NOT match.
        "买 A1B2 2个",
        # Missing 个 — must NOT match.
        "买A1B22",
        # Trailing junk — must NOT match.
        "买A1B22个谢谢",
    ],
)
async def test_buy_compact_only(script, classifier, kv, msg):
    """The 买 rule is intentionally rigid: 买[ID][数字]个 only.

    Mirrors the上架 rule's compact form (``上架鱼竿5个单价10``). Anything
    looser must fall through to :class:`Intent.kind="chat"` — the router's
    fallback for "no rule matches, let the LLM handle it." We assert on
    the *observed* intent (not on which rule did or didn't match) so the
    test stays honest if a future rule accidentally starts gobbling
    these inputs.
    """
    ev = _onebot_event(msg)
    intent = classifier.classify(ev)
    assert intent.kind == "chat", (
        f"expected chat fallback for {msg!r}, got kind={intent.kind!r} "
        f"reason={intent.reason!r} match_trigger="
        f"{intent.match.handler.trigger if intent.match else None!r}"
    )
    assert intent.match is None, (
        f"no rule should match {msg!r} but matched: {intent.match.handler.trigger}"
    )


# ---------------------------------------------------------------------------
# 摊位帮助 — the help handler is the single source of truth for booth
# command syntax, and the three page-footers point at it. Pin both: the
# help text contains the *current* syntax, and the footers no longer
# carry the old `#<挂单号> 买<数量>` placeholder.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("msg", ["摊位帮助", "摊位指令", "摊位介绍"])
async def test_booth_help_renders_current_syntax(script, classifier, kv, msg):
    """Any of the three help triggers should emit the help block."""
    ev = _onebot_event(msg)
    intent = classifier.classify(ev)
    assert intent.kind == "command", (
        f"{msg!r}: expected command, got {intent.kind!r} ({intent.reason!r})"
    )
    assert intent.match is not None
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    text = _render(result.segments)
    # The eight booth commands must each appear in their *current* form.
    for needle in (
        "摊位",          # 全摊
        "摊位鱼竿",      # 按物品
        "摊位@某人",     # @某人
        "我的摊位",      # 自己
        "查挂单",        # 详情
        "上架",          # 上架
        "买ABCD1个",     # 买
        "撤单ABCD",      # 撤单
        "4 位字母数字",  # ID 长度说明
    ):
        assert needle in text, f"{msg!r} output missing {needle!r}: {text!r}"
    # The old placeholder must NOT leak through.
    assert "买<数量>" not in text, f"stale placeholder in {msg!r}: {text!r}"
    # The help page opens with a one-line 苏苏 intro — pin it so
    # the persona stays present at the help entry point.
    assert "苏苏" in text, f"{msg!r} should open with a 苏苏 line: {text!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "msg",
    [
        # (摊位)$ — main booth list
        "摊位",
        # (摊位鱼竿|鱼饵|蛋壳)$ — category
        "摊位鱼竿",
        # (我的摊位)$ — own stall
        "我的摊位",
    ],
)
async def test_booth_page_footers_point_at_help(script, classifier, kv, msg):
    """The three list pages should NOT carry stale placeholder syntax.

    The footers used to show ``#<挂单号> 买<数量>`` — the 2026 compact
    ID format replaced that, and the help handler is now the canonical
    reference. We assert on the *absence* of the old placeholder so a
    future helper who restores it for "convenience" trips this test.

    To exercise the page footer, the sender must have at least one
    active listing; otherwise the page short-circuits to a friendly
    empty message (verified separately in
    ``test_my_stall_with_no_listings_says_so``).
    """
    # Seed at least one listing so the "has data" branch is hit.
    from linling_core.tools import ToolCtx
    from linling_tools_stdlib import trade_ops

    await _seed_seller_with_rods(kv, qty=5)
    seller_ctx = ToolCtx(kv=kv, event=_event_for(SENDER), bot_id="booth_test")
    await trade_ops.marketplace_list(
        seller_ctx, item="鱼竿", qty="2", price_each="10"
    )

    ev = _onebot_event(msg)
    intent = classifier.classify(ev)
    assert intent.match is not None, f"{msg!r} should match a booth page"
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    text = _render(result.segments)
    # The help pointer must be there …
    assert "摊位帮助" in text, f"{msg!r} footer should point at 摊位帮助: {text!r}"
    # … and the stale placeholder must not.
    assert "买<数量>" not in text, (
        f"{msg!r} footer still shows stale '买<数量>' placeholder: {text!r}"
    )


# ---------------------------------------------------------------------------
# 摊位@某人 — the rule must NOT trigger if the user is asking about
# their own stall (use 我的摊位 for that), and the @-target must be
# resolved via %AT0%, not matched as plain text.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_booth_at_other_user_renders_seller_card(script, classifier, kv):
    """``摊位@<target>`` lists that user's stall, prefixed with their name."""
    # No listings for the target yet.
    ev = _onebot_event("摊位", at_user_id=TARGET)
    intent = classifier.classify(ev)
    assert intent.match is not None
    assert intent.match.handler.trigger == "(摊位|摆摊)@.*"
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    text = _render(result.segments)
    assert "摊位" in text
    # The seller-card image is emitted even for the empty case
    # (the tool returns a 「空」placeholder).
    images = [s for s in result.segments if isinstance(s, ImageSegment)]
    assert len(images) == 1
    assert images[0].url.startswith("base64://")


@pytest.mark.asyncio
async def test_booth_at_self_redirects_to_my_stall(script, classifier, kv):
    """``摊位@self`` should tell the user to use 我的摊位 instead."""
    ev = _onebot_event("摊位", at_user_id=SENDER)  # AT == sender
    intent = classifier.classify(ev)
    assert intent.match is not None
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    assert "我的摊位" in _render(result.segments)


# ---------------------------------------------------------------------------
# 我的摊位 — the original rule only echoed the raw list-id index,
# not the formatted listings. The new rule delegates to the tool
# so users see the same shape as 摊位@self.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_my_stall_with_no_listings_says_so(script, classifier, kv):
    """Brand-new user with no seller index gets the friendly empty msg.

    The rule reads ``啊/摊位/索引/卖家/卖家`` (the trade_ops
    convention: scope = the path up to the last ``/``,
    file = the last segment, so the read path is scope + ``/`` + file).
    With an empty index the rule prints the self-oriented hint
    instead of an empty stall block.
    """
    ev = _onebot_event("我的摊位")
    intent = classifier.classify(ev)
    assert intent.match is not None
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    assert _render(result.segments) == "你还没来摆过摊呢～"


@pytest.mark.asyncio
async def test_my_stall_with_listings_renders_card(script, classifier, kv):
    """A user with at least one listing gets the formatted card view."""
    await _seed_seller_with_rods(kv, qty=3)
    # Create a listing through the tool so the indexes are populated.
    from linling_core.tools import ToolCtx
    from linling_tools_stdlib import trade_ops

    # bot_id must match the kv fixture's bot_id, otherwise the
    # marketplace rows and the DSL reads live in separate tenants.
    seller_ctx = ToolCtx(kv=kv, event=_event_for(SENDER), bot_id="booth_test")
    await trade_ops.marketplace_list(
        seller_ctx, item="鱼竿", qty="2", price_each="10"
    )
    ev = _onebot_event("我的摊位")
    intent = classifier.classify(ev)
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    text = _render(result.segments)
    assert "鱼竿" in text
    # 1 listing row, 2/2 left/total, 10 灵玉/个 — the tool's line format.
    assert "鱼竿 2/2" in text
    images = [s for s in result.segments if isinstance(s, ImageSegment)]
    assert len(images) == 1


# ---------------------------------------------------------------------------
# 查挂单 — the rule must return either the formatted detail or a
# not-found message, never the literal @ID string from the old
# if-block (that was a different bug: the if-body was dead so the
# success branch never executed, leaving the user with a stringified
# raw ID).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_existing_listing_returns_formatted(script, classifier, kv):
    """``查挂单 <id>`` for an existing listing shows the formatted summary.

    The rule now pipes the raw listing blob through ``$格式化挂单$``
    so the user sees a readable multi-line Chinese summary instead
    of the raw JSON. The summary must include the item name and
    the per-unit price; the listing id appears in the title line.
    """
    from linling_tools_stdlib.trade_ops import _SCOPE_LISTING

    list_id = "C3D4"
    await kv.write(
        _SCOPE_LISTING, "挂单", list_id,
        '{"seller":"x","item":"鱼竿","total":1,"left":1,'
        '"price_each":10,"status":"active","expire_at":9999999999}',
    )
    ev = _onebot_event(f"查挂单 {list_id}")
    intent = classifier.classify(ev)
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    text = _render(result.segments)
    # Title carries the listing id; body is the formatted summary.
    assert "挂单#" in text
    assert list_id in text
    assert "鱼竿" in text
    assert "10 灵玉/个" in text
    # Raw JSON should NOT leak through to the user.
    assert '"seller"' not in text
    assert '"left"' not in text


@pytest.mark.asyncio
async def test_inspect_unknown_listing_says_not_found(script, classifier, kv):
    """``查挂单 <bogus>`` returns the not-found message, not the body."""
    ev = _onebot_event("查挂单 ZZZZ")
    intent = classifier.classify(ev)
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    text = _render(result.segments)
    assert "找不到了" in text, f"expected not-found msg, got: {text!r}"
    # The success-template line must NOT leak into the empty case.
    assert "详情" not in text


# ---------------------------------------------------------------------------
# (helpers below — kept here so the test file is self-contained)
# ---------------------------------------------------------------------------


def _event_for(sender_id: str) -> Event:
    return Event(
        id="evt-seed",
        platform="onebot",
        bot_id=BOT_ID,
        scope=Scope(kind="group", id=TEST_GROUP, platform="onebot"),
        sender=User(id=sender_id, platform="onebot"),
    )
