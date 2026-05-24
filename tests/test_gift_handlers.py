"""End-to-end smoke tests for the QRDic gift-handler family.

QRDic-era ``赠送大飞龙@<target>`` rules require the literal ``@`` to be
present in the matched string, but OneBot delivers the inbound message
as ``[TextSegment("赠送大飞龙"), AtSegment(user_id="…")]`` — the bare
``@`` is no longer in any text segment. Without :attr:`Event.match_text`
re-projecting the AT user id, the trigger ``赠送大飞龙@.*`` would never
match and the gift would silently fall through to the chat agent.

These tests exercise the full chain — OneBot codec → classifier →
DSL VM → KV mutations → output segments — so any regression in the
glue (segment join, classifier text source, VM ``%AT0%`` resolver,
arithmetic evaluator, KV path splitter) is caught.
"""

from __future__ import annotations

import asyncio  # noqa: F401  used implicitly via @pytest.mark.asyncio

import linling_core.tools_builtin  # noqa: F401 — register DSL built-ins
import linling_tools_stdlib  # noqa: F401 — register stdlib tools
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
# Inline rule snippet — verbatim from bot/rules/main.ling §赠送大飞龙
# ---------------------------------------------------------------------------


GIFT_RULES = """\
赠送大飞龙@.*
如果:%群号%==%主群%|%群号%==待更替群
返回
如果尾
如果:%AT0%==%QQ%
不能自己送给自己喔
返回
如果尾
如果:%AT0%==0
赠送失败了˃ʍ˂
返回
如果尾
忧:$读 休闲系/珍品/大飞龙 %QQ% 0$
如果:%忧%==0
你还没有〔大飞龙〕，去扭蛋碰碰运气吧!
返回
如果尾
玉:$读 啊/灵玉系/灵玉 %AT0% 0$
$写 啊/灵玉系/灵玉 %AT0% [%玉%+500]$
$写 休闲系/珍品/大飞龙 %QQ% [%忧%-1]$
护:$读 休闲系/珍品/个人守护 %AT0% 0$
如果:%护%!=郫忧&%护%!=呦呦&%护%!=思思&%护%!=哒咩
$写 休闲系/珍品/个人守护 %AT0% 大飞龙$
如果尾
$写 休闲系/珍品/个人守护天 %AT0% %时间MMdd%$
±img=@pic:大飞龙.jpg±
赠送成功！！\\n
灵玉*500\\n
大飞龙守护1h！

赠送大飞龙([0-9]+)@.*
如果:%群号%==%主群%|%群号%==待更替群
返回
如果尾
如果:%AT0%==%QQ%
不能自己送给自己喔
返回
如果尾
如果:%AT0%==0
赠送失败了˃ʍ˂
返回
如果尾
忧:$读 休闲系/珍品/大飞龙 %QQ% 0$
如果:%忧%==0
你还没有〔大飞龙〕，去扭蛋碰碰运气吧!
返回
如果尾
如果:%忧%<%括号1%
数量不足！
返回
如果尾
玉:$读 啊/灵玉系/灵玉 %AT0% 0$
加:[%括号1%*500]
$写 啊/灵玉系/灵玉 %AT0% [%玉%+%加%]$
$写 休闲系/珍品/大飞龙 %QQ% [%忧%-%括号1%]$
护:$读 休闲系/珍品/个人守护 %AT0% 0$
如果:%护%!=郫忧&%护%!=呦呦&%护%!=思思&%护%!=哒咩
$写 休闲系/珍品/个人守护 %AT0% 大飞龙$
如果尾
$写 休闲系/珍品/个人守护时 %AT0% %时间MMddHH%$
±img=@pic:大飞龙.jpg±
赠送成功！！\\n
灵玉*%加%\\n
大飞龙守护1h！
"""

