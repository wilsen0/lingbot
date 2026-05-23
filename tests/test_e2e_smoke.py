"""End-to-end smoke tests for the linling P0 milestone.

Proves the full pipeline: DSL source → parse → VM execute → output segments.
"""

from __future__ import annotations

import re

import linling_core.tools_builtin  # noqa: F401 — ensure built-in tools are registered
import pytest
from linling_core import Event, EventBus, Scope, SqliteKVStore, TextSegment, User, registry
from linling_dsl import VM, Script, VMResult, parse

# ---------------------------------------------------------------------------
# Helper: run a handler matching the event text
# ---------------------------------------------------------------------------


async def run_handler(script: Script, event: Event, kv: SqliteKVStore) -> VMResult | None:
    """Find and execute the first matching handler for an event."""
    text = event.text
    for handler in script.handlers:
        if handler.is_internal:
            continue
        pattern = f"^{handler.trigger}$"
        match = re.match(pattern, text)
        if match:
            captures = list(match.groups())
            vm = VM(tool_registry=registry, kv=kv)
            return await vm.execute_handler(handler, event, captures=captures)
    return None


def make_event(text: str, *, user_id: str = "12345", group_id: str = "67890") -> Event:
    """Create a simple group message event for testing."""
    return Event(
        id="test-1",
        platform="test",
        bot_id="linling",
        scope=Scope(kind="group", id=group_id, platform="test"),
        sender=User(id=user_id, platform="test", display_name="测试用户"),
        segments=[TextSegment(text=text)],
    )


# ---------------------------------------------------------------------------
# The .ling script used by tests 1 and 2
# ---------------------------------------------------------------------------

LING_SCRIPT = """\
打卡
玉:$读 啊/灵玉系/灵玉 %QQ% 0$
$写 啊/灵玉系/灵玉 %QQ% [%玉%+100]$
打卡成功！灵玉+100

我的灵玉
玉:$读 啊/灵玉系/灵玉 %QQ% 0$
你的灵玉：%玉%

背包
玉:$读 啊/灵玉系/灵玉 %QQ% 0$
如果:%玉%!=0
灵玉：%玉%
如果尾
"""


# ---------------------------------------------------------------------------
# Test 1: Three-rule flow (parse → VM → output)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_checkin_write_read_flow():
    """Three rules: 打卡 writes 灵玉+100, 我的灵玉 reads it, 背包 shows items."""
    script = parse(LING_SCRIPT)
    assert len(script.handlers) == 3

    async with SqliteKVStore(bot_id="linling", db_path=":memory:") as kv:
        # Step 1: User sends "打卡"
        event1 = make_event("打卡")
        result1 = await run_handler(script, event1, kv)
        assert result1 is not None
        assert result1.segments, "打卡 should produce output"
        output1 = "".join(seg.text for seg in result1.segments if isinstance(seg, TextSegment))
        assert "打卡成功" in output1
        assert "灵玉+100" in output1

        # Verify KV was written
        stored = await kv.read("啊/灵玉系", "灵玉", "12345")
        assert stored == "100"

        # Step 2: User sends "我的灵玉"
        event2 = make_event("我的灵玉")
        result2 = await run_handler(script, event2, kv)
        assert result2 is not None
        output2 = "".join(seg.text for seg in result2.segments if isinstance(seg, TextSegment))
        assert "100" in output2

        # Step 3: User sends "背包"
        event3 = make_event("背包")
        result3 = await run_handler(script, event3, kv)
        assert result3 is not None
        output3 = "".join(seg.text for seg in result3.segments if isinstance(seg, TextSegment))
        assert "灵玉" in output3
        assert "100" in output3


# ---------------------------------------------------------------------------
# Test 2: OneBot mock — same three rules via simulated OneBot message
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_onebot_mock_message_flow():
    """Simulate a OneBot message payload through the full pipeline."""
    from linling_adapter_onebot.adapter import OneBotAdapter

    script = parse(LING_SCRIPT)
    bus = EventBus()

    adapter = OneBotAdapter(bus, ws_url="ws://localhost:0", bot_id="linling")

    # Build a OneBot-style message payload
    onebot_payload = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 99001,
        "group_id": 67890,
        "user_id": 12345,
        "sender": {
            "user_id": 12345,
            "nickname": "测试用户",
            "role": "member",
        },
        "message": [{"type": "text", "data": {"text": "打卡"}}],
    }

    # Use the adapter's internal method to build an Event
    event = adapter._build_event_from_message(onebot_payload)
    assert event is not None
    assert event.text == "打卡"
    assert event.sender.id == "12345"
    assert event.scope.kind == "group"
    assert event.scope.id == "67890"

    # Execute through VM
    async with SqliteKVStore(bot_id="linling", db_path=":memory:") as kv:
        result = await run_handler(script, event, kv)
        assert result is not None
        output = "".join(seg.text for seg in result.segments if isinstance(seg, TextSegment))
        assert "打卡成功" in output

        # Verify KV write happened
        stored = await kv.read("啊/灵玉系", "灵玉", "12345")
        assert stored == "100"

        # Now simulate "我的灵玉" via OneBot
        onebot_payload2 = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 99002,
            "group_id": 67890,
            "user_id": 12345,
            "sender": {
                "user_id": 12345,
                "nickname": "测试用户",
                "role": "member",
            },
            "message": [{"type": "text", "data": {"text": "我的灵玉"}}],
        }
        event2 = adapter._build_event_from_message(onebot_payload2)
        assert event2 is not None
        result2 = await run_handler(script, event2, kv)
        assert result2 is not None
        output2 = "".join(seg.text for seg in result2.segments if isinstance(seg, TextSegment))
        assert "100" in output2


# ---------------------------------------------------------------------------
# Test 3: CLI adapter integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cli_adapter_integration():
    """Test that CliAdapter + EventBus + handler matching works end-to-end."""
    from linling_adapter_cli.adapter import CliAdapter

    script = parse(LING_SCRIPT)
    bus = EventBus()

    # Track results
    results: list[VMResult] = []

    async with SqliteKVStore(bot_id="linling", db_path=":memory:") as kv:

        async def handler_subscriber(event: Event) -> bool | None:
            """Subscribe to bus events, match and execute handlers."""
            result = await run_handler(script, event, kv)
            if result is not None:
                results.append(result)
            return None

        bus.subscribe(handler_subscriber, name="test-handler")

        # Create CLI adapter
        adapter = CliAdapter(bus, bot_id="linling", user_id="12345")

        # Simulate sending "打卡" by building an event directly
        # (We can't use adapter.run() as it reads stdin, so we use _build_event)
        adapter._counter += 1
        event = adapter._build_event("打卡")
        assert event.text == "打卡"
        assert event.sender.id == "12345"

        # Publish to bus (simulating what adapter.run() does)
        await bus.publish(event)

        # Verify handler was triggered
        assert len(results) == 1
        output = "".join(seg.text for seg in results[0].segments if isinstance(seg, TextSegment))
        assert "打卡成功" in output

        # Verify KV was written
        stored = await kv.read("啊/灵玉系", "灵玉", "12345")
        assert stored == "100"

        # Send "我的灵玉"
        adapter._counter += 1
        event2 = adapter._build_event("我的灵玉")
        await bus.publish(event2)

        assert len(results) == 2
        output2 = "".join(seg.text for seg in results[1].segments if isinstance(seg, TextSegment))
        assert "100" in output2
