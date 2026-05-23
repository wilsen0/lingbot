"""WebUI configuration.

`WebUIConfig` 可由 `bot.yaml` 的 `webui:` 段构造，或独立用 env vars 驱动
（`LINLING_WEBUI_*`）。生产环境至少需要自定义 `jwt_secret`。
"""

from __future__ import annotations

import secrets
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_static_dir() -> Path:
    """Return the bundled static/ directory beside this file."""
    return Path(__file__).resolve().parent / "static"


class WebUIConfig(BaseSettings):
    """Runtime configuration for the WebUI server.

    Precedence: constructor args > env vars (`LINLING_WEBUI_*`) > defaults.
    """

    model_config = SettingsConfigDict(
        env_prefix="LINLING_WEBUI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Networking --------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8787
    root_path: str = ""  # for reverse-proxy mounting (e.g. "/webui")

    # ---- CORS --------------------------------------------------------
    # Empty list = same-origin only (default).
    cors_origins: list[str] = Field(default_factory=list)

    # ---- JWT ---------------------------------------------------------
    # IMPORTANT: override in production. Default is ephemeral per-process.
    jwt_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    jwt_algorithm: str = "HS256"
    access_token_ttl_s: int = 15 * 60
    refresh_token_ttl_s: int = 7 * 24 * 3600

    # ---- Storage -----------------------------------------------------
    # sqlite path holding users + refresh tokens (argon2id hashed).
    auth_db_path: Path = Path("./data/webui_auth.db")

    # ---- Static SPA --------------------------------------------------
    static_dir: Path = Field(default_factory=_default_static_dir)

    # ---- Event buffer ------------------------------------------------
    event_buffer_size: int = 500  # per-bot ring buffer capacity

    # ---- Rate limits -------------------------------------------------
    login_rate_per_minute: int = 5
    write_rate_per_minute: int = 60

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _coerce_cors(cls, v: object) -> list[str]:
        """Accept comma-separated strings from env vars."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        if isinstance(v, list):
            return [str(item) for item in v]
        return []

    @classmethod
    def from_bot_yaml_section(cls, section: dict[str, object] | None) -> WebUIConfig:
        """Build config from the ``webui:`` block of bot.yaml (may be None).

        pydantic validates the values per-field, so passing the raw
        ``dict[str, object]`` would make mypy fight each field type;
        widening to ``Any`` here keeps the call site clean while still
        getting runtime validation from pydantic.
        """
        from typing import Any  # noqa: PLC0415

        raw: dict[str, Any] = {k: v for k, v in (section or {}).items() if v is not None}
        return cls(**raw)
