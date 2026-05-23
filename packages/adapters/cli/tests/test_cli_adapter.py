"""Tests for the CLI adapter."""

from __future__ import annotations

import sys
from io import StringIO
from unittest.mock import patch

import pytest
from linling_adapter_cli.adapter import CliAdapter
from linling_core.bus import EventBus
from linling_core.events import Action, Scope
from linling_core.segments import AtSegment, ImageSegment, ReplySegment, TextSegment


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def adapter(bus: EventBus) -> CliAdapter:
    return CliAdapter(bus, bot_id="test-bot", user_id="tester")


class TestBuildEvent:
    """Test that text lines produce correct Events."""

    def test_plain_text_event(self, adapter: CliAdapter) -> None:
        adapter._counter = 1
        event = adapter._build_event("hello world")

        assert event.id == "cli-1"
        assert event.platform == "cli"
        assert event.bot_id == "test-bot"
        assert event.scope.kind == "group"
        assert event.scope.id == "cli-group"
        assert event.sender.id == "tester"
        assert event.sender.display_name == "you"
        assert len(event.segments) == 1
        assert isinstance(event.segments[0], TextSegment)
        assert event.segments[0].text == "hello world"

    def test_at_syntax_event(self, adapter: CliAdapter) -> None:
        adapter._counter = 2
        event = adapter._build_event("@alice hi there")

        assert event.id == "cli-2"
        assert event.sender.id == "alice"
        assert len(event.segments) == 2
        assert isinstance(event.segments[0], AtSegment)
        assert event.segments[0].user_id == "alice"
        assert isinstance(event.segments[1], TextSegment)
        assert event.segments[1].text == "hi there"

    def test_counter_increments(self, adapter: CliAdapter) -> None:
        adapter._counter = 5
        event = adapter._build_event("test")
        assert event.id == "cli-5"


class TestSend:
    """Test that send() formats segments correctly."""

    def test_text_segment(self, adapter: CliAdapter) -> None:
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="cli-group", platform="cli"),
            segments=[TextSegment(text="hello!")],
        )
        buf = StringIO()
        with patch.object(sys, "stdout", buf):
            adapter.send(action)
        assert buf.getvalue() == "🤖 hello!\n"

    def test_image_segment_url(self, adapter: CliAdapter) -> None:
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="cli-group", platform="cli"),
            segments=[ImageSegment(url="https://example.com/img.png")],
        )
        buf = StringIO()
        with patch.object(sys, "stdout", buf):
            adapter.send(action)
        assert buf.getvalue() == "🤖 [图片: https://example.com/img.png]\n"

    def test_image_segment_path(self, adapter: CliAdapter) -> None:
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="cli-group", platform="cli"),
            segments=[ImageSegment(path="/tmp/img.png")],
        )
        buf = StringIO()
        with patch.object(sys, "stdout", buf):
            adapter.send(action)
        assert buf.getvalue() == "🤖 [图片: /tmp/img.png]\n"

    def test_at_segment(self, adapter: CliAdapter) -> None:
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="cli-group", platform="cli"),
            segments=[AtSegment(user_id="bob"), TextSegment(text=" hey")],
        )
        buf = StringIO()
        with patch.object(sys, "stdout", buf):
            adapter.send(action)
        assert buf.getvalue() == "🤖 @bob hey\n"

    def test_other_segment(self, adapter: CliAdapter) -> None:
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="cli-group", platform="cli"),
            segments=[ReplySegment(message_id="msg-123")],
        )
        buf = StringIO()
        with patch.object(sys, "stdout", buf):
            adapter.send(action)
        assert buf.getvalue() == "🤖 [reply: ...]\n"

    def test_mixed_segments(self, adapter: CliAdapter) -> None:
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="cli-group", platform="cli"),
            segments=[
                TextSegment(text="Look: "),
                ImageSegment(url="https://img.io/x.jpg"),
                TextSegment(text=" nice!"),
            ],
        )
        buf = StringIO()
        with patch.object(sys, "stdout", buf):
            adapter.send(action)
        assert buf.getvalue() == "🤖 Look: [图片: https://img.io/x.jpg] nice!\n"


class TestModeSwitch:
    """Test /dm and /group mode switching."""

    def test_dm_switch(self, adapter: CliAdapter) -> None:
        # Simulate /dm command by directly testing scope change
        assert adapter._scope.kind == "group"
        assert adapter._scope.id == "cli-group"

        # After /dm alice, scope should switch
        adapter._scope = Scope(kind="dm", id="alice", platform="cli")
        event = adapter._build_event("private message")
        assert event.scope.kind == "dm"
        assert event.scope.id == "alice"

    def test_group_switch(self, adapter: CliAdapter) -> None:
        # Start in dm mode
        adapter._scope = Scope(kind="dm", id="alice", platform="cli")

        # Switch back to group
        adapter._scope = Scope(kind="group", id="my-group", platform="cli")
        event = adapter._build_event("group message")
        assert event.scope.kind == "group"
        assert event.scope.id == "my-group"

    @pytest.mark.asyncio
    async def test_dm_command_in_run(self, bus: EventBus) -> None:
        """Test that /dm command changes scope during run loop."""
        adapter = CliAdapter(bus, bot_id="test-bot", user_id="tester")
        published_events: list[object] = []

        async def capture(event: object) -> None:
            published_events.append(event)

        bus.subscribe(capture)  # type: ignore[arg-type]

        # Simulate stdin: /dm alice, then a message, then EOF
        inputs = iter(["/dm alice\n", "hello\n", ""])
        with (
            patch.object(sys.stdin, "readline", lambda: next(inputs)),
            patch.object(sys.stdout, "write"),
            patch.object(sys.stdout, "flush"),
        ):
            await adapter.run()

        assert len(published_events) == 1
        event = published_events[0]
        assert event.scope.kind == "dm"  # type: ignore[union-attr]
        assert event.scope.id == "alice"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_group_command_in_run(self, bus: EventBus) -> None:
        """Test that /group command changes scope during run loop."""
        adapter = CliAdapter(bus, bot_id="test-bot", user_id="tester")
        published_events: list[object] = []

        async def capture(event: object) -> None:
            published_events.append(event)

        bus.subscribe(capture)  # type: ignore[arg-type]

        # Switch to dm, then back to group, then send message
        inputs = iter(["/dm bob\n", "/group lobby\n", "hi\n", ""])
        with (
            patch.object(sys.stdin, "readline", lambda: next(inputs)),
            patch.object(sys.stdout, "write"),
            patch.object(sys.stdout, "flush"),
        ):
            await adapter.run()

        assert len(published_events) == 1
        event = published_events[0]
        assert event.scope.kind == "group"  # type: ignore[union-attr]
        assert event.scope.id == "lobby"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_eof_stops_loop(self, bus: EventBus) -> None:
        """Test that EOF (empty readline) stops the run loop."""
        adapter = CliAdapter(bus, bot_id="test-bot", user_id="tester")

        # Immediate EOF
        with (
            patch.object(sys.stdin, "readline", lambda: ""),
            patch.object(sys.stdout, "write"),
            patch.object(sys.stdout, "flush"),
        ):
            await adapter.run()
        # Should complete without hanging
