"""One-off utility: list every group in the tenant.

Not part of the check suite - this exists purely to find real Graph group
object IDs to put in `config/department_groups.json` (or pass ad hoc) when
testing onboarding.py/offboarding.py against a real tenant, since group
IDs are opaque GUIDs with no way to guess them. Unlike license SKUs, a real
tenant can plausibly have enough groups to paginate, so this is the first
of the two utility scripts that actually exercises `GraphClient`'s
pagination rather than just its single-page path.

Requires the `GroupMember.Read.All` application permission for
`GET /groups` - already granted for the ownerless-groups check, so no new
grant needed here.

Usage:
    .venv/Scripts/python.exe scripts/list_groups.py
"""

from __future__ import annotations

import json
import logging

import requests

from identity_audit.auth import get_access_token
from identity_audit.config import load_graph_config
from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient, GraphError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

GROUPS_PATH = "/groups"
_SELECT_FIELDS = "id,displayName"


def _print_error_body(token: str, url: str, params: dict) -> None:
    """Re-issue the call directly (bypassing GraphClient, which discards
    the response body on error) to print Graph's JSON error - "error.code"
    and "error.message" usually name the exact cause. Same diagnostic
    pattern used earlier to debug the MFA check's 403.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    response = requests.get(url, headers=headers, params=params)
    print(f"\nGET {GROUPS_PATH} -> {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        body = {"raw_text": response.text}
    print(json.dumps(body, indent=2))


def main() -> None:
    config = load_graph_config()
    token = get_access_token(config)
    client = GraphClient(access_token=token)

    url = f"{GRAPH_BASE_URL}{GROUPS_PATH}"
    params = {"$select": _SELECT_FIELDS}

    try:
        groups: list[dict] = []
        for page in client.get_pages(url, params=params):
            groups.extend(page)
    except GraphError:
        _print_error_body(token, url, params)
        raise

    if not groups:
        print("No groups found in this tenant.")
        return

    name_width = max(
        (len(group.get("displayName", "")) for group in groups),
        default=0,
    )
    name_width = max(name_width, len("DISPLAY NAME"))

    header = f"{'DISPLAY NAME'.ljust(name_width)}  OBJECT ID"
    print(f"\n{header}")
    print("-" * len(header))
    for group in groups:
        display_name = group.get("displayName", "")
        group_id = group.get("id", "")
        print(f"{display_name.ljust(name_width)}  {group_id}")


if __name__ == "__main__":
    main()
