"""String operations: QRDic-style ``$替换$``, ``$正则$``, ``$取中间$``.

These tools replace the placeholder entries registered by
:mod:`linling_core.tools_builtin` — they share DSL names but provide
the full QRDic semantics (separator-delimited replacement rules and
boolean regex match).

QRDic's calling convention is irregular: each of these tools accepts
*either*

* a 3-arg form ``$替换 SEP TEXT PATTERN$`` (with whitespace between
  ``TEXT`` and ``PATTERN``), or
* a 2-arg "packed" form ``$替换 SEP BLOB$`` where ``BLOB`` is
  ``TEXT<SEP>FROM<SEP>TO`` (no whitespace, sep does double duty).

Real ``dicpro.txt`` rules mix both shapes within a single handler, so
the implementations here transparently fall back from the 3-arg form
to the 2-arg form when the third argument is missing. The DSL-side
late-binding of bare identifiers (see :class:`linling_dsl.vm.VM`) keeps
the QRDic ergonomics intact.
"""

from __future__ import annotations

import re

from linling_core.tools import ToolCtx, tool


def _split_text_pattern(sep: str, text_or_blob: str, pattern: str) -> tuple[str, str, str]:
    """Resolve ``(text, from_, to)`` from either calling form.

    * 3-arg form: ``text_or_blob`` *is* the text; ``pattern`` carries
      the ``<sep>FROM<sep>TO`` rule.
    * 2-arg form: ``pattern`` is empty; ``text_or_blob`` is a blob
      ``TEXT<sep>FROM<sep>TO`` and we crack it into the trio.

    The split tolerates QRDic's habit of producing leading empty
    pieces (e.g. ``@FROM@TO``); the empty parts are skipped when
    picking from/to but the caller still gets a faithful "text" half.
    """
    if pattern:
        return text_or_blob, *_extract_from_to(sep, pattern)
    return _split_packed(sep, text_or_blob)


def _split_packed(sep: str, blob: str) -> tuple[str, str, str]:
    """Decode ``TEXT<sep>FROM<sep>TO`` blob into a three-tuple."""
    if not sep:
        return blob, "", ""
    head, mid, rest = blob.partition(sep)
    if not mid:
        # No separator at all → treat entire blob as text, empty
        # from/to so the caller becomes a no-op.
        return blob, "", ""
    from_, _, to = rest.partition(sep)
    return head, from_, to


def _extract_from_to(sep: str, pattern: str) -> tuple[str, str]:
    """Decode ``<sep>FROM<sep>TO`` (3-arg form) into ``(from, to)``.

    QRDic's pattern often has a leading empty piece (``@FROM@TO``);
    we drop empties when assigning, but a deliberately empty TO
    (``@FROM@``) survives as ``""`` because we treat the *first two
    appearances* of ``sep`` as the boundary anchors.
    """
    if not sep:
        return pattern, ""
    parts = pattern.split(sep)
    # Filter the leading empty produced by ``sep`` at index 0, but
    # keep deliberate empty mid-positions: e.g. for ``@a@`` we want
    # from="a", to="".
    filtered = [p for p in parts if p]
    if not filtered:
        return "", ""
    if len(filtered) == 1:
        return filtered[0], ""
    return filtered[0], filtered[1]


@tool(
    name="replace_sep",
    dsl_name="替换",
    description="QRDic replace: $替换 SEP TEXT PATTERN$ or $替换 SEP TEXT<SEP>FROM<SEP>TO$",
    schema={"sep": "string", "text": "string", "pattern": "string?"},
    safe=True,
)
async def replace_sep(
    ctx: ToolCtx, sep: str = "", text: str = "", pattern: str = ""
) -> str:
    """Replace every occurrence of ``FROM`` with ``TO`` inside ``TEXT``.

    Both calling forms (3-arg and 2-arg packed) work — see the module
    docstring. Returns the original input unchanged if FROM is empty
    (defensive: a malformed ``$替换 @ %M%$`` shouldn't blow up the
    handler).
    """
    actual_text, from_, to = _split_text_pattern(sep, text, pattern)
    if not from_:
        return actual_text
    return actual_text.replace(from_, to)


@tool(
    name="regex_match",
    dsl_name="正则",
    description="QRDic regex: $正则 SEP TEXT PATTERN$ or $正则 SEP TEXT<SEP>PATTERN$",
    schema={"sep": "string", "text": "string", "pattern": "string?"},
    safe=True,
)
async def regex_match(
    ctx: ToolCtx, sep: str = "", text: str = "", pattern: str = ""
) -> str:
    """Return ``"1"`` if ``PATTERN`` is present in ``TEXT``, else ``"0"``.

    The 2-arg form packs ``TEXT<sep>PATTERN`` into the second arg;
    the 3-arg form passes them separately. Either way we route them
    through :func:`re.search`. Invalid regex syntax is treated as
    no-match (matching QRDic's tolerant behaviour).
    """
    if pattern:
        actual_text = text
        actual_pattern = _extract_from_to(sep, pattern)[0]
    else:
        # 2-arg form: ``text`` is the blob ``TEXT<sep>PATTERN``.
        if not sep:
            return "0"
        actual_text, _, actual_pattern = text.partition(sep)
    if not actual_pattern:
        return "0"
    try:
        return "1" if re.search(actual_pattern, actual_text) is not None else "0"
    except re.error:
        return "0"


@tool(
    name="substring_between",
    dsl_name="取中间",
    description="QRDic substring: $取中间 SEP BLOB$ — BLOB = TEXT<SEP>FROM<SEP>TO",
    schema={"sep": "string", "blob": "string", "tail": "string?"},
    safe=True,
    llm_visible=False,
)
async def substring_between(
    ctx: ToolCtx, sep: str = "", blob: str = "", tail: str = ""
) -> str:
    """Substring of ``TEXT`` between ``FROM`` and ``TO``.

    Real handlers write ``$取中间 @ %排%@1-@-1$`` (2-arg packed) where
    ``%排%`` is the haystack and ``1-`` / ``-1`` are the anchors —
    that's the canonical form. We also tolerate the 3-arg form
    ``$取中间 SEP TEXT BLOB$`` for symmetry with ``$替换$``.

    Returns empty when either anchor is missing or FROM doesn't
    appear before TO. This keeps QRDic's "silent miss" semantics —
    rules typically chain on the result without checking.
    """
    if not sep:
        return ""
    if tail:
        # 3-arg form: blob is the haystack, tail carries the FROM/TO.
        from_, to = _extract_from_to(sep, tail)
        haystack = blob
    else:
        haystack, from_, to = _split_packed(sep, blob)
    if not from_ or not to:
        return ""
    start = haystack.find(from_)
    if start < 0:
        return ""
    start += len(from_)
    end = haystack.find(to, start)
    if end < 0:
        return ""
    return haystack[start:end]
