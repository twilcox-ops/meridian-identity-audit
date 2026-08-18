"""Tests for the guest-account check.

Forces a small `$top` so the fake session has to serve two pages, and
asserts the days-in-tenant math against a known date - all without hitting
a real tenant.
"""

from __future__ import annotations

from datetime import datetime, timezone

from identity_audit.checks.guest_accounts import GuestAccount, find_guest_accounts
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


def test_find_guest_accounts_paginates_and_computes_days_in_tenant():
    next_link = (
        "https://graph.microsoft.com/v1.0/users"
        "?$filter=userType eq 'Guest'"
        "&$select=userPrincipalName,displayName,createdDateTime"
        "&$skiptoken=abc"
    )

    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "userPrincipalName": "guest.one_external.com#EXT#@tenant.onmicrosoft.com",
                            "displayName": "Guest One",
                            # Known date: 2024 is a leap year, so Jan 1 -> Jun 1 is 152 days.
                            "createdDateTime": "2024-01-01T00:00:00Z",
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
                            "userPrincipalName": "guest.two_external.com#EXT#@tenant.onmicrosoft.com",
                            "displayName": "Guest Two",
                            "createdDateTime": "2024-05-01T00:00:00Z",
                        }
                    ]
                },
            ),
        ]
    )

    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    results = find_guest_accounts(client, page_size=1, now=NOW)

    assert results == [
        GuestAccount(
            user_principal_name="guest.one_external.com#EXT#@tenant.onmicrosoft.com",
            display_name="Guest One",
            days_in_tenant=152,
        ),
        GuestAccount(
            user_principal_name="guest.two_external.com#EXT#@tenant.onmicrosoft.com",
            display_name="Guest Two",
            days_in_tenant=31,
        ),
    ]
    assert len(session.calls) == 2

    first_url, first_params = session.calls[0]
    assert first_params == {
        "$filter": "userType eq 'Guest'",
        "$select": "userPrincipalName,displayName,createdDateTime",
        "$top": 1,
    }
    # Second call goes straight to the nextLink with no re-sent params.
    assert session.calls[1] == (next_link, None)
