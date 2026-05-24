"""Tests for the OneBot v11 adapter."""

from __future__ import annotations

from pathlib import Path

import pytest
from linling_adapter_onebot.adapter import OneBotAdapter
from linling_core.bus import EventBus
from linling_core.events import Action, Scope
from linling_core.segments import AtSegment, ImageSegment, PokeSegment, TextSegment


def _make_adapter(
    *,
    ws_url: str = "ws://127.0.0.1:8080",
    access_token: str = "",
    bot_id: str = "test_bot",
    asset_root: Path | None = None,
) -> OneBotAdapter:
    bus = EventBus()
    return OneBotAdapter(
        bus,
        ws_url=ws_url,
        access_token=access_token,
        bot_id=bot_id,
        asset_root=asset_root,
    )


class TestBuildEventFromMessage:
    """Test _build_event_from_message correctly translates OneBot payloads."""

    def test_group_message(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 12345,
            "group_id": 67890,
            "user_id": 11111,
            "sender": {"user_id": 11111, "nickname": "Alice", "role": "admin"},
            "message": [
                {"type": "text", "data": {"text": "hello "}},
                {"type": "at", "data": {"qq": "22222"}},
            ],
        }
        event = adapter._build_event_from_message(payload)
        assert event is not None
        assert event.platform == "onebot"
        assert event.bot_id == "test_bot"
        assert event.scope.kind == "group"
        assert event.scope.id == "67890"
        assert event.sender.id == "11111"
        assert event.sender.display_name == "Alice"
        assert event.sender.role == "admin"
        assert event.kind == "message"
        assert len(event.segments) == 2
        assert isinstance(event.segments[0], TextSegment)
        assert event.segments[0].text == "hello "
        assert isinstance(event.segments[1], AtSegment)
        assert event.segments[1].user_id == "22222"

    def test_private_message(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "message",
            "message_type": "private",
            "message_id": 99999,
            "user_id": 33333,
            "sender": {"user_id": 33333, "nickname": "Bob", "role": "member"},
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }
        event = adapter._build_event_from_message(payload)
        assert event is not None
        assert event.scope.kind == "dm"
        assert event.scope.id == "33333"
        assert event.sender.display_name == "Bob"

    def test_message_id_preserved(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "message",
            "message_type": "private",
            "message_id": 42,
            "user_id": 100,
            "sender": {"user_id": 100, "nickname": "X"},
            "message": [],
        }
        event = adapter._build_event_from_message(payload)
        assert event is not None
        assert event.id == "42"


class TestBuildActionPayload:
    """Test _build_action_payload correctly translates Actions to OneBot API params."""

    def test_reply_to_group(self) -> None:
        adapter = _make_adapter()
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="67890", platform="onebot"),
            segments=[TextSegment(text="world")],
        )
        payload = adapter._build_action_payload(action)
        assert payload["action"] == "send_msg"
        assert payload["params"]["message_type"] == "group"
        assert payload["params"]["group_id"] == 67890
        assert payload["params"]["message"] == [{"type": "text", "data": {"text": "world"}}]

    def test_send_to_private(self) -> None:
        adapter = _make_adapter()
        action = Action(
            kind="send",
            target=Scope(kind="dm", id="11111", platform="onebot"),
            segments=[TextSegment(text="dm msg")],
        )
        payload = adapter._build_action_payload(action)
        assert payload["action"] == "send_msg"
        assert payload["params"]["message_type"] == "private"
        assert payload["params"]["user_id"] == 11111

    def test_recall_message(self) -> None:
        adapter = _make_adapter()
        action = Action(
            kind="recall",
            target=Scope(kind="group", id="67890", platform="onebot"),
            options={"message_id": 12345},
        )
        payload = adapter._build_action_payload(action)
        assert payload["action"] == "delete_msg"
        assert payload["params"]["message_id"] == 12345

    def test_mute_user(self) -> None:
        adapter = _make_adapter()
        action = Action(
            kind="mute",
            target=Scope(kind="group", id="67890", platform="onebot"),
            options={"user_id": "11111", "duration": 300},
        )
        payload = adapter._build_action_payload(action)
        assert payload["action"] == "set_group_ban"
        assert payload["params"]["group_id"] == 67890
        assert payload["params"]["user_id"] == 11111
        assert payload["params"]["duration"] == 300

    def test_kick_user(self) -> None:
        adapter = _make_adapter()
        action = Action(
            kind="kick",
            target=Scope(kind="group", id="67890", platform="onebot"),
            options={"user_id": "11111"},
        )
        payload = adapter._build_action_payload(action)
        assert payload["action"] == "set_group_kick"
        assert payload["params"]["user_id"] == 11111


