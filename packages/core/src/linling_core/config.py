"""Bot configuration system.

Loads configuration from YAML files with environment variable expansion,
layered with pydantic-settings for env var / .env overrides.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# Pattern for ${VAR} or ${VAR:-default} expansion.
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")
# Cap how many passes :func:`expand_env` makes when chasing nested
# defaults like ``${A:-${B:-c}}``. Real-world configs nest at most a
# couple of levels; the cap exists to bound a pathological loop where
# an env value itself contains ``${...}`` referencing a still-unset
# variable.
_MAX_EXPANSION_PASSES = 8


def expand_env(value: str) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` patterns in a string.

    The two-form syntax mirrors POSIX shell parameter expansion:

    * ``${VAR}`` — substitute the environment variable's value, or
      leave the original token in place if unset (so misconfiguration
      is loud rather than silently filling in an empty string).
    * ``${VAR:-default}`` — substitute the variable's value, or
      ``default`` when unset / empty.

    Expansion runs iteratively until the result stabilises, so an
    env var whose value happens to contain another ``${...}`` token
    still resolves correctly. The regex itself does **not** support
    nested ``${...}`` *within the same token* (e.g.
    ``${A:-${B}}`` — write that as two flat fallbacks at the YAML
    level instead).

    Reused by every YAML loader in the codebase
    (:meth:`BotConfig.from_yaml`, :meth:`AgentDef.from_yaml`, …) so
    operators have a single, consistent interpolation grammar.
    """

    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        if ":-" in expr:
            var_name, default = expr.split(":-", 1)
            return os.environ.get(var_name, default)
        return os.environ.get(expr, match.group(0))

    # Iterate to a fixed point so an env var whose value contains
    # ``${OTHER}`` resolves through. Bounded by
    # ``_MAX_EXPANSION_PASSES`` to keep a pathological self-
    # referential value from looping forever; in practice we stop
    # after one or two passes.
    for _ in range(_MAX_EXPANSION_PASSES):
        next_value = _ENV_PATTERN.sub(_replace, value)
        if next_value == value:
            return next_value
        value = next_value
    return value


def expand_env_recursive(obj: Any) -> Any:
    """Recursively expand env vars in a nested structure (dict / list / str)."""
    if isinstance(obj, str):
        return expand_env(obj)
    if isinstance(obj, dict):
        return {k: expand_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_recursive(item) for item in obj]
    return obj


# Backwards-compatible aliases. The leading-underscore names predate
# the cross-package use case (``AgentDef.from_yaml`` calls the public
# spelling). Kept around so any out-of-tree caller that imported them
# doesn't break on upgrade.
_expand_env = expand_env
_expand_env_recursive = expand_env_recursive


class AdapterConfig(BaseModel):
    """Configuration for a single adapter instance."""

    kind: str  # "onebot" | "cli"
    ws_url: str = "ws://127.0.0.1:8080"
    access_token: str = ""


class StorageConfig(BaseModel):
    """Storage backend configuration."""

    kv: str = "sqlite:///./data/kv.db"
    files: str = "./data/files"
    audit: str | None = None
    """Where to persist the audit log.

    * ``None`` (default) — keep audit rows in memory only. Easiest
      starting point; rows die with the process.
    * ``sqlite:///path/to/audit.db`` — durable SQLite. The WebUI's
      ``/api/audit`` endpoint serves results out of this file, and
      ``RuleSummary`` aggregations remain accurate across restarts.

    Future backends (Postgres, Loki) plug in here without code
    changes elsewhere.
    """

    scheduler: str | None = None
    """Where to persist scheduled tasks.

    * ``None`` (default) — :class:`MemorySchedulerStore`; pending
      delays / recurring jobs die with the process. Fine for dev.
    * ``sqlite:///path/to/scheduler.db`` — :class:`SqliteSchedulerStore`.
      Tasks survive restart; overdue tasks fire on next ``run()``.

    Set this in production for any bot that uses ``$调用$`` delays or
    config-driven recurring jobs.
    """


