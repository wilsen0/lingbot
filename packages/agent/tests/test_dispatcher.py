"""Tests for the multimodal (vision) path in AgentChatDispatcher.

Covers the pure-image / mixed text+image handling introduced by the
vision feature: when ``vision_enabled`` (agent def + image resolver both
present) a pure non-text message is surfaced to the LLM as a ``[图片]``
placeholder plus resolved image content parts, while session history only
ever stores the pure-text placeholder (``content_parts`` never persist).
"""

from __future__ import annotations

import base64
from pathlib import Path

from linling_agent.agent_def import AgentDef
from linling_agent.dispatcher import AgentChatDispatcher
from linling_agent.runtime import AgentResult
from linling_core.events import Event, Scope, User
from linling_core.pipeline import ConversationKey, ConversationStore
from linling_core.segments import ImageSegment, TextSegment
from linling_core.storage.sqlite_kv import SqliteKVStore


class _FakeImageResolver:
    """Resolve image refs to fixed data URIs; records calls."""

    def __init__(self, data_uris: list[str] | None = None) -> None:
        self.data_uris = list(data_uris or [])
        self.calls: list[tuple[list[str], int | None]] = []

    async def resolve_batch(self, urls, *, limit=None):
        self.calls.append((list(urls), limit))
        return list(self.data_uris)


class _FakeAgentRuntime:
    """Minimal agent surface that ``AgentChatDispatcher.dispatch`` touches.

    Provides ``agent_def`` (for the ``vision_enabled`` property and
    ``guardrails.max_tokens``) and ``invoke`` (which the dispatcher races
    against ``session.cancel_event``). ``invoke`` records exactly what the
    dispatcher asked it to send, so tests can assert on the user text and
    ``user_content_parts`` without standing up a full ReAct loop.
    """

    def __init__(self, *, vision_enabled: bool = False, content: str = "ok") -> None:
        self.agent_def = AgentDef(
            name="fake",
            model="mock",
            system="",
            vision_enabled=vision_enabled,
        )
        self.result = AgentResult(content=content)
        self.invoke_calls: list[dict[str, object]] = []

    async def invoke(
        self,
        user_input,
        *,
        event=None,
        history=None,
        context_max_tokens=None,
        action_sink=None,
        user_content_parts=None,
    ):
        self.invoke_calls.append(
            {
                "user_input": user_input,
                "history": list(history) if history is not None else None,
                "user_content_parts": (
                    list(user_content_parts) if user_content_parts is not None else None
                ),
            }
        )
        return self.result


def _event(segments: list) -> Event:
    return Event(
        id="m1",
        platform="test",
        bot_id="bot1",
        scope=Scope(kind="group", id="g1", platform="test"),
        sender=User(id="u1", platform="test", display_name="小明"),
        segments=segments,
    )


def _image_event(url: str = "https://x/a.png") -> Event:
    return _event([ImageSegment(url=url)])


def _mixed_event(text: str = "看这个", url: str = "https://x/a.png") -> Event:
    return _event([TextSegment(text=text), ImageSegment(url=url)])


async def _session():
    store = ConversationStore(rate_per_second=100, burst=100)
    return await store.get_or_create(ConversationKey("bot1", "g1", "u1"))


async def test_vision_disabled_pure_image_dropped() -> None:
    """No resolver → pure image message is dropped, nothing recorded."""
    agent = _FakeAgentRuntime(vision_enabled=False)
    dispatcher = AgentChatDispatcher(agent=agent)
    session = await _session()

    result = await dispatcher.dispatch(_image_event(), session)

    assert result is None
    assert list(session.history) == []
    assert agent.invoke_calls == []


async def test_vision_enabled_pure_image_processed() -> None:
    """Vision on: image is surfaced as ``[图片]`` + content parts; history
    stores only the pure-text placeholder (the noise-reduction contract)."""
    resolver = _FakeImageResolver(["data:image/png;base64,AAAA"])
    agent = _FakeAgentRuntime(vision_enabled=True)
    dispatcher = AgentChatDispatcher(agent=agent, image_resolver=resolver)
    session = await _session()

    result = await dispatcher.dispatch(_image_event(), session)

    assert result is not None
    assert len(agent.invoke_calls) == 1
    call = agent.invoke_calls[0]
    assert call["user_input"] == "[图片]"
    parts = call["user_content_parts"]
    assert parts is not None
    assert [p.type for p in parts] == ["text", "image_url"]
    assert parts[0].text == "[图片]"
    assert parts[1].image_url == "data:image/png;base64,AAAA"
    # History: only the text placeholder, never content parts.
    assert len(session.history) == 2
    user_msg = session.history[0]
    assert user_msg.role == "user"
    assert user_msg.content == "[图片]"
    assert user_msg.content_parts is None


async def test_vision_enabled_no_resolvable_image_dropped() -> None:
    """Vision on but the resolver returns nothing → message is dropped."""
    resolver = _FakeImageResolver([])  # download failed
    agent = _FakeAgentRuntime(vision_enabled=True)
    dispatcher = AgentChatDispatcher(agent=agent, image_resolver=resolver)
    session = await _session()

    result = await dispatcher.dispatch(_image_event(), session)

    assert result is None
    assert list(session.history) == []
    assert agent.invoke_calls == []


