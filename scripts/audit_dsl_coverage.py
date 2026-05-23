"""Print every ``$tool$`` head referenced in dicpro.txt and whether
the runtime has a handler registered for it.

Run after rule changes (or when adding new built-in tools) to spot
unwired references — the kind that produce a silent empty string at
runtime instead of a clear error.

    uv run python scripts/audit_dsl_coverage.py

Output columns: count, status, tool name. ``status`` is one of:

* ``OK``        — registered with a Python implementation
* ``MISSING``   — referenced in dicpro.txt but no DSL ``dsl_name`` entry
* ``STUB``      — registered but the implementation is a logged no-op
                  (currently: ``读文件`` ``写文件`` ``词库操作`` ``执行`` ``BSH``
                  plus the adapter-RPC stubs that need a live OneBot)

Exits 0 if every used tool is at least registered, 1 if any are
``MISSING`` so CI can catch regressions.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import linling_tools_stdlib  # noqa: F401 — registers stdlib tools on import
from linling_core.tools import registry

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "QRDic" / "dicpro.txt"

# Registered tools whose implementation is intentionally a no-op stub.
# ``$BSH$`` / ``$执行$`` are *refusals* (script-eval is unsafe).
# The adapter RPC group needs a live OneBot client and degrades to
# logged calls until an adapter is wired into ``ctx.extras``.
_KNOWN_STUBS = frozenset(
    {
        "BSH",
        "执行",
        "读文件",
        "写文件",
        "词库操作",
        "撤回",
        "禁",
        "全体禁言",
        "设置群状态",
        "退出群",
        "申请群",
        "改",
        "群头衔",
        "访问",  # http_get is currently a placeholder (returns empty)
    }
)


def _extract_tool_heads(source: str) -> Counter[str]:
    """Return a Counter of ``$head args$`` heads found in ``source``.

    We pull out every ``$...$`` block, strip the delimiters, take the
    first whitespace-separated token, and drop the obvious
    non-tool shapes (jump targets, comparison fragments, label refs).
    """
    counter: Counter[str] = Counter()
    for match in re.finditer(r"\$([^$\s][^$]*?)\$", source):
        inner = match.group(1).strip()
        if not inner:
            continue
        head = inner.split(" ", 1)[0]
        if not head:
            continue
        # Comparison fragments inside conditions sometimes get caught
        # because ``$...$`` only delimits *tool* calls when not nested
        # inside a ``如果:`` line. Skip the obvious non-names.
        if head[0] in "%[":
            continue
        if re.match(r"^[<>=!]", head):
            continue
        if head in {"jump", "跳"}:
            continue
        counter[head] += 1
    return counter


def main() -> int:
    if not RULES.exists():
        print(f"can't find {RULES}", file=sys.stderr)
        return 2
    counter = _extract_tool_heads(RULES.read_text(encoding="utf-8"))

    rows: list[tuple[int, str, str]] = []
    missing: list[str] = []
    for name, count in counter.most_common():
        td = registry.get_by_dsl_name(name) or registry.get(name)
        if td is None:
            status = "MISSING"
            missing.append(name)
        elif name in _KNOWN_STUBS:
            status = "STUB"
        else:
            status = "OK"
        rows.append((count, status, name))

    width = max(len(name) for _, _, name in rows) if rows else 4
    print(f"{'count':>6}  {'status':<8}  name")
    print("-" * (6 + 2 + 8 + 2 + width))
    for count, status, name in rows:
        print(f"{count:6d}  {status:<8}  {name}")

    print()
    if missing:
        print(f"✗ {len(missing)} tool name(s) referenced but NOT registered:")
        for name in missing:
            print(f"  - {name}")
        return 1
    print("✓ every referenced tool has a registered handler")
    return 0


if __name__ == "__main__":
    sys.exit(main())
