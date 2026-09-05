"""Tests for the sticker collection tools (save / list / send).

These tools are ``vision_only``: they are only exposed to the LLM when
the agent has ``vision_enabled=True``. They read their dependencies out
of ``ctx.extras`` — an ``image_resolver`` (anything with ``async
resolve(url) -> data_uri | None``), a ``sticker_dir`` on disk, and an
``action_sink`` for delivery — so the tests inject fakes for all three
and drive the tools straight through the global registry, exactly like
a DSL/LLM dispatch would.

Behaviour pinned here:

* ``save_sticker`` resolves the first image reference in the message,
  persists the bytes into ``StickerStore`` and reports ``ok: saved …``.
  Missing resolver/sticker_dir, missing image, download failure and
  store errors all degrade to ``error: …`` strings.
* ``list_stickers`` renders the collection (newest first, max 20 rows)
  and supports a name/tag keyword filter.
* ``send_sticker`` re-sends a saved sticker by name through the
  ``action_sink`` as an ``Action(kind="send")`` carrying an
  ``ImageSegment``.
"""

from __future__ import annotations

import base64
from pathlib import Path

import linling_tools_stdlib  # noqa: F401 — registers sticker_tools into the global registry
import pytest
from linling_core import SqliteKVStore
from linling_core.events import Action, Event, Scope, User
from linling_core.segments import ImageSegment, TextSegment
from linling_core.tools import ToolCtx, registry

# A minimal valid 1x1 transparent PNG.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _png_data_uri(*extra: int) -> str:
    """Build a ``data:image/png;base64,…`` URI over distinct bytes.

    ``StickerStore.save`` keys by md5 of the payload, so two saves must
    use different bytes to produce two separate stickers. The optional
    ``extra`` bytes keep the payload distinct while staying structurally
    irrelevant (the store never inspects the content).
    """
    raw = _PNG_BYTES + bytes(extra)
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeImageResolver:
    """Minimal stand-in for ``linling_agent.images.ImageContentResolver``.

    Resolves URLs against an explicit mapping, falling back to a fixed
    ``default``. Returning ``None`` (mapping value or default) exercises
    the download-failure path.
    """

    def __init__(
        self,
        mapping: dict[str, str | None] | None = None,
        default: str | None = None,
    ) -> None:
        self._mapping = mapping or {}
        self._default = default
        self.resolved_urls: list[str] = []

    async def resolve(self, url: str) -> str | None:
        self.resolved_urls.append(url)
        return self._mapping.get(url, self._default)


class FakeActionSink:
    """Async action sink that records every delivered Action, in order."""

    def __init__(self) -> None:
        self.actions: list[Action] = []

    async def __call__(self, action: Action) -> None:
        self.actions.append(action)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_event(*segments) -> Event:
    """Build a minimal group-message Event carrying the given segments."""
    return Event(
        id="msg-1",
        platform="onebot",
        bot_id="test-bot",
        scope=Scope(kind="group", id="g1", platform="onebot"),
        sender=User(id="u1", platform="onebot"),
        segments=list(segments),
        raw={},
    )


def _make_ctx(
    kv: SqliteKVStore,
    event: Event,
    *,
    resolver: FakeImageResolver | None = None,
    sticker_dir: Path | None = None,
    sink: FakeActionSink | None = None,
) -> ToolCtx:
    """Build a ToolCtx with only the extras the caller opted into."""
    extras: dict = {}
    if resolver is not None:
        extras["image_resolver"] = resolver
    if sticker_dir is not None:
        extras["sticker_dir"] = sticker_dir
    if sink is not None:
        extras["action_sink"] = sink
    return ToolCtx(kv=kv, event=event, bot_id="test-bot", extras=extras)


async def _call_tool(tool_name: str, ctx: ToolCtx, **kwargs) -> str:
    """Invoke a registered tool through the global registry, like the runtime does."""
    td = registry.get(tool_name)
    assert td is not None, f"{tool_name} not registered"
    return await td.fn(ctx, **kwargs)


@pytest.fixture
async def kv() -> SqliteKVStore:
    async with SqliteKVStore("test-bot", ":memory:") as store:
        yield store


@pytest.fixture
def sticker_dir(tmp_path: Path) -> Path:
    return tmp_path / "stickers"


# ---------------------------------------------------------------------------
# save_sticker
# ---------------------------------------------------------------------------


async def test_save_sticker_success(kv, sticker_dir) -> None:
    """A message with one image resolves, persists, and shows up in the list."""
    resolver = FakeImageResolver(default=_png_data_uri())
    event = _make_event(ImageSegment(url="http://x/a.png"))
    ctx = _make_ctx(kv, event, resolver=resolver, sticker_dir=sticker_dir)

    result = await _call_tool("save_sticker", ctx, name="猫")
    assert result.startswith("ok: saved")
    assert "猫" in result

    # The sticker is now listed.
    listing = await _call_tool("list_stickers", ctx)
    assert "猫" in listing
    # And the resolver was asked for the image reference in the message.
    assert resolver.resolved_urls == ["http://x/a.png"]