class ClassifierConfig(BaseModel):
    """Message classifier settings.

    ``command_prefixes`` is the set of text prefixes that force the
    message to resolve as a command (DSL handler). Default covers
    Slack-style ``/`` and Unix-style ``!``. Set to ``[]`` to disable
    prefix-mode and rely entirely on implicit DSL triggers (the QRDic
    default behaviour, where ``我的灵玉`` matches without any prefix).

    ``block_scope_ids`` / ``block_sender_ids`` let operators silence a
    specific group / user without touching the ruleset — useful for
    SLA-style banning.
    """

    command_prefixes: list[str] = ["/", "!"]
    block_scope_ids: list[str] = []
    block_sender_ids: list[str] = []


class RouterConfigBlock(BaseModel):
    """Router tunables.

    These map 1:1 to :class:`linling_core.router.RouterConfig`. Defaults
    here track the defaults there; override in ``bot.yaml`` for
    production sizing.
    """

    max_concurrent_events: int = 128
    enqueue_timeout_s: float = 1.0
    # Per-session lock hold timeout. The router refuses to wait
    # longer than this for an in-flight handler on the same session
    # before giving up on a new event. Default lowered from the
    # historic 30s to 10s after we saw real adapters (NapCat /
    # Lagrange) occasionally hold the lock waiting on a dropped
    # ``echo`` from QQ side; 30s + a chatty group made every later
    # command line up behind one stuck send. The OneBot adapter's
    # own ``call_api`` timeout (5s) is now strictly shorter, so a
    # single bad ``$发送$`` no longer wedges the session at all —
    # this 10s is the back-stop for handlers that hit *several*
    # adapter calls in sequence.
    session_timeout_s: float = 10.0
    unknown_command_reply: str = "Unknown command. Try /help."
    busy_reply: str = "Bot is busy, please try again."
    busy_session_reply: str = "You're sending messages too fast, slow down."


class ConversationConfig(BaseModel):
    """Per-session state store tunables.

    See :class:`linling_core.pipeline.ConversationStore`. Defaults match
    a single-node deployment serving a few hundred concurrent users;
    tune ``max_sessions`` / ``ttl_seconds`` up for larger fleets and
    swap the store implementation entirely for multi-node.

    The ``ledger_*`` family wires the optional DSL Action Ledger
    feature. Defaults match the spec (``Ledger_Maxlen`` 20,
    ``Single_Char_Budget`` 200, ``Total_Char_Budget`` 800,
    1-hour TTL, ``Global_Default_Expose`` ``True``); leaving them at
    defaults keeps behaviour identical to the pre-feature build
    because the dispatcher integration is gated on whether a backing
    store is wired up at bootstrap.
    """

    max_sessions: int = 10_000
    ttl_seconds: float | None = 3_600.0
    history_turns: int = 16
    rate_per_second: float = 1.0
    burst: float = 5.0
    # DSL Action Ledger knobs. Pinned to the spec's defaults; bootstrap
    # constructs ``KVDslLedgerStore`` / ``LedgerWriter`` /
    # ``LedgerRenderer`` only when the ledger is opt-in via the
    # ``ledger_enabled`` flag.
    ledger_enabled: bool = False
    ledger_maxlen: int = 20
    ledger_ttl_seconds: int = 3600
    ledger_single_char_budget: int = 200
    ledger_total_char_budget: int = 800
    ledger_global_default_expose: bool = True


