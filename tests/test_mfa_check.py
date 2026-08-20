"""Tests for the users-without-MFA check.

Forces a small `$top` so the fake session has to serve two pages, proving
`find_users_without_mfa` stitches paginated results together correctly -
without hitting a real tenant.
"""

from __future__ import annotations

from identity_audit.checks.mfa import UserMfaStatus, find_users_without_mfa
from identity_audit.graph_client import GraphClient

from .fakes import FakeResponse, FakeSession


def test_find_users_without_mfa_paginates_and_maps_fields():
    next_link = (
        "https://graph.microsoft.com/v1.0/reports/authenticationMethods/"
        "userRegistrationDetails?$skiptoken=xyz"
    )

    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "userPrincipalName": "dave@example.com",
                            "userDisplayName": "Dave Test",
                            "isMfaRegistered": False,
                        }
                    ],
                    "@odata.nextLink": next_link,
                },
            ),
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "userPrincipalName": "erin@example.com",
                            "userDisplayName": "Erin Test",
                            "isMfaRegistered": False,
                        }
                    ]
                },
            ),
        ]
    )

    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    results = find_users_without_mfa(client, page_size=1)

    assert results == [
        UserMfaStatus(user_principal_name="dave@example.com", display_name="Dave Test"),
        UserMfaStatus(user_principal_name="erin@example.com", display_name="Erin Test"),
    ]
    assert len(session.calls) == 2

    first_url, first_params = session.calls[0]
    assert first_params == {"$filter": "isMfaRegistered eq false", "$top": 1}
    # Second call goes straight to the nextLink with no re-sent params.
    assert session.calls[1] == (next_link, None)