async def test_save_sticker_no_image(kv, sticker_dir) -> None:
    """A text-only message has no image reference → friendly error."""
    resolver = FakeImageResolver(default=_png_data_uri())
    event = _make_event(TextSegment(text="hello"))
    ctx = _make_ctx(kv, event, resolver=resolver, sticker_dir=sticker_dir)

    result = await _call_tool("save_sticker", ctx, name="猫")
    assert result.startswith("error: no image found")


async def test_save_sticker_no_resolver(kv, sticker_dir) -> None:
    """Missing image_resolver extra → sticker tools unavailable."""
    event = _make_event(ImageSegment(url="http://x/a.png"))
    # sticker_dir is present; only the resolver is missing.
    ctx = _make_ctx(kv, event, sticker_dir=sticker_dir)

    result = await _call_tool("save_sticker", ctx, name="猫")
    assert result == "error: sticker tools not available"


async def test_save_sticker_download_fails(kv, sticker_dir) -> None:
    """Resolver returning None → download failure, nothing persisted."""
    resolver = FakeImageResolver(default=None)
    event = _make_event(ImageSegment(url="http://x/a.png"))
    ctx = _make_ctx(kv, event, resolver=resolver, sticker_dir=sticker_dir)

    result = await _call_tool("save_sticker", ctx, name="猫")
    assert result.startswith("error: could not download")

    # Nothing got persisted.
    listing = await _call_tool("list_stickers", ctx)
    assert listing == "You have no collected stickers yet."


# ---------------------------------------------------------------------------
# list_stickers
# ---------------------------------------------------------------------------


async def test_list_stickers_empty(kv, sticker_dir) -> None:
    """Empty collection → friendly "no stickers yet" message."""
    ctx = _make_ctx(kv, _make_event(), sticker_dir=sticker_dir)
    result = await _call_tool("list_stickers", ctx)
    assert result == "You have no collected stickers yet."


async def test_list_stickers_with_items(kv, sticker_dir) -> None:
    """Saved stickers render as ``- name`` lines, filterable by keyword."""
    resolver = FakeImageResolver(
        {
            "http://x/a.png": _png_data_uri(),
            "http://x/b.png": _png_data_uri(1),
        },
        default=_png_data_uri(),
    )

    ctx_a = _make_ctx(
        kv,
        _make_event(ImageSegment(url="http://x/a.png")),
        resolver=resolver,
        sticker_dir=sticker_dir,
    )
    await _call_tool("save_sticker", ctx_a, name="猫", tags="meme funny")

    ctx_b = _make_ctx(
        kv,
        _make_event(ImageSegment(url="http://x/b.png")),
        resolver=resolver,
        sticker_dir=sticker_dir,
    )
    await _call_tool("save_sticker", ctx_b, name="狗")

    listing = await _call_tool("list_stickers", ctx_a)
    assert "猫" in listing
    assert "狗" in listing
    # Tags are rendered inline.
    assert "tags: meme funny" in listing

    # Keyword filter on name.
    filtered = await _call_tool("list_stickers", ctx_a, query="猫")
    assert "猫" in filtered
    assert "狗" not in filtered

    # Keyword filter matches tags too.
    tag_filtered = await _call_tool("list_stickers", ctx_a, query="funny")
    assert "猫" in tag_filtered
    assert "狗" not in tag_filtered


# ---------------------------------------------------------------------------
# send_sticker
# ---------------------------------------------------------------------------


async def test_send_sticker_success(kv, sticker_dir) -> None:
    """A saved sticker is re-sent through the action sink as an ImageSegment."""
    resolver = FakeImageResolver(default=_png_data_uri())
    save_ctx = _make_ctx(
        kv,
        _make_event(ImageSegment(url="http://x/a.png")),
        resolver=resolver,
        sticker_dir=sticker_dir,
    )
    await _call_tool("save_sticker", save_ctx, name="猫")

    sink = FakeActionSink()
    send_ctx = _make_ctx(
        kv,
        _make_event(TextSegment(text="send it")),
        sticker_dir=sticker_dir,
        sink=sink,
    )
    result = await _call_tool("send_sticker", send_ctx, name="猫")
    assert result == "ok: sent sticker '猫'"

    assert len(sink.actions) == 1
    action = sink.actions[0]
    assert action.kind == "send"
    assert action.target.kind == "group"
    assert action.target.id == "g1"
    assert len(action.segments) == 1
    seg = action.segments[0]
    assert isinstance(seg, ImageSegment)
    assert seg.b64  # non-empty base64 payload
    # The payload round-trips back to the saved PNG bytes.
    assert base64.b64decode(seg.b64) == _PNG_BYTES
    # 原图语义(subType=1):避免 QQ 服务端把收藏的小图压缩放大。
    assert seg.extras.get("subType") == 1


