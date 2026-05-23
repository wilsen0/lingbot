"""Tests for the linling DSL parser."""

import pytest
from linling_dsl import (
    ArithExpr,
    Assign,
    FuncCall,
    FuncCallExpr,
    IfStmt,
    JsonAccess,
    Jump,
    Label,
    Literal,
    OutputImage,
    OutputText,
    ReturnStmt,
    VarRef,
    parse,
)
from linling_dsl.parser import ParseError


class TestSimpleHandler:
    """Test basic handler parsing."""

    def test_trigger_with_output_text(self):
        source = "你好\n欢迎来到苏苏的世界"
        script = parse(source)
        assert len(script.handlers) == 1
        h = script.handlers[0]
        assert h.trigger == "你好"
        assert h.is_internal is False
        assert h.line == 1
        assert len(h.body) == 1
        assert isinstance(h.body[0], OutputText)

    def test_trigger_only_no_body(self):
        source = "0\n返回"
        script = parse(source)
        assert len(script.handlers) == 1
        h = script.handlers[0]
        assert h.trigger == "0"
        assert len(h.body) == 1
        assert isinstance(h.body[0], ReturnStmt)

    def test_trigger_with_regex(self):
        source = "查看昵称(.*)\n$读 小苏苏/自定义昵称/昵称 %括号1% 0$"
        script = parse(source)
        h = script.handlers[0]
        assert h.trigger == "查看昵称(.*)"


class TestInternalHandler:
    """Test [内部] prefix handling."""

    def test_internal_prefix_detected(self):
        source = "[内部]数据恢复\n返回"
        script = parse(source)
        h = script.handlers[0]
        assert h.is_internal is True
        assert h.trigger == "数据恢复"

    def test_non_internal_handler(self):
        source = "普通触发\n返回"
        script = parse(source)
        h = script.handlers[0]
        assert h.is_internal is False
        assert h.trigger == "普通触发"


class TestIfStatement:
    """Test 如果/如果尾 blocks."""

    def test_simple_if(self):
        source = "触发\n如果:%群号%==754800438\n返回\n如果尾"
        script = parse(source)
        h = script.handlers[0]
        assert len(h.body) == 1
        stmt = h.body[0]
        assert isinstance(stmt, IfStmt)
        assert stmt.condition.text == "%群号%==754800438"
        assert len(stmt.body) == 1
        assert isinstance(stmt.body[0], ReturnStmt)

    def test_nested_if(self):
        source = "触发\n如果:%a%==1\n如果:%b%==2\n返回\n如果尾\n如果尾"
        script = parse(source)
        h = script.handlers[0]
        outer_if = h.body[0]
        assert isinstance(outer_if, IfStmt)
        inner_if = outer_if.body[0]
        assert isinstance(inner_if, IfStmt)
        assert inner_if.condition.text == "%b%==2"

    def test_if_with_func_in_condition(self):
        """如果: conditions can contain $读 ...$."""
        source = "触发\n如果:$读 啊/x %QQ% 0$==1\n返回\n如果尾"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, IfStmt)
        assert stmt.condition.text == "$读 啊/x %QQ% 0$==1"

    def test_regex_alias(self):
        """正则: is an alias for 如果:."""
        source = "触发\n正则:%x%==1\n返回\n如果尾"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, IfStmt)
        assert stmt.condition.text == "%x%==1"

    def test_unmatched_if_raises_error(self):
        source = "触发\n如果:%a%==1\n返回"
        with pytest.raises(ParseError, match="如果尾"):
            parse(source)

    def test_lenient_ignores_unmatched_endif(self):
        """An extra 如果尾 with no matching 如果: is silently skipped in lenient mode."""
        source = "触发\n如果尾\n返回"
        script = parse(source, strict=False)
        h = script.handlers[0]
        # The stray 如果尾 is dropped; only the 返回 survives.
        assert len(h.body) == 1
        assert isinstance(h.body[0], ReturnStmt)

    def test_lenient_auto_closes_missing_endif(self):
        """A missing 如果尾 at end of body auto-closes the 如果 in lenient mode."""
        source = "触发\n如果:%a%==1\n返回"
        script = parse(source, strict=False)
        h = script.handlers[0]
        assert len(h.body) == 1
        stmt = h.body[0]
        assert isinstance(stmt, IfStmt)
        assert stmt.condition.text == "%a%==1"
        assert len(stmt.body) == 1
        assert isinstance(stmt.body[0], ReturnStmt)