class TestNoticeEvents:
    """Test notice event handling (poke)."""

    def test_poke_notice(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": 67890,
            "user_id": 11111,
            "target_id": 22222,
        }
        event = adapter._build_event_from_notice(payload)
        assert event is not None
        assert event.kind == "notice"
        assert event.scope.kind == "group"
        assert event.scope.id == "67890"
        assert event.sender.id == "11111"
        assert len(event.segments) == 1
        assert isinstance(event.segments[0], PokeSegment)
        assert event.segments[0].target_user_id == "22222"

    def test_poke_notice_private(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": 0,
            "user_id": 11111,
            "target_id": 22222,
        }
        event = adapter._build_event_from_notice(payload)
        assert event is not None
        # group_id is falsy (0), so scope should be dm
        assert event.scope.kind == "dm"

    def test_generic_notice(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 67890,
            "user_id": 11111,
        }
        event = adapter._build_event_from_notice(payload)
        assert event is not None
        assert event.kind == "notice"
        assert event.segments == []


class TestAccessToken:
    """Test that access_token is included in headers."""

    def test_no_token_empty_headers(self) -> None:
        adapter = _make_adapter(access_token="")
        headers = adapter._build_ws_headers()
        assert "Authorization" not in headers

    def test_token_in_headers(self) -> None:
        adapter = _make_adapter(access_token="my_secret_token")
        headers = adapter._build_ws_headers()
        assert headers["Authorization"] == "Bearer my_secret_token"


class TestPendingFutureLifecycle:
    """Pending OneBot API call_api futures must not leak across reconnects.

    Regression: previously a WebSocket disconnect would leave any
    in-flight ``call_api`` waiter blocked for the full 30s timeout.
    The fix wires ``_fail_pending`` into both the disconnect path and
    ``stop()`` so callers see a ``ConnectionError`` immediately.
    """

    def test_fail_pending_resolves_outstanding_waiters(self) -> None:
        import asyncio

        adapter = _make_adapter()

        async def _exercise() -> None:
            loop = asyncio.get_running_loop()
            f1: asyncio.Future[dict[str, object]] = loop.create_future()
            f2: asyncio.Future[dict[str, object]] = loop.create_future()
            adapter._pending["echo-1"] = f1
            adapter._pending["echo-2"] = f2

            adapter._fail_pending(ConnectionError("test disconnect"))

            assert "echo-1" not in adapter._pending
            assert "echo-2" not in adapter._pending
            for f in (f1, f2):
                assert f.done()
                exc = f.exception()
                assert isinstance(exc, ConnectionError)

        asyncio.run(_exercise())

    def test_fail_pending_skips_already_resolved_futures(self) -> None:
        import asyncio

        adapter = _make_adapter()

        async def _exercise() -> None:
            loop = asyncio.get_running_loop()
            done: asyncio.Future[dict[str, object]] = loop.create_future()
            done.set_result({"ok": True})
            adapter._pending["already-done"] = done

            # Must not raise InvalidStateError when failing pending.
            adapter._fail_pending(ConnectionError("test"))
            assert done.result() == {"ok": True}
            assert adapter._pending == {}

        asyncio.run(_exercise())


# ---------------------------------------------------------------------------
# QRSpeed compatibility — synthetic [系统] / [退群] / [上下管理] events
# ---------------------------------------------------------------------------


