"""Tests for the stale-but-licensed-account check.

Forces a small `$top` so the fake session has to serve two pages, and
covers the never-signed-in edge case (Graph omits `lastSignInDateTime`
entirely rather than reporting a date) - all without hitting a real tenant.
"""

from __future__ import annotations

from datetime import datetime, timezone

from identity_audit.checks.stale_accounts import (
    StaleLicensedUser,
    find_stale_licensed_users,
)
from identity_audit.graph_client import GraphClient


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json_data


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


def test_find_stale_licensed_users_paginates_and_applies_90_day_threshold():
    next_link = (
        "https://graph.microsoft.com/v1.0/users"
        "?$select=userPrincipalName,displayName,signInActivity,assignedLicenses"
        "&$skiptoken=abc"
    )

    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [
                        # Stale: licensed, last signed in well over 90 days ago.
                        {
                            "userPrincipalName": "stale.licensed@example.com",
                            "displayName": "Stale Licensed",
                            "assignedLicenses": [{"skuId": "abc-123"}],
                            "signInActivity": {
                                "lastSignInDateTime": "2023-01-01T00:00:00Z"
                            },
                        },
                        # Not stale: no license at all, regardless of sign-in gap.
                        {
                            "userPrincipalName": "unlicensed.dormant@example.com",
                            "displayName": "Unlicensed Dormant",
                            "assignedLicenses": [],
                            "signInActivity": {
                                "lastSignInDateTime": "2022-01-01T00:00:00Z"
                            },
                        },
                    ],
                    "@odata.nextLink": next_link,
                },
            ),
            FakeResponse(
                200,
                {
                    "value": [
                        # Not stale: licensed but signed in recently.
                        {
                            "userPrincipalName": "active.licensed@example.com",
                            "displayName": "Active Licensed",
                            "assignedLicenses": [{"skuId": "abc-123"}],
                            "signInActivity": {
                                "lastSignInDateTime": "2024-05-25T00:00:00Z"
                            },
                        },
                        # Stale edge case: licensed, never signed in - Graph
                        # reports this by omitting lastSignInDateTime, not by
                        # returning a date.
                        {
                            "userPrincipalName": "never.signedin@example.com",
                            "displayName": "Never Signed In",
                            "assignedLicenses": [{"skuId": "abc-123"}],
                            "signInActivity": {},
                        },
                    ]
                },
            ),
        ]
    )

    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    results = find_stale_licensed_users(client, page_size=2, now=NOW)

    assert results == [
        StaleLicensedUser(
            user_principal_name="stale.licensed@example.com",
            display_name="Stale Licensed",
            last_sign_in="2023-01-01T00:00:00Z",
        ),
        StaleLicensedUser(
            user_principal_name="never.signedin@example.com",
            display_name="Never Signed In",
            last_sign_in=None,
        ),
    ]
    assert len(session.calls) == 2

    first_url, first_params = session.calls[0]
    assert first_params == {
        "$select": "userPrincipalName,displayName,signInActivity,assignedLicenses",
        "$top": 2,
    }
    assert session.calls[1] == (next_link, None)


def test_find_stale_licensed_users_handles_missing_signinactivity_key():
    # Some tenants/records omit signInActivity entirely rather than sending {}.
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "userPrincipalName": "no.activity.block@example.com",
                            "displayName": "No Activity Block",
                            "assignedLicenses": [{"skuId": "abc-123"}],
                        }
                    ]
                },
            )
        ]
    )

    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    results = find_stale_licensed_users(client, now=NOW)

    assert results == [
        StaleLicensedUser(
            user_principal_name="no.activity.block@example.com",
            display_name="No Activity Block",
            last_sign_in=None,
        )
    ]
