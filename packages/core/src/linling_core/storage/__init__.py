"""Storage abstractions for linling.

At P0 we ship the KV store (:mod:`linling_core.storage.kv`) backed by
SQLite. File store, vector store, and scheduler persistence land in
later milestones.

The KV store mirrors the semantics of the original QRDic
``$读/$写/$删除/$排行榜`` DSL primitives but without being tied to
Java Properties files on disk.
"""

from linling_core.storage.kv import KVStore, RankOrder, RankRow
from linling_core.storage.sqlite_kv import SqliteKVStore

__all__ = ["KVStore", "RankOrder", "RankRow", "SqliteKVStore"]