class TestQrspeedSyntheticEvents:
    """Notice / request → synthetic message-shape events with QRSpeed triggers.

    The adapter publishes the original Event(kind="notice"|"request")
    *and* a parallel Event(kind="message", text="[系统]") so legacy
    DSL handlers using QRSpeed's bracket-trigger convention fire.
    """

    def test_group_increase_emits_system_trigger(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "notice",
            "notice_type": "group_increase",
            "sub_type": "approve",
            "group_id": 67890,
            "user_id": 11111,
            "operator_id": 22222,
            "user_nickname": "Alice",
            "operator_nickname": "Inviter",
        }
        ev = adapter._build_qrspeed_synthetic(payload)
        assert ev is not None
        assert ev.kind == "message"
        # The synthesised text matches the QRSpeed [系统] trigger.
        assert ev.text == "[系统]"
        assert ev.scope.kind == "group"
        assert ev.scope.id == "67890"
        # %QQ% maps to event.sender.id → the affected user.
        assert ev.sender.id == "11111"
        # %Status% surfaces the QRSpeed-historic code (33 for invite-driven join).
        assert ev.raw["status"] == 33
        # %Code% maps to operator_id.
        assert ev.raw["operator_id"] == 22222
        # %UinName% / %Inviteename% map to user_name / operator_name.
        assert ev.raw["user_name"] == "Alice"
        assert ev.raw["operator_name"] == "Inviter"
        assert ev.raw["sub_type"] == "approve"

    def test_group_decrease_emits_leave_trigger(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "notice",
            "notice_type": "group_decrease",
            "sub_type": "leave",
            "group_id": 67890,
            "user_id": 11111,
        }
        ev = adapter._build_qrspeed_synthetic(payload)
        assert ev is not None
        assert ev.text == "[退群]"
        assert ev.scope.id == "67890"
        assert ev.sender.id == "11111"
        # status=1 for self-leave per QRSpeed table.
        assert ev.raw["status"] == 1

    def test_group_admin_set_emits_promote_trigger(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "notice",
            "notice_type": "group_admin",
            "sub_type": "set",
            "group_id": 67890,
            "user_id": 11111,
        }
        ev = adapter._build_qrspeed_synthetic(payload)
        assert ev is not None
        assert ev.text == "[上下管理]"
        assert ev.raw["status"] == 1  # 1 = set, 0 = unset

    def test_group_admin_unset_status_zero(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "notice",
            "notice_type": "group_admin",
            "sub_type": "unset",
            "group_id": 67890,
            "user_id": 11111,
        }
        ev = adapter._build_qrspeed_synthetic(payload)
        assert ev is not None
        assert ev.raw["status"] == 0

    def test_group_join_request_emits_system_trigger_with_status_87(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "group_id": 67890,
            "user_id": 11111,
            "comment": "let me in",
            "flag": "abc123",
        }
        ev = adapter._build_qrspeed_synthetic(payload)
        assert ev is not None
        assert ev.text == "[系统]"
        assert ev.raw["status"] == 87
        # Reqid carries the OneBot ``flag`` so $进群审核$ can resolve it.
        assert ev.raw["request_id"] == "abc123"
        assert ev.raw["sub_type"] == "add"

    def test_friend_request_emits_system_trigger_with_status_84(self) -> None:
        adapter = _make_adapter()
        payload = {
            "post_type": "request",
            "request_type": "friend",
            "user_id": 11111,
            "flag": "xyz",
        }
        ev = adapter._build_qrspeed_synthetic(payload)
        assert ev is not None
        assert ev.raw["status"] == 84

    def test_poke_notice_synthesises_zhuo_yi_zhuo_trigger(self) -> None:
        """Pokes synthesise both a structured PokeSegment notice (via the regular
        notice path) AND a ``[戳一戳]``-text message event so dicpro.txt's
        ``[戳一戳]`` handler can fire.

        Regression: pokes used to be excluded from synthesis on the theory
        that the structured PokeSegment notice was enough, but the classifier
        skips ``kind="notice"`` events and only routes ``message``-shaped
        ones — so the [戳一戳] handler never matched a real OneBot poke.
        """
        adapter = _make_adapter()
        payload = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": 67890,
            "user_id": 11111,
            "target_id": 22222,
        }
        ev = adapter._build_qrspeed_synthetic(payload)
        assert ev is not None
        assert ev.kind == "message"
        assert ev.segments[0].text == "[戳一戳]"
        assert ev.scope.id == "67890"
        # ``%QQ%`` resolves to event.sender.id — that's the poker
        # (``user_id``), matching QRSpeed semantics where the rule's
        # ``$写 ... 戳一戳冷却 %QQ% ...$`` keys off the poker id.
        assert ev.sender.id == "11111"
        # ``_synthetic_qrspeed`` marker so unmatched events fall
        # through to ``ignore`` instead of leaking to the chat agent.
        assert ev.raw["_synthetic_qrspeed"] is True
        # ``%Code%`` resolves from operator_id; OneBot poke notices
        # don't always carry one (and target_id is the carrier here),
        # so we just assert the field is present and stringy.
        assert "operator_id" in ev.raw


