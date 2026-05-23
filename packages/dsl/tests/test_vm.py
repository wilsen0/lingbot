"""Tests for the DSL v0 VM / interpreter."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import linling_core.tools_builtin  # noqa: F401 — ensure tools are registered
import pytest
from linling_core.events import Event, Scope, User
from linling_core.segments import AtSegment, ImageSegment, TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry
from linling_dsl.ast_nodes import (
    ArithExpr,
    Assign,
    Condition,
    FuncCall,
    FuncCallExpr,
    Handler,
    IfStmt,
    JsonAccess,
    Jump,
    Label,
    Literal,
    OutputImage,
    OutputText,
    ReturnStmt,
    VarRef,
)
from linling_dsl.vm import VM, SandboxError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_event(
    text: str = "hello",
    sender_id: str = "12345",
    group_id: str = "67890",
    display_name: str | None = "小明",
    bot_id: str = "linling",
    at_ids: list[str] | None = None,
) -> Event:
    """Create a test event."""
    segments = [TextSegment(text=text)]
    if at_ids:
        for uid in at_ids:
            segments.append(AtSegment(user_id=uid))
    return Event(
        id="msg001",
        platform="test",
        bot_id=bot_id,
        scope=Scope(kind="group", id=group_id, platform="test"),
        sender=User(id=sender_id, platform="test", display_name=display_name),
        segments=segments,
    )


@pytest.fixture
async def kv():
    store = SqliteKVStore(bot_id="linling", db_path=":memory:")
    async with store:
        yield store


@pytest.fixture
def vm(kv):
    return VM(tool_registry=registry, kv=kv, bot_id="linling")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_simple_output_text(vm):
    """Simple output text with literal."""
    handler = Handler(
        trigger="hello",
        is_internal=False,
        body=[OutputText(parts=[Literal(value="你好世界")], line=2)],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert len(result.segments) == 1
    assert isinstance(result.segments[0], TextSegment)
    assert result.segments[0].text == "你好世界"


async def test_variable_assignment_and_interpolation(vm):
    """Variable assignment and interpolation in output."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="x", value=Literal(value="world"), line=2),
            OutputText(parts=[Literal(value="hello "), VarRef(name="x")], line=3),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "hello world"


async def test_event_context_qq(vm):
    """%QQ% resolves to sender id."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[OutputText(parts=[VarRef(name="QQ")], line=2)],
        line=1,
    )
    event = _make_event(sender_id="99999")
    result = await vm.execute_handler(handler, event)
    assert result.segments[0].text == "99999"


async def test_event_context_group(vm):
    """%群号% resolves to scope id."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[OutputText(parts=[VarRef(name="群号")], line=2)],
        line=1,
    )
    event = _make_event(group_id="11111")
    result = await vm.execute_handler(handler, event)
    assert result.segments[0].text == "11111"


async def test_event_context_nickname(vm):
    """%昵称% resolves to display_name."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[OutputText(parts=[VarRef(name="昵称")], line=2)],
        line=1,
    )
    event = _make_event(display_name="小红")
    result = await vm.execute_handler(handler, event)
    assert result.segments[0].text == "小红"


async def test_capture_groups(vm):
    """%括号1% resolves to regex capture groups."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            OutputText(
                parts=[VarRef(name="括号1"), Literal(value="-"), VarRef(name="括号2")], line=2
            )
        ],
        line=1,
    )
    event = _make_event()
    result = await vm.execute_handler(handler, event, captures=["foo", "bar"])
    assert result.segments[0].text == "foo-bar"


