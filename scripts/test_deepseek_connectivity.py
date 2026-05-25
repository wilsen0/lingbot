"""Connectivity smoke test for DeepSeek V4 Flash via OpenAIProvider.

Verifies:
1. Single-turn chat works and reasoning_content is parsed
2. Multi-turn with reasoning_content round-trip (tool_calls scenario)
3. No 400 error from missing reasoning_content

Run with::

    uv run python scripts/test_deepseek_connectivity.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    _load_dotenv(repo_root / ".env")

    from linling_agent.llm import Message, ToolCall, ToolSchema
    from linling_agent.providers.openai import OpenAIProvider

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    model = os.environ.get("LINLING_MODEL", "").strip() or "gpt-4o-mini"
    proxy = os.environ.get("OPENAI_HTTPS_PROXY", "").strip() or None

    if not api_key:
        print("[FAIL] no OPENAI_API_KEY")
        return 2

    masked = f"{api_key[:6]}…{api_key[-4:]}" if len(api_key) > 12 else "***"
    print("--- DeepSeek V4 Flash Connectivity ---")
    print(f"  base_url = {base_url}")
    print(f"  model    = {model}")
    print(f"  api_key  = {masked}")
    print(f"  proxy    = {proxy or '(direct)'}")
    print()

    provider = OpenAIProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        proxy=proxy,
        timeout=30.0,
    )

    exit_code = 0
    try:
        # --- Test 1: single turn ---
        print("[TEST 1] Single-turn chat...")
        resp = await provider.chat(
            [Message(role="user", content="回复一个字:好")],
            max_tokens=64,
        )
        print(f"  content           = {resp.message.content!r}")
        print(f"  reasoning_content = {resp.message.reasoning_content!r}")
        if resp.message.reasoning_content is None:
            print("  [WARN] reasoning_content is None — thinking mode may be off")
        else:
            print("  [OK] reasoning_content present")

        # --- Test 2: multi-turn with reasoning_content round-trip ---
        print("\n[TEST 2] Multi-turn with reasoning_content round-trip...")
        # Simulate: turn 1 response had tool_calls + reasoning_content
        # Then tool result comes back, then we send turn 2
        turn1_assistant = Message(
            role="assistant",
            content="",
            reasoning_content="我需要调用工具来获取日期",
            tool_calls=[
                ToolCall(id="call_001", name="get_date", arguments="{}")
            ],
        )
        turn1_tool_result = Message(
            role="tool",
            content="2026-05-25",
            name="get_date",
            tool_call_id="call_001",
        )
        messages = [
            Message(role="user", content="今天几号?"),
            turn1_assistant,
            turn1_tool_result,
        ]
        try:
            resp2 = await provider.chat(messages, max_tokens=64)
            print(f"  content           = {resp2.message.content!r}")
            print(f"  reasoning_content = {resp2.message.reasoning_content!r}")
            print("  [OK] No 400 error — reasoning_content round-trip works")
        except Exception as exc:
            print(f"  [FAIL] {type(exc).__name__}: {exc}")
            exit_code = 1

        # --- Test 3: multi-turn WITHOUT tool_calls (reasoning_content ignored) ---
        print("\n[TEST 3] Multi-turn without tool_calls (reasoning_content optional)...")
        messages3 = [
            Message(role="user", content="你好"),
            Message(role="assistant", content="你好呀！", reasoning_content="用户打招呼"),
            Message(role="user", content="1+1=?"),
        ]
        try:
            resp3 = await provider.chat(messages3, max_tokens=64)
            print(f"  content = {resp3.message.content!r}")
            print("  [OK] Multi-turn without tool_calls works")
        except Exception as exc:
            print(f"  [FAIL] {type(exc).__name__}: {exc}")
            exit_code = 1

    finally:
        await provider.aclose()

    print()
    if exit_code == 0:
        print("All tests passed. DeepSeek V4 Flash is ready.")
    else:
        print("Some tests failed — see above.")
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
