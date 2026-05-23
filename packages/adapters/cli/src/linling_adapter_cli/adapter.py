"""CLI adapter for local debugging.

Reads lines from stdin, converts them to Events, and publishes to the
EventBus. Subscribes to Actions and prints formatted output to stdout.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from linling_core.events import Action, Event, Scope, User
from linling_core.segments import AtSegment, ImageSegment, TextSegment

if TYPE_CHECKING:
    from linling_core.bus import EventBus


class CliAdapter:
    """Minimal CLI adapter for interactive debugging."""

    platform = "cli"

    def __init__(
        self,
        bus: EventBus,
        *,
        bot_id: str = "linling",
        user_id: str = "cli-user",
    ) -> None:
        self._bus = bus
        self._bot_id = bot_id
        self._user_id = user_id
        self._counter = 0
        self._scope = Scope(kind="group", id="cli-group", platform="cli")

    async def run(self) -> None:
        """Main loop: read stdin lines, parse, and publish as Events."""
        loop = asyncio.get_running_loop()
        while True:
            sys.stdout.write("> ")
            sys.stdout.flush()
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                # EOF (Ctrl+D)
                break
            line = line.rstrip("\n")
            if not line:
                continue

            # Handle special commands
            if line.startswith("/dm "):
                target = line[4:].strip() or self._user_id
                self._scope = Scope(kind="dm", id=target, platform="cli")
                continue
            if line.startswith("/group "):
                target = line[7:].strip() or "cli-group"
                self._scope = Scope(kind="group", id=target, platform="cli")
                continue

            # Build event
            self._counter += 1
            event = self._build_event(line)
            await self._bus.publish(event)

    def send(self, action: Action) -> None:
        """Format an Action and print to stdout."""
        parts: list[str] = []
        for seg in action.segments:
            if isinstance(seg, TextSegment):
                parts.append(seg.text)
            elif isinstance(seg, ImageSegment):
                url_or_path = seg.url or seg.path or seg.b64 or "?"
                parts.append(f"[图片: {url_or_path}]")
            elif isinstance(seg, AtSegment):
                parts.append(f"@{seg.user_id}")
            else:
                parts.append(f"[{seg.kind}: ...]")
        output = "".join(parts)
        sys.stdout.write(f"🤖 {output}\n")
        sys.stdout.flush()

    def _build_event(self, line: str) -> Event:
        """Parse a line into an Event, handling @user syntax."""
        segments: list[TextSegment | AtSegment] = []
        sender_id = self._user_id

        if line.startswith("@") and " " in line:
            at_part, rest = line.split(" ", 1)
            target_user = at_part[1:]  # strip leading @
            segments.append(AtSegment(user_id=target_user))
            segments.append(TextSegment(text=rest))
            sender_id = target_user
        else:
            segments.append(TextSegment(text=line))

        return Event(
            id=f"cli-{self._counter}",
            platform="cli",
            bot_id=self._bot_id,
            scope=self._scope,
            sender=User(id=sender_id, platform="cli", display_name="you"),
            segments=segments,
        )
