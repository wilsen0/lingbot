"""Shared parser for the ``{"actions": [...]}`` multi-message reply payload.

The chat-side LLM may decide to fan a single turn out into several outbound
messages by emitting a JSON envelope. Two dispatchers share the same shape:

* :class:`linling_agent.dispatcher.AgentChatDispatcher` — DM / WebUI path,
  no concept of an "incoming batch", so ``message_id`` is informational.
* :class:`linling_agent.group_batch.GroupBatchChatDispatcher` — group fallback,
  knows the candidate batch so it can validate ``message_id`` against the
  buffer.

This module owns the protocol so both dispatchers parse the same wire shape.

Recognised entries (``type`` defaults to a plain send when omitted but
``text`` is present):

* ``{"type": "send" | "send_group" | "send_dm", "text": "..."}`` — a fresh
  outbound message in the current scope.
* ``{"type": "reply" | "reply_to_message", "message_id": "...", "text": "..."}``
  — an explicit @+quote reply. ``message_id`` may be validated by the caller.

Unrecognised types and entries with empty/non-string ``text`` are skipped
silently.

Important contract:if the wire payload *parses* as an actions envelope at
all (a dict with an ``"actions"`` list), the result is a structured outcome
even when zero entries survive. Callers must use the
:class:`ActionsParseOutcome` discriminator to distinguish

* ``not_actions`` — content was not in the actions envelope shape; caller
  should fall back to plain-text single-message handling.
* ``actions`` — content *was* the actions envelope; caller honors the
  parsed list verbatim, including the empty list (which means the LLM
  asked for nothing to be sent).

This is what prevents the dispatcher from leaking a raw JSON string back
to the user when the LLM emits ``{"actions":[]}`` or an envelope where
every entry is malformed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Cheap pre-filter so we don't run a full JSON decoder on every plain-text
# reply. Anything that doesn't start with ``{`` and mention ``"actions"``
# is treated as plain prose.
_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*")
_FENCE_CLOSE_RE = re.compile(r"\s*```$")


@dataclass(frozen=True)
class ParsedAction:
    """A single normalised action entry.

    ``kind`` is the abstract intent — ``"send"`` for a fresh message,
    ``"reply"`` for an explicit quote-reply. The dispatcher decides how
    to materialise it (e.g. add a ``ReplySegment`` for the group case,
    drop the ``message_id`` for DM).
    """

    kind: str  # "send" | "reply"
    text: str
    message_id: str = ""


@dataclass(frozen=True)
class ActionsParseOutcome:
    """Result of trying to parse an actions envelope.

    * ``recognised=False`` → content is plain prose; caller falls back
      to its single-message default.
    * ``recognised=True`` → content was the JSON envelope; caller honors
      ``entries`` exactly. An empty list means "send nothing"; the
      original JSON string MUST NOT be forwarded to the user.
    """

    recognised: bool
    entries: list[ParsedAction]


def parse_actions_envelope(content: str) -> ActionsParseOutcome:
    """Best-effort parse of an ``{"actions":[...]}`` envelope from prose.

    Tolerates leading/trailing whitespace, fenced code blocks
    (```` ```json ... ``` ````), and a JSON object embedded inside other
    text (we look for the first ``{`` and last ``}``).

    Returns a :class:`ActionsParseOutcome` — see the module docstring for
    the meaning of the ``recognised`` flag.
    """
    payload = _try_decode_envelope(content)
    if payload is None:
        return ActionsParseOutcome(recognised=False, entries=[])
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list):
        return ActionsParseOutcome(recognised=False, entries=[])
    entries: list[ParsedAction] = []
    for item in raw_actions:
        parsed = _normalise_entry(item)
        if parsed is not None:
            entries.append(parsed)
    return ActionsParseOutcome(recognised=True, entries=entries)


def _try_decode_envelope(content: str) -> dict[str, object] | None:
    text = (content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = _FENCE_OPEN_RE.sub("", text, count=1)
        text = _FENCE_CLOSE_RE.sub("", text, count=1).strip()
    if "\"actions\"" not in text:
        return None

    # Fast path for a clean JSON-only response.
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list):
        return parsed

    # LLMs sometimes wrap the envelope in role-play prose despite the
    # prompt. Scan for a standalone JSON object and accept the first
    # one that is actually an actions envelope. ``raw_decode`` handles
    # nested objects and braces inside JSON strings; a regex would not.
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            candidate, _end = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("actions"), list):
            return candidate
    return None


def _normalise_entry(item: object) -> ParsedAction | None:
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    raw_type = item.get("type")
    typ = raw_type.strip() if isinstance(raw_type, str) else ""
    raw_message_id = item.get("message_id")
    message_id = raw_message_id if isinstance(raw_message_id, str) else ""
    if typ in ("reply", "reply_to_message"):
        return ParsedAction(kind="reply", text=text, message_id=message_id)
    # ``send`` / ``send_group`` / ``send_dm`` / missing-type-with-text
    # all collapse to the abstract "send" intent. Anything else is
    # treated as malformed.
    if typ in ("", "send", "send_group", "send_dm"):
        return ParsedAction(kind="send", text=text, message_id=message_id)
    return None
