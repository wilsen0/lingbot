"""Standard built-in tools for linling.

These tools auto-register into the global :data:`linling_core.tools.registry`
on import. They cover the core DSL primitives (KV read/write/delete/rank)
plus common utilities (random, http, string replacement).

Two tool shapes share the same underlying KV store:

* **Python / Agent view** — ``read_kv``, ``write_kv``, ``delete_kv``,
  ``rank_kv`` with an explicit ``(scope, file, key, …)`` signature. These
  are what LLM tool-calling loops and python integrations talk to.
* **DSL view** — ``dsl_read_kv``, ``dsl_write_kv``, ``dsl_delete_kv``,
  ``dsl_rank_kv`` registered under the Chinese DSL names ``读/写/删除/排行榜``.
  They accept the historical QRDic shape ``"scope/file key default"`` and
  split the path on the last ``/`` internally.

Keeping the two shapes decoupled lets LLMs see a clean typed API while
QRDic scripts continue to work unchanged (2000+ call sites in the
original ``dicpro.txt``).
"""

from __future__ import annotations

import logging
import random

from linling_core.storage.kv import RankOrder
from linling_core.tools import ToolCtx, tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers for DSL ↔ KV path munging
# ---------------------------------------------------------------------------


def _split_path(path: str) -> tuple[str, str]:
    """Split a QRDic-style ``scope/…/file`` path on the last ``/``.

    Examples::

        "啊/灵玉系/灵玉"                 → ("啊/灵玉系",       "灵玉")
        "小苏苏/好感/12345/好感"         → ("小苏苏/好感/12345", "好感")
        "偷玉游戏/谁在偷我玉"             → ("偷玉游戏",         "谁在偷我玉")
        "standalone"                     → ("standalone",       "")

    A trailing empty ``file`` segment (i.e. path without ``/``) is
    preserved so callers can treat it as a scope-level operation.
    """
    scope, sep, file = path.rpartition("/")
    if not sep:
        # No slash — the entire token is the scope name (e.g. a top-level
        # QRDic directory like "偷玉游戏"). Keep file empty so delete_kv
        # can still target the whole scope.
        return path, ""
    return scope, file


# QRDic filesystem path prefix for compat with ``$删除 /storage/...$``.
_QRDIC_FS_PREFIX = "/storage/emulated/0/QR/QRDic/data/"


def _normalize_rank_order(raw: str) -> RankOrder:
    """Coerce an order token into :class:`RankOrder`.

    Accepts the Chinese aliases (``反序``/``正序``), the English words
    (``desc``/``asc``), and is forgiving to whitespace. Anything else
    defaults to ``DESC`` — QRDic scripts almost exclusively sort by
    ``反序`` so that's the least-surprising fallback.
    """
    token = (raw or "").strip()
    if token in {"desc", "反序", ""}:
        return RankOrder.DESC
    if token in {"asc", "正序"}:
        return RankOrder.ASC
    logger.warning("rank_kv: unknown order token %r; defaulting to desc", token)
    return RankOrder.DESC


def _normalize_sep(sep: str) -> str:
    """Translate the handful of literal escape encodings the old DSL used.

    ``\\n`` in source scripts reaches the tool as the 2-char string ``"\\n"``;
    ``%0A`` is the URL-encoded form some rules used to slip past the
    space-splitting tokenizer. Both should become a real newline.
    """
    return sep.replace("\\n", "\n").replace("%0A", "\n").replace("%0a", "\n")


# ---------------------------------------------------------------------------
# Python / Agent facing KV tools (clean (scope, file, key) API)
# ---------------------------------------------------------------------------


@tool(
    name="read_kv",
    dsl_name="",  # DSL uses dsl_read_kv; see below.
    description="Read a key-value pair from storage",
    schema={"scope": "string", "file": "string", "key": "string", "default": "string?"},
    safe=True,
)
async def read_kv(
    ctx: ToolCtx, scope: str, file: str, key: str, default: str | None = None
) -> str | None:
    """Read a value from the KV store."""
    return await ctx.kv.read(scope, file, key, default)


@tool(
    name="write_kv",
    dsl_name="",
    description="Write a key-value pair to storage",
    schema={"scope": "string", "file": "string", "key": "string", "value": "string"},
    safe=False,
)
async def write_kv(ctx: ToolCtx, scope: str, file: str, key: str, value: str) -> None:
    """Write a value to the KV store."""
    await ctx.kv.write(scope, file, key, value)