class AgentConfig(BaseModel):
    """Pointers into the default agent wired to the chat path.

    A bot with no chat agent at all can leave ``default_agent`` unset;
    the router will still serve DSL commands and reply with the
    configured ``fallback_reply`` to any free-form message.

    ``allowed_scopes`` is the *group-chat* allowlist (LLM fallback
    only; DSL commands always run regardless). Private chats (DM
    scopes — including the WebUI's synthesised ``%群号%==0``) are
    always allowed: they're 1:1 conversations the operator opted
    into by messaging the bot directly, so the gate doesn't apply.
    For group scopes the list maps to ``%群号%`` of the inbound
    event:

    * ``None`` (default) — group allowlist disabled; every group
      can chat with the LLM (legacy behaviour).
    * ``[]`` — every group is denied. DMs still reach the LLM
      because the kind=dm bypass is unconditional. Group messages
      fall back to ``fallback_reply`` (or stay silent if it's
      empty).
    * ``["123", "456"]`` — only these group ids reach the LLM.
      Other groups get ``fallback_reply``.

    This is the safe-rollout knob: deploy the bot to a real group
    first with ``allowed_scopes: []`` so DSL handlers run and DMs
    work but no random group can blast the LLM, confirm nothing
    leaks, then add scopes one at a time.
    """

    default_agent: str | None = None  # path to agent yaml
    fallback_reply: str = "Sorry, I don't have a chat brain configured."
    allowed_scopes: list[str] | None = None


class MetricsConfig(BaseModel):
    """Metrics export configuration.

    ``enabled=False`` (default) installs :class:`NullMetrics` — zero
    overhead, no optional dependency needed. ``enabled=True`` requires
    the ``prometheus_client`` package and turns on an isolated
    ``CollectorRegistry`` plus the ``/metrics`` endpoint on the WebUI
    (when attached).

    ``auth_required`` decides whether the ``/metrics`` endpoint needs
    the same JWT as the rest of the WebUI. Defaults to ``False``
    because Prometheus scrapers typically talk to a network-ACL'd
    endpoint; set to ``True`` if the endpoint is internet-facing.
    """

    enabled: bool = False
    auth_required: bool = False


class BotConfig(BaseSettings):
    """Top-level bot configuration loaded from bot.yaml + env vars."""

    model_config = SettingsConfigDict(
        env_prefix="LINLING_",
        env_file=".env",
        env_file_encoding="utf-8",
        # ``.env`` is shared with adapters / agents / providers, so it
        # legitimately holds keys that ``BotConfig`` doesn't model
        # (``OPENAI_API_KEY``, ``LOG_LEVEL`` …).
        # pydantic-settings 2.14 loads every dotenv key into the data
        # dict before validation, so we must ``ignore`` extras here or
        # construction blows up the moment somebody puts a provider
        # secret next to the bot config.
        extra="ignore",
    )

    bot_id: str = "linling"
    name: str = "linling"
    admin_users: list[str] = []
    main_group: str = ""

    storage: StorageConfig = StorageConfig()
    adapters: list[AdapterConfig] = []

    rules: list[str] = ["rules/**/*.ling"]

    classifier: ClassifierConfig = ClassifierConfig()
    router: RouterConfigBlock = RouterConfigBlock()
    conversation: ConversationConfig = ConversationConfig()
    agent: AgentConfig = AgentConfig()
    metrics: MetricsConfig = MetricsConfig()

    # Opaque pass-through for the ``webui:`` section. The WebUI package
    # owns its own schema (:class:`linling_webui.config.WebUIConfig`),
    # so we keep the payload untyped here and hand it off verbatim when
    # the bot is started with ``--webui``. This avoids a dependency
    # from ``linling_core`` onto ``linling_webui``.
    webui: dict[str, object] = {}

    @classmethod
    def from_yaml(cls, path: str | Path) -> BotConfig:
        """Load config from a YAML file, with env var expansion."""
        raw = Path(path).read_text(encoding="utf-8")
        return cls.from_yaml_str(raw)

    @classmethod
    def from_yaml_str(cls, raw: str) -> BotConfig:
        """Load config from a YAML string, with env var expansion."""
        data = yaml.safe_load(raw) or {}
        expanded = expand_env_recursive(data)
        return cls(**expanded)
