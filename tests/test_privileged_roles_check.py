"""Tests for the privileged-directory-role check.

Mocks both legs of the two-step call pattern (roles list, then members per
role) and proves a user in more than one role is aggregated into a single
entry rather than appearing once per role - all without a real tenant.
"""

from __future__ import annotations

from identity_audit.checks.privileged_roles import (
    PrivilegedUser,
    find_privileged_role_holders,
)
from identity_audit.graph_client import GraphClient

from .fakes import FakeResponse, FakeSession


def test_find_privileged_role_holders_aggregates_multi_role_user():
    session = FakeSession(
        [
            # Step 1: list activated directory roles.
            FakeResponse(
                200,
                {
                    "value": [
                        {"id": "r1", "displayName": "Global Administrator"},
                        {"id": "r2", "displayName": "User Administrator"},
                    ]
                },
            ),
            # Step 2a: members of Global Administrator - just Alice.
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "id": "u1",
                            "userPrincipalName": "alice@example.com",
                            "displayName": "Alice Admin",
                        }
                    ]
                },
            ),
            # Step 2b: members of User Administrator - Alice again, plus Bob,
            # plus a role-assignable group with no userPrincipalName.
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "id": "u1",
                            "userPrincipalName": "alice@example.com",
                            "displayName": "Alice Admin",
                        },
                        {
                            "id": "u2",
                            "userPrincipalName": "bob@example.com",
                            "displayName": "Bob User",
                        },
                        {"id": "g1", "displayName": "Some Role-Assignable Group"},
                    ]
                },
            ),
        ]
    )

    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    results = find_privileged_role_holders(client)

    # Alice appears in both role member lists but must be a single entry,
    # aggregating both role names - not two separate rows.
    assert results == [
        PrivilegedUser(
            user_principal_name="alice@example.com",
            display_name="Alice Admin",
            roles=("Global Administrator", "User Administrator"),
        ),
        PrivilegedUser(
            user_principal_name="bob@example.com",
            display_name="Bob User",
            roles=("User Administrator",),
        ),
    ]

    assert len(session.calls) == 3
    roles_url, roles_params = session.calls[0]
    assert roles_url == "https://graph.microsoft.com/v1.0/directoryRoles"
    assert roles_params == {"$select": "id,displayName"}

    members_r1_url, _ = session.calls[1]
    members_r2_url, _ = session.calls[2]
    assert members_r1_url == "https://graph.microsoft.com/v1.0/directoryRoles/r1/members"
    assert members_r2_url == "https://graph.microsoft.com/v1.0/directoryRoles/r2/members"
