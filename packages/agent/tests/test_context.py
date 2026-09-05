"""Multimodal (image) support in the context budgeting layer."""

from __future__ import annotations

from linling_agent.context import (
    _IMAGE_TOKEN_COST,
    ContextBudget,
    ContextManager,
    estimate_messages_tokens,
    fit_messages_to_budget,
)
from linling_agent.llm import ContentPart, LLMResponse, Message, TokenUsage


class _SummaryProvider:
    """Records when _summarize is invoked (single-message 'Summarize...' prompt)."""

    def __init__(self) -> None:
        self.summarize_calls = 0

    @property
    def name(self) -> str:
        return "summary"

    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None):
        if len(messages) == 1 and messages[0].content.startswith("Summarize"):
            self.summarize_calls += 1
        return LLMResponse(
            message=Message(role="assistant", content="compressed"),
            usage=TokenUsage(total_tokens=3),
        )

    async def chat_stream(self, messages, **kwargs):
        raise NotImplementedError


class _MemSummaryStore:
    def __init__(self) -> None:
        self._summaries: dict[tuple[str, str], str] = {}

    async def load_summary(self, scope_id: str, sender_id: str) -> str:
        return self._summaries.get((scope_id, sender_id), "")

    async def save_summary(self, scope_id: str, sender_id: str, summary: str) -> None:
        self._summaries[(scope_id, sender_id)] = summary


def _budget(**overrides: int) -> ContextBudget:
    kwargs = dict(
        max_tokens=200,
        summary_trigger_tokens=80,
        summary_keep_recent_turns=1,
        summary_max_tokens=50,
    )
    kwargs.update(overrides)
    return ContextBudget(**kwargs)


def _long_history() -> list[Message]:
    history: list[Message] = []
    for i in range(6):
        history.append(Message(role="user", content=f"old user {i} " + "很长" * 20))
        history.append(Message(role="assistant", content=f"old assistant {i}"))
    return history


def _latest_user_with_image() -> Message:
    return Message(
        role="user",
        content="latest question",
        content_parts=(
            ContentPart(type="text", text="latest question"),
            ContentPart(type="image_url", image_url="data:image/png;base64,IMG1"),
        ),
    )


def test_image_token_cost_constant() -> None:
    assert _IMAGE_TOKEN_COST == 100


def test_estimate_messages_tokens_counts_images() -> None:
    text = "describe this photo"
    plain = Message(role="user", content=text)
    with_images = Message(
        role="user",
        content=text,
        content_parts=(
            ContentPart(type="text", text=text),
            ContentPart(type="image_url", image_url="data:image/png;base64,AAA"),
            ContentPart(type="image_url", image_url="data:image/png;base64,BBB"),
        ),
    )
    # Two image_url parts add exactly 2 * 100 tokens over the plain text.
    assert estimate_messages_tokens([with_images]) == estimate_messages_tokens([plain]) + 200


def test_fit_messages_preserves_content_parts() -> None:
    messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="old question"),
        _latest_user_with_image(),
    ]
    # Tight budget: the newest image-bearing user message is kept but its
    # text content is truncated to a suffix, while the older user message
    # no longer fits and is dropped.
    fitted = fit_messages_to_budget(messages, 127)

    assert fitted[0].role == "system"
    assert len(fitted) == 2
    latest = fitted[-1]
    assert latest.role == "user"
    # Content was clipped to a non-empty suffix of the original text.
    assert latest.content
    assert "latest question".endswith(latest.content)
    # Truncation (dataclasses.replace) must preserve content_parts untouched.
    assert latest.content_parts is not None
    assert len(latest.content_parts) == 2
    assert latest.content_parts[0].type == "text"
    assert latest.content_parts[1].type == "image_url"
    assert latest.content_parts[1].image_url == "data:image/png;base64,IMG1"
    # The old user message no longer fits.
    assert not any(m.role == "user" and "old" in m.content for m in fitted)


def test_fit_messages_drops_old_not_new() -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="old question" + "x" * 500),
        Message(
            role="user",
            content="new question",
            content_parts=(
                ContentPart(type="text", text="new question"),
                ContentPart(type="image_url", image_url="data:image/png;base64,IMG1"),
            ),
        ),
    ]
    fitted = fit_messages_to_budget(messages, 130)

    assert [m.role for m in fitted] == ["system", "user"]
    latest = fitted[-1]
    assert latest.content == "new question"
    assert len(latest.content_parts or ()) == 2
    assert all("old" not in m.content for m in fitted)


async def test_prepare_reserves_image_budget() -> None:
    provider = _SummaryProvider()
    store = _MemSummaryStore()
    cm = ContextManager(
        provider=provider,
        model="mock",
        temperature=0.3,
        budget=_budget(),
        store=store,
    )
    history = _long_history()

    messages_plain, _ = await cm.prepare(
        scope_id="s1", sender_id="u1", history=history, current_input_text="now"
    )
    messages_with_images, _ = await cm.prepare(
        scope_id="s1",
        sender_id="u1",
        history=history,
        current_input_text="now",
        current_image_count=3,
    )

    # Compaction ran for both calls.
    assert provider.summarize_calls == 2
    # Without images the folded history still fits under the 200-token budget.
    assert messages_plain != []
    assert estimate_messages_tokens(messages_plain) <= 200
    # 3 reserved images (3 * 100 tokens) push budget_for_history to zero,
    # so no history survives the clip.
    assert messages_with_images == []


async def test_prepare_accepts_current_image_count() -> None:
    provider = _SummaryProvider()
    cm = ContextManager(
        provider=provider,
        model="mock",
        temperature=0.3,
        budget=_budget(max_tokens=10_000, summary_trigger_tokens=1_000_000),
        store=_MemSummaryStore(),
    )
    history = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    messages, replacement = await cm.prepare(
        scope_id="s1",
        sender_id="u1",
        history=history,
        current_input_text="what's in this photo?",
        current_image_count=1,
    )

    assert provider.summarize_calls == 0
    assert replacement is None
    assert messages == history
