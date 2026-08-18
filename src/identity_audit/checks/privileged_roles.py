"""Part A check: users holding privileged directory roles.

Directory roles aren't a single flat query the way users are - Graph models
"who has a privileged role" as two steps:

1. `GET /directoryRoles` - list roles that are *activated* in this tenant.
   Entra only activates a directoryRole the first time it's assigned; roles
   from the template catalog that have never been assigned don't appear
   here at all, so this is already scoped to "roles that matter."
2. `GET /directoryRoles/{id}/members` - list the members of each activated
   role, one call per role.

A user can hold more than one privileged role, so results are aggregated by
user (keyed on directory object id) rather than emitted as one row per
(user, role) pair - the same user showing up under two roles becomes one
row with two roles, not two rows.

Role membership can include groups or service principals (e.g. PIM for
Groups, role-assignable groups), not just users - members lacking a
`userPrincipalName` are skipped, since this check is specifically about
user accounts.

Requires the `RoleManagement.Read.Directory` application permission - a
distinct, more sensitive permission surface than the `User.Read.All` /
`AuditLog.Read.All` / `Reports.Read.All` already granted for the other
checks. None of those cover role membership; Graph requires its own grant
for privileged-access data. See README Permissions section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient

logger = logging.getLogger(__name__)

DIRECTORY_ROLES_PATH = "/directoryRoles"

_ROLE_SELECT_FIELDS = "id,displayName"
_MEMBER_SELECT_FIELDS = "id,userPrincipalName,displayName"


@dataclass(frozen=True)
class PrivilegedUser:
    user_principal_name: str
    display_name: str
    roles: tuple[str, ...]


def find_privileged_role_holders(
    client: GraphClient, page_size: int | None = None
) -> list[PrivilegedUser]:
    """Return every user holding at least one activated directory role.

    `page_size` sets `$top` on the initial request of both the roles-list
    call and every per-role members call, so tests can force pagination on
    either leg without a real tenant.
    """
    roles_url = f"{GRAPH_BASE_URL}{DIRECTORY_ROLES_PATH}"
    roles_params: dict[str, object] = {"$select": _ROLE_SELECT_FIELDS}
    if page_size is not None:
        roles_params["$top"] = page_size

    roles: list[dict] = []
    for page in client.get_pages(roles_url, params=roles_params):
        roles.extend(page)

    # Directory object id -> accumulated state, so a user in multiple roles
    # ends up as one entry with multiple roles, not one entry per role.
    holders: dict[str, dict[str, object]] = {}

    for role in roles:
        role_name = role.get("displayName", "")
        members_url = f"{GRAPH_BASE_URL}{DIRECTORY_ROLES_PATH}/{role['id']}/members"
        members_params: dict[str, object] = {"$select": _MEMBER_SELECT_FIELDS}
        if page_size is not None:
            members_params["$top"] = page_size

        for page in client.get_pages(members_url, params=members_params):
            for member in page:
                user_id = member.get("id")
                upn = member.get("userPrincipalName")
                if not user_id or not upn:
                    continue  # group/service-principal member - not a user

                entry = holders.setdefault(
                    user_id,
                    {
                        "user_principal_name": upn,
                        "display_name": member.get("displayName", ""),
                        "roles": [],
                    },
                )
                entry["roles"].append(role_name)

    results = [
        PrivilegedUser(
            user_principal_name=entry["user_principal_name"],
            display_name=entry["display_name"],
            roles=tuple(entry["roles"]),
        )
        for entry in holders.values()
    ]

    logger.info(
        "Privileged-role check complete: %d activated role(s), %d user(s) holding them",
        len(roles),
        len(results),
    )
    return results
