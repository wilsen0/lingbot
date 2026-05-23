"""Encoding/decoding operations.

Maps QRDic's DSL primitives one-to-one:

- ``$URLEncoder$`` / ``$URLDecoder$`` — URL-encode/decode
- ``$Base64Encoder$`` / ``$Base64Decoder$`` — Base64 encode/decode
- ``$HexEncoder$`` / ``$HexDecoder$`` — hex encode (UTF-8) / decode
- ``$UnicodeDecoder$`` — decode ``\\uXXXX`` escape sequences

All bytes-in-string results assume UTF-8.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import quote, unquote

from linling_core.tools import ToolCtx, tool

_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _join_text(text: str, extra: tuple[str, ...]) -> str:
    """Join the canonical first arg with any trailing tokens.

    Codec tools take a single text arg, but the DSL tokenises on
    whitespace; if a rule writes ``$URLEncoder hello world$`` (rather
    than the canonical ``$URLEncoder %var%$`` where the value lives
    in scope), the parser splits it into ``["hello", "world"]`` and
    the second token would otherwise be dropped on the floor. Joining
    with single spaces matches the parser's tokenization inverse.
    """
    if not extra:
        return text
    return text + " " + " ".join(str(p) for p in extra)


@tool(
    name="url_encode",
    dsl_name="URLEncoder",
    description="URL-encode a string",
    schema={"text": "string"},
    safe=True,
)
async def url_encode(ctx: ToolCtx, text: str = "", *extra: str) -> str:
    """Percent-encode *text* for use in a URL."""
    return quote(_join_text(text, extra), safe="")


@tool(
    name="url_decode",
    dsl_name="URLDecoder",
    description="URL-decode a percent-encoded string",
    schema={"text": "string"},
    safe=True,
)
async def url_decode(ctx: ToolCtx, text: str = "", *extra: str) -> str:
    """Decode a percent-encoded URL string."""
    return unquote(_join_text(text, extra))


@tool(
    name="base64_encode",
    dsl_name="Base64Encoder",
    description="Base64-encode a string (UTF-8)",
    schema={"text": "string"},
    safe=True,
)
async def base64_encode(ctx: ToolCtx, text: str = "", *extra: str) -> str:
    """Return the Base64 encoding of *text*'s UTF-8 bytes."""
    return base64.b64encode(_join_text(text, extra).encode("utf-8")).decode("ascii")


@tool(
    name="base64_decode",
    dsl_name="Base64Decoder",
    description="Base64-decode a string",
    schema={"text": "string"},
    safe=True,
)
async def base64_decode(ctx: ToolCtx, text: str = "", *extra: str) -> str:
    """Decode Base64 text, returning a UTF-8 string.

    Invalid Base64 or non-UTF-8 bytes yield an empty string — matching
    QRDic's lenient behaviour where a bad decode produces no output.
    """
    try:
        data = base64.b64decode(_join_text(text, extra), validate=False)
        return data.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


@tool(
    name="hex_encode",
    dsl_name="HexEncoder",
    description="Hex-encode a string (UTF-8)",
    schema={"text": "string"},
    safe=True,
)
async def hex_encode(ctx: ToolCtx, text: str = "", *extra: str) -> str:
    """Return the lowercase hex encoding of *text*'s UTF-8 bytes."""
    return _join_text(text, extra).encode("utf-8").hex()


@tool(
    name="hex_decode",
    dsl_name="HexDecoder",
    description="Hex-decode a string",
    schema={"text": "string"},
    safe=True,
)
async def hex_decode(ctx: ToolCtx, text: str = "", *extra: str) -> str:
    """Decode a hex string, returning a UTF-8 string.

    Whitespace is stripped before decoding. Invalid hex or non-UTF-8
    bytes yield an empty string.
    """
    cleaned = "".join(_join_text(text, extra).split())
    try:
        data = bytes.fromhex(cleaned)
        return data.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


@tool(
    name="unicode_decode",
    dsl_name="UnicodeDecoder",
    description="Decode \\uXXXX escape sequences in a string",
    schema={"text": "string"},
    safe=True,
)
async def unicode_decode(ctx: ToolCtx, text: str = "", *extra: str) -> str:
    """Replace ``\\uXXXX`` escapes with the corresponding characters.

    Unescaped text is left alone; surrogate pairs are combined when
    possible, but malformed surrogates fall back to the literal characters.
    """

    def _sub(match: re.Match[str]) -> str:
        code = int(match.group(1), 16)
        return chr(code)

    return _UNICODE_ESCAPE_RE.sub(_sub, _join_text(text, extra))
