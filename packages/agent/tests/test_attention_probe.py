"""Unit and property tests for :mod:`linling_agent.attention_probe`.

Covers the parser semantics (Property 7), the call shape / no-history
invariant (Property 8), and the failure-containment guarantees
(Property 6) introduced by the lightweight attention probe.

The probe itself is exercised against a fake provider so no network
round-trip is needed; the parser is a pure function so it is
hammered with hypothesis at high iteration counts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import structlog
from hypothesis import HealthCheck, given, settings, strategies as st

from linling_agent.attention_probe import (
    AttentionProbe,
    _NO_TOKENS,
    _YES_TOKENS,
    _build_user_prompt,
    _normalise_token,
    _parse_verdict,
    _ProbeBatchInput,
)
from linling_agent.errors import LLMAuthError, LLMError, LLMRateLimitError
from linling_agent.llm import LLMResponse, Message


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Stand-in for :class:`OpenAIProvider`.

    Records every ``chat`` call and returns either a queued response
    or raises a queued exception. The probe owns its provider via
    composition, so we replace ``_provider`` after construction
    rather than monkeypatching the class.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: list[LLMResponse | BaseException] = []
        self.aclose_count = 0

    def queue_response(self, content: str) -> None:
        self._responses.append(
            LLMResponse(message=Message(role="assistant", content=content))
        )

    def queue_exception(self, exc: BaseException) -> None:
        self._responses.append(exc)

    @property
    def name(self) -> str:
        return "fake-provider"

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: Any | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if not self._responses:
            raise AssertionError("no queued response for fake provider")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.aclose_count += 1


def _make_probe(fake: _FakeProvider) -> AttentionProbe:
    """Build an :class:`AttentionProbe` and swap its provider for a fake."""
    probe = AttentionProbe(
        api_key="sk-test", base_url="https://example.com/v1", model="probe-mini"
    )
    probe._provider = fake  # type: ignore[assignment]
    return probe


def _batch(*texts: str) -> list[_ProbeBatchInput]:
    return [
        _ProbeBatchInput(
            message_id=f"m{idx}",
            sender_name="user",
            timestamp=f"2026-01-01T00:00:0{idx}",
            text=text,
        )
        for idx, text in enumerate(texts)
    ]


# ---------------------------------------------------------------------------
# Property 7: yes-token-prefix parser
# ---------------------------------------------------------------------------


@settings(max_examples=500)
@given(
    base_token=st.sampled_from(
        sorted(_YES_TOKENS | _NO_TOKENS) + ["maybe", "idk", "perhaps", "可能"]
    ),
    leading_ws=st.sampled_from(["", " ", "\t", "\n", "  \t \n"]),
    trailing_punct=st.sampled_from(
        ["", ",", ".", "!", "?", "，", "。", "！", "？", ":", "：", ";", "；"]
    ),
    suffix=st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=0x4E00, blacklist_characters="\r\n"),
        min_size=0,
        max_size=20,
    ),
    casing=st.sampled_from(["lower", "upper", "title", "mixed"]),
)
def test_parse_verdict_yes_token_prefix_property(
    base_token: str,
    leading_ws: str,
    trailing_punct: str,
    suffix: str,
    casing: str,
) -> None:
    """Feature: lightweight-attention-probe, Property 7: yes-token-prefix parser.

    For any string composed of (optional whitespace)(yes/no/random
    token)(optional punctuation)(arbitrary suffix), the parser
    returns ``True`` if and only if the first whitespace-split token
    after normalisation is a yes-token.
    """
    if casing == "upper":
        token = base_token.upper()
    elif casing == "title":
        token = base_token.capitalize()
    elif casing == "mixed":
        token = "".join(
            ch.upper() if i % 2 == 0 else ch.lower() for i, ch in enumerate(base_token)
        )
    else:
        token = base_token

    # If the suffix begins with a non-whitespace char, attach it
    # directly so we exercise the "first internal punctuation splits
    # the token" path. Otherwise the suffix forms a separate
    # whitespace-split token, which the parser ignores.
    if suffix and suffix[0] not in (" ", "\t", "\n"):
        composed = f"{leading_ws}{token}{trailing_punct}{suffix}"
    else:
        composed = f"{leading_ws}{token}{trailing_punct} {suffix}".rstrip()

    expected = _normalise_token(composed) in _YES_TOKENS
    assert _parse_verdict(composed) is expected


def test_parse_verdict_empty_inputs() -> None:
    for s in ("", "   ", "\t", "\n", "  \t \n  "):
        assert _parse_verdict(s) is False


def test_parse_verdict_known_cases() -> None:
    """Anchor tests for the parser. Cheap regression coverage on top of PBT."""
    yes_cases = [
        "yes",
        "YES",
        "  YES, please  ",
        "是",
        "需要",
        "回复",
        "y",
        "1",
        "true",
        "yes，可以",
        '"yes"',
        "[yes]",
        "yes and no",
        "TRUE\n",
        "yes.",
    ]
    no_cases = [
        "no",
        "NO",
        "I think no",
        "",
        "   ",
        "0",
        "false",
        "maybe",
        "不需要",
        "不回复",
        "No, definitely not",
        "{\"ok\": true}",  # malformed JSON shape — first token is "{"...
        "Sure thing",
    ]
    for s in yes_cases:
        assert _parse_verdict(s) is True, f"expected yes for {s!r}"
    for s in no_cases:
        assert _parse_verdict(s) is False, f"expected no for {s!r}"


def test_build_user_prompt_caps_to_max_chars() -> None:
    big = "x" * 1_000
    batch = _batch(big, big, big, big, big)
    prompt = _build_user_prompt(batch, max_chars=2_000)
    # Header always present, at least one message kept even when the
    # cap forces truncation.
    assert prompt.startswith("群聊候选消息")
    assert "x" in prompt
    # Truncation kicked in — the rendered prompt is bounded but each
    # message is a self-contained JSON line.
    for line in prompt.splitlines()[1:]:
        if line:
            assert line.startswith("{") and line.endswith("}")


# ---------------------------------------------------------------------------
# Property 8: two-message body and no-history invariant
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    texts=st.lists(
        st.text(min_size=1, max_size=80, alphabet=st.characters(min_codepoint=32, max_codepoint=0x4E00)),
        min_size=1,
        max_size=10,
    )
)
async def test_judge_call_shape_property(texts: list[str]) -> None:
    """Feature: lightweight-attention-probe, Property 8: two-message body.

    Every probe invocation produces exactly two messages (system + user),
    no tools, ``temperature=0.0``, ``max_tokens<=32``.
    """
    fake = _FakeProvider()
    fake.queue_response("yes")
    probe = _make_probe(fake)
    batch = _batch(*texts)

    await probe.judge(batch, scope_id="g-test")

    assert len(fake.calls) == 1
    call = fake.calls[0]
    msgs: list[Message] = call["messages"]
    assert len(msgs) == 2
    assert msgs[0].role == "system"
    assert msgs[1].role == "user"
    # Verify *no other roles ever appear*: this is the no-history
    # invariant. Even an `assistant` placeholder would break it.
    assert {m.role for m in msgs} == {"system", "user"}
    assert call["tools"] is None
    assert call["temperature"] == 0.0
    assert call["max_tokens"] is not None
    assert call["max_tokens"] <= 32


# ---------------------------------------------------------------------------
# Property 6: failure containment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected_category",
    [
        (httpx.TimeoutException("slow"), "timeout"),
        (asyncio.TimeoutError(), "timeout"),
        (httpx.ConnectError("dns"), "network"),
        (LLMAuthError("forbidden"), "auth"),
        (LLMRateLimitError("slow down"), "rate_limit"),
        (LLMError("HTTP 500: kaboom"), "http_5xx"),
        (LLMError("HTTP 400: bad"), "http_4xx"),
        (ValueError("decode oops"), "other"),
    ],
)
async def test_judge_collapses_failures_to_false_with_one_warning(
    exc: BaseException, expected_category: str
) -> None:
    fake = _FakeProvider()
    fake.queue_exception(exc)
    probe = _make_probe(fake)

    with structlog.testing.capture_logs() as records:
        verdict = await probe.judge(_batch("hi"), scope_id="g1")

    assert verdict is False
    failures = [
        r
        for r in records
        if r.get("event") == "group_batch.attention_probe.failed"
    ]
    assert len(failures) == 1
    assert failures[0]["category"] == expected_category
    assert failures[0]["scope_id"] == "g1"


async def test_judge_propagates_cancelled_error() -> None:
    fake = _FakeProvider()
    fake.queue_exception(asyncio.CancelledError())
    probe = _make_probe(fake)

    with pytest.raises(asyncio.CancelledError):
        await probe.judge(_batch("hi"), scope_id="g1")


async def test_judge_malformed_output_emits_warning_and_returns_false() -> None:
    fake = _FakeProvider()
    fake.queue_response("indeterminate")
    probe = _make_probe(fake)

    with structlog.testing.capture_logs() as records:
        verdict = await probe.judge(_batch("hi"), scope_id="g1")

    assert verdict is False
    failures = [
        r
        for r in records
        if r.get("event") == "group_batch.attention_probe.failed"
    ]
    assert len(failures) == 1
    assert failures[0]["category"] == "malformed"


async def test_judge_no_token_response_does_not_warn_malformed() -> None:
    fake = _FakeProvider()
    fake.queue_response("no")
    probe = _make_probe(fake)

    with structlog.testing.capture_logs() as records:
        verdict = await probe.judge(_batch("hi"), scope_id="g1")

    assert verdict is False
    assert all(
        r.get("event") != "group_batch.attention_probe.failed" for r in records
    )


# ---------------------------------------------------------------------------
# Empty-batch short-circuit (R10)
# ---------------------------------------------------------------------------


async def test_judge_empty_batch_skips_http_call() -> None:
    fake = _FakeProvider()
    probe = _make_probe(fake)
    verdict = await probe.judge([], scope_id="g1")
    assert verdict is False
    assert fake.calls == []


async def test_judge_all_whitespace_batch_skips_http_call() -> None:
    fake = _FakeProvider()
    probe = _make_probe(fake)
    verdict = await probe.judge(_batch("   ", "\t\t", "\n\n"), scope_id="g1")
    assert verdict is False
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Successful verdict round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content,expected", [
    ("yes", True),
    ("YES.", True),
    ("是", True),
    ("需要", True),
    ("回复", True),
    ("no", False),
    ("否", False),
    ("不需要", False),
    ("not relevant", False),
])
async def test_judge_returns_correct_verdict(content: str, expected: bool) -> None:
    fake = _FakeProvider()
    fake.queue_response(content)
    probe = _make_probe(fake)
    assert await probe.judge(_batch("hi"), scope_id="g1") is expected


# ---------------------------------------------------------------------------
# aclose lifecycle
# ---------------------------------------------------------------------------


async def test_aclose_is_idempotent() -> None:
    fake = _FakeProvider()
    probe = _make_probe(fake)
    await probe.aclose()
    await probe.aclose()
    assert fake.aclose_count == 2


def test_constructor_rejects_invalid_timeout() -> None:
    with pytest.raises(ValueError):
        AttentionProbe(api_key="k", base_url="u", model="m", timeout=0)
    with pytest.raises(ValueError):
        AttentionProbe(api_key="k", base_url="u", model="m", timeout=11.0)


def test_constructor_rejects_invalid_max_chars() -> None:
    with pytest.raises(ValueError):
        AttentionProbe(api_key="k", base_url="u", model="m", max_chars=0)
