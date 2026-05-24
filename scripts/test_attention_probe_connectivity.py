"""Connectivity smoke test for the lightweight attention-probe LLM.

Loads the workspace ``.env`` (via the same path the CLI uses), constructs
an :class:`AttentionProbe` exactly the way ``linling_cli.bootstrap``
does, and fires two real ``judge`` calls — one batch that should clearly
warrant a reply, one that should not — so we can confirm both routing
paths work end-to-end against the configured endpoint.

Run with::

    uv run python scripts/test_attention_probe_connectivity.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _load_dotenv(env_path: Path) -> None:
    """Minimal .env loader.

    We avoid importing ``python-dotenv`` so this script stays runnable
    even if the dev deps aren't synced. Existing env vars win — same
    precedence rule the real CLI uses.
    """
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

    # Import after .env is loaded so any module-level env reads see it.
    from linling_agent.attention_probe import AttentionProbe, _ProbeBatchInput

    api_key = (
        os.environ.get("ATTENTION_PROBE_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    base_url = (
        os.environ.get("ATTENTION_PROBE_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or "https://api.openai.com/v1"
    )
    model = os.environ.get("ATTENTION_PROBE_MODEL", "").strip() or "gpt-4o-mini"
    proxy = (
        os.environ.get("ATTENTION_PROBE_HTTPS_PROXY", "").strip()
        or os.environ.get("OPENAI_HTTPS_PROXY", "").strip()
        or None
    )

    if not api_key:
        print("[FAIL] no API key resolved (ATTENTION_PROBE_API_KEY / OPENAI_API_KEY)")
        return 2

    masked = f"{api_key[:6]}…{api_key[-4:]}" if len(api_key) > 12 else "***"
    print("--- Attention Probe Connectivity ---")
    print(f"  base_url = {base_url}")
    print(f"  model    = {model}")
    print(f"  api_key  = {masked}")
    print(f"  proxy    = {proxy or '(direct)'}")
    print()

    probe = AttentionProbe(api_key=api_key, base_url=base_url, model=model, proxy=proxy)

    # Case 1: a clearly addressed question — expect yes.
    yes_batch = [
        _ProbeBatchInput(
            message_id="m1",
            sender_name="Alice",
            timestamp="2026-05-24T12:00:00",
            text="@bot 帮我查一下今天北京的天气怎么样?",
        ),
        _ProbeBatchInput(
            message_id="m2",
            sender_name="Bob",
            timestamp="2026-05-24T12:00:05",
            text="是啊,顺便看看明天会不会下雨。",
        ),
    ]

    # Case 2: pure off-topic chatter — expect no.
    no_batch = [
        _ProbeBatchInput(
            message_id="m3",
            sender_name="Carol",
            timestamp="2026-05-24T12:01:00",
            text="哈哈哈哈哈",
        ),
        _ProbeBatchInput(
            message_id="m4",
            sender_name="Dave",
            timestamp="2026-05-24T12:01:02",
            text="6",
        ),
    ]

    exit_code = 0
    try:
        for label, batch, expected in (
            ("yes_case", yes_batch, True),
            ("no_case", no_batch, False),
        ):
            try:
                verdict = await probe.judge(batch, scope_id=f"smoke::{label}")
            except Exception as exc:  # noqa: BLE001 — we want to surface anything.
                print(f"[FAIL] {label}: judge() raised {type(exc).__name__}: {exc}")
                exit_code = 1
                continue
            ok = "OK" if verdict is expected else "WARN"
            print(
                f"[{ok}] {label}: verdict={verdict} "
                f"(expected={expected})"
            )
            # A wrong verdict is not a connectivity failure — the call
            # still succeeded — so we don't flip the exit code on it.
    finally:
        await probe.aclose()

    if exit_code == 0:
        print("\nProbe is reachable and responded for both batches.")
    else:
        print("\nProbe call failed; see messages above.")
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
