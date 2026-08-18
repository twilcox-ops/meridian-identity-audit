"""Part A check: groups with no owner.

Like the privileged-role check, group ownership isn't a single flat query -
Graph models it as two steps:

1. `GET /groups` - list every group in the tenant.
2. `GET /groups/{id}/owners` - list the owners of each group, one call per
   group.

Unlike the privileged-role check, the outer list here isn't small and
bounded - a tenant typically has only a handful of *activated* directory
roles, but every group in the tenant is fair game here, which can run into
the hundreds or thousands. That makes this check's call volume roughly N+1
Graph requests for N groups, each independently paginated and
throttle-handled through GraphClient. This is exactly the kind of fan-out
Graph's `$batch` endpoint (bundling up to 20 requests per round trip) is
built for, and the check most likely to actually trigger a real 429 in a
tenant with many groups - worth revisiting once batching exists (see README
Design constraints); not addressed here.

Requires the `GroupMember.Read.All` application permission - the least
privileged of the three Graph accepts for both `/groups` and
`/groups/{id}/owners` (`Group.Read.All` and `Directory.Read.All` are the
broader alternatives). See README Permissions section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient

logger = logging.getLogger(__name__)

GROUPS_PATH = "/groups"

_GROUP_SELECT_FIELDS = "id,displayName"
_OWNER_SELECT_FIELDS = "id"


@dataclass(frozen=True)
class OwnerlessGroup:
    group_display_name: str
    group_id: str


def find_ownerless_groups(
    client: GraphClient, page_size: int | None = None
) -> list[OwnerlessGroup]:
    """Return every group with zero owners.

    `page_size` sets `$top` on the initial request of both the groups-list
    call and every per-group owners call, so tests can force pagination on
    either leg without a real tenant.
    """
    groups_url = f"{GRAPH_BASE_URL}{GROUPS_PATH}"
    groups_params: dict[str, object] = {"$select": _GROUP_SELECT_FIELDS}
    if page_size is not None:
        groups_params["$top"] = page_size

    groups: list[dict] = []
    for page in client.get_pages(groups_url, params=groups_params):
        groups.extend(page)

    results: list[OwnerlessGroup] = []
    for group in groups:
        group_id = group.get("id", "")
        owners_url = f"{GRAPH_BASE_URL}{GROUPS_PATH}/{group_id}/owners"
        owners_params: dict[str, object] = {"$select": _OWNER_SELECT_FIELDS}
        if page_size is not None:
            owners_params["$top"] = page_size

        owner_count = 0
        for page in client.get_pages(owners_url, params=owners_params):
            owner_count += len(page)

        if owner_count == 0:
            results.append(
                OwnerlessGroup(
                    group_display_name=group.get("displayName", ""),
                    group_id=group_id,
                )
            )

    logger.info(
        "Ownerless-group check complete: %d of %d group(s) have no owner",
        len(results),
        len(groups),
    )
    return results
