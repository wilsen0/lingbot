"""linling core package.

Provides the foundation shared by every other package:

- Unified event/action/segment models (:mod:`linling_core.events`, :mod:`linling_core.segments`)
- In-process event bus (:mod:`linling_core.bus`)
- OneBot v11 codec helpers (:mod:`linling_core.onebot_codec`)

DSL, Agent, and adapters depend on this package but not on each other.
"""

from linling_core.adapters import Adapter
from linling_core.audit import AuditEntry, AuditSink, NullAuditSink
from linling_core.bus import EventBus, Subscriber
from linling_core.classifier import (
    DEFAULT_COMMAND_PREFIXES,
    HandlerMatch,
    Intent,
    MessageClassifier,
)
from linling_core.config import AdapterConfig, BotConfig, StorageConfig
from linling_core.events import Action, ActionKind, Event, Scope, User
from linling_core.metrics import (
    ACTIVE_SESSIONS,
    DISPATCH_DURATION_SECONDS,
    LLM_CALLS_TOTAL,
    LLM_DURATION_SECONDS,
    LLM_TOKENS_TOTAL,
    ROUTER_DUPLICATES_TOTAL,
    ROUTER_EVENTS_TOTAL,
    SINK_FAILURES_TOTAL,
    MetricsSink,
    NullMetrics,
    get_metrics,
    set_metrics,
)
from linling_core.pipeline import (
    ConversationKey,
    ConversationStore,
    SeenEvents,
    Session,
    TokenBucket,
)
from linling_core.router import (
    ActionSink,
    ChatDispatcher,
    CommandDispatcher,
    HistoryReset,
    Router,
    RouterConfig,
)
from linling_core.scheduler import ScheduledTask, Scheduler
from linling_core.segments import (
    AtSegment,
    CardSegment,
    FaceSegment,
    FileSegment,
    ImageSegment,
    PokeSegment,
    ReplySegment,
    Segment,
    TextSegment,
    VideoSegment,
    VoiceSegment,
    XmlSegment,
    at,
    image,
    plain_text,
    reply,
    text,
)
from linling_core.storage import KVStore, RankOrder, RankRow, SqliteKVStore
from linling_core.tools import ToolCtx, ToolDef, ToolRegistry, registry, tool
from linling_core.version import __version__

__all__ = [
    "DEFAULT_COMMAND_PREFIXES",
    "Action",
    "ActionKind",
    "ActionSink",
    "Adapter",
    "AdapterConfig",
    "AtSegment",
    "AuditEntry",
    "AuditSink",
    "BotConfig",
    "CardSegment",
    "ChatDispatcher",
    "CommandDispatcher",
    "ConversationKey",
    "ConversationStore",
    "Event",
    "EventBus",
    "FaceSegment",
    "FileSegment",
    "HandlerMatch",
    "HistoryReset",
    "ImageSegment",
    "Intent",
    "KVStore",
    "MessageClassifier",
    "MetricsSink",
    "NullAuditSink",
    "NullMetrics",
    "PokeSegment",
    "RankOrder",
    "RankRow",
    "ReplySegment",
    "Router",
    "RouterConfig",
    "ScheduledTask",
    "Scheduler",
    "Scope",
    "SeenEvents",
    "Segment",
    "Session",
    "SqliteKVStore",
    "StorageConfig",
    "Subscriber",
    "TextSegment",
    "TokenBucket",
    "ToolCtx",
    "ToolDef",
    "ToolRegistry",
    "User",
    "VideoSegment",
    "VoiceSegment",
    "XmlSegment",
    "__version__",
    "at",
    "image",
    "plain_text",
    "registry",
    "reply",
    "text",
    "tool",
]
