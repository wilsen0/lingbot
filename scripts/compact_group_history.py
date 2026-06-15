"""手动触发所有群聊会话的历史压缩流程。

读取 KV 存储中的群聊历史，对每个有足够 turn 的群调用 LLM 做摘要压缩，
然后把压缩后的近期历史 + 摘要写回。

用法:
    uv run python scripts/compact_group_history.py
    uv run python scripts/compact_group_history.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "packages" / "core" / "src"))
sys.path.insert(0, str(_PROJECT_ROOT / "packages" / "agent" / "src"))
sys.path.insert(0, str(_PROJECT_ROOT / "packages" / "dsl" / "src"))

from linling_core.storage.sqlite_kv import SqliteKVStore

from linling_agent.context import ContextBudget, ContextManager
from linling_agent.history import KVHistoryStore
from linling_agent.llm import Message
from linling_agent.providers.openai import OpenAIProvider

_HISTORY_PREFIX = "__history__/"
_GROUP_FILE = "_group"

_BOT_ID = "linling"
_DB_PATH = _PROJECT_ROOT / "bot" / "data.sqlite"

# Match daily_summary_keep_recent_turns from bot.yaml (not the regular
# summary_keep_recent_turns=8), since group batch compaction uses this
# smaller value to keep group history tight.
_KEEP_RECENT_TURNS = 2


def _load_env() -> dict[str, str]:
    env_path = _PROJECT_ROOT / "bot" / ".env"
    if not env_path.exists():
        env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
    return {
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "base_url": os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        "model": os.environ.get("LINLING_MODEL", "gpt-4o-mini"),
    }


async def _find_group_scopes(kv: SqliteKVStore) -> list[str]:
    scopes = await kv.scopes()
    group_scopes: list[str] = []
    for scope in scopes:
        if not scope.startswith(_HISTORY_PREFIX):
            continue
        files = await kv.files(scope)
        if _GROUP_FILE in files:
            group_scopes.append(scope)
    return sorted(group_scopes)


async def _load_messages(kv: SqliteKVStore, scope: str) -> list[Message]:
    history = KVHistoryStore(kv, max_turns=64)
    scope_id = scope[len(_HISTORY_PREFIX):]
    return await history.load(scope_id, "")


async def _load_summary(kv: SqliteKVStore, scope: str) -> str:
    history = KVHistoryStore(kv, max_turns=64)
    scope_id = scope[len(_HISTORY_PREFIX):]
    return await history.load_summary(scope_id, "")


async def main() -> None:
    parser = argparse.ArgumentParser(description="手动压缩群聊历史")
    parser.add_argument("--dry-run", action="store_true", help="只检查不执行")
    args = parser.parse_args()

    if not _DB_PATH.exists():
        print(f"KV 数据库不存在: {_DB_PATH}")
        sys.exit(1)

    env = _load_env()
    if not env["api_key"]:
        print("未找到 LLM_API_KEY，无法调用 LLM 做摘要")
        sys.exit(1)

    kv = SqliteKVStore(bot_id=_BOT_ID, db_path=str(_DB_PATH))
    try:
        group_scopes = await _find_group_scopes(kv)
        if not group_scopes:
            print("未找到任何群聊历史")
            return

        print(f"找到 {len(group_scopes)} 个群聊历史\n")

        provider = OpenAIProvider(
            api_key=env["api_key"],
            base_url=env["base_url"],
            model=env["model"],
        )
        history_store = KVHistoryStore(kv, max_turns=64)
        budget = ContextBudget(
            max_tokens=65536,
            summary_trigger_tokens=60000,
            summary_keep_recent_turns=_KEEP_RECENT_TURNS,
            summary_max_tokens=8000,
        )
        context = ContextManager(
            provider=provider,
            model=env["model"],
            temperature=0.3,
            budget=budget,
            store=history_store,
        )

        for scope in group_scopes:
            scope_id = scope[len(_HISTORY_PREFIX):]
            messages = await _load_messages(kv, scope)
            existing_summary = await _load_summary(kv, scope)
            turn_count = len(messages)

            status_parts = [f"[{scope_id}]"]
            status_parts.append(f"{turn_count} turns")
            if existing_summary:
                status_parts.append(f"已有摘要 ({len(existing_summary)} 字)")
            else:
                status_parts.append("无摘要")

            keep_msgs = _KEEP_RECENT_TURNS * 2
            older_count = max(0, turn_count - keep_msgs)

            if older_count == 0:
                print(f"  {' '.join(status_parts)} → 跳过 (全部在保留范围内)")
                continue

            print(f"  {' '.join(status_parts)}")

            if args.dry_run:
                print(f"    → [dry-run] 将压缩 {older_count} 条旧消息, 保留最近 {keep_msgs} 条")
                continue

            visible, replacement = await context.prepare(
                scope_id=scope_id,
                sender_id="",
                history=list(messages),
                current_input_text="",
                force_compaction=True,
                summary_keep_recent_turns=_KEEP_RECENT_TURNS,
            )

            if replacement is None:
                print(f"    → 未触发压缩 (历史可能已经足够短)")
                continue

            await history_store.save(scope_id, "", replacement)

            new_summary = await history_store.load_summary(scope_id, "")
            print(
                f"    → 压缩完成: {len(messages)} → {len(replacement)} 条消息, "
                f"摘要 {len(new_summary)} 字"
            )

        await provider.aclose()
    finally:
        await kv.close()


if __name__ == "__main__":
    asyncio.run(main())
