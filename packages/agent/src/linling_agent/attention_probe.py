"""Lightweight attention probe for group-chat batching.

The :class:`AttentionProbe` owns its own :class:`OpenAIProvider` instance
— separate from the main agent's provider — so it can target a cheaper /
faster model (e.g. Groq llama-3.1-8b) at a different base URL / API key.

Primary usage: ``GroupBatchChatDispatcher._dispatch_batch_with_tools``
calls ``probe.provider.chat(messages, tools=...)`` with the **same full
context** (system prompt + conversation history + batch + tool schemas)
that the main LLM would see. If the small model produces no tool_calls
(i.e. chooses not to reply), the main LLM call is skipped entirely.

The legacy ``judge()`` method (yes/no single-token call) is retained for
the connectivity smoke test script but is no longer on the hot path.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx
import structlog

from linling_agent.errors import LLMAuthError, LLMError, LLMRateLimitError
from linling_agent.llm import Message
from linling_agent.providers.openai import OpenAIProvider

logger = structlog.get_logger(__name__)


# Yes / no tokens checked against the *first whitespace-split token* of
# the lowercased response (see :func:`_parse_verdict`). Any first-token
# match against ``_YES_TOKENS`` is a positive verdict; everything else —
# including ``_NO_TOKENS`` matches, malformed output, empty strings —
# falls through to :data:`False`. The fail-closed default keeps the
# cost-control semantic of the existing attention gate intact.
#
# Tokens are intentionally short and unambiguous in both English and
# Chinese so a small model can produce them reliably under
# ``max_tokens=32``. ``"是"`` / ``"否"`` cover bare Chinese answers;
# ``"需要"`` / ``"不需要"`` and ``"回复"`` / ``"不回复"`` cover the verb
# forms a small instruction-tuned model tends to emit when asked
# "should I reply".
_YES_TOKENS: frozenset[str] = frozenset({"yes", "y", "true", "1", "是", "需要", "回复"})
_NO_TOKENS: frozenset[str] = frozenset({"no", "n", "false", "0", "否", "不需要", "不回复"})


# Soft cap on how many bytes we ship in the user prompt. The dispatcher
# already trims its buffer to ``GroupBatchConfig.max_chars`` (default
# 6_000); we mirror that as the probe default so a probe call is always
# strictly smaller than a main-LLM call against the same batch.
_DEFAULT_MAX_CHARS: int = 6_000


# Hard caps from Requirement 13 ("Cost and Latency Caps"). Codified as
# module constants so both the probe and any future audit / metrics
# observer can reference them without re-reading the spec.
_MAX_TOKENS: int = 32
_TEMPERATURE: float = 0.0
_DEFAULT_TIMEOUT_S: float = 8.0
_MAX_TIMEOUT_S: float = 10.0


# Fixed system prompt. Kept short on purpose: the model only has to
# emit one of the yes / no tokens, and a longer prompt would cost
# tokens without buying accuracy. Bilingual instruction so EN-only and
# CN-only small models both respond in a recognisable token.
_SYSTEM_PROMPT: str = (
    "你是群聊消息选择器。判断下面这批群消息里,"
    "是否至少有一条值得机器人回复。只回答 yes 或 no,不要解释。"
    " You are a group-chat selector: answer 'yes' if any message is"
    " worth a reply, otherwise 'no'. Reply with a single token."
)


@dataclass(frozen=True)
class _ProbeBatchInput:
    """Snapshot of a single buffered message passed into the probe.

    Decoupled from :class:`linling_agent.group_batch._BufferedMessage`
    so this module has no inbound dependency on group-batch internals.
    The dispatcher builds a ``list[_ProbeBatchInput]`` from its own
    buffered state at probe-call time and hands ownership to the probe.
    """

    message_id: str
    sender_name: str
    timestamp: str
    text: str


# Punctuation set used by :func:`_normalise_token` to peel boundary
# decoration off a yes/no token. Includes the common ASCII set plus
# the most frequent CJK / full-width counterparts so a model that
# emits ``"yes，"`` or ``"yes。"`` collapses to ``"yes"`` like the
# bare-ASCII forms. Built once at import time so the per-call path
# does not re-allocate.
_PUNCT_CHARS: str = ",.!?，。！？:：;；'\"`<>()[]{}（）「」『』"


def _normalise_token(content: str) -> str:
    """Extract the canonical first token for yes/no resolution.

    Returns ``""`` for empty / whitespace-only input. Otherwise:

    1. Lowercase the first whitespace-split token.
    2. Peel surrounding punctuation from both ends in one pass via
       :meth:`str.strip` (C-implemented; handles repeated punct
       like ``"!!yes!!"`` natively).
    3. Truncate at the first remaining internal punctuation char so
       outputs like ``"yes，可以"`` collapse to ``"yes"``.

    Used by :func:`_parse_verdict` (yes-routing) and by
    :meth:`AttentionProbe.judge` (malformed-output detection) so the
    two sites agree on what counts as a recognisable token.
    """
    stripped = content.strip()
    if not stripped:
        return ""
    first = stripped.split(maxsplit=1)[0].lower().strip(_PUNCT_CHARS)
    for idx, ch in enumerate(first):
        if ch in _PUNCT_CHARS:
            return first[:idx]
    return first


def _parse_verdict(content: str) -> bool:
    """Return ``True`` iff the first whitespace token is a yes-token.

    Strictly fail-closed: the first whitespace-split token is
    normalised by :func:`_normalise_token`, then checked against
    :data:`_YES_TOKENS`. Empty input, :data:`_NO_TOKENS` matches,
    malformed JSON, partial tokens, and prose answers all return
    ``False``.

    Anchoring on the *first* token rather than substring search keeps
    benign explanations like ``"yes, because ..."`` unambiguous while
    refusing ambiguous outputs like ``"yes and no"``.
    """
    return _normalise_token(content) in _YES_TOKENS


def _build_user_prompt(batch: list[_ProbeBatchInput], max_chars: int) -> str:
    """Render a JSON-line snapshot of the batch, capped at ``max_chars``.

    The format mirrors the candidate-messages section of the main LLM
    selector's prompt so a model that has been calibrated on one set
    of inputs behaves consistently when calibrated on the other. Each
    line is a single JSON object so the model can parse linearly
    without needing to decode the whole document at once.

    The cap is enforced by truncating *whole lines* rather than mid-
    line, so a partially-rendered JSON object never reaches the
    upstream API. If a single message exceeds the cap on its own, we
    keep it (the probe needs at least one message to judge); the
    upstream model handles oversized inputs via its own truncation.
    """
    header = "群聊候选消息(请只回答 yes 或 no):"
    lines = [header]
    used = len(header)
    for msg in batch:
        line = json.dumps(
            {
                "message_id": msg.message_id,
                "sender_name": msg.sender_name,
                "time": msg.timestamp,
                "text": msg.text,
            },
            ensure_ascii=False,
        )
        # +1 for the newline join. Always keep at least the header +
        # the first message so the probe never sees zero candidates;
        # subsequent messages only join if they fit.
        if lines and len(lines) > 1 and used + 1 + len(line) > max_chars:
            break
        lines.append(line)
        used += 1 + len(line)
    return "\n".join(lines)


class AttentionProbe:
    """Lightweight yes/no LLM gate for the group-batch flush loop.

    Constructed by the bootstrap when both
    ``AgentConfig.group_batch_attention_probe_enabled`` is ``True`` and
    a usable API key resolves (``ATTENTION_PROBE_API_KEY`` →
    ``OPENAI_API_KEY``). Held as an optional dependency on
    :class:`GroupBatchChatDispatcher` (constructor kwarg ``probe``).

    Each invocation is a single chat-completion round-trip: one system
    message, one user message, no history, ``temperature=0.0``,
    ``max_tokens=32``, no tools. Failures of any kind (network, HTTP
    4xx/5xx, malformed yes/no output) collapse to ``verdict=False`` so
    the dispatcher's contract stays "verdict in {True, False},
    exceptions never escape" — the only exception that propagates is
    :class:`asyncio.CancelledError`, which has to bubble up so the
    surrounding flush task can shut down cleanly.

    The probe is ignorant of conversation history by design (Requirement
    4.4): "continuity" with the main agent is only at the platform /
    tooling level (same provider class, same env-var conventions),
    never at the chat level.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_chars: int = _DEFAULT_MAX_CHARS,
        proxy: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError(f"AttentionProbe timeout must be positive; got {timeout!r}")
        if timeout > _MAX_TIMEOUT_S:
            raise ValueError(f"AttentionProbe timeout must be <= {_MAX_TIMEOUT_S}; got {timeout!r}")
        if max_chars <= 0:
            raise ValueError(f"AttentionProbe max_chars must be positive; got {max_chars!r}")
        self._model = model
        self._base_url = base_url
        self._max_chars = max_chars
        # The probe owns its own httpx client. Using the same
        # ``OpenAIProvider`` class as the main LLM keeps the HTTP
        # surface (auth header, User-Agent, ``trust_env=False``)
        # uniform; pointing it at a different model / base_url / key
        # is what makes the probe "lightweight".
        self._provider: OpenAIProvider = OpenAIProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            default_temperature=_TEMPERATURE,
            default_max_tokens=1024,
            proxy=proxy,
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def provider(self) -> OpenAIProvider:
        """Expose the underlying provider for full-context pre-flight calls."""
        return self._provider

    async def aclose(self) -> None:
        """Close the underlying httpx client.

        Idempotent — :meth:`OpenAIProvider.aclose` may be called
        repeatedly safely (httpx tolerates re-close).
        :meth:`GroupBatchChatDispatcher.stop` invokes this after
        cancelling its flush tasks so any in-flight :meth:`judge` call
        sees ``asyncio.CancelledError`` first and the underlying
        connection pool gets released.
        """
        await self._provider.aclose()

    async def judge(
        self,
        batch: list[_ProbeBatchInput],
        *,
        scope_id: str,
    ) -> bool:
        """Return ``True`` iff the model says any message is worth a reply.

        Empty / all-whitespace batches short-circuit to ``False`` with
        no HTTP call (Requirement 10). Every exception that is not
        :class:`asyncio.CancelledError` is caught and converted to
        ``False`` plus exactly one ``warning``-level log record
        (Requirement 9). On success, one ``debug``-level log record is
        emitted with the verdict.
        """
        # Requirement 10.1 / 10.2: skip the API call when there's
        # nothing meaningful to probe. We treat both "no messages" and
        # "all messages whitespace-only" identically — the dispatcher
        # caller still records ``attention_probed=True`` so the
        # short-circuit counts toward the per-Batch_Lifecycle cap.
        if not batch or not any(msg.text.strip() for msg in batch):
            logger.debug(
                "group_batch.attention_probe.skipped_empty",
                scope_id=scope_id,
                batch_size=len(batch),
            )
            return False

        user_prompt = _build_user_prompt(batch, self._max_chars)
        # Requirement 4.4 / Property 8: exactly two messages, no history.
        messages: list[Message] = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=user_prompt),
        ]

        try:
            response = await self._provider.chat(
                messages,
                tools=None,
                temperature=_TEMPERATURE,
                max_tokens=_MAX_TOKENS,
            )
        except asyncio.CancelledError:
            # Re-raise so the surrounding flush task can clean up.
            # Never converted into a verdict — cancellation is
            # structural, not a yes/no decision.
            raise
        except LLMAuthError:
            # Auth errors are intentionally *not* sticky (Requirement
            # 9.4 / Property 9): the probe state is per-process-startup,
            # so the next batch will issue its own call. An operator
            # who rotates a key mid-run gets exactly one warning per
            # batch until the rotation completes.
            logger.warning(
                "group_batch.attention_probe.failed",
                scope_id=scope_id,
                category="auth",
            )
            return False
        except LLMRateLimitError:
            logger.warning(
                "group_batch.attention_probe.failed",
                scope_id=scope_id,
                category="rate_limit",
            )
            return False
        except (httpx.TimeoutException, TimeoutError):
            logger.warning(
                "group_batch.attention_probe.failed",
                scope_id=scope_id,
                category="timeout",
            )
            return False
        except httpx.TransportError:
            logger.warning(
                "group_batch.attention_probe.failed",
                scope_id=scope_id,
                category="network",
            )
            return False
        except LLMError as exc:
            # Generic LLM error path — covers HTTP 4xx (non-auth) and
            # 5xx surfaced by ``OpenAIProvider._handle_error_response``,
            # plus JSON-decode errors raised from the same site.
            # The category is best-effort: we inspect the message
            # prefix to distinguish 5xx from other 4xx for the log
            # only. Verdict is ``False`` regardless.
            category = "http_5xx" if " 5" in f" {exc} " else "http_4xx"
            logger.warning(
                "group_batch.attention_probe.failed",
                scope_id=scope_id,
                category=category,
            )
            return False
        except Exception:
            # Defence-in-depth: any other exception (ValueError from
            # a malformed provider response, unexpected runtime error)
            # is contained. We log with ``exc_info`` because an
            # uncategorised failure deserves a stack trace; the
            # categorised paths above are noisy enough without one.
            logger.warning(
                "group_batch.attention_probe.failed",
                scope_id=scope_id,
                category="other",
                exc_info=True,
            )
            return False

        # Normalise once and use the canonical token for both the
        # yes-routing and the malformed-detection branches. Calling
        # ``_normalise_token`` twice would re-do the same lowercase /
        # punctuation peel for every successful call.
        head = _normalise_token(response.message.content or "")
        verdict = head in _YES_TOKENS
        if not verdict and response.message.content and head and head not in _NO_TOKENS:
            # Distinguish "model said no" from "model said something
            # we couldn't parse" so an operator tuning the prompt can
            # see when the parser is rejecting unrecognised intent.
            # Logged at ``warning`` (Requirement 14.3) because
            # malformed output is a regression signal.
            logger.warning(
                "group_batch.attention_probe.failed",
                scope_id=scope_id,
                category="malformed",
            )
        logger.info(
            "group_batch.attention_probe.judged",
            scope_id=scope_id,
            batch_size=len(batch),
            verdict=verdict,
        )
        return verdict
