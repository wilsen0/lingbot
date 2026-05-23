"""Audit every public handler in dicpro.txt by dry-running it.

For each non-internal handler we:

* synthesise an Event whose text matches the trigger (literal triggers
  are sent verbatim; regex triggers get a best-effort canned input
  derived from the trigger pattern),
* execute the handler against a scratch in-memory KV store,
* classify the outcome:
  ``ok-emits``      — produced ≥1 text segment
  ``ok-silent``     — ran cleanly but emitted nothing (e.g. early ``返回``)
  ``vm-error``      — VM raised (sandbox limit, undefined var, etc.)
  ``no-input``      — couldn't construct a sample input for the regex trigger

The script prints one line per handler plus a summary breakdown so an
operator can immediately see how many handlers fail to behave as
intended. A 100% pass rate isn't the goal — many QRDic rules guard on
``群号==754800438`` and intentionally ``返回`` for everything else, so
``ok-silent`` is healthy. ``vm-error`` is the bucket that needs eyes.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
from collections import Counter
from pathlib import Path

import linling_tools_stdlib  # noqa: F401 — registers all stdlib tools
from linling_core import (
    Event,
    Scope,
    SqliteKVStore,
    TextSegment,
    User,
    registry,
)
from linling_dsl.parser import parse
from linling_dsl.vm import VM, VMError

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "QRDic" / "dicpro.txt"


def _sample_input(trigger: str) -> tuple[str, list[str]] | None:
    """Generate a canned (event_text, captures) pair for ``trigger``.

    Literal triggers map to themselves with no captures.

    Regex triggers get best-effort substitution: we walk the pattern
    and replace the common QRDic capture groups with placeholders that
    Python's ``re.fullmatch`` accepts. If a trigger uses something
    unusual (e.g. complex character classes) we bail and the auditor
    records ``no-input`` rather than guess wrong.
    """
    text = trigger.strip()
    if not text:
        return None
    # Cheap literal check — if the trigger has no *dynamic* regex
    # metacharacters (``.^$*+?{}()|\\``) we pass it through as-is.
    # ``[`` / ``]`` alone don't disqualify because QRSpeed's
    # ``[戳一戳]``-shape triggers are bracket literals, not character
    # classes — the classifier treats them the same way.
    if not any(ch in text for ch in r".^$*+?{}()|\\"):
        return text, []

    # Substitute the canonical capture groups with placeholder values
    # that match their character class.
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

    # Resolve simple ``(a|b|c)`` alternations by picking the first
    # branch. Only safe when the group has no capture-target metachars
    # itself (we deliberately won't recurse).
    sample, alt_captures = _expand_simple_alternations(sample)
    captures.extend(alt_captures)

    # Strip a trailing ``\n`` literal that some QRDic triggers carry —
    # those are display artifacts in the source file, not part of
    # what the user types.
    sample = sample.rstrip("\\n").rstrip("\n")
    text_pattern = text.rstrip("\\n").rstrip("\n")

    try:
        compiled = re.compile(text_pattern)
        m = compiled.fullmatch(sample)
        if m is None:
            return None
    except re.error:
        return None
    return sample, list(m.groups())


def _expand_simple_alternations(text: str) -> tuple[str, list[str]]:
    """Replace ``(a|b|c)`` groups with their first branch.

    Returns the rewritten text plus any captures we added — picking
    the first branch in order of appearance keeps the regex match
    deterministic.
    """
    out: list[str] = []
    i = 0
    captures: list[str] = []
    while i < len(text):
        ch = text[i]
        if ch == "(":
            close = _find_matching_paren(text, i)
            if close is None:
                out.append(ch)
                i += 1
                continue
            body = text[i + 1 : close]
            # Only handle plain ``a|b|c`` (no nested groups, no special
            # constructs). Anything else falls back unchanged.
            if "|" in body and not any(c in body for c in "()[]?*+\\"):
                first = body.split("|", 1)[0]
                out.append(first)
                captures.append(first)
                i = close + 1
                continue
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), captures


def _find_matching_paren(text: str, start: int) -> int | None:
    """Index of the matching ``)`` for an opening paren at ``start``."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


async def main() -> int:
    rules = RULES.read_text(encoding="utf-8")
    script = parse(rules, strict=False)

    public = [h for h in script.handlers if not h.is_internal and h.trigger.strip()]
    print(f"loaded {len(script.handlers)} handlers; {len(public)} public")
    print()

    counter: Counter[str] = Counter()
    failures: list[tuple[str, str]] = []  # (trigger, error message)
    silents: list[str] = []
    no_inputs: list[str] = []

    kv = SqliteKVStore(bot_id="audit", db_path=":memory:")
    try:
        # Seed a couple of values that QRDic rules read while routing.
        await kv.write("啊", "灵玉系/灵玉", "12345", "100")
        await kv.write("啊", "禁言系/妖力", "12345", "5")
        await kv.write("啊", "活动系/玫瑰花", "12345", "2")

        for h in public:
            sample = _sample_input(h.trigger)
            if sample is None:
                counter["no-input"] += 1
                no_inputs.append(h.trigger)
                continue
            text, captures = sample

            ev = Event(
                id=f"audit-{h.line}",
                platform="cli",
                bot_id="audit",
                scope=Scope(kind="group", id="754800438", platform="cli"),
                # 754800438 is QRDic's "main group" — most rules check
                # this and short-circuit. We actually want them to run,
                # so use a different group id when possible.
                sender=User(id="12345", platform="cli"),
                segments=[TextSegment(text=text)],
            )
            ev = ev.model_copy(
                update={
                    "scope": Scope(kind="group", id="11111", platform="cli"),
                }
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
            except Exception as exc:  # noqa: BLE001 — auditor: we want every error
                counter["python-error"] += 1
                failures.append((h.trigger, f"{type(exc).__name__}: {exc}"))
                continue

            text_out = "".join(s.text for s in result.segments if hasattr(s, "text"))
            if text_out.strip() or any(s.kind != "text" for s in result.segments):
                counter["ok-emits"] += 1
            else:
                counter["ok-silent"] += 1
                silents.append(h.trigger)
    finally:
        with contextlib.suppress(Exception):
            await kv.close()

    print("=== summary ===")
    for k, v in counter.most_common():
        print(f"  {k:>14s}: {v}")
    print()

    if failures:
        print(f"=== first 30 vm/python errors (out of {len(failures)}) ===")
        for trig, err in failures[:30]:
            print(f"  [{trig[:40]:40s}] {err[:120]}")
        print()

    if no_inputs:
        print(f"=== sample of triggers with no synthesisable input ({len(no_inputs)} total) ===")
        for trig in no_inputs[:15]:
            print(f"  {trig!r}")

    return 0 if counter.get("vm-error", 0) + counter.get("python-error", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