async def test_vision_enabled_mixed_message() -> None:
    """Text + image: LLM sees both, history keeps the plain text only."""
    resolver = _FakeImageResolver(["data:image/png;base64,BBBB"])
    agent = _FakeAgentRuntime(vision_enabled=True)
    dispatcher = AgentChatDispatcher(agent=agent, image_resolver=resolver)
    session = await _session()

    result = await dispatcher.dispatch(_mixed_event(), session)

    assert result is not None
    assert len(agent.invoke_calls) == 1
    call = agent.invoke_calls[0]
    assert call["user_input"] == "看这个"
    parts = call["user_content_parts"]
    assert parts is not None
    assert parts[0].type == "text" and parts[0].text == "看这个"
    assert parts[1].type == "image_url"
    assert parts[1].image_url == "data:image/png;base64,BBBB"
    user_msg = session.history[0]
    assert user_msg.content == "看这个"
    assert user_msg.content_parts is None


def test_vision_enabled_property() -> None:
    # No resolver → off even when the agent def allows vision.
    dispatcher = AgentChatDispatcher(agent=_FakeAgentRuntime(vision_enabled=True))
    assert dispatcher.vision_enabled is False

    # Agent def vision off → off regardless of the resolver.
    dispatcher = AgentChatDispatcher(
        agent=_FakeAgentRuntime(vision_enabled=False),
        image_resolver=_FakeImageResolver(["data:image/png;base64,AAAA"]),
    )
    assert dispatcher.vision_enabled is False

    # Both present → on.
    dispatcher = AgentChatDispatcher(
        agent=_FakeAgentRuntime(vision_enabled=True),
        image_resolver=_FakeImageResolver(["data:image/png;base64,AAAA"]),
    )
    assert dispatcher.vision_enabled is True


# ---------------------------------------------------------------------------
# Sticker collection collage (DM path)
# ---------------------------------------------------------------------------


def _collage_dispatcher(
    tmp_path, *, vision: bool = True
) -> tuple[AgentChatDispatcher, _FakeAgentRuntime, SqliteKVStore, Path]:
    kv = SqliteKVStore(bot_id="bot1", db_path=":memory:")
    sticker_dir = tmp_path / "stickers"
    agent = _FakeAgentRuntime(vision_enabled=vision)
    dispatcher = AgentChatDispatcher(
        agent=agent,
        image_resolver=_FakeImageResolver(["data:image/png;base64,AAAA"]),
        kv=kv,
        sticker_dir=sticker_dir,
    )
    return dispatcher, agent, kv, sticker_dir


async def _seed_collage_stickers(kv: SqliteKVStore, sticker_dir: Path, count: int) -> None:
    from linling_agent.sticker_store import StickerStore

    # A minimal valid 1x1 transparent PNG; PIL must be able to decode it
    # for the collage thumbnails. Trailing distinct bytes keep each save
    # content-unique without breaking the format.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    store = StickerStore(kv, sticker_dir)
    for index in range(count):
        await store.save(png + bytes([index]), name=f"猫{index}")


def _image_urls(parts) -> list[str]:
    return [p.image_url for p in parts if p.type == "image_url"]


async def test_vision_collage_attached_when_stickers_saved(tmp_path) -> None:
    """DM vision on + saved stickers → the collage rides along as the last
    image part (candidate image first), and the system prompt explains it."""
    dispatcher, agent, kv, sticker_dir = _collage_dispatcher(tmp_path)
    await _seed_collage_stickers(kv, sticker_dir, 1)
    session = await _session()

    await dispatcher.dispatch(_image_event(), session)

    assert len(agent.invoke_calls) == 1
    parts = agent.invoke_calls[0]["user_content_parts"]
    urls = _image_urls(parts)
    assert len(urls) == 2  # 1 candidate + 1 collage
    assert urls[-1].startswith("data:image/jpeg;base64,")
    # The collage explanation is injected as a system prefix message.
    history = agent.invoke_calls[0]["history"]
    system_msgs = [m for m in history if m.role == "system"]
    assert any("九宫格" in m.content for m in system_msgs)


async def test_vision_collage_omitted_when_no_stickers(tmp_path) -> None:
    """DM vision on but nothing saved → only the candidate image."""
    dispatcher, agent, kv, sticker_dir = _collage_dispatcher(tmp_path)
    await _seed_collage_stickers(kv, sticker_dir, 0)
    session = await _session()

    await dispatcher.dispatch(_image_event(), session)

    assert len(agent.invoke_calls) == 1
    parts = agent.invoke_calls[0]["user_content_parts"]
    assert len(_image_urls(parts)) == 1


async def test_vision_collage_on_text_message(tmp_path) -> None:
    """DM vision on + saved stickers → even a plain-text message carries the
    collage (text part first, collage after)."""
    dispatcher, agent, kv, sticker_dir = _collage_dispatcher(tmp_path)
    await _seed_collage_stickers(kv, sticker_dir, 1)
    session = await _session()

    await dispatcher.dispatch(_event([TextSegment(text="你好")]), session)

    assert len(agent.invoke_calls) == 1
    parts = agent.invoke_calls[0]["user_content_parts"]
    assert parts is not None
    assert parts[0].type == "text" and parts[0].text == "你好"
    urls = _image_urls(parts)
    assert len(urls) == 1
    assert urls[0].startswith("data:image/jpeg;base64,")


async def test_vision_collage_omitted_when_vision_off(tmp_path) -> None:
    """DM vision off → no collage, and pure-image messages stay dropped."""
    dispatcher, agent, kv, sticker_dir = _collage_dispatcher(tmp_path, vision=False)
    await _seed_collage_stickers(kv, sticker_dir, 1)
    session = await _session()

    result = await dispatcher.dispatch(_image_event(), session)

    assert result is None
    assert agent.invoke_calls == []
