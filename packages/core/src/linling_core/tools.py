"""Tool registry for linling.

Provides:

- :class:`ToolCtx` — runtime context passed to every tool invocation.
- :class:`ToolDef` — metadata about a registered tool.
- :func:`tool` — decorator that registers a function into the global registry.
- :class:`ToolRegistry` — lookup by Python name, DSL name, or LLM schema.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from linling_core.events import Event
from linling_core.storage.kv import KVStore


@dataclass
class ToolCtx:
    """Runtime context passed to every tool invocation."""

    kv: KVStore
    event: Event | None
    bot_id: str
    extras: dict[str, Any] = field(default_factory=dict)


# Cached signature view of a tool function. Computed once at
# registration so the hot dispatch path doesn't pay :func:`inspect`
# cost on every DSL ``$func$`` call.
@dataclass(frozen=True)
class _ToolSignature:
    param_names: tuple[str, ...]  # positional-or-keyword names, in order, ctx excluded
    int_params: frozenset[str]  # names whose annotation is ``int``
    accepts_var_args: bool  # True iff the function declares ``*args``


def _inspect_tool(fn: Callable[..., Any]) -> _ToolSignature:
    sig = inspect.signature(fn)
    names: list[str] = []
    int_params: set[str] = set()
    accepts_var_args = False
    for i, (pname, param) in enumerate(sig.parameters.items()):
        if i == 0:
            continue  # skip ``ctx``
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            accepts_var_args = True
            names.append(pname)  # placeholder; not callable as kw
            continue
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        names.append(pname)
        if param.annotation is int or param.annotation == "int":
            int_params.add(pname)
    return _ToolSignature(
        param_names=tuple(names),
        int_params=frozenset(int_params),
        accepts_var_args=accepts_var_args,
    )


@dataclass
class ToolDef:
    """Metadata about a registered tool."""

    name: str
    """Canonical Python name (used in code)."""

    dsl_name: str
    """Chinese name used in DSL ``$...$`` calls.

    Empty string means "not exposed to DSL" — useful for clean Python/LLM
    tools that have a separate, messier DSL-facing shim (see e.g.
    ``read_kv`` vs ``dsl_read_kv`` in :mod:`linling_core.tools_builtin`).
    """

    description: str
    """Human-readable description (also used as LLM tool description)."""

    schema: dict[str, Any]
    """Simplified schema: param names → type strings (e.g. ``"string"``, ``"int"``, ``"string?"``).

    The ``?`` suffix marks optional parameters. :meth:`ToolRegistry.llm_schemas`
    converts these to proper OpenAI function-calling JSON Schema.
    """

    safe: bool
    """If True, can be called from DSL sandbox without extra permission."""

    fn: Callable[..., Any]
    """The actual async function."""

    llm_visible: bool = True
    """If False, the tool is hidden from :meth:`ToolRegistry.llm_schemas`.

    DSL compatibility shims use this so the LLM-facing catalog stays on
    the clean ``(scope, file, key)`` API rather than the historical QRDic
    ``"path key default"`` shape.
    """

    vision_only: bool = False
    """If True, the tool is hidden unless the agent runs with
    ``vision_enabled=True``. Filtering happens in
    ``AgentRuntime._build_tool_schemas`` (NOT :meth:`ToolRegistry.llm_schemas`,
    which exposes every ``llm_visible`` tool); keeping the registry catalog
    complete lets the DSL and tests see all tools regardless of vision mode.
    Used for sticker collection tools that require multimodal input."""

    signature: _ToolSignature = field(init=False)
    """Cached :func:`inspect`-derived view of ``fn``. Filled in
    automatically by :meth:`__post_init__` so the dispatch hot path
    never reflects on a function shape it has already seen."""

    def __post_init__(self) -> None:
        self.signature = _inspect_tool(self.fn)


# Mapping from simplified type strings to JSON Schema types.
_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
}


def tool_parameters_schema(tool_def: ToolDef) -> dict[str, Any]:
    """Convert a :class:`ToolDef`'s simplified ``schema`` into JSON Schema params.

    The single source of truth for the simplified-type → JSON-Schema mapping,
    shared by :meth:`ToolRegistry.llm_schemas`, ``AgentRuntime`` and
    ``ProfileUpdater`` so schema generation can't drift between call sites.
    Returns the ``{"type": "object", "properties": {...}, "required": [...]}``
    object expected under an OpenAI function ``parameters`` key.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, type_str in tool_def.schema.items():
        optional = type_str.endswith("?")
        base_type = type_str.rstrip("?")
        properties[param_name] = {"type": _TYPE_MAP.get(base_type, "string")}
        if not optional:
            required.append(param_name)
    return {"type": "object", "properties": properties, "required": required}


class ToolRegistry:
    """Registry of tool definitions with lookup by Python name or DSL name."""

    def __init__(self) -> None:
        self._by_name: dict[str, ToolDef] = {}
        self._by_dsl_name: dict[str, ToolDef] = {}

    def register(self, tool_def: ToolDef) -> None:
        """Register a tool definition.

        ``dsl_name`` may be empty to register a Python-only tool; in that
        case the DSL lookup table is not updated.
        """
        self._by_name[tool_def.name] = tool_def
        if tool_def.dsl_name:
            self._by_dsl_name[tool_def.dsl_name] = tool_def

    def get(self, name: str) -> ToolDef | None:
        """Lookup by Python name."""
        return self._by_name.get(name)

    def get_by_dsl_name(self, dsl_name: str) -> ToolDef | None:
        """Lookup by DSL name."""
        return self._by_dsl_name.get(dsl_name)

    def all(self) -> list[ToolDef]:
        """Return all registered tool definitions."""
        return list(self._by_name.values())

    def llm_schemas(self) -> list[dict[str, Any]]:
        """Generate OpenAI-compatible function schemas for all LLM-visible tools."""
        schemas: list[dict[str, Any]] = []
        for td in self._by_name.values():
            if not td.llm_visible:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": td.name,
                        "description": td.description,
                        "parameters": tool_parameters_schema(td),
                    },
                }
            )
        return schemas


# Module-level singleton that the @tool decorator writes to.
registry = ToolRegistry()


def tool(
    *,
    name: str,
    dsl_name: str,
    description: str,
    schema: dict[str, Any],
    safe: bool = False,
    llm_visible: bool = True,
    vision_only: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that registers a function into the global registry.

    Usage::

        @tool(
            name="read_kv",
            dsl_name="读",
            description="Read a key-value pair",
            schema={"scope": "string", "file": "string", "key": "string", "default": "string?"},
            safe=True,
        )
        async def read_kv(ctx: ToolCtx, scope: str, file: str, key: str, default: str | None = None) -> str | None:
            ...

    Pass ``dsl_name=""`` to register a tool that is not callable from the
    DSL (useful when a separate DSL-facing shim handles the old syntax).
    Pass ``llm_visible=False`` to hide the tool from LLM tool catalogs
    (useful for DSL compatibility shims whose signature would confuse the
    model).
    Pass ``vision_only=True`` to restrict the tool to vision-enabled agents
    (e.g. sticker collection tools that need image input).
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        td = ToolDef(
            name=name,
            dsl_name=dsl_name,
            description=description,
            schema=schema,
            safe=safe,
            fn=fn,
            llm_visible=llm_visible,
            vision_only=vision_only,
        )
        registry.register(td)
        return fn

    return decorator
