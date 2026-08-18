"""Entry point for Part A: run the audit checks that exist so far.

Console output only for now - a severity-ranked HTML report lands once
there's more than one check to rank.
"""

from __future__ import annotations

import logging
import sys

from identity_audit.auth import get_access_token
from identity_audit.checks.mfa import find_users_without_mfa
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
    users = find_users_without_mfa(client)

    print(f"\nUsers without MFA registered ({len(users)}):")
    for user in users:
        print(f"  {user.user_principal_name}  ({user.display_name})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
