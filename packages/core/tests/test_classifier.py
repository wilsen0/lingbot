"""Tests for :class:`MessageClassifier`.

These pin down the QRSpeed-compatible behaviours that would otherwise
be easy to break: bracket-literal triggers, synthetic-event ignore
fallback, command prefix handling, etc.
"""

from __future__ import annotations

from linling_core.classifier import MessageClassifier
from linling_core.events import Event, Scope, User
from linling_core.segments import AtSegment, TextSegment
from linling_dsl.parser import parse


def _ev(text: str, raw: dict[str, object] | None = None) -> Event:
    return Event(
        id="x",
        platform="test",
        bot_id="b",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        kind="message",
        segments=[TextSegment(text=text)],
        raw=raw or {},
    )


# ---------------------------------------------------------------------------
# Bracket-literal triggers (QRSpeed convention)
# ---------------------------------------------------------------------------


class TestBracketLiteralTriggers:
    """``[戳一戳]`` and friends are literal text, not regex character classes.

    The original implementation compiled them as regex and matched a
    single arbitrary char inside the brackets — so ``戳`` matched but
    ``[戳一戳]`` didn't. We now detect the QRSpeed convention and
    treat them as literals.
    """

    def test_bracket_trigger_matches_full_literal(self) -> None:
        script = parse("[戳一戳]\nok\n", strict=False)
        c = MessageClassifier(script=script)
        intent = c.classify(_ev("[戳一戳]"))
        assert intent.kind == "command"
        assert intent.match is not None
        assert intent.match.handler.trigger == "[戳一戳]"

    def test_bracket_trigger_does_not_match_inner_char(self) -> None:
        script = parse("[戳一戳]\nok\n", strict=False)
        c = MessageClassifier(script=script)
        # A single char from inside the brackets MUST NOT match.
        for char in ("戳", "一"):
            intent = c.classify(_ev(char))
            assert intent.kind == "chat", f"single char {char!r} wrongly matched"

    def test_system_bracket_trigger_matches(self) -> None:
        script = parse("[系统]\nwelcome\n", strict=False)
        c = MessageClassifier(script=script)
        intent = c.classify(_ev("[系统]"))
        assert intent.kind == "command"

    def test_internal_bracket_prefix_strips_correctly(self) -> None:
        """``[内部]接扔瓶子`` registers as internal handler with stripped trigger."""
        script = parse("[内部]接扔瓶子\nok\n", strict=False)
        # The ``[内部]`` prefix is stripped at parse time, leaving an
        # internal handler whose trigger is ``接扔瓶子``. Internal
        # handlers don't appear in the classifier's lookup at all.
        h = script.handlers[0]
        assert h.is_internal
        assert h.trigger == "接扔瓶子"
        c = MessageClassifier(script=script)
        # User typing the post-strip text doesn't trigger the internal handler.
        assert c.classify(_ev("接扔瓶子")).kind == "chat"
        assert c.classify(_ev("[内部]接扔瓶子")).kind == "chat"


# ---------------------------------------------------------------------------
# Synthetic-event ignore fallback
# ---------------------------------------------------------------------------


class TestSyntheticIgnore:
    """Adapter-synthesised events fall to ``ignore`` when no rule matches.

    OneBot translates notice / request payloads into synthetic message
    events with text like ``[系统]`` / ``[退群]`` so legacy DSL handlers
    can match them. If the rule set doesn't define such a handler we
    don't want the LLM to receive the literal bracket string.
    """

    def test_synthetic_ignored_without_handler(self) -> None:
        script = parse("打卡\nok\n", strict=False)  # no [系统] handler
        c = MessageClassifier(script=script)
        intent = c.classify(_ev("[系统]", raw={"_synthetic_qrspeed": True}))
        assert intent.kind == "ignore"
        assert intent.reason == "synthetic-no-handler"

    def test_real_message_with_bracket_text_still_chats(self) -> None:
        script = parse("打卡\nok\n", strict=False)
        c = MessageClassifier(script=script)
        # Without the synthetic marker, the literal text falls through
        # to chat (it's a regular user message).
        intent = c.classify(_ev("[系统]"))
        assert intent.kind == "chat"


# ---------------------------------------------------------------------------
# Command prefix handling
# ---------------------------------------------------------------------------


class TestCommandPrefix:
    def test_prefix_stripped_match(self) -> None:
        script = parse("我的灵玉\n0\n", strict=False)
        c = MessageClassifier(script=script)
        intent = c.classify(_ev("/我的灵玉"))
        assert intent.kind == "command"
        assert intent.reason.startswith("prefix:")

    def test_prefix_unknown_returns_command_match_none(self) -> None:
        script = parse("打卡\nok\n", strict=False)
        c = MessageClassifier(script=script)
        intent = c.classify(_ev("/totally-unknown"))
        # We classify it as command (so the operator gets a friendly
        # "unknown command" reply rather than an LLM round-trip).
        assert intent.kind == "command"
        assert intent.match is None

    def test_implicit_trigger_no_prefix(self) -> None:
        script = parse("打卡\nok\n", strict=False)
        c = MessageClassifier(script=script)
        intent = c.classify(_ev("打卡"))
        assert intent.kind == "command"
        assert intent.reason == "implicit-trigger"


# ---------------------------------------------------------------------------
# Self-loop / blocked / non-message guards
# ---------------------------------------------------------------------------


