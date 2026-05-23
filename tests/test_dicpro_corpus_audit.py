"""Corpus-level regression: every public handler in dicpro.txt should
either emit output or short-circuit cleanly on a synthesised input.

This is the "system is closed-loop" guarantee, expressed as a test:
if a future change to the parser, VM, or stdlib makes any of the 300+
production handlers raise instead of running, this fires before
deploy. We don't pin the *number* of ok-emits / ok-silent because
ruleset edits naturally shift those; we only require that vm-error
and python-error stay at zero.

Counts no-input cases separately because they're a limitation of the
test probe (regex triggers we can't synthesise input for), not the
system.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import Counter
from pathlib import Path

import linling_tools_stdlib  # noqa: F401 — registers stdlib tools
import pytest
from linling_core import Event, Scope, SqliteKVStore, TextSegment, User, registry
from linling_dsl.parser import parse
from linling_dsl.vm import VM, VMError

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "QRDic" / "dicpro.txt"


def _expand_simple_alternations(text: str) -> tuple[str, list[str]]:
    out: list[str] = []
    captures: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "(":
            close = _matching_paren(text, i)
            if close is None:
                out.append(text[i])
                i += 1
                continue
            body = text[i + 1 : close]
            if "|" in body and not any(c in body for c in "()[]?*+\\"):
                first = body.split("|", 1)[0]
                out.append(first)
                captures.append(first)
                i = close + 1
                continue
            out.append(text[i])
            i += 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out), captures


def _matching_paren(text: str, start: int) -> int | None:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _sample_input(trigger: str) -> tuple[str, list[str]] | None:
    text = trigger.strip()
    if not text:
        return None
    if not any(ch in text for ch in r".^$*+?{}[]()|\\"):
        return text, []
    placeholders = {
        r"([0-9]+)": "12345",
        r"(\d+)": "12345",
        r"(.*)": "tester",
        r"(.+)": "tester",
        r"([\s\S]+)": "tester",
        r"([\s\S]*)": "tester",
        r"(\\S+)": "tester",
    }
    sample = text
    captures: list[str] = []
    for token, value in placeholders.items():
        while token in sample:
            sample = sample.replace(token, value, 1)
            captures.append(value)
    sample, alt_caps = _expand_simple_alternations(sample)
    captures.extend(alt_caps)
    sample = sample.rstrip("\\n").rstrip("\n")
    pattern_text = text.rstrip("\\n").rstrip("\n")
    try:
        compiled = re.compile(pattern_text)
        m = compiled.fullmatch(sample)
        if m is None:
            return None
    except re.error:
        return None
    return sample, list(m.groups())


@pytest.mark.asyncio
async def test_no_handler_in_dicpro_raises_at_runtime() -> None:
    """Every public dicpro.txt handler must not raise during a dry run.

    Probe each handler with a synthesised input matching its trigger;
    accept any of:
      * ``ok-emits``    — produced text/image segments,
      * ``ok-silent``   — early-returned (typically a ``群号`` guard),
      * ``no-input``    — probe couldn't synthesise input (script
                          limitation, not a runtime issue).
    Reject anything that raised (``vm-error`` / ``python-error``).
    """
    if not RULES.exists():
        pytest.skip("QRDic/dicpro.txt not present in this checkout")

    rules = RULES.read_text(encoding="utf-8")
    script = parse(rules, strict=False)

    public = [h for h in script.handlers if not h.is_internal and h.trigger.strip()]

    counter: Counter[str] = Counter()
    failures: list[tuple[str, str]] = []

    kv = SqliteKVStore(bot_id="audit", db_path=":memory:")
    try:
        for h in public:
            sample = _sample_input(h.trigger)
            if sample is None:
                counter["no-input"] += 1
                continue
            text, captures = sample

            ev = Event(
                id=f"audit-{h.line}",
                platform="cli",
                bot_id="audit",
                # Use a non-main group id so handlers that guard on
                # ``群号==754800438`` actually proceed past the guard.
                scope=Scope(kind="group", id="11111", platform="cli"),
                sender=User(id="12345", platform="cli"),
                segments=[TextSegment(text=text)],
            )

            vm = VM(
                tool_registry=registry,
                kv=kv,
                bot_id="audit",
                max_steps=20_000,
                timeout_ms=2_000,
            )
            try:
                result = await vm.execute_handler(h, ev, captures=captures)
            except VMError as exc:
                counter["vm-error"] += 1
                failures.append((h.trigger, f"{type(exc).__name__}: {exc}"))
                continue
            except Exception as exc:  # noqa: BLE001 — auditor wants every error
                counter["python-error"] += 1
                failures.append((h.trigger, f"{type(exc).__name__}: {exc}"))
                continue

            text_out = "".join(s.text for s in result.segments if hasattr(s, "text"))
            if text_out.strip() or any(s.kind != "text" for s in result.segments):
                counter["ok-emits"] += 1
            else:
                counter["ok-silent"] += 1
    finally:
        with contextlib.suppress(Exception):
            await kv.close()

    error_count = counter.get("vm-error", 0) + counter.get("python-error", 0)
    assert error_count == 0, (
        f"{error_count} handlers raised during dry-run. First few:\n"
        + "\n".join(f"  [{trig}] {err}" for trig, err in failures[:10])
    )

    # Sanity: most handlers should be reachable. If we suddenly probe
    # 0 of them something is very wrong upstream.
    assert counter["ok-emits"] + counter["ok-silent"] > 200, dict(counter)
