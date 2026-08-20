"""Tests for the service-principal-credential-expiry check.

Forces a small `$top` so the fake session has to serve two pages, and
covers all three expiry cases the check must distinguish: expiring soon
(included), far in the future (excluded), and already expired (included,
tagged separately per the confirmed design decision) - all without hitting
a real tenant.
"""

from __future__ import annotations

from datetime import datetime, timezone

from identity_audit.checks.service_principal_credentials import (
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    ExpiringCredential,
    find_expiring_service_principal_credentials,
)
from identity_audit.graph_client import GraphClient

from .fakes import FakeResponse, FakeSession

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


def test_find_expiring_credentials_paginates_and_classifies_correctly():
    next_link = (
        "https://graph.microsoft.com/v1.0/servicePrincipals"
        "?$select=displayName,appId,passwordCredentials,keyCredentials"
        "&$skiptoken=abc"
    )

    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "displayName": "Payroll Sync",
                            "appId": "app-1",
                            "passwordCredentials": [
                                # Expiring soon: 12 days out, inside the
                                # 30-day warning window.
                                {"endDateTime": "2024-06-13T00:00:00Z"},
                                # Far in the future: excluded entirely.
                                {"endDateTime": "2025-01-01T00:00:00Z"},
                            ],
                            "keyCredentials": [],
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
                            "displayName": "Legacy Connector",
                            "appId": "app-2",
                            "passwordCredentials": [],
                            # Already expired: 40 days before "now".
                            "keyCredentials": [
                                {"endDateTime": "2024-04-22T00:00:00Z"}
                            ],
                        }
                    ]
                },
            ),
        ]
    )

    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    results = find_expiring_service_principal_credentials(client, page_size=1, now=NOW)

    assert results == [
        ExpiringCredential(
            sp_display_name="Payroll Sync",
            app_id="app-1",
            credential_type="password",
            days_until_expiry=12,
            status=STATUS_EXPIRING_SOON,
        ),
        ExpiringCredential(
            sp_display_name="Legacy Connector",
            app_id="app-2",
            credential_type="key",
            days_until_expiry=-40,
            status=STATUS_EXPIRED,
        ),
    ]
    assert len(session.calls) == 2

    first_url, first_params = session.calls[0]
    assert first_params == {
        "$select": "displayName,appId,passwordCredentials,keyCredentials",
        "$top": 1,
    }
    assert session.calls[1] == (next_link, None)
