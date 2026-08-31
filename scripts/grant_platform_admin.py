"""Grant or revoke platform-administrator access for a user, by email.

    python scripts/grant_platform_admin.py alice@example.com
    python scripts/grant_platform_admin.py alice@example.com --revoke

Platform admins can read every organization through the /api/v1/admin routes.
The change is written to the audit log.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.domains.audit.models import AuditAction
from app.settings import settings
from app.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="the user's email address")
    parser.add_argument(
        "--revoke", action="store_true", help="remove platform-admin access instead"
    )
    args = parser.parse_args()

    grant = not args.revoke
    store = Store(settings.database_target)
    try:
        user = store.get_user_by_email(args.email)
        if user is None:
            raise SystemExit(f"no user with email {args.email!r}")
        store.set_platform_admin(str(user["id"]), grant)
        store.record_audit(
            AuditAction.PLATFORM_ADMIN_GRANTED.value,
            actor_user_id=None,
            target_type="user",
            target_id=str(user["id"]),
            metadata={"email": args.email, "granted": grant, "source": "cli"},
        )
    finally:
        store.close()

    print(f"{'granted' if grant else 'revoked'} platform admin for {args.email}")


if __name__ == "__main__":
    main()