@tool(
    name="delete_kv",
    dsl_name="",
    description="Delete a key (or scope/file) from storage",
    schema={"scope": "string", "file": "string?", "key": "string?"},
    safe=False,
)
async def delete_kv(
    ctx: ToolCtx, scope: str, file: str | None = None, key: str | None = None
) -> int:
    """Delete from the KV store. Returns number of rows removed."""
    return await ctx.kv.delete(scope, file, key)


@tool(
    name="rank_kv",
    dsl_name="",
    description="Get a formatted leaderboard from storage",
    schema={
        "scope": "string",
        "file": "string",
        "order": "string?",
        "top": "int?",
        "sep": "string?",
        "fmt": "string?",
    },
    safe=True,
)
async def rank_kv(
    ctx: ToolCtx,
    scope: str,
    file: str,
    order: str = "desc",
    top: int = 10,
    sep: str = "\n",
    fmt: str = "[序号]. [键] [值]",
) -> str:
    """Get a formatted leaderboard."""
    return await ctx.kv.rank(
        scope, file, order=_normalize_rank_order(order), top=top, sep=sep, fmt=fmt
    )


# ---------------------------------------------------------------------------
# DSL-facing KV shims (QRDic "$读 path key default$" shape)
# ---------------------------------------------------------------------------


@tool(
    name="dsl_read_kv",
    dsl_name="读",
    description="DSL shim: $读 path key default$ → read_kv(scope, file, key, default)",
    schema={"path": "string", "key": "string", "default": "string?"},
    safe=True,
    llm_visible=False,
)
async def dsl_read_kv(
    ctx: ToolCtx,
    path: str = "",
    key: str = "",
    default: str | None = None,
) -> str | None:
    """DSL shim for ``$读 path key default$``.

    Splits ``path`` on the last ``/`` into ``(scope, file)`` and returns
    ``default`` on miss — matching the original QRDic semantics where the
    third argument is always a default, never an absent parameter.

    Empty ``path`` or ``key`` returns ``default`` immediately so a
    malformed ``$读 t/x$`` (missing key) doesn't crash the handler.
    """
    if not path or not key:
        return default
    scope, file = _split_path(path)
    value = await ctx.kv.read(scope, file, key, default)
    return value if value is not None else default


@tool(
    name="dsl_write_kv",
    dsl_name="写",
    description="DSL shim: $写 path key value$ → write_kv(scope, file, key, value)",
    schema={"path": "string", "key": "string", "value": "string"},
    safe=False,
    llm_visible=False,
)
async def dsl_write_kv(
    ctx: ToolCtx,
    path: str = "",
    key: str = "",
    value: str = "",
) -> str:
    """DSL shim for ``$写 path key value$``.

    All args default to empty so a malformed call no-ops cleanly
    rather than raising. A path with no key is also a no-op — there's
    no sensible row to write.
    """
    if not path or not key:
        return ""
    scope, file = _split_path(path)
    await ctx.kv.write(scope, file, key, value)
    return ""


@tool(
    name="dsl_delete_kv",
    dsl_name="删除",
    description="DSL shim: $删除 path$ → delete_kv with best-effort path parsing",
    schema={"path": "string"},
    safe=False,
    llm_visible=False,
)
async def dsl_delete_kv(ctx: ToolCtx, path: str = "") -> str:
    """DSL shim for ``$删除 path$``.

    * ``/storage/emulated/0/QR/QRDic/data/<...>`` — strip the prefix,
      then treat the tail as either a scope or a scope+file target.
      Because QRDic's filesystem semantics are ambiguous in a flat KV
      world (``data/A/B`` could be a directory named ``B`` under ``A``
      or a file named ``B`` under scope ``A``) the shim tries a
      scope-level delete first and falls back to scope+file if nothing
      matched.
    * Any other absolute path (cache directory, ``%QQ%.txt`` at the
      QRDic root, …) is left alone and a warning is logged. Those call
      sites are out of scope for the KV store.
    * Relative paths use the same scope-first / scope+file fallback.

    Empty path → no-op returning ``"0"``.
    """
    if not path:
        return "0"
    raw = path.strip()
    if raw.startswith(_QRDIC_FS_PREFIX):
        rel = raw[len(_QRDIC_FS_PREFIX) :].strip("/")
        if not rel:
            logger.warning("dsl_delete_kv: empty data path %r", path)
            return "0"
    elif raw.startswith("/storage/"):
        logger.warning("dsl_delete_kv: ignoring non-KV absolute path %r", path)
        return "0"
    else:
        rel = raw

    # Strip legacy Properties suffixes — the old KV store used .txt/.bak files.
    if rel.endswith((".txt", ".bak")):
        rel = rel.rsplit(".", 1)[0]

    # 1) Scope-level: interpret the entire path as a scope name and delete
    #    everything under it (including all files). This matches QRDic's
    #    "remove this directory" semantics.
    removed = await ctx.kv.delete(rel)
    if removed:
        return str(removed)

    # 2) Fall back to scope+file split — perhaps the tail was a file name
    #    rather than a sub-scope.
    if "/" in rel:
        scope, file = _split_path(rel)
        removed = await ctx.kv.delete(scope, file)
        return str(removed)

    return "0"


