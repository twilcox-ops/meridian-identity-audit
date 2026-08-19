"""One-off utility: check whether a specific user is a member of a
specific group.

Not part of the check suite or the onboarding/offboarding modules - exists
to verify group membership state directly against a real tenant (e.g.
confirming an onboarding run's add_to_group step actually took effect, or
that a rollback's re-add landed), without needing to run the full
ownerless-groups check or trust an audit-trail entry alone.

Uses `GET /groups/{id}/members` and filters for the user, rather than
`checkMemberGroups`/`checkMemberObjects` - those are POST actions with a
list-in/list-out shape built for checking many groups or objects at once;
for a single group/user pair, a filtered `members` read is simpler and
matches every other utility script's pattern (plain `$select` +
`GraphClient.get_pages`) instead of introducing a new call shape.

Requires the `GroupMember.Read.All` application permission for
`GET /groups/{id}/members` - already granted for the ownerless-groups
check and scripts/list_groups.py, so no new grant needed here.

Usage:
    .venv/Scripts/python.exe scripts/check_group_membership.py \\
        --group-id <group-object-id> --user-id <user-object-id>
"""

from __future__ import annotations

import argparse
import json
import logging

import requests

from identity_audit.auth import get_access_token
from identity_audit.config import load_graph_config
from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient, GraphError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

_SELECT_FIELDS = "id,displayName"


def _print_error_body(token: str, members_path: str, params: dict) -> None:
    """Re-issue the call directly (bypassing GraphClient, which discards
    the response body on error) to print Graph's JSON error - "error.code"
    and "error.message" usually name the exact cause. Same diagnostic
    pattern used earlier to debug the MFA check's 403 and the license-SKU
    script's 403.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    response = requests.get(f"{GRAPH_BASE_URL}{members_path}", headers=headers, params=params)
    print(f"\nGET {members_path} -> {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        body = {"raw_text": response.text}
    print(json.dumps(body, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-id", required=True, help="Graph group object ID.")
    parser.add_argument("--user-id", required=True, help="Graph user object ID to look for.")
    args = parser.parse_args()

    config = load_graph_config()
    token = get_access_token(config)
    client = GraphClient(access_token=token)

    members_path = f"/groups/{args.group_id}/members"
    url = f"{GRAPH_BASE_URL}{members_path}"
    params = {"$select": _SELECT_FIELDS}

    try:
        members: list[dict] = []
        for page in client.get_pages(url, params=params):
            members.extend(page)
    except GraphError:
        _print_error_body(token, members_path, params)
        raise

    match = next((m for m in members if m.get("id") == args.user_id), None)

    print(f"\nGroup {args.group_id} has {len(members)} member(s).")
    if match is not None:
        print(f"User {args.user_id} IS a member ({match.get('displayName', '')}).")
    else:
        print(f"User {args.user_id} is NOT a member of this group.")


if __name__ == "__main__":
    main()
