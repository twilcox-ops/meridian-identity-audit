"""Entry point for Part A: run the audit checks that exist so far.

Console output only for now - a severity-ranked HTML report lands once
there's more than one check to rank.
"""

from __future__ import annotations

import logging
import sys

from identity_audit.auth import get_access_token
from identity_audit.checks.mfa import find_users_without_mfa
from identity_audit.checks.stale_accounts import (
    STALE_SIGN_IN_THRESHOLD_DAYS,
    find_stale_licensed_users,
)
from identity_audit.config import load_graph_config
from identity_audit.graph_client import GraphClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def main() -> int:
    try:
        config = load_graph_config()
        token = get_access_token(config)
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    client = GraphClient(access_token=token)

    mfa_gaps = find_users_without_mfa(client)
    print(f"\nUsers without MFA registered ({len(mfa_gaps)}):")
    for user in mfa_gaps:
        print(f"  {user.user_principal_name}  ({user.display_name})")

    stale_users = find_stale_licensed_users(client)
    print(
        f"\nLicensed users inactive {STALE_SIGN_IN_THRESHOLD_DAYS}+ days "
        f"({len(stale_users)}):"
    )
    for user in stale_users:
        last_seen = user.last_sign_in or "never signed in"
        print(f"  {user.user_principal_name}  ({user.display_name})  last sign-in: {last_seen}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
