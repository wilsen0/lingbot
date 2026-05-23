"""Process-wide global variable storage.

Replaces QRDic's ``$全局变量$`` / ``$取变量$`` primitives. Unlike the KV
store these values are **not** persisted: they live in a module-level
dict and vanish on process restart. That matches the original Java
implementation, which used in-memory static maps.
"""

from __future__ import annotations

from linling_core.tools import ToolCtx, tool

# Module-level dict for process-wide globals.
_globals: dict[str, str] = {}


@tool(
    name="set_global",
    dsl_name="全局变量",
    description="Set a process-wide global variable (not persisted)",
    schema={"key": "string", "value": "string"},
    safe=False,
)
async def set_global(ctx: ToolCtx, key: str = "", value: str = "") -> str:
    """Store *value* under *key* in the process-wide globals map.

    Returns the stored value for easy chaining, matching QRDic.
    Empty key is a no-op returning empty.
    """
    if not key:
        return ""
    _globals[key] = value
    return value


@tool(
    name="get_global",
    dsl_name="取变量",
    description="Read a process-wide global variable",
    schema={"key": "string", "default": "string?"},
    safe=True,
)
async def get_global(ctx: ToolCtx, key: str = "", default: str = "") -> str:
    """Return the value stored under *key*, or *default* if unset."""
    if not key:
        return default
    return _globals.get(key, default)


def _reset_globals_for_tests() -> None:
    """Testing hook: clear the globals dict between tests."""
    _globals.clear()