MAIN_GROUP = "754800438"
TEST_GROUP = "999999"  # not the main group → guard passes
SENDER = "111111"
TARGET = "222222"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _onebot_event(text: str, at_user_id: str | None) -> Event:
    """Build the Event you'd see if NapCat / OneBot delivered ``<text>@<at>``.

    Mirrors the real wire shape: a ``text`` segment followed (when
    @-mentioned) by an ``at`` segment carrying the target's user id.
    """
    payload: list[dict[str, object]] = [{"type": "text", "data": {"text": text}}]
    if at_user_id is not None:
        payload.append({"type": "at", "data": {"qq": at_user_id}})
    segments = from_onebot_msg(payload)
    return Event(
        id="evt-gift",
        platform="onebot",
        bot_id="susu",
        scope=Scope(kind="group", id=TEST_GROUP, platform="onebot"),
        sender=User(id=SENDER, platform="onebot"),
        segments=segments,
    )


@pytest.fixture
def script():
    return parse(GIFT_RULES, strict=False)


@pytest.fixture
def classifier(script):
    return MessageClassifier(script, command_prefixes=())


@pytest.fixture
async def kv():
    store = SqliteKVStore(bot_id="susu_test", db_path=":memory:")
    try:
        yield store
    finally:
        await store.close()


def _vm(kv: SqliteKVStore) -> VM:
    return VM(
        tool_registry=registry,
        kv=kv,
        bot_id="susu_test",
        extras={"admin_users": ("9999",), "main_group": MAIN_GROUP},
    )


def _render(segments) -> str:
    return "".join(s.text for s in segments if isinstance(s, TextSegment))


# ---------------------------------------------------------------------------
# Classifier — the original bug
# ---------------------------------------------------------------------------


def test_classifier_routes_gift_at_trigger(script):
    """The trigger ``赠送大飞龙@.*`` must match an OneBot ``[Text, At]`` event.

    Regression: before :attr:`Event.match_text` was added, the literal
    ``@`` lived only on the ``AtSegment`` and never reached the
    classifier; the trigger missed and the event silently fell through
    to the chat agent.
    """
    classifier = MessageClassifier(script, command_prefixes=())
    ev = _onebot_event("赠送大飞龙", at_user_id=TARGET)
    intent = classifier.classify(ev)
    assert intent.kind == "command"
    assert intent.match is not None
    assert intent.match.handler.trigger == "赠送大飞龙@.*"
    assert intent.match.captures == []


def test_classifier_routes_bulk_gift_with_count(script):
    """The bulk variant ``赠送大飞龙([0-9]+)@.*`` captures the count."""
    classifier = MessageClassifier(script, command_prefixes=())
    ev = _onebot_event("赠送大飞龙3", at_user_id=TARGET)
    intent = classifier.classify(ev)
    assert intent.kind == "command"
    assert intent.match is not None
    assert intent.match.handler.trigger == "赠送大飞龙([0-9]+)@.*"
    assert intent.match.captures == ["3"]


def test_classifier_misses_when_no_at_mention(script):
    """No ``@target`` → trigger should not fire (handler is defensive)."""
    classifier = MessageClassifier(script, command_prefixes=())
    ev = _onebot_event("赠送大飞龙", at_user_id=None)
    intent = classifier.classify(ev)
    assert intent.kind == "chat"  # falls through to chat agent


# ---------------------------------------------------------------------------
# Single-gift happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_gift_happy_path(script, classifier, kv):
    """``赠送大飞龙@target`` debits 1 大飞龙, credits 500 灵玉, sets 守护."""
    await kv.write("休闲系/珍品", "大飞龙", SENDER, "1")
    await kv.write("啊/灵玉系", "灵玉", TARGET, "200")

    ev = _onebot_event("赠送大飞龙", at_user_id=TARGET)
    intent = classifier.classify(ev)
    assert intent.match is not None
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )

    text = _render(result.segments)
    assert "赠送成功" in text
    assert "灵玉*500" in text
    assert "大飞龙守护1h" in text
    # An image segment for @pic:大飞龙.jpg should also be emitted.
    images = [s for s in result.segments if isinstance(s, ImageSegment)]
    assert len(images) == 1
    assert images[0].url == "@pic:大飞龙.jpg"

    # KV mutations
    assert await kv.read("休闲系/珍品", "大飞龙", SENDER) == "0"
    assert await kv.read("啊/灵玉系", "灵玉", TARGET) == "700"
    assert await kv.read("休闲系/珍品", "个人守护", TARGET) == "大飞龙"


