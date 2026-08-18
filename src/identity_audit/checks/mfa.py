"""Part A check: users with no MFA method registered.

Uses the authentication methods registration report
(`reportsAuthenticationMethodUserRegistrationDetail`, at
`/reports/authenticationMethods/userRegistrationDetails`) rather than
iterating every user individually - Graph already computes
`isMfaRegistered` per user in this one resource, and the `$filter` below
does the narrowing server-side.

Requires the `Reports.Read.All` application permission - see README
Permissions section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient

logger = logging.getLogger(__name__)

REGISTRATION_DETAILS_PATH = "/reports/authenticationMethods/userRegistrationDetails"


@dataclass(frozen=True)
class UserMfaStatus:
    user_principal_name: str
    display_name: str


def find_users_without_mfa(
    client: GraphClient, page_size: int | None = None
) -> list[UserMfaStatus]:
    """Return every user with `isMfaRegistered == false`, across all pages.

    `page_size` sets `$top` on the initial request only, so tests can force
    a small value and prove pagination works without a real tenant. Left
    unset in normal runs, where Graph picks its own default page size.
    """
    params: dict[str, object] = {"$filter": "isMfaRegistered eq false"}
    if page_size is not None:
        params["$top"] = page_size

    url = f"{GRAPH_BASE_URL}{REGISTRATION_DETAILS_PATH}"

    results: list[UserMfaStatus] = []
    for page in client.get_pages(url, params=params):
        for record in page:
            results.append(
                UserMfaStatus(
                    user_principal_name=record.get("userPrincipalName", ""),
                    display_name=record.get("userDisplayName", ""),
                )
            )

    logger.info("MFA check complete: %d user(s) without MFA registered", len(results))
    return results