class TestQrspeedSyntheticIgnoredWhenNoHandler:
    """Synthetic ``[系统]`` events fall through to ``ignore`` when no rule matches.

    Without this guard, an OneBot group_increase / group_decrease
    notice would be translated into a synthetic message event, fail
    to find a matching DSL trigger, and then be routed to the chat
    agent as the literal string ``[系统]`` / ``[退群]``. That would
    burn LLM tokens on an event the operator clearly didn't intend
    to expose.
    """

    def test_synthetic_marker_is_set_on_raw(self) -> None:
        adapter = _make_adapter()
        ev = adapter._build_qrspeed_synthetic(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "approve",
                "group_id": 67890,
                "user_id": 11111,
            }
        )
        assert ev is not None
        assert ev.raw.get("_synthetic_qrspeed") is True

    def test_classifier_ignores_synthetic_without_handler(self) -> None:
        from linling_core.classifier import MessageClassifier
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment
        from linling_dsl.parser import parse

        # Rule file with no [系统] handler.
        script = parse("打卡\nok\n", strict=False)
        classifier = MessageClassifier(script=script)

        synth = Event(
            id="x",
            platform="onebot",
            bot_id="b",
            scope=Scope(kind="group", id="g", platform="onebot"),
            sender=User(id="u", platform="onebot"),
            kind="message",
            segments=[TextSegment(text="[系统]")],
            raw={"_synthetic_qrspeed": True},
        )
        intent = classifier.classify(synth)
        # Falls to ignore, NOT chat — even though [系统] would
        # otherwise look like a literal that should reach the LLM.
        assert intent.kind == "ignore"
        assert intent.reason == "synthetic-no-handler"

    def test_classifier_fires_handler_for_synthetic_when_defined(self) -> None:
        """If the rule set DOES define [系统], the synthetic event matches it."""
        from linling_core.classifier import MessageClassifier
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment
        from linling_dsl.parser import parse

        script = parse("[系统]\nwelcome\n", strict=False)
        classifier = MessageClassifier(script=script)

        synth = Event(
            id="x",
            platform="onebot",
            bot_id="b",
            scope=Scope(kind="group", id="g", platform="onebot"),
            sender=User(id="u", platform="onebot"),
            kind="message",
            segments=[TextSegment(text="[系统]")],
            raw={"_synthetic_qrspeed": True},
        )
        intent = classifier.classify(synth)
        assert intent.kind == "command"
        assert intent.match is not None

    def test_classifier_ignores_real_messages_with_bracket_text(self) -> None:
        """Without the synthetic marker, a literal '[系统]' text still falls to chat.

        That keeps regular user messages typing the bracket text
        flowing to the LLM (a user who genuinely types ``[系统]``
        gets a chat response, not a silent drop).
        """
        from linling_core.classifier import MessageClassifier
        from linling_core.events import Event, Scope, User
        from linling_core.segments import TextSegment
        from linling_dsl.parser import parse

        script = parse("打卡\nok\n", strict=False)
        classifier = MessageClassifier(script=script)

        regular = Event(
            id="x",
            platform="onebot",
            bot_id="b",
            scope=Scope(kind="group", id="g", platform="onebot"),
            sender=User(id="u", platform="onebot"),
            kind="message",
            segments=[TextSegment(text="[系统]")],
            raw={},  # no synthetic marker
        )
        intent = classifier.classify(regular)
        assert intent.kind == "chat"


# ---------------------------------------------------------------------------
# Non-blocking dispatch + connection lifecycle
# ---------------------------------------------------------------------------


