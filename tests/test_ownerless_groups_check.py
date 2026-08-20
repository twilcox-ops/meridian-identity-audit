"""Tests for the ownerless-group check.

Proves the group-list leg still paginates normally, and the owner-lookup
leg now goes through `GraphClient.batch()`: many groups' owner lookups
become one `$batch` call instead of one call per group, a failed
sub-request for one group doesn't break the check for the others, and
more than 20 groups correctly chunks into multiple `$batch` calls. All
names/IDs below are fictional placeholders, not real tenant data.
"""

from __future__ import annotations

from identity_audit.checks.ownerless_groups import (
    OwnerlessGroup,
    find_ownerless_groups,
)
from identity_audit.graph_client import GraphClient

from .fakes import FakeResponse, FakeSessionAllVerbs as FakeSession


def _client(session: FakeSession) -> GraphClient:
    return GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)


def _group(i: int) -> dict:
    return {"id": f"group-{i}", "displayName": f"Fictional Team {i}"}


def _batch_response(entries):
    """entries: list of (sub_request_id, status, body)."""
    return FakeResponse(
        200, {"responses": [{"id": i, "status": s, "body": b} for i, s, b in entries]}
    )


def test_owner_lookups_for_8_groups_make_one_batch_call_not_8():
    groups = [_group(i) for i in range(8)]

    session = FakeSession(
        [
            FakeResponse(200, {"value": groups}),  # groups list, single page
            _batch_response(
                [(g["id"], 200, {"value": [{"id": "owner-1"}]}) for g in groups]
            ),  # every group has an owner
        ]
    )
    client = _client(session)

    results = find_ownerless_groups(client)

    assert results == []
    post_calls = [c for c in session.calls if c[0] == "POST"]
    assert len(post_calls) == 1
    _, batch_url, batch_body = post_calls[0]
    assert batch_url == "https://graph.microsoft.com/v1.0/$batch"
    assert len(batch_body["requests"]) == 8


def test_partial_batch_failure_does_not_break_the_rest():
    groups = [_group(1), _group(2), _group(3)]

    session = FakeSession(
        [
            FakeResponse(200, {"value": groups}),
            _batch_response(
                [
                    ("group-1", 200, {"value": []}),  # ownerless -> included
                    ("group-2", 403, {"error": {"code": "Forbidden"}}),  # failed -> skipped
                    ("group-3", 200, {"value": [{"id": "owner-1"}]}),  # has an owner -> excluded
                ]
            ),
        ]
    )
    client = _client(session)

    results = find_ownerless_groups(client)

    assert results == [
        OwnerlessGroup(group_display_name="Fictional Team 1", group_id="group-1")
    ]


def test_more_than_20_groups_chunks_into_multiple_batch_calls():
    groups = [_group(i) for i in range(25)]

    session = FakeSession(
        [
            FakeResponse(200, {"value": groups}),
            _batch_response([(g["id"], 200, {"value": []}) for g in groups[:20]]),
            _batch_response([(g["id"], 200, {"value": []}) for g in groups[20:]]),
        ]
    )
    client = _client(session)

    results = find_ownerless_groups(client)

    assert len(results) == 25  # nobody has an owner in this fixture
    post_calls = [c for c in session.calls if c[0] == "POST"]
    assert len(post_calls) == 2
    assert len(post_calls[0][2]["requests"]) == 20
    assert len(post_calls[1][2]["requests"]) == 5


def test_groups_list_still_paginates_with_forced_small_page_size():
    next_link = (
        "https://graph.microsoft.com/v1.0/groups"
        "?$select=id,displayName&$skiptoken=abc"
    )
    session = FakeSession(
        [
            FakeResponse(
                200, {"value": [_group(1)], "@odata.nextLink": next_link}
            ),
            FakeResponse(200, {"value": [_group(2)]}),
            _batch_response(
                [
                    ("group-1", 200, {"value": [{"id": "owner-1"}]}),
                    ("group-2", 200, {"value": []}),
                ]
            ),
        ]
    )
    client = _client(session)

    results = find_ownerless_groups(client, page_size=1)

    assert results == [
        OwnerlessGroup(group_display_name="Fictional Team 2", group_id="group-2")
    ]
    get_calls = [c for c in session.calls if c[0] == "GET"]
    assert len(get_calls) == 2
    assert get_calls[0] == (
        "GET",
        "https://graph.microsoft.com/v1.0/groups",
        {"$select": "id,displayName", "$top": 1},
    )
    assert get_calls[1] == ("GET", next_link, None)


def test_no_groups_short_circuits_without_a_batch_call():
    session = FakeSession([FakeResponse(200, {"value": []})])
    client = _client(session)

    results = find_ownerless_groups(client)

    assert results == []
    assert len(session.calls) == 1  # the groups-list GET only, no $batch POST