class TestAssignment:
    """Test variable assignment parsing."""

    def test_simple_assign(self):
        source = "触发\n玉:100"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, Assign)
        assert stmt.name == "玉"
        assert isinstance(stmt.value, Literal)
        assert stmt.value.value == "100"

    def test_assign_with_var_ref(self):
        source = "触发\n玉:%括号1%"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, Assign)
        assert isinstance(stmt.value, VarRef)
        assert stmt.value.name == "括号1"

    def test_assign_with_func_call(self):
        source = "触发\n玉:$读 啊/灵玉系/灵玉 %QQ% 0$"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, Assign)
        assert stmt.name == "玉"
        assert isinstance(stmt.value, FuncCallExpr)
        assert stmt.value.name == "读"

    def test_assign_with_arith(self):
        source = "触发\n玉:[%玉%+100]"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, Assign)
        assert isinstance(stmt.value, ArithExpr)
        assert stmt.value.text == "%玉%+100"


class TestVarInterpolation:
    """Test %var% interpolation in output."""

    def test_var_in_output(self):
        source = "触发\n你的灵玉是%玉%个"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, OutputText)
        assert len(stmt.parts) == 3
        assert isinstance(stmt.parts[0], Literal)
        assert stmt.parts[0].value == "你的灵玉是"
        assert isinstance(stmt.parts[1], VarRef)
        assert stmt.parts[1].name == "玉"
        assert isinstance(stmt.parts[2], Literal)
        assert stmt.parts[2].value == "个"


class TestFuncCall:
    """Test $func arg1 arg2$ calls."""

    def test_standalone_func_call(self):
        source = "触发\n$写 啊/灵玉系/灵玉 %QQ% 100$"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, FuncCall)
        assert stmt.name == "写"
        assert len(stmt.args) == 3

    def test_func_call_no_args(self):
        source = "触发\n$删除 /path$"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, FuncCall)
        assert stmt.name == "删除"

    def test_inline_func_in_output(self):
        source = "触发\n你好$群昵称 %群号% %QQ%$欢迎"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, OutputText)
        # Should have: Literal("你好"), FuncCallExpr("群昵称"), Literal("欢迎")
        assert len(stmt.parts) == 3
        assert isinstance(stmt.parts[1], FuncCallExpr)
        assert stmt.parts[1].name == "群昵称"


class TestImageOutput:
    """Test ±img=url± image output."""

    def test_simple_image(self):
        source = "触发\n±img=/storage/emulated/0/pic.jpg±"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, OutputImage)
        assert isinstance(stmt.src, Literal)
        assert stmt.src.value == "/storage/emulated/0/pic.jpg"

    def test_image_with_var(self):
        source = "触发\n±img=%专%±"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, OutputImage)
        assert isinstance(stmt.src, VarRef)
        assert stmt.src.name == "专"


class TestLabelAndJump:
    """Test :label and $jump :label$."""

    def test_label(self):
        source = "触发\n:形象标记\n返回"
        script = parse(source)
        stmts = script.handlers[0].body
        assert isinstance(stmts[0], Label)
        assert stmts[0].name == "形象标记"

    def test_jump(self):
        source = "触发\n$jump :形象标记$"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, Jump)
        assert stmt.target == "形象标记"

    def test_jump_chinese_alias(self):
        """$跳 :label$ is an alias for $jump :label$."""
        source = "触发\n$跳 :循环1$"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, Jump)
        assert stmt.target == "循环1"


class TestReturnAndComplete:
    """Test 返回 and 完成."""

    def test_return(self):
        source = "触发\n返回"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, ReturnStmt)

    def test_complete_alias(self):
        source = "触发\n完成"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, ReturnStmt)


class TestComments:
    """Test that comments are skipped."""

    def test_comment_skipped(self):
        source = "触发\n//这是注释\n返回"
        script = parse(source)
        stmts = script.handlers[0].body
        assert len(stmts) == 1
        assert isinstance(stmts[0], ReturnStmt)

    def test_config_comment_skipped(self):
        source = "&&<配置>兼容模式:是\n\n触发\n返回"
        script = parse(source)
        # The && line is treated as a handler block but skipped
        assert len(script.handlers) == 1
        assert script.handlers[0].trigger == "触发"


class TestMultipleHandlers:
    """Test multiple handlers separated by blank lines."""

    def test_two_handlers(self):
        source = "触发1\n返回\n\n触发2\n完成"
        script = parse(source)
        assert len(script.handlers) == 2
        assert script.handlers[0].trigger == "触发1"
        assert script.handlers[1].trigger == "触发2"

    def test_multiple_blank_lines(self):
        source = "触发1\n返回\n\n\n\n触发2\n完成"
        script = parse(source)
        assert len(script.handlers) == 2

    def test_three_handlers(self):
        source = "a\n返回\n\nb\n完成\n\nc\n返回"
        script = parse(source)
        assert len(script.handlers) == 3


class TestJsonAccess:
    """Test @var[field] JSON access."""

    def test_simple_json_access(self):
        source = "触发\na:@result[code]"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, Assign)
        assert isinstance(stmt.value, JsonAccess)
        assert stmt.value.var == "result"
        assert stmt.value.path == ["code"]

    def test_nested_json_access(self):
        source = "触发\nb:@a[data][msg]"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, Assign)
        assert isinstance(stmt.value, JsonAccess)
        assert stmt.value.var == "a"
        assert stmt.value.path == ["data", "msg"]

    def test_json_access_in_output(self):
        source = "触发\n@a[data][articleList][1][title]"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, OutputText)
        assert len(stmt.parts) == 1
        assert isinstance(stmt.parts[0], JsonAccess)
        assert stmt.parts[0].path == ["data", "articleList", "1", "title"]


