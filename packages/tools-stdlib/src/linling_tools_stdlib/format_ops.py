"""Formatting / hashing primitives.

Two QRSpeed-side tools that linling-dsl previously left without an
implementation:

* ``$时间 fmt$`` — accepts an arbitrary ``strftime`` format string and
  returns the current local time formatted accordingly. The ``%时间...%``
  context-variable form is whitelist-only (15 known suffixes); this
  tool form is the escape hatch for unusual formats (e.g. signing /
  签到 rules that key on ``yyyyDD``).

* ``$MD5 text$`` — hex MD5 digest of the UTF-8 encoded text. Used by
  community samples (留言板 / 问答 keying) as a stable opaque id.

Both tools are pure (no side effects, no I/O) so they're freely safe
for the LLM tool catalog as well — the ``llm_visible`` default is left
on. ``MD5`` is offered as a hash, not a security primitive; the ``safe``
flag is just the registry's "doesn't mutate state" tag.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from linling_core.tools import ToolCtx, tool


@tool(
    name="format_time",
    dsl_name="时间",
    description=(
        "Return the current local time formatted via the given strftime spec. "
        "Mirrors QRSpeed's $时间 fmt$ — equivalent to %时间...% but with "
        "an arbitrary fmt rather than the whitelisted suffixes."
    ),
    schema={"fmt": "string"},
    safe=True,
)
async def format_time(ctx: ToolCtx, *fmt_parts: str) -> str:
    """Format ``datetime.now()`` using ``fmt`` (joined with spaces).

    Variadic so a multi-token format like ``$时间 yyyy-MM-dd HH:mm$``
    survives the DSL's space-tokenizing arg parser.

    The fmt is interpreted as Python's :meth:`datetime.strftime`
    syntax. QRSpeed's most common formats (``yyyy-MM-dd``,
    ``yyyyMMddHH``, ``yyyyDD``) use the same letters as
    Java/JodaTime — most of which Python supports natively. We
    translate three common Java-isms automatically so the rule
    files don't have to know the difference:

    * ``yyyy`` → ``%Y``
    * ``MM``   → ``%m``
    * ``dd``   → ``%d``
    * ``HH``   → ``%H``
    * ``mm``   → ``%M``
    * ``ss``   → ``%S``
    * ``DD``   → ``%j`` (day-of-year — what ``yyyyDD`` rules want)

    An empty fmt returns ISO-formatted ``YYYY-mm-dd HH:MM:SS``.
    """
    fmt = " ".join(fmt_parts).strip()
    now = datetime.now()
    if not fmt:
        return now.strftime("%Y-%m-%d %H:%M:%S")
    py_fmt = (
        fmt.replace("yyyy", "%Y")
        .replace("MM", "%m")
        .replace("dd", "%d")
        .replace("HH", "%H")
        .replace("mm", "%M")
        .replace("ss", "%S")
        .replace("DD", "%j")
    )
    try:
        return now.strftime(py_fmt)
    except (ValueError, TypeError):
        return ""


@tool(
    name="md5_hex",
    dsl_name="MD5",
    description="Return the lowercase hex MD5 digest of the UTF-8 encoded text",
    schema={"text": "string"},
    safe=True,
)
async def md5_hex(ctx: ToolCtx, *text_parts: str) -> str:
    """``$MD5 text…$`` → 32-char lowercase hex digest.

    Variadic so multi-token text (``$MD5 hello world$``) survives
    the DSL's space-tokenizing arg parser as a single hashable string.

    QRSpeed community rules use this as a stable opaque identifier
    (留言板 keys, 问答 词条 fingerprints). It's intentionally not a
    security primitive; treat it as a hash, not authentication.
    """
    text = " ".join(text_parts)
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()