async def test_time_variable_hh(vm):
    """%时间HH% resolves to current hour."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[OutputText(parts=[VarRef(name="时间HH")], line=2)],
        line=1,
    )
    with patch("linling_dsl.vm.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2024, 6, 15, 14, 30, 0)
        result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "14"


async def test_time_variable_mm(vm):
    """%时间mm% resolves to current minute."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[OutputText(parts=[VarRef(name="时间mm")], line=2)],
        line=1,
    )
    with patch("linling_dsl.vm.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2024, 6, 15, 14, 5, 0)
        result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "05"


async def test_if_stmt_true(vm):
    """IfStmt with true condition executes body."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="x", value=Literal(value="10"), line=2),
            IfStmt(
                condition=Condition(text="%x%==10", line=3),
                body=[OutputText(parts=[Literal(value="yes")], line=4)],
                line=3,
            ),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert len(result.segments) == 1
    assert result.segments[0].text == "yes"


async def test_if_stmt_false(vm):
    """IfStmt with false condition skips body."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="x", value=Literal(value="10"), line=2),
            IfStmt(
                condition=Condition(text="%x%==20", line=3),
                body=[OutputText(parts=[Literal(value="yes")], line=4)],
                line=3,
            ),
            OutputText(parts=[Literal(value="done")], line=6),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert len(result.segments) == 1
    assert result.segments[0].text == "done"


async def test_nested_if(vm):
    """Nested if statements."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="x", value=Literal(value="5"), line=2),
            Assign(name="y", value=Literal(value="10"), line=3),
            IfStmt(
                condition=Condition(text="%x%==5", line=4),
                body=[
                    IfStmt(
                        condition=Condition(text="%y%==10", line=5),
                        body=[OutputText(parts=[Literal(value="nested")], line=6)],
                        line=5,
                    ),
                ],
                line=4,
            ),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "nested"


async def test_numeric_comparison_gt(vm):
    """Numeric comparison > in conditions."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="x", value=Literal(value="15"), line=2),
            IfStmt(
                condition=Condition(text="%x%>10", line=3),
                body=[OutputText(parts=[Literal(value="big")], line=4)],
                line=3,
            ),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "big"


async def test_numeric_comparison_lt(vm):
    """Numeric comparison < in conditions."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="x", value=Literal(value="3"), line=2),
            IfStmt(
                condition=Condition(text="%x%<10", line=3),
                body=[OutputText(parts=[Literal(value="small")], line=4)],
                line=3,
            ),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "small"


async def test_string_comparison_eq(vm):
    """String comparison == in conditions."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="name", value=Literal(value="alice"), line=2),
            IfStmt(
                condition=Condition(text="%name%==alice", line=3),
                body=[OutputText(parts=[Literal(value="match")], line=4)],
                line=3,
            ),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "match"


async def test_string_comparison_ne(vm):
    """String comparison != in conditions."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="name", value=Literal(value="alice"), line=2),
            IfStmt(
                condition=Condition(text="%name%!=bob", line=3),
                body=[OutputText(parts=[Literal(value="diff")], line=4)],
                line=3,
            ),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "diff"


async def test_condition_and(vm):
    """AND (&) connector in conditions."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="a", value=Literal(value="1"), line=2),
            Assign(name="b", value=Literal(value="2"), line=3),
            IfStmt(
                condition=Condition(text="%a%==1&%b%==2", line=4),
                body=[OutputText(parts=[Literal(value="both")], line=5)],
                line=4,
            ),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "both"


async def test_condition_or(vm):
    """OR (|) connector in conditions."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="a", value=Literal(value="1"), line=2),
            IfStmt(
                condition=Condition(text="%a%==99|%a%==1", line=3),
                body=[OutputText(parts=[Literal(value="either")], line=4)],
                line=3,
            ),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "either"


async def test_return_stops_execution(vm):
    """ReturnStmt stops execution."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            OutputText(parts=[Literal(value="first")], line=2),
            ReturnStmt(line=3),
            OutputText(parts=[Literal(value="second")], line=4),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert len(result.segments) == 1
    assert result.segments[0].text == "first"
    assert result.returned is True


async def test_label_and_jump(vm):
    """Label + Jump flow control."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="x", value=Literal(value="0"), line=2),
            Jump(target="end", line=3),
            OutputText(parts=[Literal(value="skipped")], line=4),
            Label(name="end", line=5),
            OutputText(parts=[Literal(value="reached")], line=6),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert len(result.segments) == 1
    assert result.segments[0].text == "reached"


async def test_func_call_read_write_kv(kv, vm):
    """FuncCall calling read_kv/write_kv through registry.

    Uses the QRDic-style 3-token form: ``$写 scope/file key value$``.
    The DSL shim splits ``test/file1`` on the last ``/`` into
    ``scope="test"`` and ``file="file1"`` internally.
    """
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            # $写 test/file1 mykey hello$
            FuncCall(
                name="写",
                args=[
                    Literal(value="test/file1"),
                    Literal(value="mykey"),
                    Literal(value="hello"),
                ],
                line=2,
            ),
            # result:$读 test/file1 mykey$
            Assign(
                name="result",
                value=FuncCallExpr(
                    name="读",
                    args=[
                        Literal(value="test/file1"),
                        Literal(value="mykey"),
                    ],
                ),
                line=3,
            ),
            OutputText(parts=[VarRef(name="result")], line=4),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "hello"
    # Sanity: the value really landed in the expected (scope, file, key).
    assert await kv.read("test", "file1", "mykey") == "hello"


async def test_output_image(vm):
    """OutputImage produces ImageSegment."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[OutputImage(src=Literal(value="https://example.com/img.png"), line=2)],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert len(result.segments) == 1
    assert isinstance(result.segments[0], ImageSegment)
    assert result.segments[0].url == "https://example.com/img.png"


async def test_arith_addition(vm):
    """ArithExpr: [%x%+100]."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="x", value=Literal(value="50"), line=2),
            OutputText(parts=[ArithExpr(text="%x%+100")], line=3),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "150"


async def test_arith_multiplication(vm):
    """ArithExpr: [%a%*2]."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="a", value=Literal(value="7"), line=2),
            OutputText(parts=[ArithExpr(text="%a%*2")], line=3),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "14"


async def test_arith_subtraction(vm):
    """ArithExpr: [%a%-%b%]."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(name="a", value=Literal(value="100"), line=2),
            Assign(name="b", value=Literal(value="30"), line=3),
            OutputText(parts=[ArithExpr(text="%a%-%b%")], line=4),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "70"


async def test_sandbox_max_steps(kv):
    """Sandbox: max_steps exceeded raises SandboxError."""
    vm = VM(tool_registry=registry, kv=kv, bot_id="linling", max_steps=5)
    # Create a handler with a loop via jump that will exceed max_steps
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Label(name="loop", line=2),
            Assign(name="x", value=Literal(value="1"), line=3),
            Assign(name="y", value=Literal(value="2"), line=4),
            Assign(name="z", value=Literal(value="3"), line=5),
            Jump(target="loop", line=6),
        ],
        line=1,
    )
    with pytest.raises(SandboxError, match="max_steps"):
        await vm.execute_handler(handler, _make_event())


async def test_sandbox_max_output_segments(kv):
    """Sandbox: max_output_segments exceeded raises SandboxError."""
    vm = VM(tool_registry=registry, kv=kv, bot_id="linling", max_output_segments=3)
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            OutputText(parts=[Literal(value="a")], line=2),
            OutputText(parts=[Literal(value="b")], line=3),
            OutputText(parts=[Literal(value="c")], line=4),
            OutputText(parts=[Literal(value="d")], line=5),
        ],
        line=1,
    )
    with pytest.raises(SandboxError, match="max_output_segments"):
        await vm.execute_handler(handler, _make_event())


async def test_complete_handler_checkin_flow(kv, vm):
    """Complete handler: '打卡' flow — write to KV, read back, output.

    Uses the QRDic-style 3-token form (``scope/file key value``).
    """
    handler = Handler(
        trigger="打卡",
        is_internal=False,
        body=[
            # Write checkin record
            FuncCall(
                name="写",
                args=[
                    Literal(value="打卡记录/签到"),
                    VarRef(name="QQ"),
                    Literal(value="1"),
                ],
                line=2,
            ),
            # Read it back
            Assign(
                name="count",
                value=FuncCallExpr(
                    name="读",
                    args=[
                        Literal(value="打卡记录/签到"),
                        VarRef(name="QQ"),
                        Literal(value="0"),
                    ],
                ),
                line=3,
            ),
            # Output
            OutputText(
                parts=[
                    VarRef(name="昵称"),
                    Literal(value=" 打卡成功！次数: "),
                    VarRef(name="count"),
                ],
                line=4,
            ),
        ],
        line=1,
    )
    event = _make_event(sender_id="12345", display_name="小明")
    result = await vm.execute_handler(handler, event)
    assert result.segments[0].text == "小明 打卡成功！次数: 1"
    assert await kv.read("打卡记录", "签到", "12345") == "1"


async def test_condition_with_inline_func_call(kv, vm):
    """Condition with inline $读$ function call (QRDic 3-token form)."""
    # Pre-write a value; the DSL shim routes "test/flags" → scope="test", file="flags".
    await kv.write("test", "flags", "vip", "1")

    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            IfStmt(
                condition=Condition(text="$读 test/flags vip 0$==1", line=2),
                body=[OutputText(parts=[Literal(value="is_vip")], line=3)],
                line=2,
            ),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "is_vip"


async def test_json_access(vm):
    """JsonAccess: @var[field] lookup."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(
                name="data",
                value=Literal(value='{"name":"alice","age":20}'),
                line=2,
            ),
            OutputText(parts=[JsonAccess(var="data", path=["name"])], line=3),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "alice"


