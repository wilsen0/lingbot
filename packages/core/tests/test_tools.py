"""Tests for the tool registry."""

from __future__ import annotations

from pathlib import Path

from linling_core import SqliteKVStore
from linling_core.tools import ToolCtx, ToolDef, ToolRegistry, registry

# ---------------------------------------------------------------------------
# Decorator registration
# ---------------------------------------------------------------------------


def test_tool_decorator_registers_correctly() -> None:
    """The @tool decorator should register a ToolDef into the global registry."""
    # The builtin tools are registered on import
    import linling_core.tools_builtin  # noqa: F401

    td = registry.get("read_kv")
    assert td is not None
    assert td.name == "read_kv"
    # Python-facing read_kv is *not* DSL-callable; that goes through the
    # DSL shim ``dsl_read_kv`` registered under the Chinese name ``读``.
    assert td.dsl_name == ""
    assert td.safe is True
    assert callable(td.fn)


def test_lookup_by_name() -> None:
    """Registry.get() should find tools by Python name."""
    import linling_core.tools_builtin  # noqa: F401

    assert registry.get("write_kv") is not None
    assert registry.get("nonexistent") is None


def test_lookup_by_dsl_name() -> None:
    """Registry.get_by_dsl_name() should find tools by Chinese DSL name."""
    import linling_core.tools_builtin  # noqa: F401

    # The DSL-callable "写" maps to the shim dsl_write_kv, not the
    # Python-facing write_kv (which has a different, clean signature).
    td = registry.get_by_dsl_name("写")
    assert td is not None
    assert td.name == "dsl_write_kv"
    assert td.safe is False

    assert registry.get_by_dsl_name("不存在") is None


