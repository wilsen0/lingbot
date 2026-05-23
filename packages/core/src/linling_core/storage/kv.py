"""KV store protocol.

Maps onto the original QRDic DSL primitives via the DSL shim tools in
:mod:`linling_core.tools_builtin` (``dsl_read_kv`` / ``dsl_write_kv`` /
``dsl_delete_kv`` / ``dsl_rank_kv``):

- ``$读 scope/file key default$`` → :meth:`KVStore.read` (after path split)
- ``$写 scope/file key value$`` → :meth:`KVStore.write` (after path split)
- ``$排行榜 scope/file 反序 N sep fmt$`` → :meth:`KVStore.rank`

The protocol itself speaks the clean three-segment API. The shim layer
is responsible for the ``rpartition('/')`` split and for coercing the
quirkier QRDic argument shapes (Chinese order aliases, stringified ints,
``\\n`` / ``%0A`` newline escapes).

All values are strings. Non-numeric values are preserved; rank ordering
casts them to a real and treats invalid literals as 0.0 (consistent with
QRDic's ``CAST AS REAL`` semantics).

``scope`` and ``file`` are kept as separate columns (per design.md §8.1)
so that migrating Java Properties trees yields an obvious mapping:
``data/啊/灵玉系/灵玉`` → ``scope="啊/灵玉系"``, ``file="灵玉"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class RankOrder(StrEnum):
    """Ordering for :meth:`KVStore.rank` / :meth:`KVStore.rank_rows`.

    The Chinese aliases (``反序``/``正序``) match the original DSL so that
    rule scripts can be translated 1:1 without a case table.
    """

    DESC = "desc"  # 反序: largest first (rank #1 is the highest value)
    ASC = "asc"  # 正序: smallest first

    @classmethod
    def parse(cls, value: str) -> RankOrder:
        """Accept both English and Chinese aliases."""
        if value in ("desc", "反序"):
            return cls.DESC
        if value in ("asc", "正序"):
            return cls.ASC
        raise ValueError(f"unknown rank order: {value!r}")


@dataclass(frozen=True, slots=True)
class RankRow:
    """One entry in a leaderboard result."""

    rank: int  # 1-based
    key: str
    value: str  # original string; callers may reinterpret
    numeric: float  # the float the backend sorted on


@runtime_checkable
class KVStore(Protocol):
    """Abstract key-value store.

    All methods are async to accommodate network-backed implementations
    (Postgres, Redis) even though the SQLite backend runs in-process.

    Identity
    --------
    A KV store is bound to a single ``bot_id`` at construction. Cross-bot
    access must go through separate instances; this keeps the tool
    registry from having to thread tenancy through every call.
    """

    # --- basic CRUD -----------------------------------------------------

    async def read(self, scope: str, file: str, key: str, default: str | None = None) -> str | None:
        """Return the value, or ``default`` if the key is absent."""
        ...

    async def write(self, scope: str, file: str, key: str, value: str) -> None:
        """Upsert a value. Empty strings are valid; use :meth:`delete` to remove."""
        ...

    async def delete(self, scope: str, file: str | None = None, key: str | None = None) -> int:
        """Delete a single key, a whole file, or a whole scope.

        - ``(scope, file, key)`` — delete that single entry.
        - ``(scope, file, None)`` — delete all keys under that file.
        - ``(scope, None, None)`` — delete everything under that scope.

        :returns: number of rows removed.
        """
        ...

    # --- discovery ------------------------------------------------------

    async def keys(self, scope: str, file: str) -> list[str]:
        """Return every key under ``scope/file`` (unordered)."""
        ...

    async def files(self, scope: str) -> list[str]:
        """Return every distinct ``file`` under ``scope``."""
        ...

    async def scopes(self) -> list[str]:
        """Return every distinct ``scope`` in the store (unordered)."""
        ...

    # --- ranking --------------------------------------------------------

    async def rank_rows(
        self,
        scope: str,
        file: str,
        *,
        order: RankOrder = RankOrder.DESC,
        top: int = 10,
    ) -> list[RankRow]:
        """Return the top-N rows ordered by numeric interpretation of value."""
        ...

    async def rank(
        self,
        scope: str,
        file: str,
        *,
        order: RankOrder = RankOrder.DESC,
        top: int = 10,
        sep: str = "\n",
        fmt: str = "[序号]. [键] [值]",
    ) -> str:
        """Convenience formatter: render :meth:`rank_rows` output to one string.

        Supported format tokens:

        - ``[序号]`` — 1-based rank
        - ``[键]`` — key
        - ``[值]`` — value (original string)

        Richer tokens (``[键转昵称群号]`` etc.) are a DSL-layer concern
        and should be applied post-hoc by the caller.
        """
        ...

    # --- lifecycle ------------------------------------------------------

    async def close(self) -> None:
        """Release connections and flush caches."""
        ...
