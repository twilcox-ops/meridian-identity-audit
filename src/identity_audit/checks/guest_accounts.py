"""Part A check: guest accounts and how long they've been in the tenant.

Filters `/users` to `userType eq 'Guest'` server-side. Unlike
`signInActivity` (see stale_accounts.py), `userType` is a directly
filterable property in Graph v1.0 with no advanced-query requirements, so
this check doesn't need a client-side workaround for its `$filter`.

Requires the `User.Read.All` application permission - see README
Permissions section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient
from identity_audit.graph_dates import parse_graph_datetime

logger = logging.getLogger(__name__)

USERS_PATH = "/users"

_SELECT_FIELDS = "userPrincipalName,displayName,createdDateTime"
_GUEST_FILTER = "userType eq 'Guest'"


@dataclass(frozen=True)
class GuestAccount:
    user_principal_name: str
    display_name: str
    days_in_tenant: int


def find_guest_accounts(
    client: GraphClient,
    page_size: int | None = None,
    now: datetime | None = None,
) -> list[GuestAccount]:
    """Return every guest account with how many days it's existed in the tenant.

    `page_size` sets `$top` on the initial request only, so tests can force
    pagination without a real tenant. `now` is injectable so tests get
    deterministic "days since createdDateTime" math instead of depending on
    wall-clock time.
    """
    reference_time = now or datetime.now(timezone.utc)

    params: dict[str, object] = {
        "$filter": _GUEST_FILTER,
        "$select": _SELECT_FIELDS,
    }
    if page_size is not None:
        params["$top"] = page_size

    url = f"{GRAPH_BASE_URL}{USERS_PATH}"

    results: list[GuestAccount] = []
    for page in client.get_pages(url, params=params):
        for record in page:
            created_at = parse_graph_datetime(record["createdDateTime"])
            days_in_tenant = (reference_time - created_at).days
            results.append(
                GuestAccount(
                    user_principal_name=record.get("userPrincipalName", ""),
                    display_name=record.get("displayName", ""),
                    days_in_tenant=days_in_tenant,
                )
            )

    logger.info(
        "Guest-account check complete: %d guest account(s) found", len(results)
    )
    return results
