"""`linling webui-init-user` — seed or rotate an admin user.

Usage:
    python -m linling_webui.scripts.init_user --username admin --password ... --db ./data/webui_auth.db

If ``--password`` is omitted, a strong random password is generated and
printed (remember to save it).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from linling_webui.auth import AuthStore, random_password


def main() -> None:
    parser = argparse.ArgumentParser(description="seed linling-webui admin user")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", default=None)
    parser.add_argument(
        "--role", default="superadmin", choices=["superadmin", "bot_admin", "readonly"]
    )
    parser.add_argument(
        "--db",
        default="./data/webui_auth.db",
        help="path to auth sqlite db (matches WebUIConfig.auth_db_path)",
    )
    parser.add_argument(
        "--bot", action="append", default=[], help="bot_id this user can see (repeatable)"
    )
    args = parser.parse_args()

    password = args.password or random_password()
    store = AuthStore(Path(args.db))
    store.upsert_user(args.username, password, role=args.role, bots=args.bot)
    store.close()

    if args.password is None:
        print(f"Seeded user '{args.username}' (role={args.role}) with random password:")
        print(f"  {password}")
    else:
        print(f"Seeded user '{args.username}' (role={args.role}).")


if __name__ == "__main__":
    main()
