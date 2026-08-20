"""Tests for the device-compliance check.

Forces a small `$top` so the fake session has to serve two pages, and
covers all four cases the check must distinguish: non-compliant + recent
(included), compliant + stale (included), compliant + recent (excluded),
and never checked in (included, edge case) - all without hitting a real
tenant.
"""

from __future__ import annotations

from datetime import datetime, timezone

from identity_audit.checks.device_compliance import (
    FlaggedDevice,
    find_noncompliant_or_stale_devices,
)
from identity_audit.graph_client import GraphClient

from .fakes import FakeResponse, FakeSession

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


def test_find_noncompliant_or_stale_devices_paginates_and_classifies_correctly():
    next_link = (
        "https://graph.microsoft.com/v1.0/devices"
        "?$select=displayName,isCompliant,approximateLastSignInDateTime"
        "&$skiptoken=abc"
    )

    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [
                        # Non-compliant, checked in 5 days ago - included on
                        # compliance alone, not staleness.
                        {
                            "displayName": "Laptop-NonCompliant",
                            "isCompliant": False,
                            "approximateLastSignInDateTime": "2024-05-27T00:00:00Z",
                        },
                        # Compliant, checked in 200 days ago - included on
                        # staleness alone, not compliance.
                        {
                            "displayName": "Laptop-Stale",
                            "isCompliant": True,
                            "approximateLastSignInDateTime": "2023-11-14T00:00:00Z",
                        },
                    ],
                    "@odata.nextLink": next_link,
                },
            ),
            FakeResponse(
                200,
                {
                    "value": [
                        # Compliant and recent - excluded on both counts.
                        {
                            "displayName": "Laptop-Healthy",
                            "isCompliant": True,
                            "approximateLastSignInDateTime": "2024-05-30T00:00:00Z",
                        },
                        # Edge case: never checked in at all - Graph omits
                        # the field entirely rather than returning a date.
                        {
                            "displayName": "Laptop-NeverSeen",
                            "isCompliant": True,
                        },
                    ]
                },
            ),
        ]
    )

    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    results = find_noncompliant_or_stale_devices(client, page_size=2, now=NOW)

    assert results == [
        FlaggedDevice(
            display_name="Laptop-NonCompliant",
            is_compliant=False,
            days_since_check_in=5,
        ),
        FlaggedDevice(
            display_name="Laptop-Stale",
            is_compliant=True,
            days_since_check_in=200,
        ),
        FlaggedDevice(
            display_name="Laptop-NeverSeen",
            is_compliant=True,
            days_since_check_in=None,
        ),
    ]
    assert len(session.calls) == 2

    first_url, first_params = session.calls[0]
    assert first_params == {
        "$select": "displayName,isCompliant,approximateLastSignInDateTime",
        "$top": 2,
    }
    assert session.calls[1] == (next_link, None)
