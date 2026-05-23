"""Corpus-level guard: every ``$tool$`` referenced in dicpro.txt must
have a registered DSL implementation.

A regression here means a new rule (or a port of an old one) referenced
a tool name that the registry doesn't know about. Such calls would
silently return the empty string at runtime — invisible until a user
hits the rule and gets a broken bubble.

This test re-uses :mod:`scripts.audit_dsl_coverage` so the script
output and the CI gate stay in sync. STUB and OK statuses both pass —
we only fail on MISSING.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import linling_tools_stdlib  # noqa: F401 — registers stdlib tools on import
from linling_core.tools import registry

ROOT = Path(__file__).resolve().parents[1]
DICPRO = ROOT / "QRDic" / "dicpro.txt"


def _extract_tool_heads(source: str) -> Counter[str]:
    """Same extraction used by ``scripts/audit_dsl_coverage.py``."""
    counter: Counter[str] = Counter()
    for match in re.finditer(r"\$([^$\s][^$]*?)\$", source):
        inner = match.group(1).strip()
        if not inner:
            continue
        head = inner.split(" ", 1)[0]
        if not head:
            continue
        if head[0] in "%[":
            continue
        if re.match(r"^[<>=!]", head):
            continue
        if head in {"jump", "跳"}:
            continue
        counter[head] += 1
    return counter


def test_every_referenced_tool_is_registered() -> None:
    assert DICPRO.is_file(), f"dicpro.txt missing at {DICPRO}"
    counter = _extract_tool_heads(DICPRO.read_text(encoding="utf-8"))
    missing = [
        name
        for name in counter
        if registry.get_by_dsl_name(name) is None
        and registry.get(name) is None
    ]
    assert not missing, (
        "dicpro.txt references DSL tools with no registered implementation. "
        "Either add them to packages/tools-stdlib or remove the reference. "
        f"Missing: {missing}"
    )
