"""JSON operations — replicates QRDic's ``$JSON ...$`` primitives.

QRDic syntax uses sub-commands::

    $JSON 长度 var$            → length of array/object/string
    $JSON 获取 var idx_or_key$ → element at numeric index or dotted path
    $JSON 添加 var value$      → append value to array (returns new array text)
    $JSON 删除 var idx$        → remove element at numeric index

The first non-keyword argument is the *value* of a DSL variable
(after VM late-binding) — i.e. the JSON text itself, not the variable
name. The DSL VM in :mod:`linling_dsl.vm` rewrites bare identifiers
to their scope value before invoking us.

All functions take and return string values, because the DSL only
traffics in strings.
"""

from __future__ import annotations

import json
from typing import Any

from linling_core.tools import ToolCtx, tool


def _load(text: str) -> Any:
    """Parse JSON text; return the raw string if it isn't valid JSON."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _dump(value: Any) -> str:
    """Serialise a Python value back to compact JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _length(text: str) -> str:
    value = _load(text)
    if isinstance(value, list | dict | str):
        return str(len(value))
    return "0"


def _contains(text: str, key: str) -> str:
    """Return ``"1"`` if ``key`` is a member of the JSON value, ``""`` otherwise.

    QRSpeed's ``$JSON 包含 var key$`` semantics: for a dict it checks
    keys, for a list it checks element membership (string compare).
    Empty / non-JSON inputs always return empty (matching QRSpeed's
    silent-miss convention).
    """
    value = _load(text)
    if isinstance(value, dict):
        return "1" if key in value else ""
    if isinstance(value, list):
        return "1" if key in [str(item) for item in value] else ""
    return ""


def _keys(text: str) -> str:
    """Return a JSON array of dict keys (or list indices)."""
    value = _load(text)
    if isinstance(value, dict):
        return json.dumps(list(value.keys()), ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps([str(i) for i in range(len(value))], ensure_ascii=False)
    return "[]"


def _get(text: str, path: str) -> str:
    """Resolve ``path`` inside ``text`` (a JSON document).

    Path segments are separated by ``.`` (matching QRDic). A single
    integer segment indexes into a list; a string segment indexes a
    dict. Returns an empty string on miss — what QRDic returns when
    a path is missing.
    """
    value = _load(text)
    if not path:
        return _dump(value)
    for raw in path.split("."):
        if isinstance(value, list):
            try:
                value = value[int(raw)]
            except (ValueError, IndexError):
                return ""
        elif isinstance(value, dict):
            if raw not in value:
                return ""
            value = value[raw]
        else:
            return ""
    return _dump(value)


def _add(text: str, value: str) -> str:
    """Append ``value`` to the JSON array in ``text``.

    If ``text`` isn't a valid array, replace it with ``[value]`` —
    matches QRDic's tolerant behaviour where ``$JSON 添加 X foo$``
    against an empty/malformed ``X`` initialises the array.
    """
    arr = _load(text)
    if not isinstance(arr, list):
        arr = []
    try:
        parsed: Any = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = value
    arr.append(parsed)
    return json.dumps(arr, ensure_ascii=False)


def _delete(text: str, key_or_idx: str) -> str:
    """Remove an element from a JSON array by index.

    Returns the new array as JSON text. Out-of-range indices are
    silently no-ops (QRDic ignores them rather than erroring).
    """
    arr = _load(text)
    if not isinstance(arr, list):
        return text
    try:
        idx = int(key_or_idx)
    except (TypeError, ValueError):
        return _dump(arr)
    if 0 <= idx < len(arr):
        del arr[idx]
    return json.dumps(arr, ensure_ascii=False)


@tool(
    name="json_op",
    dsl_name="JSON",
    description=(
        "QRDic JSON op dispatcher: $JSON 长度|获取|添加|删除|包含|键 ...$ — "
        "subcommand selects the operation."
    ),
    schema={
        "subcommand": "string",
        "text": "string",
        "arg": "string?",
    },
    safe=True,
    llm_visible=False,
)
async def json_op(
    ctx: ToolCtx, subcommand: str = "", text: str = "", arg: str = ""
) -> str:
    """Dispatch JSON operations by sub-command.

    The DSL writes ``$JSON 长度 <var>$`` / ``$JSON 获取 <var> <path>$``
    etc., so the parser hands us ``subcommand`` plus the remaining
    args. We dispatch internally to keep one DSL name slot per tool.
    """
    sub = subcommand.strip()
    if sub in ("长度", "length"):
        return _length(text)
    if sub in ("获取", "get"):
        return _get(text, arg)
    if sub in ("添加", "append", "add"):
        return _add(text, arg)
    if sub in ("删除", "remove", "delete"):
        return _delete(text, arg)
    if sub in ("包含", "contains", "has"):
        return _contains(text, arg)
    if sub in ("键", "keys"):
        return _keys(text)
    return ""
