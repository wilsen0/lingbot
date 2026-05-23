"""Platform adapter protocol.

All adapter packages (``linling-adapter-onebot``, ``linling-adapter-cli``,
test doubles) implement :class:`Adapter` implicitly. Because it's a
``Protocol``, adapter classes don't inherit from anything — they just
need methods with matching signatures.

The minimum surface:

* ``platform``: a string label matching :attr:`Scope.platform` on the
  actions the adapter should receive. One adapter per platform is the
  common case.
* ``send(action) -> Awaitable[...] | Any``: deliver one outbound
  action. Sync ``send`` is accepted — the bootstrap's sink wraps it.
* ``run() -> Awaitable[None]`` (optional): the adapter's own main
  loop (polling / WebSocket listen / stdin read). The bootstrap
  schedules it as a Task when :meth:`RunningBot.start` is called.
* ``stop() -> Awaitable[None] | None`` (optional): graceful shutdown
  hook invoked before the task is cancelled.

We keep the Protocol ``runtime_checkable`` so ``isinstance`` works in
ad-hoc wiring code, but all actual hotpaths dispatch on ``platform``
alone — a lightweight duck typed label — rather than full structural
matching.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol, runtime_checkable

from linling_core.events import Action


@runtime_checkable
class Adapter(Protocol):
    """Duck-typed platform adapter surface."""

    platform: str

    def send(self, action: Action) -> Awaitable[Any] | Any: ...

    # ``run`` / ``stop`` are optional; use ``getattr`` at call sites to
    # tolerate adapters that omit them (e.g. pure send-only sinks).