def test_self_message_ignored() -> None:
    script = parse("ok\nyo\n", strict=False)
    c = MessageClassifier(script=script)
    ev = Event(
        id="x",
        platform="test",
        bot_id="b",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="b", platform="test"),  # sender == bot
        kind="message",
        segments=[TextSegment(text="ok")],
    )
    assert c.classify(ev).kind == "ignore"


def test_non_message_event_ignored() -> None:
    script = parse("ok\nyo\n", strict=False)
    c = MessageClassifier(script=script)
    ev = Event(
        id="x",
        platform="test",
        bot_id="b",
        scope=Scope(kind="group", id="g", platform="test"),
        sender=User(id="u", platform="test"),
        kind="notice",  # not a message
        segments=[],
    )
    assert c.classify(ev).kind == "ignore"


def test_blocked_scope_ignored() -> None:
    script = parse("ok\nyo\n", strict=False)
    c = MessageClassifier(script=script, block_scope_ids=frozenset({"g"}))
    intent = c.classify(_ev("ok"))
    assert intent.kind == "ignore"
    assert intent.reason == "blocked-scope"


# ---------------------------------------------------------------------------
# AT-bearing triggers (QRDic gift / admin-style)
# ---------------------------------------------------------------------------


class TestAtSegmentTriggerMatching:
    """``赠送大飞龙@.*`` and friends require the @ to be present in the
    matched string.

    OneBot delivers ``赠送大飞龙@<target>`` as
    ``[TextSegment("赠送大飞龙"), AtSegment(user_id="…")]`` — the literal
    ``@`` is no longer in any text segment. Without re-projecting the
    AT user_id, the trigger ``赠送大飞龙@.*`` would never match and the
    gift handler would never fire (events would silently fall through
    to the chat agent).

    The classifier consults :attr:`Event.match_text`, which stitches
    ``@<user_id>`` back in for matching purposes only — :attr:`Event.text`
    (used by chat dispatch / ``%参数N%`` / audit / UI) is unaffected.
    """

    def _ev_with_at(self, head_text: str, at_user_id: str, *, tail_text: str = "") -> Event:
        segments: list[TextSegment | AtSegment] = [TextSegment(text=head_text)]
        segments.append(AtSegment(user_id=at_user_id))
        if tail_text:
            segments.append(TextSegment(text=tail_text))
        return Event(
            id="x",
            platform="onebot",
            bot_id="b",
            scope=Scope(kind="group", id="g", platform="onebot"),
            sender=User(id="u", platform="onebot"),
            kind="message",
            segments=segments,
        )

    def test_gift_trigger_matches_with_at_segment(self) -> None:
        """``赠送大飞龙@.*`` matches an AtSegment-bearing event."""
        script = parse("赠送大飞龙@.*\nok\n", strict=False)
        c = MessageClassifier(script=script)
        ev = self._ev_with_at("赠送大飞龙", "99999")
        intent = c.classify(ev)
        assert intent.kind == "command"
        assert intent.match is not None
        assert intent.match.handler.trigger == "赠送大飞龙@.*"

    def test_gift_trigger_misses_without_at_segment(self) -> None:
        """No AT segment → trigger doesn't match (handler stays defensive)."""
        script = parse("赠送大飞龙@.*\nok\n", strict=False)
        c = MessageClassifier(script=script)
        ev = Event(
            id="x",
            platform="onebot",
            bot_id="b",
            scope=Scope(kind="group", id="g", platform="onebot"),
            sender=User(id="u", platform="onebot"),
            kind="message",
            segments=[TextSegment(text="赠送大飞龙")],
        )
        intent = c.classify(ev)
        assert intent.kind == "chat"

    def test_bulk_gift_trigger_matches_and_captures_count(self) -> None:
        """``赠送大飞龙([0-9]+)@.*`` matches and captures the count."""
        script = parse(
            "赠送大飞龙@.*\nsingle\n\n赠送大飞龙([0-9]+)@.*\nbulk\n",
            strict=False,
        )
        c = MessageClassifier(script=script)
        ev = self._ev_with_at("赠送大飞龙3", "99999")
        intent = c.classify(ev)
        assert intent.kind == "command"
        assert intent.match is not None
        assert intent.match.handler.trigger == "赠送大飞龙([0-9]+)@.*"
        assert intent.match.captures == ["3"]

    def test_admin_trigger_with_at_then_space_then_number(self) -> None:
        """``苏苏加好感@[\\s\\S]* [0-9]+`` matches admin-style commands."""
        script = parse("苏苏加好感@[\\s\\S]* [0-9]+\nok\n", strict=False)
        c = MessageClassifier(script=script)
        ev = self._ev_with_at("苏苏加好感", "12345", tail_text=" 50")
        intent = c.classify(ev)
        assert intent.kind == "command"
        assert intent.match is not None
        assert intent.match.handler.trigger == "苏苏加好感@[\\s\\S]* [0-9]+"

    def test_event_text_still_excludes_at(self) -> None:
        """``Event.text`` is unchanged — AT user_ids do *not* leak there."""
        ev = self._ev_with_at("赠送大飞龙", "99999")
        # plain_text view (used by chat dispatch, %参数N%, audit, UI):
        assert ev.text == "赠送大飞龙"
        # match_text view (only used by the classifier):
        assert ev.match_text == "赠送大飞龙@99999"
