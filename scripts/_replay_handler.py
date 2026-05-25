"""Replay a single ``main.ling`` handler for a given inbound message.

Useful when audit shows ``command:implicit-trigger`` with sub-millisecond
latency (= silent early-return) and you need to know which guard inside
the handler body actually fired. Loads the live KV store, the live
rule set, and runs the handler with realistic context vars (`%QQ%`,
`%群号%`, `%括号N%`, `%管理员%` from bot.yaml).

Usage::

    uv run python scripts/_replay_handler.py 苏苏 \\
        --user 2986537370 --scope 271891388

The script prints:
* the matched handler trigger,
* every output segment the VM produced (TextSegment / ImageSegment / …),
* a final summary so an empty-output run is obvious.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from linling_core.classifier import MessageClassifier
from linling_core.config import BotConfig
from linling_core.events import Event, Scope, User
from linling_core.segments import ImageSegment, TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import registry

# Side-effect: register every built-in tool in the registry.
import linling_core.tools_builtin  # noqa: F401
import linling_tools_stdlib  # noqa: F401
from linling_dsl.parser import parse
from linling_dsl.vm import VM


REPO = Path(__file__).resolve().parents[1]


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("text", help="inbound message text (e.g. '苏苏')")
    p.add_argument("--user", required=True, help="sender user id")
    p.add_argument("--scope", required=True, help="scope id (group id or DM)")
    p.add_argument("--scope-kind", default="group", choices=("group", "dm"))
    p.add_argument(
        "--bot-yaml",
        type=Path,
        default=REPO / "bot" / "bot.yaml",
        help="bot.yaml to read for KV path / admin / etc.",
    )
    p.add_argument(
        "--rules",
        type=Path,
        default=REPO / "bot" / "rules" / "main.ling",
    )
    args = p.parse_args()

    cfg = BotConfig.from_yaml(args.bot_yaml)
    # Resolve KV path relative to bot.yaml's directory (mirrors bootstrap).
    kv_uri = cfg.storage.kv
    db_path = kv_uri.replace("sqlite:///", "")
    db_full = (args.bot_yaml.parent / db_path).resolve()
    if not db_full.is_file():
        print(f"KV not found: {db_full}", file=sys.stderr)
        sys.exit(2)

    print(f"# loading rules: {args.rules}")
    script = parse(args.rules.read_text(encoding="utf-8"), strict=False)
    classifier = MessageClassifier(script)

    ev = Event(
        id="replay",
        platform="onebot",
        bot_id=cfg.bot_id,
        scope=Scope(kind=args.scope_kind, id=args.scope, platform="onebot"),
        sender=User(id=args.user, platform="onebot"),
        segments=[TextSegment(text=args.text)],
    )

    intent = classifier.classify(ev)
    print(f"# classifier verdict: kind={intent.kind} reason={intent.reason}")
    if intent.kind != "command" or intent.match is None:
        print("# (no DSL handler matched)")
        return

    print(f"# matched trigger: {intent.match.handler.trigger!r}")
    print(f"# captures: {intent.match.captures}")

    kv = SqliteKVStore(bot_id=cfg.bot_id, db_path=db_full)
    try:
        vm = VM(
            tool_registry=registry,
            kv=kv,
            bot_id=cfg.bot_id,
            extras={"admin_users": tuple(cfg.admin_users)},
        )
        result = await vm.execute_handler(
            intent.match.handler, ev, captures=intent.match.captures
        )
    finally:
        await kv.close()

    print(f"# segments emitted: {len(result.segments)}")
    for i, seg in enumerate(result.segments):
        if isinstance(seg, TextSegment):
            print(f"  [{i}] text: {seg.text!r}")
        elif isinstance(seg, ImageSegment):
            print(f"  [{i}] image: url={seg.url!r}")
        else:
            print(f"  [{i}] {type(seg).__name__}: {seg!r}")

    if not result.segments:
        print("\n!! HANDLER EARLY-RETURNED WITHOUT EMITTING ANYTHING !!")
        print(
            "Look for ``如果:... 返回 / 如果尾`` blocks inside the handler body "
            "and trace which guard fired against the live KV state."
        )


asyncio.run(main())