class TestSpawnDispatchIsNonBlocking:
    """Inbound frames must not block the WS reader on downstream work.

    Regression: the reader used to ``await`` ``_dispatch`` inline,
    which meant a slow LLM round-trip (5–30s) stalled the read loop.
    websockets stops draining its receive queue, NapCat declares the
    link dead, and the adapter logs ``onebot_ws_disconnected`` every
    few minutes. The fix fans out via ``asyncio.create_task``; this
    test exercises that fan-out without involving real websockets.
    """

    def test_spawn_dispatch_returns_immediately(self) -> None:
        import asyncio

        adapter = _make_adapter()

        async def _exercise() -> None:
            slow_started = asyncio.Event()
            slow_release = asyncio.Event()

            async def slow_publish(_event: object) -> None:
                slow_started.set()
                await slow_release.wait()

            # Patch the bus so dispatch awaits an event we control.
            adapter._bus.publish = slow_publish  # type: ignore[method-assign]

            # Schedule three frames back-to-back. With the old code
            # we'd block on the first await; with the fix all three
            # land in _dispatch_tasks before any of them complete.
            for _ in range(3):
                adapter._spawn_dispatch(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "message_id": 1,
                        "user_id": 1,
                        "sender": {"user_id": 1, "nickname": "x"},
                        "message": [{"type": "text", "data": {"text": "hi"}}],
                    }
                )

            # Yield once so the tasks start running.
            await asyncio.sleep(0)
            await slow_started.wait()

            assert len(adapter._dispatch_tasks) == 3
            for t in adapter._dispatch_tasks:
                assert not t.done()

            # Release and drain.
            slow_release.set()
            await asyncio.gather(*adapter._dispatch_tasks, return_exceptions=True)
            assert adapter._dispatch_tasks == set()

        asyncio.run(_exercise())

    def test_dispatch_exception_is_logged_not_raised(self) -> None:
        """A crashing handler must not propagate out and kill the reader."""
        import asyncio

        adapter = _make_adapter()

        async def _exercise() -> None:
            async def boom(_event: object) -> None:
                raise RuntimeError("synthetic failure")

            adapter._bus.publish = boom  # type: ignore[method-assign]

            adapter._spawn_dispatch(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "message_id": 1,
                    "user_id": 1,
                    "sender": {"user_id": 1, "nickname": "x"},
                    "message": [{"type": "text", "data": {"text": "hi"}}],
                }
            )
            # Drain; the done-callback should swallow the exception.
            await asyncio.gather(*adapter._dispatch_tasks, return_exceptions=True)
            assert adapter._dispatch_tasks == set()

        asyncio.run(_exercise())

    def test_stop_cancels_dispatch_tasks(self) -> None:
        """``stop`` must drain in-flight dispatch tasks (no leaks at shutdown)."""
        import asyncio

        adapter = _make_adapter()

        async def _exercise() -> None:
            release = asyncio.Event()

            async def hang(_event: object) -> None:
                await release.wait()

            adapter._bus.publish = hang  # type: ignore[method-assign]
            adapter._spawn_dispatch(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "message_id": 1,
                    "user_id": 1,
                    "sender": {"user_id": 1, "nickname": "x"},
                    "message": [{"type": "text", "data": {"text": "hi"}}],
                }
            )
            await asyncio.sleep(0)
            assert len(adapter._dispatch_tasks) == 1
            await adapter.stop()
            # ``stop`` cancels the in-flight task and drains the set.
            assert adapter._dispatch_tasks == set()
            # Releasing afterwards must be a no-op.
            release.set()

        asyncio.run(_exercise())


class TestMetaEventHandling:
    """OneBot meta_event frames are recognised and don't crash dispatch."""

    def test_heartbeat_meta_event_does_not_publish(self) -> None:
        import asyncio

        adapter = _make_adapter()

        async def _exercise() -> None:
            published: list[object] = []

            async def capture(event: object) -> None:
                published.append(event)

            adapter._bus.publish = capture  # type: ignore[method-assign]
            await adapter._dispatch(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "interval": 30000,
                    "status": {"online": True, "good": True},
                }
            )
            # Heartbeats are pure observability — never published as events.
            assert published == []

        asyncio.run(_exercise())

    def test_lifecycle_meta_event_does_not_publish(self) -> None:
        import asyncio

        adapter = _make_adapter()

        async def _exercise() -> None:
            published: list[object] = []

            async def capture(event: object) -> None:
                published.append(event)

            adapter._bus.publish = capture  # type: ignore[method-assign]
            await adapter._dispatch(
                {"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect"}
            )
            assert published == []

        asyncio.run(_exercise())