class TestArithExpr:
    """Test [expr] arithmetic expressions."""

    def test_arith_in_assign(self):
        source = "触发\n玉:[%玉%+100]"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, Assign)
        assert isinstance(stmt.value, ArithExpr)
        assert stmt.value.text == "%玉%+100"

    def test_arith_in_output(self):
        source = "触发\n第[%i%+1]张"
        script = parse(source)
        stmt = script.handlers[0].body[0]
        assert isinstance(stmt, OutputText)
        assert isinstance(stmt.parts[1], ArithExpr)
        assert stmt.parts[1].text == "%i%+1"


class TestErrorReporting:
    """Test error reporting with line numbers."""

    def test_unmatched_if_reports_line(self):
        source = "触发\n返回\n如果:%a%==1\n返回"
        # The 如果 is on line 3 (trigger is line 1, 返回 is line 2)
        with pytest.raises(ParseError) as exc_info:
            parse(source)
        assert exc_info.value.line == 3

    def test_unexpected_if_end(self):
        source = "触发\n如果尾"
        with pytest.raises(ParseError, match="如果尾"):
            parse(source)


class TestRealQRDicHandler:
    """Test with real QRDic handler examples."""

    def test_lookup_nickname_handler(self):
        """The 查看昵称 handler from the spec."""
        source = "查看昵称(.*)\n$读 小苏苏/自定义昵称/昵称 %括号1% 0$"
        script = parse(source)
        assert len(script.handlers) == 1
        h = script.handlers[0]
        assert h.trigger == "查看昵称(.*)"
        assert len(h.body) == 1
        stmt = h.body[0]
        assert isinstance(stmt, FuncCall)
        assert stmt.name == "读"
        assert len(stmt.args) == 3
        # First arg: "小苏苏/自定义昵称/昵称"
        assert isinstance(stmt.args[0], Literal)
        assert stmt.args[0].value == "小苏苏/自定义昵称/昵称"
        # Second arg: %括号1%
        assert isinstance(stmt.args[1], VarRef)
        assert stmt.args[1].name == "括号1"
        # Third arg: "0"
        assert isinstance(stmt.args[2], Literal)
        assert stmt.args[2].value == "0"

    def test_compensation_handler(self):
        """A more complex handler with if/assign/arith."""
        source = (
            "补偿([0-9]+)数量([0-9]+)\n"
            "如果:%QQ%!=2078123478\n"
            "返回\n"
            "如果尾\n"
            "玉:$读 啊/灵玉系/灵玉 %括号1% 0$\n"
            "$写 啊/灵玉系/灵玉 %括号1% [%玉%+%括号2%]$\n"
            "完成"
        )
        script = parse(source)
        h = script.handlers[0]
        assert h.trigger == "补偿([0-9]+)数量([0-9]+)"
        assert len(h.body) == 4  # IfStmt, Assign, FuncCall, ReturnStmt
        assert isinstance(h.body[0], IfStmt)
        assert isinstance(h.body[1], Assign)
        assert isinstance(h.body[2], FuncCall)
        assert isinstance(h.body[3], ReturnStmt)

    def test_poke_handler_with_labels_and_jumps(self):
        """Handler with labels and jumps (simplified from [戳一戳])."""
        source = (
            "[戳一戳]\n"
            "如果:%Code%!=%Robot%\n"
            "返回\n"
            "如果尾\n"
            "专:$读 啊/主页系/专属形象 %QQ% 0$\n"
            "如果:%专%!=0\n"
            "±img=%专%±\n"
            "如果尾\n"
            ":形象标记\n"
            "$写 啊/苏苏状态/戳一戳冷却 %QQ% %时间mm%$\n"
            "返回"
        )
        script = parse(source)
        h = script.handlers[0]
        assert h.trigger == "[戳一戳]"
        # body: IfStmt, Assign, IfStmt, Label, FuncCall, ReturnStmt
        assert isinstance(h.body[0], IfStmt)
        assert isinstance(h.body[1], Assign)
        assert isinstance(h.body[2], IfStmt)
        assert isinstance(h.body[3], Label)
        assert h.body[3].name == "形象标记"
        assert isinstance(h.body[4], FuncCall)
        assert isinstance(h.body[5], ReturnStmt)

    def test_internal_handler_with_image(self):
        """Internal handler with image output."""
        source = "[内部]转图片\n±img=http://example.com/api?text=%图%±"
        script = parse(source)
        h = script.handlers[0]
        assert h.is_internal is True
        assert h.trigger == "转图片"
        assert isinstance(h.body[0], OutputImage)