async def test_json_access_nested(vm):
    """JsonAccess: @var[field1][field2] nested lookup."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[
            Assign(
                name="data",
                value=Literal(value='{"user":{"name":"bob"}}'),
                line=2,
            ),
            OutputText(parts=[JsonAccess(var="data", path=["user", "name"])], line=3),
        ],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "bob"


async def test_at_segment_context(vm):
    """%AT0% resolves to at-mentioned user IDs."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[OutputText(parts=[VarRef(name="AT0")], line=2)],
        line=1,
    )
    event = _make_event(at_ids=["77777"])
    result = await vm.execute_handler(handler, event)
    assert result.segments[0].text == "77777"


async def test_event_text_param(vm):
    """%参数-1% resolves to full event text."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[OutputText(parts=[VarRef(name="参数-1")], line=2)],
        line=1,
    )
    event = _make_event(text="hello world")
    result = await vm.execute_handler(handler, event)
    assert result.segments[0].text == "hello world"


async def test_undefined_var_in_output_literal(vm):
    """Undefined var in literal text is kept as-is (no crash)."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[OutputText(parts=[Literal(value="hi %unknown% there")], line=2)],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.segments[0].text == "hi %unknown% there"


async def test_vmresult_returned_false_by_default(vm):
    """VMResult.returned is False when no return statement hit."""
    handler = Handler(
        trigger="test",
        is_internal=False,
        body=[OutputText(parts=[Literal(value="hi")], line=2)],
        line=1,
    )
    result = await vm.execute_handler(handler, _make_event())
    assert result.returned is False
