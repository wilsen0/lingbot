"""Helpers to wire a running FastAPI app to linling subsystems.

The WebUI can be used standalone (for a static SPA preview) or embedded
alongside a running bot process. These helpers provide a clean seam for
the latter without forcing every caller to know the internal shape of
`WebUIState`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from linling_webui.state import BotInfo

if TYPE_CHECKING:
    from linling_agent.bridge import AgentRegistry
    from linling_core.bus import EventBus
    from linling_core.scheduler import Scheduler
    from linling_core.storage.kv import KVStore


def wire_bot(
    app: FastAPI,
    *,
    bot_id: str,
    platform: str = "unknown",
    name: str = "",
    kv: KVStore | None = None,
    bus: EventBus | None = None,
    scheduler: Scheduler | None = None,
) -> None:
    """Register a bot's runtime components with the WebUI state."""
    state = app.state.runtime
    state.register_bot(BotInfo(id=bot_id, platform=platform, name=name, online=True))
    if kv is not None:
        state.kv_stores[bot_id] = kv
    if bus is not None:
        state.bus = bus
    if scheduler is not None:
        state.scheduler = scheduler


def wire_agents(app: FastAPI, registry: AgentRegistry) -> None:
    """Attach the agent registry (shared across bots)."""
    app.state.runtime.agent_registry = registry


def wire_hot_reload(app: FastAPI, callback: Any) -> None:
    """Attach a hot-reload callable: `async (bot_id: str) -> dict`.

    The callable should return something like {"reloaded": int, "errors": [...]}
    which will be forwarded to the WebUI client.
    """
    app.state.runtime.hot_reload_callback = callback
