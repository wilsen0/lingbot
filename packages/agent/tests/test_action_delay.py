from __future__ import annotations

import pytest
from linling_agent.actions_protocol import ParsedAction
from linling_agent.dispatcher import _expand_actions_for_dm
from linling_agent.group_batch import GroupBatchChatDispatcher, GroupBatchConfig
from linling_core.events import (
    ACTION_DELAY_BEFORE_OPTION,
    Action,
    Event,
    Scope,
    User,
)
from linling_core.segments import TextSegment


def _event() -> Event:
    return Event(
        id="e1",
        platform="test",
        bot_id="bot1",
        scope=Scope(kind="dm", id="u1", platform="test"),
        sender=User(id="u1", platform="test"),
        segments=[TextSegment(text="hi")],
    )


def test_dm_multi_actions_mark_later_messages_with_delay() -> None:
    actions = _expand_actions_for_dm(
        [
            ParsedAction(kind="send", text="第一句"),
            ParsedAction(kind="send", text="第二句"),
            ParsedAction(kind="send", text="第三句"),
        ],
        event=_event(),
        max_actions=3,
        max_chars=500,
        delay_min_s=3,
        delay_max_s=3,
    )

    assert len(actions) == 3
    assert ACTION_DELAY_BEFORE_OPTION not in actions[0].options
    assert actions[1].options[ACTION_DELAY_BEFORE_OPTION] == 3
    assert actions[2].options[ACTION_DELAY_BEFORE_OPTION] == 3


def test_group_batch_delay_range_is_validated() -> None:
    with pytest.raises(ValueError, match="multi_reply_delay_max_s"):
        GroupBatchConfig(multi_reply_delay_min_s=8, multi_reply_delay_max_s=2)


def test_group_batch_marks_later_messages_with_delay() -> None:
    dispatcher = GroupBatchChatDispatcher(
        inner=object(),
        config=GroupBatchConfig(multi_reply_delay_min_s=4, multi_reply_delay_max_s=4),
    )
    action = Action(
        kind="send",
        target=Scope(kind="group", id="g1", platform="test"),
        segments=[TextSegment(text="第二句")],
    )

    delayed = dispatcher._with_multi_reply_delay(action, sent_count=1)

    assert delayed.options[ACTION_DELAY_BEFORE_OPTION] == 4
