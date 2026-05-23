"""Authentication layer for linling-webui.

- Password hashing: argon2id (m=64MB, t=3, p=2).
- Tokens: JWT HS256, access 15m, refresh 7d; refresh tokens are stored in a
  sqlite table so they can be revoked on logout / password change.
- User table is also sqlite, keyed by username.

The store is intentionally local (sqlite) and independent of the main KV
store so that rotating auth DBs doesn't touch bot state.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from jose import JWTError, jwt

Role = Literal["superadmin", "bot_admin", "readonly"]

# argon2 params match the design doc.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # KiB → 64 MiB
    parallelism=2,
)

_USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username     TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role         TEXT NOT NULL,
    bots_json    TEXT NOT NULL DEFAULT '[]',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    jti          TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    issued_at    INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS refresh_user ON refresh_tokens(username);
"""


@dataclass
class User:
    """Authenticated user summary (no secrets)."""

    username: str
    role: Role
    bots: list[str]


@dataclass
class TokenPair:
    """Access + refresh JWT pair returned by /login and /refresh."""

    access: str
    refresh: str
    access_expires_at: int  # epoch seconds
    refresh_expires_at: int


class AuthStore:
    """Synchronous sqlite-backed auth store.

    Sqlite sync is fine for login volume (login, refresh, profile). We
    keep a dedicated connection per instance; uvicorn's default thread
    pool handles concurrent requests.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_USER_SCHEMA)
        self._conn.commit()

    # ---- user CRUD --------------------------------------------------

    def upsert_user(
        self,
        username: str,
        password: str,
        *,
        role: Role = "superadmin",
        bots: list[str] | None = None,
    ) -> None:
        """Create or update a user's password / role / bots.

        Side effect: any existing refresh tokens for the user are
        revoked. Rationale: an admin who resets a compromised user's
        password expects every active session to drop, not just the
        access tokens (which expire in 15 minutes anyway). The newly
        seeded password issues new tokens at the next ``/login``.
        """
        existed = self.get_user(username) is not None
        now = int(time.time())
        password_hash = _hasher.hash(password)

        self._conn.execute(
            "INSERT INTO users(username, password_hash, role, bots_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET "
            "  password_hash=excluded.password_hash, "
            "  role=excluded.role, bots_json=excluded.bots_json, "
            "  updated_at=excluded.updated_at",
            (username, password_hash, role, json.dumps(bots or []), now, now),
        )
        if existed:
            self._conn.execute(
                "UPDATE refresh_tokens SET revoked=1 WHERE username=?", (username,)
            )
        self._conn.commit()

    def delete_user(self, username: str) -> None:
        self._conn.execute("DELETE FROM users WHERE username=?", (username,))
        self._conn.execute("DELETE FROM refresh_tokens WHERE username=?", (username,))
        self._conn.commit()

    def has_any_user(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) > 0

    def get_user(self, username: str) -> User | None:
        row = self._conn.execute(
            "SELECT username, role, bots_json FROM users WHERE username=?", (username,)
        ).fetchone()
        if row is None:
            return None
        return User(
            username=str(row["username"]),
            role=str(row["role"]),  # type: ignore[arg-type]
            bots=json.loads(row["bots_json"]) or [],
        )

    def verify_password(self, username: str, password: str) -> User | None:
        """Return the user if password matches; None otherwise.

        Runs argon2 even for unknown users to preserve timing behaviour.
        """
        row = self._conn.execute(
            "SELECT password_hash FROM users WHERE username=?", (username,)
        ).fetchone()
        if row is None:
            # Dummy verify to smooth timing
            with contextlib.suppress(VerifyMismatchError, InvalidHash, Exception):
                _hasher.verify(
                    "$argon2id$v=19$m=65536,t=3,p=2$YWJjZGVmZ2hpamtsbW5vcA$"
                    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    password,
                )
            return None
        try:
            _hasher.verify(str(row["password_hash"]), password)
        except VerifyMismatchError:
            return None
        except (InvalidHash, Exception):
            return None
        return self.get_user(username)

    # ---- refresh token tracking ------------------------------------

    def record_refresh(self, username: str, jti: str, expires_at: int) -> None:
        self._conn.execute(
            "INSERT INTO refresh_tokens(jti, username, issued_at, expires_at, revoked) "
            "VALUES(?,?,?,?,0)",
            (jti, username, int(time.time()), expires_at),
        )
        self._conn.commit()

    def is_refresh_valid(self, jti: str) -> bool:
        row = self._conn.execute(
            "SELECT expires_at, revoked FROM refresh_tokens WHERE jti=?", (jti,)
        ).fetchone()
        if row is None:
            return False
        if int(row["revoked"]) == 1:
            return False
        return int(row["expires_at"]) > int(time.time())

    def revoke_refresh(self, jti: str) -> None:
        self._conn.execute("UPDATE refresh_tokens SET revoked=1 WHERE jti=?", (jti,))
        self._conn.commit()

    def revoke_all_for(self, username: str) -> None:
        self._conn.execute("UPDATE refresh_tokens SET revoked=1 WHERE username=?", (username,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ---- JWT helpers -----------------------------------------------------


def issue_tokens(
    user: User,
    *,
    secret: str,
    algorithm: str,
    access_ttl_s: int,
    refresh_ttl_s: int,
    store: AuthStore | None = None,
) -> TokenPair:
    """Issue a fresh (access, refresh) pair for the given user."""
    now = int(time.time())
    access_exp = now + access_ttl_s
    refresh_exp = now + refresh_ttl_s
    refresh_jti = uuid.uuid4().hex

    access_payload: dict[str, Any] = {
        "sub": user.username,
        "role": user.role,
        "bots": user.bots,
        "typ": "access",
        "iat": now,
        "exp": access_exp,
    }
    refresh_payload: dict[str, Any] = {
        "sub": user.username,
        "typ": "refresh",
        "jti": refresh_jti,
        "iat": now,
        "exp": refresh_exp,
    }

    access = jwt.encode(access_payload, secret, algorithm=algorithm)
    refresh = jwt.encode(refresh_payload, secret, algorithm=algorithm)

    if store is not None:
        store.record_refresh(user.username, refresh_jti, refresh_exp)

    return TokenPair(
        access=access,
        refresh=refresh,
        access_expires_at=access_exp,
        refresh_expires_at=refresh_exp,
    )


def decode_token(token: str, *, secret: str, algorithm: str) -> dict[str, Any] | None:
    """Verify the JWT signature + exp and return the claims, or None."""
    try:
        claims: dict[str, Any] = jwt.decode(token, secret, algorithms=[algorithm])
    except JWTError:
        return None
    return claims


def random_password() -> str:
    """Generate a readable random password (used by `webui_init_user`)."""
    return secrets.token_urlsafe(16)
