"""Part A check: groups with no owner.

Like the privileged-role check, group ownership isn't a single flat query -
Graph models it as two steps:

1. `GET /groups` - list every group in the tenant.
2. Look up the owners of each group.

Step 2 used to be `GET /groups/{id}/owners`, one call per group - N+1 Graph
requests for N groups, the check most likely to trigger a real 429 in a
tenant with many groups. It now goes through `GraphClient.batch()` instead:
every group's owner lookup becomes one sub-request in a `$batch` call,
chunked at Graph's 20-per-batch limit, so N groups costs `ceil(N/20)`
requests instead of N.

**The individual-call path has been fully removed, not kept as a
fallback.** It only ever existed because batching didn't exist yet - the
check's own docstring said as much ("worth revisiting once batching
exists... not addressed here"). Keeping both would mean two code paths
answering the same question with no correctness difference between them
(batching handles partial per-group failure correctly, see below), just
double the tests and double the surface area to keep in sync for zero
benefit. This is the one and only path now.

Each owner sub-request asks for `$top=1`, not a full page: this check only
needs to know whether a group has *any* owner, not how many, so Graph
returning at most one is enough to answer that - and it's the smallest
payload that still answers it correctly. A first page that comes back
empty is conclusive proof of zero owners on its own (Graph fills pages
before creating a `@odata.nextLink`; an empty first page is never followed
by a non-empty one), so no further pagination per group is needed at all,
independent of the `$top=1` optimization.

A group whose owner sub-request comes back non-2xx is logged and *skipped*
- not silently counted as ownerless. Treating "the lookup failed" the same
as "the lookup succeeded and found nothing" would turn a transient error
into a false security finding, which is worse than an incomplete result
this run.

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

    `page_size` sets `$top` on the initial groups-list request, so tests
    can force pagination on that leg without a real tenant. It no longer
    applies to owner lookups - those are now batched, one sub-request per
    group asking for `$top=1` regardless of `page_size` (see module
    docstring for why that's always correct here).
    """
    groups_url = f"{GRAPH_BASE_URL}{GROUPS_PATH}"
    groups_params: dict[str, object] = {"$select": _GROUP_SELECT_FIELDS}
    if page_size is not None:
        groups_params["$top"] = page_size

    groups: list[dict] = []
    for page in client.get_pages(groups_url, params=groups_params):
        groups.extend(page)

    if not groups:
        logger.info("Ownerless-group check complete: 0 of 0 group(s) have no owner")
        return []

    sub_requests = [
        {
            "id": group["id"],
            "url": f"{GROUPS_PATH}/{group['id']}/owners?$select={_OWNER_SELECT_FIELDS}&$top=1",
        }
        for group in groups
        if group.get("id")
    ]
    batch_results = client.batch(sub_requests)

    results: list[OwnerlessGroup] = []
    for group in groups:
        group_id = group.get("id", "")
        sub_result = batch_results.get(group_id)

        if sub_result is None:
            logger.warning(
                "No batch response for group %s - skipping, not counted either way",
                group_id,
            )
            continue

        if sub_result["status"] >= 400:
            logger.warning(
                "Owner lookup for group %s failed with status %s - skipping, "
                "not counted as ownerless",
                group_id,
                sub_result["status"],
            )
            continue

        owners = sub_result["body"].get("value", [])
        if not owners:
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
