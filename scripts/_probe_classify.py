"""Classify a few candidate WebUI inputs against the live rules to find one
that lands on chat:fallback (i.e. actually exercises the LLM)."""
import asyncio
import sys
from pathlib import Path

from linling_core.classifier import MessageClassifier
from linling_core.events import Event, Scope, User
from linling_core.segments import TextSegment
from linling_dsl.parser import parse


async def main() -> None:
    rules = Path("bot/rules/main.ling").read_text(encoding="utf-8")
    script = parse(rules, strict=False)
    classifier = MessageClassifier(script=script)

    candidates = sys.argv[1:] or [
        "今天天气怎么样",
        "讲个冷笑话",
        "你好啊",
        "我有点伤心",
        "嗯嗯",
        "随便聊聊",
        "在吗",
        "吃了吗",
        "做朋友吗",
        "讲个故事",
    ]
    for text in candidates:
        ev = Event(
            id="probe",
            platform="webui",
            bot_id="linling",
            scope=Scope(kind="dm", id="0", platform="webui"),
            sender=User(id="admin", platform="webui"),
            kind="message",
            segments=[TextSegment(text=text)],
        )
        intent = classifier.classify(ev)
        match = ""
        if intent.match is not None:
            match = f" trigger={intent.match.handler.trigger!r}"
        print(f"{text!r:30s} -> kind={intent.kind} reason={intent.reason}{match}")


asyncio.run(main())