@tool(
    name="dsl_rank_kv",
    dsl_name="排行榜",
    description=(
        "DSL shim: $排行榜 path order top sep fmt$ → formatted leaderboard "
        "with the legacy QRDic argument order."
    ),
    schema={
        "path": "string",
        "order": "string?",
        "top": "string?",
        "sep": "string?",
        "fmt": "string?",
    },
    safe=True,
    llm_visible=False,
)
async def dsl_rank_kv(
    ctx: ToolCtx,
    path: str = "",
    order: str = "反序",
    top: str = "10",
    sep: str = "\\n",
    fmt: str = "[序号]. [键] [值]",
) -> str:
    """DSL shim for ``$排行榜 path order top sep fmt$``.

    * ``order`` accepts ``反序/正序/desc/asc``.
    * ``top`` is passed as a string from the DSL tokenizer; we coerce it.
    * ``sep`` understands ``\\n`` and ``%0A`` as newline escapes, which
      are both used in the original scripts to sidestep the DSL
      whitespace tokenizer.
    * ``fmt`` flows through unchanged; supported tokens are documented
      on :meth:`KVStore.rank`.

    Empty path → empty result so a malformed ``$排行榜$`` doesn't crash.
    """
    if not path:
        return ""
    scope, file = _split_path(path)
    try:
        top_int = int(top)
    except (TypeError, ValueError):
        logger.warning("dsl_rank_kv: invalid top=%r; defaulting to 10", top)
        top_int = 10
    return await ctx.kv.rank(
        scope,
        file,
        order=_normalize_rank_order(order),
        top=top_int,
        sep=_normalize_sep(sep),
        fmt=fmt,
    )


# ---------------------------------------------------------------------------
# Misc utilities
# ---------------------------------------------------------------------------


@tool(
    name="random_int",
    dsl_name="随机数",
    description="Random integer in [min, max] inclusive. Accepts $随机数 1 5$ or $随机数 1-5$",
    schema={"min": "string", "max": "string?"},
    safe=True,
)
async def random_int(ctx: ToolCtx, min: str = "", max: str = "") -> str:
    """Return a random integer in ``[min, max]`` inclusive.

    Accepts both QRDic shapes:

    * ``$随机数 1 5$`` — two positional args, lo and hi separately.
    * ``$随机数 1-5$`` — one packed arg using ``-`` as the separator,
      same convention as the inline ``%随机数1-5%`` shorthand.

    Lo > Hi is silently swapped (matches QRDic's lenient behaviour);
    malformed input returns ``"0"`` rather than raising — keeping
    rule files crash-free even when a typo slips through.
    """
    if not max:
        # Single-arg form: split the dash-encoded blob.
        if "-" not in min:
            return "0"
        lo_s, _, hi_s = min.partition("-")
    else:
        lo_s, hi_s = min, max
    try:
        lo = int(lo_s)
        hi = int(hi_s)
    except (TypeError, ValueError):
        return "0"
    if lo > hi:
        lo, hi = hi, lo
    return str(random.randint(lo, hi))


@tool(
    name="http_get",
    dsl_name="访问",
    description="Perform an HTTP GET request (placeholder)",
    schema={"url": "string"},
    safe=False,
)
async def http_get(ctx: ToolCtx, url: str = "") -> str:
    """Placeholder for HTTP GET — not yet implemented."""
    if not url:
        return ""
    return "http_get not implemented yet"


@tool(
    name="replace_str",
    dsl_name="替换",
    description="Replace occurrences of a pattern in text",
    schema={"sep": "string", "text": "string", "pattern": "string", "replacement": "string"},
    safe=True,
)
async def replace_str(ctx: ToolCtx, sep: str, text: str, pattern: str, replacement: str) -> str:
    """Replace all occurrences of pattern with replacement in text."""
    return text.replace(pattern, replacement)