async def test_send_sticker_not_found(kv, sticker_dir) -> None:
    """Unknown sticker name → friendly error, nothing sent."""
    sink = FakeActionSink()
    ctx = _make_ctx(
        kv,
        _make_event(TextSegment(text="send it")),
        sticker_dir=sticker_dir,
        sink=sink,
    )
    result = await _call_tool("send_sticker", ctx, name="不存在的猫")
    assert result.startswith("error: no sticker named")
    assert sink.actions == []


async def test_send_sticker_no_sink(kv, sticker_dir) -> None:
    """Missing action_sink extra → sticker tools unavailable."""
    resolver = FakeImageResolver(default=_png_data_uri())
    save_ctx = _make_ctx(
        kv,
        _make_event(ImageSegment(url="http://x/a.png")),
        resolver=resolver,
        sticker_dir=sticker_dir,
    )
    await _call_tool("save_sticker", save_ctx, name="猫")

    # No action_sink in extras.
    ctx = _make_ctx(
        kv,
        _make_event(TextSegment(text="send it")),
        sticker_dir=sticker_dir,
    )
    result = await _call_tool("send_sticker", ctx, name="猫")
    assert result == "error: sticker tools not available"


def _scheduler_event() -> Event:
    """A scheduler-originated event with no platform (like ``send_reply``)."""
    return Event(
        id="msg-2",
        platform="",
        bot_id="test-bot",
        scope=Scope(kind="group", id="g1", platform="onebot"),
        sender=User(id="u1", platform="onebot"),
        segments=[TextSegment(text="send it")],
        raw={},
    )


async def test_send_sticker_platform_fallback(kv, sticker_dir) -> None:
    """Scheduler events (empty platform) fall back to ``primary_platform``."""
    resolver = FakeImageResolver(default=_png_data_uri())
    save_ctx = _make_ctx(
        kv,
        _make_event(ImageSegment(url="http://x/a.png")),
        resolver=resolver,
        sticker_dir=sticker_dir,
    )
    await _call_tool("save_sticker", save_ctx, name="猫")

    sink = FakeActionSink()
    ctx = ToolCtx(
        kv=kv,
        event=_scheduler_event(),
        bot_id="test-bot",
        extras={"sticker_dir": sticker_dir, "action_sink": sink, "primary_platform": "onebot"},
    )
    result = await _call_tool("send_sticker", ctx, name="猫")
    assert result == "ok: sent sticker '猫'"
    assert sink.actions[0].target.platform == "onebot"


async def test_send_sticker_no_platform(kv, sticker_dir) -> None:
    """Neither event platform nor primary_platform → error, nothing sent."""
    resolver = FakeImageResolver(default=_png_data_uri())
    save_ctx = _make_ctx(
        kv,
        _make_event(ImageSegment(url="http://x/a.png")),
        resolver=resolver,
        sticker_dir=sticker_dir,
    )
    await _call_tool("save_sticker", save_ctx, name="猫")

    sink = FakeActionSink()
    ctx = ToolCtx(
        kv=kv,
        event=_scheduler_event(),
        bot_id="test-bot",
        extras={"sticker_dir": sticker_dir, "action_sink": sink},
    )
    result = await _call_tool("send_sticker", ctx, name="猫")
    assert result == "error: cannot determine platform"
    assert sink.actions == []


async def test_save_sticker_unnamed_keeps_existing_name(kv, sticker_dir) -> None:
    """Re-saving the same bytes with an empty name must NOT overwrite the name.

    ``StickerStore`` dedups by content hash; metadata is only refreshed when
    a non-empty value is provided, so an unnamed re-save keeps the original
    name instead of clobbering it with a blank.
    """
    resolver = FakeImageResolver(default=_png_data_uri())
    ctx = _make_ctx(
        kv,
        _make_event(ImageSegment(url="http://x/a.png")),
        resolver=resolver,
        sticker_dir=sticker_dir,
    )
    first = await _call_tool("save_sticker", ctx, name="猫")
    assert "猫" in first

    # Same bytes (same URL → same data URI), no name this time.
    unnamed = await _call_tool("save_sticker", ctx)
    assert unnamed.startswith("ok: saved (id=")
    assert "猫" not in unnamed

    # The original name survives.
    listing = await _call_tool("list_stickers", ctx)
    assert "猫" in listing
