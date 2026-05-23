"""Hot-path microbench: classifier match + VM dispatch on the real
QRDic ruleset under simulated multi-user concurrent load.

Timed paths:
  1) MessageClassifier.classify   — 1 event per call
  2) VM.execute_handler            — dispatching a small handler
  3) Concurrent gather             — N tasks racing classifier+VM

Rough reading guide: smaller numbers = better. The "ops/s" number is
the throughput a single Python process on this CPU can sustain.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from pathlib import Path

import linling_tools_stdlib  # noqa: F401 — register all tools
from linling_core import (
    Event,
    Scope,
    SqliteKVStore,
    TextSegment,
    User,
    registry,
)
from linling_core.classifier import MessageClassifier
from linling_dsl.parser import parse
from linling_dsl.vm import VM


def _event(text: str, sender_id: str = "u1") -> Event:
    return Event(
        id=f"e-{sender_id}-{text[:8]}",
        platform="cli",
        bot_id="susu",
        scope=Scope(kind="group", id="g1", platform="cli"),
        sender=User(id=sender_id, platform="cli"),
        segments=[TextSegment(text=text)],
    )


def bench_classifier(classifier: MessageClassifier) -> tuple[float, float]:
    triggers = ["背包", "查看消息", "我的灵玉", "上一页", "完全没有匹配的随机文本"]
    iters = 10_000
    t0 = time.perf_counter()
    for i in range(iters):
        classifier.classify(_event(triggers[i % len(triggers)]))
    t = time.perf_counter() - t0
    return t, iters / t


async def bench_vm(kv: SqliteKVStore) -> tuple[float, float]:
    body = """body
玉:$读 啊/灵玉系/灵玉 %QQ% 0$
%玉%"""
    script = parse("trigger\n" + body, strict=False)
    handler = script.handlers[0]

    iters = 1_000
    t0 = time.perf_counter()
    for i in range(iters):
        vm = VM(tool_registry=registry, kv=kv, bot_id="susu")
        await vm.execute_handler(handler, _event("trigger", sender_id=f"u{i}"))
    t = time.perf_counter() - t0
    return t, iters / t


async def bench_concurrent(
    kv: SqliteKVStore, classifier: MessageClassifier, *, concurrency: int
) -> tuple[float, float]:
    body = """body
玉:$读 啊/灵玉系/灵玉 %QQ% 0$
%玉%"""
    script = parse("trigger\n" + body, strict=False)
    handler = script.handlers[0]

    async def one_user(uid: str) -> None:
        vm = VM(tool_registry=registry, kv=kv, bot_id="susu")
        for _ in range(50):
            classifier.classify(_event("背包", sender_id=uid))
            await vm.execute_handler(handler, _event("trigger", sender_id=uid))

    iters = concurrency * 50
    t0 = time.perf_counter()
    await asyncio.gather(*(one_user(f"u{i}") for i in range(concurrency)))
    t = time.perf_counter() - t0
    return t, iters / t


async def main() -> None:
    rules_path = Path("QRDic/dicpro.txt")
    rules = rules_path.read_text(encoding="utf-8")
    script = parse(rules, strict=False)
    classifier = MessageClassifier(script)
    kv = SqliteKVStore(bot_id="susu", db_path=":memory:")

    print(f"loaded {len(script.handlers)} handlers")
    print(f"  literal_index size = {len(classifier._literal_index)}")
    print(f"  regex_list size    = {len(classifier._regex_list)}")
    print()

    print("== classifier (10k events, mix of 5 triggers, 1 unmatched) ==")
    runs = [bench_classifier(classifier) for _ in range(3)]
    for t, ops in runs:
        print(f"  {t:.3f}s  -> {ops:>10,.0f} ops/s")
    print(f"  median: {statistics.median([r[1] for r in runs]):.0f} ops/s")
    print()

    print("== VM execute_handler (1k sequential) ==")
    runs2 = [await bench_vm(kv) for _ in range(3)]
    for t, ops in runs2:
        print(f"  {t:.3f}s  -> {ops:>10,.0f} ops/s")
    print(f"  median: {statistics.median([r[1] for r in runs2]):.0f} ops/s")
    print()

    print("== concurrent 16 users x 50 msgs (classify+VM round-trip) ==")
    runs3 = [await bench_concurrent(kv, classifier, concurrency=16) for _ in range(3)]
    for t, ops in runs3:
        print(f"  {t:.3f}s  -> {ops:>10,.0f} ops/s")
    print(f"  median: {statistics.median([r[1] for r in runs3]):.0f} ops/s")
    print()

    print("== concurrent 64 users x 50 msgs ==")
    runs4 = [await bench_concurrent(kv, classifier, concurrency=64) for _ in range(3)]
    for t, ops in runs4:
        print(f"  {t:.3f}s  -> {ops:>10,.0f} ops/s")
    print(f"  median: {statistics.median([r[1] for r in runs4]):.0f} ops/s")

    await kv.close()


if __name__ == "__main__":
    asyncio.run(main())
