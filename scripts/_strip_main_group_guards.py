"""Strip every ``%主群%`` reference out of a .ling file.

We're removing the legacy QRDic "sibling-bot territory / route-tracker
home" semantics from the project entirely. After this script runs:

* Every standalone ``如果:%群号%==%主群%\\n返回\\n如果尾`` block is
  deleted (handler now fires everywhere).
* Every standalone ``如果:%群号%!=%主群%\\n返回\\n如果尾`` block is
  deleted (handler used to be gated to ``%主群%`` only — now fires
  everywhere; a follow-up may reintroduce a different config key if
  scoped behaviour is needed).
* Compound conditions that include a ``%主群%`` clause have that
  clause stripped while keeping the rest of the boolean intact:
  ``%群号%==%主群%|%群号%==待更替群`` → ``%群号%==待更替群``.
  ``!=%主群%|%QQ%==%Robot%`` → ``%QQ%==%Robot%``.
* Compound bodies whose only condition is the ``%主群%`` clause
  collapse to a no-op ``如果:0==1`` so the surrounding ``返回`` / body
  block becomes unreachable instead of always-true. (We keep the
  block structure to avoid accidentally orphaning ``如果尾`` — a
  follow-up cleanup pass can prune dead blocks if any remain.)

We do NOT touch lines that don't mention ``%主群%`` so
unrelated handler structure is preserved.

Usage::

    python scripts/_strip_main_group_guards.py             # rewrite
    python scripts/_strip_main_group_guards.py --dry-run   # report
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_LING = REPO / "bot" / "rules" / "main.ling"

_GUARD_PATTERN = "%主群%"


def _strip_main_group_clauses(condition: str) -> str:
    """Remove every ``...%主群%...`` boolean clause from ``condition``.

    The DSL boolean uses ``&`` for AND and ``|`` for OR — same shape
    as QRSpeed's. A clause is the maximal substring not containing
    ``&`` or ``|``. We split on those operators, drop the clauses
    that mention ``%主群%``, and re-join with whatever connector was
    between them.

    Returns ``""`` when every clause referenced ``%主群%`` (caller
    decides what to do — typically replace with a never-true
    sentinel).
    """
    # Split keeping the operators so we can re-emit them.
    parts = re.split(r"([&|])", condition)
    kept: list[str] = []
    for part in parts:
        if part in ("&", "|"):
            kept.append(part)
            continue
        if _GUARD_PATTERN in part:
            # Drop the clause; we'll clean up the dangling operator
            # in the post-pass below.
            kept.append("")
        else:
            kept.append(part)

    # Sweep: an operator preceded or followed by an empty clause is
    # itself dropped; an empty clause at the boundary disappears.
    cleaned: list[str] = []
    for token in kept:
        if token == "":
            # Drop the *previous* operator if any.
            if cleaned and cleaned[-1] in ("&", "|"):
                cleaned.pop()
            continue
        if token in ("&", "|") and not cleaned:
            # Operator at start without a left clause; drop.
            continue
        cleaned.append(token)

    # Trailing operator without a right clause (because we dropped
    # the last clause): pop it.
    while cleaned and cleaned[-1] in ("&", "|"):
        cleaned.pop()

    return "".join(cleaned)


def _is_block_open(line: str) -> bool:
    return line.startswith("如果:")


def _is_block_close(line: str) -> bool:
    return line.strip() == "如果尾"


def transform(text: str) -> tuple[str, dict[str, int]]:
    """Apply all three strip rules to ``text``.

    Returns the rewritten text and a counter dict for reporting.
    """
    lines = text.split("\n")
    out: list[str] = []
    counts = {
        "guard_blocks_removed": 0,
        "compound_clauses_stripped": 0,
        "fully_empty_conditions_neutralised": 0,
    }

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("如果:") and _GUARD_PATTERN in stripped:
            condition = stripped[len("如果:") :]
            new_cond = _strip_main_group_clauses(condition)

            # Look ahead for the matching 如果尾 (single-level only;
            # the existing rules don't nest 如果 blocks inside the
            # %主群% guards we're touching).
            j = i + 1
            depth = 1
            while j < len(lines) and depth > 0:
                nxt = lines[j].strip()
                if nxt.startswith("如果:"):
                    depth += 1
                elif nxt == "如果尾":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1

            body_lines = lines[i + 1 : j]  # everything between 如果: and 如果尾
            body_text = "\n".join(body_lines).strip()

            # Case A: full guard collapsed (every clause was %主群%) AND
            # the body is just `返回`. Drop the entire 3-line
            # block — handler proceeds past where the guard used to
            # be.
            if new_cond == "" and body_text == "返回":
                counts["guard_blocks_removed"] += 1
                # Drop lines i..j inclusive (条件 + body + 如果尾).
                i = j + 1
                continue

            # Case B: compound condition lost its %主群% clause but
            # something else remains. Keep the block structure with
            # the trimmed condition.
            if new_cond:
                out.append(line.replace(stripped, f"如果:{new_cond}"))
                counts["compound_clauses_stripped"] += 1
                i += 1
                continue

            # Case C: condition is now empty but body is non-trivial
            # (i.e. the guard wasn't a simple ``返回`` — the original
            # block did real work only inside %主群%). Replace the
            # condition with a never-true sentinel so the body
            # becomes unreachable. This is intentional: the rule
            # previously only fired in %主群%, so removing %主群%
            # semantics means it should never fire. A follow-up pass
            # may delete the dead block; we play it safe today.
            counts["fully_empty_conditions_neutralised"] += 1
            out.append(line.replace(stripped, "如果:0==1"))
            i += 1
            continue

        out.append(line)
        i += 1

    return "\n".join(out), counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_LING,
        help="Path to the .ling file (default: bot/rules/main.ling)",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if not args.path.is_file():
        print(f"not found: {args.path}", file=sys.stderr)
        return 2

    original = args.path.read_text(encoding="utf-8")
    new, counts = transform(original)

    print(f"file: {args.path.relative_to(REPO)}")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    remaining = sum(1 for line in new.split("\n") if _GUARD_PATTERN in line)
    if remaining:
        print(f"\nWARNING: {remaining} lines still mention {_GUARD_PATTERN}")
        for n, line in enumerate(new.split("\n"), 1):
            if _GUARD_PATTERN in line:
                print(f"  L{n}: {line.strip()}")

    if not args.dry_run:
        args.path.write_text(new, encoding="utf-8")
        print(f"wrote {args.path.relative_to(REPO)}")
    return 0 if remaining == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