@pytest.mark.asyncio
async def test_single_gift_main_group_silent_return(script, classifier, kv):
    """Sender in the main group: handler exits silently (no-op guard)."""
    await kv.write("休闲系/珍品", "大飞龙", SENDER, "1")
    ev = _onebot_event("赠送大飞龙", at_user_id=TARGET)
    # Override scope to the configured main group.
    ev = ev.model_copy(
        update={"scope": Scope(kind="group", id=MAIN_GROUP, platform="onebot")}
    )
    intent = classifier.classify(ev)
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    assert result.segments == []
    # KV must be untouched.
    assert await kv.read("休闲系/珍品", "大飞龙", SENDER) == "1"


@pytest.mark.asyncio
async def test_single_gift_to_self_rejected(script, classifier, kv):
    """``%AT0% == %QQ%`` → 不能自己送给自己喔; no KV mutation."""
    await kv.write("休闲系/珍品", "大飞龙", SENDER, "1")
    ev = _onebot_event("赠送大飞龙", at_user_id=SENDER)  # AT == sender
    intent = classifier.classify(ev)
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    assert _render(result.segments) == "不能自己送给自己喔"
    assert await kv.read("休闲系/珍品", "大飞龙", SENDER) == "1"


@pytest.mark.asyncio
async def test_single_gift_no_inventory(script, classifier, kv):
    """Sender doesn't own a 大飞龙 → friendly error; no KV mutation."""
    ev = _onebot_event("赠送大飞龙", at_user_id=TARGET)
    intent = classifier.classify(ev)
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    assert "你还没有〔大飞龙〕" in _render(result.segments)
    assert await kv.read("休闲系/珍品", "大飞龙", SENDER) is None
    assert await kv.read("啊/灵玉系", "灵玉", TARGET) is None


# ---------------------------------------------------------------------------
# Bulk-gift happy path + edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_gift_happy_path(script, classifier, kv):
    """``赠送大飞龙3@target`` debits 3, credits 1500 灵玉."""
    await kv.write("休闲系/珍品", "大飞龙", SENDER, "5")
    await kv.write("啊/灵玉系", "灵玉", TARGET, "100")

    ev = _onebot_event("赠送大飞龙3", at_user_id=TARGET)
    intent = classifier.classify(ev)
    assert intent.match is not None
    assert intent.match.captures == ["3"]
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    text = _render(result.segments)
    assert "赠送成功" in text
    assert "灵玉*1500" in text
    assert await kv.read("休闲系/珍品", "大飞龙", SENDER) == "2"
    assert await kv.read("啊/灵玉系", "灵玉", TARGET) == "1600"


@pytest.mark.asyncio
async def test_bulk_gift_insufficient_inventory(script, classifier, kv):
    """``%忧%<%括号1%`` → 数量不足; no KV mutation."""
    await kv.write("休闲系/珍品", "大飞龙", SENDER, "2")
    ev = _onebot_event("赠送大飞龙10", at_user_id=TARGET)
    intent = classifier.classify(ev)
    assert intent.match is not None
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    assert "数量不足" in _render(result.segments)
    # Inventory unchanged.
    assert await kv.read("休闲系/珍品", "大飞龙", SENDER) == "2"


# ---------------------------------------------------------------------------
# Existing 个人守护 should NOT be overwritten if it's one of the four reserved
# names (郫忧/呦呦/思思/哒咩)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_gift_preserves_reserved_guardian(script, classifier, kv):
    """If target already has a reserved guardian (郫忧/呦呦/思思/哒咩) the
    gift handler must leave 个人守护 untouched (only stamps the day)."""
    await kv.write("休闲系/珍品", "大飞龙", SENDER, "1")
    await kv.write("休闲系/珍品", "个人守护", TARGET, "郫忧")
    ev = _onebot_event("赠送大飞龙", at_user_id=TARGET)
    intent = classifier.classify(ev)
    result = await _vm(kv).execute_handler(
        intent.match.handler, ev, captures=intent.match.captures
    )
    assert "赠送成功" in _render(result.segments)
    # 个人守护 stays "郫忧"; only 个人守护天 should be written.
    assert await kv.read("休闲系/珍品", "个人守护", TARGET) == "郫忧"
    assert await kv.read("休闲系/珍品", "个人守护天", TARGET) is not None
