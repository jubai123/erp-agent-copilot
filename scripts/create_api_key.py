"""One-off bootstrap: mint the first API key directly against the database.

Production runs in ``auth_mode=api_key`` (config.py), so the API cannot issue
its own first credential — there is no key to authenticate the caller yet.
This script is the chicken-and-egg escape hatch: run it once on a host with
database access and the same ``.env`` as the API, then keep the printed
plaintext key secure (it is shown once and never stored).

Usage:
    uv run python scripts/create_api_key.py --tenant <tenant-id> --user <user-id> [--label ci]
"""

from __future__ import annotations

import argparse

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import get_session, init_db
from erp_copilot.security.api_keys import create_api_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap an API key for a (tenant, user).")
    parser.add_argument("--tenant", required=True, help="Tenant ID the key binds to.")
    parser.add_argument("--user", required=True, help="User ID the key authenticates as.")
    parser.add_argument("--label", default="bootstrap", help="Free-form label for the key.")
    args = parser.parse_args()

    settings = Settings()  # type: ignore[call-arg]
    init_db(settings)

    session = get_session()
    try:
        plain = create_api_key(
            session,
            tenant_id=args.tenant,
            user_id=args.user,
            label=args.label,
            pepper=settings.api_key_pepper,
        )
    finally:
        session.close()

    print(plain)


if __name__ == "__main__":
    main()