def test_llm_schemas_structure() -> None:
    """llm_schemas() should return valid OpenAI function-calling format."""
    import linling_core.tools_builtin  # noqa: F401

    schemas = registry.llm_schemas()
    assert isinstance(schemas, list)
    assert len(schemas) > 0

    # Check structure of first schema
    for schema in schemas:
        assert schema["type"] == "function"
        func = schema["function"]
        assert "name" in func
        assert "description" in func
        assert "parameters" in func
        params = func["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert "required" in params

    # Verify read_kv schema specifically
    read_schema = next(s for s in schemas if s["function"]["name"] == "read_kv")
    params = read_schema["function"]["parameters"]
    assert "scope" in params["properties"]
    assert "file" in params["properties"]
    assert "key" in params["properties"]
    assert "default" in params["properties"]
    # "default" is optional (string?) so should NOT be in required
    assert "default" not in params["required"]
    assert "scope" in params["required"]


def test_llm_schemas_type_mapping() -> None:
    """llm_schemas() should map simplified types to JSON Schema types."""
    import linling_core.tools_builtin  # noqa: F401

    schemas = registry.llm_schemas()
    # ``random_int`` accepts the QRDic dash-encoded form (``$随机数 1-5$``)
    # so its schema is now ``string?``. Use ``rank_kv`` instead — it
    # has explicit ``int`` ``top`` param that exercises the integer
    # mapping.
    rank_schema = next(s for s in schemas if s["function"]["name"] == "rank_kv")
    params = rank_schema["function"]["parameters"]
    assert params["properties"]["top"]["type"] == "integer"
    assert params["properties"]["scope"]["type"] == "string"


# ---------------------------------------------------------------------------
# Calling tools through the registry
# ---------------------------------------------------------------------------


async def test_call_tool_through_registry(tmp_path: Path) -> None:
    """Tools can be called through the registry with a ToolCtx."""
    import linling_core.tools_builtin  # noqa: F401

    async with SqliteKVStore("bot1", tmp_path / "test.db") as kv:
        ctx = ToolCtx(kv=kv, event=None, bot_id="bot1")

        td = registry.get("random_int")
        assert td is not None
        result = await td.fn(ctx, min=1, max=1)
        assert result == "1"


async def test_replace_str_tool() -> None:
    """The replace_str tool should replace patterns in text."""
    import linling_core.tools_builtin  # noqa: F401

    async with SqliteKVStore("bot1", ":memory:") as kv:
        ctx = ToolCtx(kv=kv, event=None, bot_id="bot1")

        td = registry.get("replace_str")
        assert td is not None
        result = await td.fn(
            ctx, sep=",", text="hello world", pattern="world", replacement="linling"
        )
        assert result == "hello linling"


# ---------------------------------------------------------------------------
# Integration with real SqliteKVStore
# ---------------------------------------------------------------------------


async def test_read_kv_with_real_store(tmp_path: Path) -> None:
    """read_kv tool should work with a real SqliteKVStore."""
    import linling_core.tools_builtin  # noqa: F401

    async with SqliteKVStore("bot1", tmp_path / "test.db") as kv:
        ctx = ToolCtx(kv=kv, event=None, bot_id="bot1")

        # Read missing key returns None
        td = registry.get("read_kv")
        assert td is not None
        result = await td.fn(ctx, scope="test", file="data", key="missing")
        assert result is None

        # Read with default
        result = await td.fn(ctx, scope="test", file="data", key="missing", default="0")
        assert result == "0"


async def test_write_kv_with_real_store(tmp_path: Path) -> None:
    """write_kv and read_kv should work together with a real SqliteKVStore."""
    import linling_core.tools_builtin  # noqa: F401

    async with SqliteKVStore("bot1", tmp_path / "test.db") as kv:
        ctx = ToolCtx(kv=kv, event=None, bot_id="bot1")

        # Write then read
        write_td = registry.get("write_kv")
        read_td = registry.get("read_kv")
        assert write_td is not None
        assert read_td is not None

        await write_td.fn(ctx, scope="啊/灵玉系", file="灵玉", key="user1", value="100")
        result = await read_td.fn(ctx, scope="啊/灵玉系", file="灵玉", key="user1")
        assert result == "100"

        # Overwrite
        await write_td.fn(ctx, scope="啊/灵玉系", file="灵玉", key="user1", value="200")
        result = await read_td.fn(ctx, scope="啊/灵玉系", file="灵玉", key="user1")
        assert result == "200"


async def test_delete_kv_with_real_store(tmp_path: Path) -> None:
    """delete_kv tool should work with a real SqliteKVStore."""
    import linling_core.tools_builtin  # noqa: F401

    async with SqliteKVStore("bot1", tmp_path / "test.db") as kv:
        ctx = ToolCtx(kv=kv, event=None, bot_id="bot1")

        write_td = registry.get("write_kv")
        read_td = registry.get("read_kv")
        delete_td = registry.get("delete_kv")
        assert write_td is not None
        assert read_td is not None
        assert delete_td is not None

        await write_td.fn(ctx, scope="test", file="f", key="k", value="v")
        assert await read_td.fn(ctx, scope="test", file="f", key="k") == "v"

        removed = await delete_td.fn(ctx, scope="test", file="f", key="k")
        assert removed == 1
        assert await read_td.fn(ctx, scope="test", file="f", key="k") is None


# ---------------------------------------------------------------------------
# Isolated registry tests
# ---------------------------------------------------------------------------


def test_registry_all() -> None:
    """Registry.all() should return all registered tools."""
    import linling_core.tools_builtin  # noqa: F401

    all_tools = registry.all()
    # 4 Python-facing KV tools + 4 DSL shims + 3 misc = 11 minimum.
    assert len(all_tools) >= 11
    names = {t.name for t in all_tools}
    assert "read_kv" in names
    assert "write_kv" in names
    assert "delete_kv" in names
    assert "rank_kv" in names
    # DSL shims also present
    assert "dsl_read_kv" in names
    assert "dsl_write_kv" in names
    assert "dsl_delete_kv" in names
    assert "dsl_rank_kv" in names
    assert "random_int" in names
    assert "http_get" in names
    assert "replace_str" in names


def test_custom_registry() -> None:
    """A fresh ToolRegistry should work independently."""
    r = ToolRegistry()
    assert r.get("anything") is None
    assert r.all() == []

    td = ToolDef(
        name="test_tool",
        dsl_name="测试",
        description="A test tool",
        schema={"x": "int"},
        safe=True,
        fn=lambda ctx, x: x,
    )
    r.register(td)
    assert r.get("test_tool") is td
    assert r.get_by_dsl_name("测试") is td
    assert len(r.all()) == 1