class TestAssetResolution:
    """``@pic:`` and ``/storage/...`` URLs get rewritten to inline
    ``base64://...`` payloads before being sent to NapCat.

    Without this, NapCat would receive the literal shorthand and either
    bail out (image not found) or attempt an HTTP fetch on a non-URL.
    Inlining as base64 (rather than ``file://``) keeps images working
    when NapCat doesn't share the host filesystem — e.g. the supported
    Docker deployment where ``bot/assets`` isn't bind-mounted into the
    container.
    """

    def test_pic_shorthand_with_extension(self, tmp_path: Path) -> None:
        # Stand up a fake asset bundle on disk.
        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        target = asset_root / "picture" / "思思.jpg"
        target.write_bytes(b"\xff\xd8\xff\xe0fake")

        adapter = _make_adapter(asset_root=asset_root)
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[ImageSegment(url="@pic:思思.jpg")],
        )
        payload = adapter._build_action_payload(action)
        msg = payload["params"]["message"]
        assert len(msg) == 1
        assert msg[0]["type"] == "image"
        # base64:// inline — file bytes encoded into the URL.
        file_field = msg[0]["data"]["file"]
        assert file_field.startswith("base64://")
        import base64 as _b64

        decoded = _b64.b64decode(file_field[len("base64://") :])
        assert decoded == b"\xff\xd8\xff\xe0fake"

    def test_pic_shorthand_no_extension_defaults_to_jpg(self, tmp_path: Path) -> None:
        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        (asset_root / "picture" / "郫忧.jpg").write_bytes(b"\xff\xd8\xff\xe0pic")

        adapter = _make_adapter(asset_root=asset_root)
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[ImageSegment(url="@pic:郫忧")],
        )
        payload = adapter._build_action_payload(action)
        file_field = payload["params"]["message"][0]["data"]["file"]
        assert file_field.startswith("base64://")
        import base64 as _b64

        assert _b64.b64decode(file_field[len("base64://") :]) == b"\xff\xd8\xff\xe0pic"

    def test_pic_falls_back_to_svg_when_jpg_missing(self, tmp_path: Path) -> None:
        # The DSL still references ``@pic:道具宝箱.jpg`` from the legacy
        # migration but only the .svg replacement ships on disk; the
        # adapter promotes the extension transparently.
        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        (asset_root / "picture" / "道具宝箱.svg").write_bytes(b"<svg/>")

        adapter = _make_adapter(asset_root=asset_root)
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[ImageSegment(url="@pic:道具宝箱.jpg")],
        )
        payload = adapter._build_action_payload(action)
        file_field = payload["params"]["message"][0]["data"]["file"]
        assert file_field.startswith("base64://")
        import base64 as _b64

        # The .svg sibling was inlined, even though the request asked
        # for the .jpg name.
        assert _b64.b64decode(file_field[len("base64://") :]) == b"<svg/>"

    def test_legacy_storage_path_passes_through(self, tmp_path: Path) -> None:
        # The migrator removed all ``/storage/...`` references from the
        # ruleset; if a future rule re-introduces the shape we'd
        # rather treat it as opaque (NapCat will fail-loud) than try
        # to interpret it. Confirms the resolver doesn't accidentally
        # rewrite paths it no longer claims to handle.
        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        (asset_root / "picture" / "思思.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")

        adapter = _make_adapter(asset_root=asset_root)
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[ImageSegment(url="/storage/emulated/0/QR/QRDic/data/picture/思思.jpg")],
        )
        payload = adapter._build_action_payload(action)
        file_field = payload["params"]["message"][0]["data"]["file"]
        # Untouched — DSL is the source of truth, not the adapter.
        assert file_field.startswith("/storage/")

    def test_remote_url_unchanged(self, tmp_path: Path) -> None:
        # Real http(s) URLs flow through to NapCat verbatim — it can
        # fetch them itself and the resolver mustn't second-guess.
        adapter = _make_adapter(asset_root=tmp_path)
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[ImageSegment(url="https://example.com/img.png")],
        )
        payload = adapter._build_action_payload(action)
        file_field = payload["params"]["message"][0]["data"]["file"]
        assert file_field == "https://example.com/img.png"

    def test_no_asset_root_passes_url_through(self) -> None:
        adapter = _make_adapter()
        assert adapter._asset_root is None
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[ImageSegment(url="@pic:思思.jpg")],
        )
        payload = adapter._build_action_payload(action)
        # Without a configured root we can't resolve safely; emit the
        # shorthand verbatim so operators see the broken-image cause
        # rather than a path that points nowhere.
        file_field = payload["params"]["message"][0]["data"]["file"]
        assert file_field == "@pic:思思.jpg"

    def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        # Adversarial DSL emitting ``@pic:../something`` must not
        # escape the asset root. We pass it through unchanged so
        # NapCat ignores it rather than accidentally serving a file.
        asset_root = tmp_path / "assets"
        asset_root.mkdir()
        (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")

        adapter = _make_adapter(asset_root=asset_root)
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[ImageSegment(url="@pic:../secret.txt")],
        )
        payload = adapter._build_action_payload(action)
        file_field = payload["params"]["message"][0]["data"]["file"]
        # The invariant we care about: never *leak* the escaped target.
        # Either the resolver bails out (and emits the shorthand as-is,
        # which NapCat treats as a broken image — safe failure mode),
        # or it resolves to something inside the asset root. It must
        # never inline the contents of ``secret.txt`` as base64, nor
        # produce a ``file://`` URL pointing outside the root.
        if file_field.startswith("base64://"):
            import base64 as _b64

            decoded = _b64.b64decode(file_field[len("base64://") :])
            assert b"nope" not in decoded
        elif file_field.startswith("file://"):
            resolved = Path(file_field[len("file://") :]).resolve()
            assert asset_root.resolve() in resolved.parents
        else:
            # Unresolved shorthand fallback — this is the actual code
            # path today and is the safest outcome.
            assert file_field == "@pic:../secret.txt"

    def test_oversized_asset_falls_back_to_file_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Files above the inline cap stay on ``file://`` so we don't
        blow the WS frame budget.

        We monkeypatch the cap down to a tiny value so the test can
        exercise the branch with a small fixture file (the production
        default is 4 MiB and the bundled sprites are all <100 KiB).
        """
        from linling_adapter_onebot import adapter as adapter_module

        monkeypatch.setattr(adapter_module, "_ASSET_INLINE_MAX_BYTES", 4)

        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        (asset_root / "picture" / "huge.jpg").write_bytes(b"\x00" * 16)

        adapter = _make_adapter(asset_root=asset_root)
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[ImageSegment(url="@pic:huge.jpg")],
        )
        payload = adapter._build_action_payload(action)
        file_field = payload["params"]["message"][0]["data"]["file"]
        assert file_field.startswith("file://")
        assert file_field.endswith("/huge.jpg")

    def test_pic_inline_caches_repeat_lookups(self, tmp_path: Path) -> None:
        """Re-resolving the same asset hits the in-memory cache and
        avoids re-reading the file off disk.

        We verify the cache by patching ``Path.read_bytes`` after the
        first resolve: a second resolve that still returns the same
        bytes proves the bytes came from the cache, not the (now
        un-callable) disk path.
        """
        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        target = asset_root / "picture" / "思思.jpg"
        target.write_bytes(b"\xff\xd8\xff\xe0fake")

        adapter = _make_adapter(asset_root=asset_root)
        first = adapter._resolve_asset_url("@pic:思思.jpg")
        assert first.startswith("base64://")
        assert len(adapter._asset_b64_cache) == 1

        # Track read_bytes calls; a cache hit must skip it entirely.
        original_read = Path.read_bytes
        read_calls = 0

        def counting_read(self: Path) -> bytes:
            nonlocal read_calls
            read_calls += 1
            return original_read(self)

        Path.read_bytes = counting_read  # type: ignore[method-assign]
        try:
            second = adapter._resolve_asset_url("@pic:思思.jpg")
        finally:
            Path.read_bytes = original_read  # type: ignore[method-assign]

        assert second == first
        assert read_calls == 0  # cache hit, no disk read

    def test_pic_inline_invalidates_on_disk_edit(self, tmp_path: Path) -> None:
        """An edit on disk (mtime changes) busts the cache transparently."""
        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        target = asset_root / "picture" / "思思.jpg"
        target.write_bytes(b"\xff\xd8\xff\xe0v1")

        adapter = _make_adapter(asset_root=asset_root)
        first = adapter._resolve_asset_url("@pic:思思.jpg")
        import base64 as _b64

        assert _b64.b64decode(first[len("base64://") :]) == b"\xff\xd8\xff\xe0v1"

        # Bump mtime forward so the cache key changes (some
        # filesystems have second-granularity mtime, so do an explicit
        # ``os.utime`` rather than relying on a fast write to differ).
        import os
        import time

        target.write_bytes(b"\xff\xd8\xff\xe0v2")
        future = time.time() + 5.0
        os.utime(target, (future, future))

        second = adapter._resolve_asset_url("@pic:思思.jpg")
        assert _b64.b64decode(second[len("base64://") :]) == b"\xff\xd8\xff\xe0v2"

    def test_pic_inline_cache_evicts_oldest_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache size is bounded; FIFO eviction drops the oldest entry."""
        from linling_adapter_onebot import adapter as adapter_module

        # Shrink the cap so the test only needs three sprite files.
        monkeypatch.setattr(adapter_module, "_ASSET_CACHE_MAX_ENTRIES", 2)

        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        for n, data in (("a.jpg", b"AAA"), ("b.jpg", b"BBB"), ("c.jpg", b"CCC")):
            (asset_root / "picture" / n).write_bytes(data)

        adapter = _make_adapter(asset_root=asset_root)
        adapter._resolve_asset_url("@pic:a.jpg")
        adapter._resolve_asset_url("@pic:b.jpg")
        # Cache holds {a, b}.
        assert len(adapter._asset_b64_cache) == 2
        adapter._resolve_asset_url("@pic:c.jpg")
        # ``c`` insertion evicts ``a`` (oldest); cache now {b, c}.
        assert len(adapter._asset_b64_cache) == 2
        cached_paths = {key[0].rsplit("/", 1)[-1] for key in adapter._asset_b64_cache}
        assert cached_paths == {"b.jpg", "c.jpg"}

    def test_drift_bottle_remote_https_passes_through(self, tmp_path: Path) -> None:
        """The 漂流瓶 / 接扔瓶子 DSL stashes remote QQ-CDN URLs from
        ``%IMG0%`` and replays them. Those are remote ``https://`` URLs,
        not ``@pic:`` shorthands — the resolver must not touch them.
        Mixed outbound (text + remote image, which is what
        ``±img=@pic:捡到一个瓶子.svg±`` followed by a stashed remote
        image becomes once both segments are flushed) must keep the
        remote URL verbatim while inlining the local sprite.
        """
        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        (asset_root / "picture" / "捡到一个瓶子.svg").write_bytes(b"<svg/>")

        adapter = _make_adapter(asset_root=asset_root)
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[
                TextSegment(text="捡到了一个瓶子"),
                ImageSegment(url="@pic:捡到一个瓶子.svg"),
                ImageSegment(url="https://multimedia.nt.qq.com.cn/download?fileid=stub"),
            ],
        )
        msg = adapter._build_action_payload(action)["params"]["message"]
        assert [m["type"] for m in msg] == ["text", "image", "image"]
        # Local sprite is inlined.
        assert msg[1]["data"]["file"].startswith("base64://")
        # Remote stays verbatim — NapCat will fetch it.
        assert msg[2]["data"]["file"] == "https://multimedia.nt.qq.com.cn/download?fileid=stub"

    def test_voice_segment_url_is_not_rewritten(self, tmp_path: Path) -> None:
        """``VoiceSegment`` URLs flow through the codec unchanged.

        The resolver only knows about images. ``±ptt=`` rules using
        host-only ``file://`` paths or ``@pic:`` shorthands would
        still fail on the Docker NapCat deployment — but the
        existing ruleset doesn't actually use ``±ptt=`` (the only
        reference is dead code), and there's no historical
        ``@ptt:`` shorthand to handle. This test pins that boundary
        so a future change that does add audio asset support fails
        explicitly here, not silently in production.
        """
        from linling_core.segments import VoiceSegment

        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        adapter = _make_adapter(asset_root=asset_root)
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[VoiceSegment(url="@pic:speech.amr")],
        )
        msg = adapter._build_action_payload(action)["params"]["message"]
        assert msg[0]["type"] == "record"
        assert msg[0]["data"]["file"] == "@pic:speech.amr"

    def test_svg_request_returns_svg_as_is(self, tmp_path: Path) -> None:
        """``@pic:foo.svg`` resolves to the SVG file as-is.

        The adapter does not second-guess what the rule asked for.
        Whether QQ ultimately accepts an encoded SVG is a separate
        deployment concern: rules that target QQ should reference
        already-rasterised sprites (``.png`` / ``.gif``), produced by
        ``scripts/rasterize_assets.py``. The WebUI surface renders
        SVG natively, so rules that reference SVG still work there.
        """
        asset_root = tmp_path / "assets"
        (asset_root / "picture").mkdir(parents=True)
        (asset_root / "picture" / "lonely.svg").write_bytes(b"<svg/>")
        # A raster sibling exists, but the rule explicitly asked for
        # the SVG — we honour that and don't substitute.
        (asset_root / "picture" / "lonely.png").write_bytes(b"\x89PNGfake")

        adapter = _make_adapter(asset_root=asset_root)
        action = Action(
            kind="reply",
            target=Scope(kind="group", id="100", platform="onebot"),
            segments=[ImageSegment(url="@pic:lonely.svg")],
        )
        msg = adapter._build_action_payload(action)["params"]["message"]
        file_field = msg[0]["data"]["file"]
        assert file_field.startswith("base64://")
        import base64 as _b64

        assert _b64.b64decode(file_field[len("base64://") :]) == b"<svg/>"
