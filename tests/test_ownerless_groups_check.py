"""Tests for the ownerless-group check.

Mocks both legs of the two-step call pattern (groups list, then owners per
group), forces a small `$top` on the groups list to prove pagination, and
covers a group with an owner (excluded) vs. a group with an empty owners
list (included) - all without hitting a real tenant. Group/owner names are
fictional placeholders, not real tenant data.
"""

from __future__ import annotations

from identity_audit.checks.ownerless_groups import (
    OwnerlessGroup,
    find_ownerless_groups,
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


def test_find_ownerless_groups_paginates_groups_and_checks_owners():
    groups_next_link = (
        "https://graph.microsoft.com/v1.0/groups"
        "?$select=id,displayName&$skiptoken=abc"
    )

    session = FakeSession(
        [
            # Step 1a: first page of the groups list.
            FakeResponse(
                200,
                {
                    "value": [
                        {"id": "group-1", "displayName": "Sample Project Team"}
                    ],
                    "@odata.nextLink": groups_next_link,
                },
            ),
            # Step 1b: second page of the groups list.
            FakeResponse(
                200,
                {
                    "value": [
                        {"id": "group-2", "displayName": "Legacy Archive Group"}
                    ]
                },
            ),
            # Step 2a: owners of "Sample Project Team" - has one, excluded.
            FakeResponse(200, {"value": [{"id": "placeholder-owner-1"}]}),
            # Step 2b: owners of "Legacy Archive Group" - none, included.
            FakeResponse(200, {"value": []}),
        ]
    )

    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    results = find_ownerless_groups(client, page_size=1)

    assert results == [
        OwnerlessGroup(group_display_name="Legacy Archive Group", group_id="group-2")
    ]

    assert len(session.calls) == 4

    groups_url, groups_params = session.calls[0]
    assert groups_url == "https://graph.microsoft.com/v1.0/groups"
    assert groups_params == {"$select": "id,displayName", "$top": 1}
    assert session.calls[1] == (groups_next_link, None)

    owners_group1_url, owners_group1_params = session.calls[2]
    assert owners_group1_url == "https://graph.microsoft.com/v1.0/groups/group-1/owners"
    assert owners_group1_params == {"$select": "id", "$top": 1}

    owners_group2_url, owners_group2_params = session.calls[3]
    assert owners_group2_url == "https://graph.microsoft.com/v1.0/groups/group-2/owners"
    assert owners_group2_params == {"$select": "id", "$top": 1}
