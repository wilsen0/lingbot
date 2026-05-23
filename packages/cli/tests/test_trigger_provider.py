"""End-to-end: ``/api/agents/<name>/triggers`` reflects the bot's live ruleset.

Pairs with :mod:`tests.test_agents_triggers` (in the WebUI tests),
which covers the route logic with a hand-rolled provider. This test
exercises the *real* wiring: parse a ``.ling`` file, attach the bot
to the WebUI, hit the endpoint, and assert the cleaned labels match
what the inline-suggest panel will render.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from linling_cli.bootstrap import bootstrap_bot
from linling_cli.wire_webui import attach_bot_to_webui
from linling_core.config import BotConfig
from linling_webui.app import create_app
from linling_webui.config import WebUIConfig


class _FakeAgent:
    def __init__(self) -> None:
        self.name = "susu"
        self.provider_name = "fake"
        self.model = "fake-1"

    async def invoke(self, content: str, *, event=None, history=None):  # type: ignore[no-untyped-def]
        class _R:
            content = ""
            tool_calls_made = 0
            total_tokens = 0

        return _R()


def _write(base: Path, rel: str, content: str) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def trig_app(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    # A small but representative slice: literal triggers, regex
    # captures, and a ``[内部]`` block (which must NOT show up in
    # the suggest panel — internal handlers are unreachable from
    # user text).
    _write(
        tmp_path,
        "rules/main.ling",
        "我的灵玉\n灵玉数 999\n\n"
        "反馈丢失(.*)\n好的\n\n"
        "充值([0-9]+)\n收到\n\n"
        "狐妖更新微博备用\n咕\n\n"
        "[内部]接扔瓶子\n返回\n",
    )
    cfg_path = _write(
        tmp_path,
        "bot.yaml",
        "bot_id: trig_test\n"
        "name: trig_test\n"
        "storage:\n"
        "  kv: ':memory:'\n"
        "rules:\n"
        "  - 'rules/**/*.ling'\n",
    )

    import asyncio

    cfg = BotConfig.from_yaml(cfg_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = loop.run_until_complete(bootstrap_bot(cfg, base_dir=tmp_path))
    bot.agents = {"susu": _FakeAgent()}

    web_cfg = WebUIConfig(
        auth_db_path=tmp_path / "auth.db",
        jwt_secret="test-secret",
        access_token_ttl_s=60,
        refresh_token_ttl_s=300,
        login_rate_per_minute=10_000,
    )
    app = create_app(web_cfg)
    app.state.runtime.auth.upsert_user("admin", "Test-Pwd-9!aB", role="superadmin")
    attach_bot_to_webui(app, bot)

    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Test-Pwd-9!aB"},
        ).json()["access"]
        try:
            yield client, token
        finally:
            loop.run_until_complete(bot.stop())
            loop.close()


def test_triggers_endpoint_returns_cleaned_labels(trig_app) -> None:
    client, token = trig_app
    r = client.get(
        "/api/agents/susu/triggers",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    by_label = {item["label"]: item for item in body}

    # Literal trigger: round-trips intact, no args.
    assert "我的灵玉" in by_label
    assert by_label["我的灵玉"]["has_args"] is False
    assert by_label["我的灵玉"]["literal_prefix"] == "我的灵玉"

    # Regex trigger: capture groups become a `…` placeholder, with
    # the literal_prefix carrying just the leading text.
    assert "反馈丢失…" in by_label
    assert by_label["反馈丢失…"]["has_args"] is True
    assert by_label["反馈丢失…"]["literal_prefix"] == "反馈丢失"

    assert "充值…" in by_label
    assert by_label["充值…"]["has_args"] is True
    assert by_label["充值…"]["literal_prefix"] == "充值"

    # ``[内部]`` handlers must never leak — they're not user-typeable.
    for item in body:
        assert "接扔瓶子" not in item["label"]
        assert "[内部]" not in item["raw"]

    # ``备用`` rules are spare / not-yet-live — hidden from the picker.
    # Their handlers still run when typed verbatim; we just don't
    # advertise them.
    for item in body:
        assert "备用" not in item["label"], item
