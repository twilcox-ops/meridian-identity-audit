"""Tests for GraphClient's pagination and 429-retry logic.

Uses a fake session in place of `requests.Session` so these prove the
pagination and throttling behavior without ever hitting a real tenant.
"""

from __future__ import annotations

from identity_audit.graph_client import GraphClient


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json_data


class FakeSession:
    """Returns queued responses in call order; records every call made."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # list of (url, params_or_body)

    def get(self, url, headers=None, params=None):
        self.calls.append((url, params))
        return self._responses.pop(0)

    def post(self, url, headers=None, json=None):
        self.calls.append((url, json))
        return self._responses.pop(0)


def test_get_pages_follows_next_link_with_forced_small_page_size():
    page_one_url = (
        "https://graph.microsoft.com/v1.0/reports/authenticationMethods/"
        "userRegistrationDetails"
    )
    page_two_url = page_one_url + "?$skiptoken=abc"

    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [{"userPrincipalName": "alice@example.com"}],
                    "@odata.nextLink": page_two_url,
                },
            ),
            FakeResponse(200, {"value": [{"userPrincipalName": "bob@example.com"}]}),
        ]
    )

    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    pages = list(client.get_pages(page_one_url, params={"$top": 1}))

    assert [item["userPrincipalName"] for page in pages for item in page] == [
        "alice@example.com",
        "bob@example.com",
    ]
    assert len(session.calls) == 2
    # $top is forced small and sent on the first call only - the nextLink
    # already carries the paging state for every call after that.
    assert session.calls[0] == (page_one_url, {"$top": 1})
    assert session.calls[1] == (page_two_url, None)


def test_get_pages_retries_429_by_honoring_retry_after_header():
    url = (
        "https://graph.microsoft.com/v1.0/reports/authenticationMethods/"
        "userRegistrationDetails"
    )

    session = FakeSession(
        [
            FakeResponse(429, {}, headers={"Retry-After": "2"}),
            FakeResponse(200, {"value": [{"userPrincipalName": "carol@example.com"}]}),
        ]
    )

    sleeps: list[float] = []
    client = GraphClient(access_token="fake-token", session=session, sleep=sleeps.append)

    pages = list(client.get_pages(url))

    assert [item["userPrincipalName"] for page in pages for item in page] == [
        "carol@example.com"
    ]
    # Slept for exactly what the header said - not a fixed constant, and it
    # did wait (not an immediate retry) before the second call landed.
    assert sleeps == [2.0]
    assert len(session.calls) == 2


def test_get_pages_raises_on_non_retryable_error_status():
    url = "https://graph.microsoft.com/v1.0/reports/authenticationMethods/userRegistrationDetails"
    session = FakeSession([FakeResponse(403, {"error": {"code": "Forbidden"}})])
    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    try:
        list(client.get_pages(url))
    except Exception as exc:
        assert "403" in str(exc)
    else:
        raise AssertionError("expected GraphError for a 403 response")


def test_batch_correlates_by_id_not_response_order():
    # Graph doesn't guarantee sub-responses come back in request order -
    # this response is deliberately reversed to prove correlation is by id.
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "responses": [
                        {"id": "2", "status": 200, "body": {"value": ["second"]}},
                        {"id": "1", "status": 404, "body": {"error": {"code": "NotFound"}}},
                    ]
                },
            )
        ]
    )
    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    results = client.batch(
        [
            {"id": "1", "url": "/groups/group-1/owners?$select=id"},
            {"id": "2", "url": "/groups/group-2/owners?$select=id"},
        ]
    )

    assert results["1"] == {"status": 404, "body": {"error": {"code": "NotFound"}}}
    assert results["2"] == {"status": 200, "body": {"value": ["second"]}}
    assert len(session.calls) == 1  # one POST for two sub-requests


def test_batch_chunks_at_the_20_request_limit():
    sub_requests = [
        {"id": str(i), "url": f"/groups/group-{i}/owners?$select=id"} for i in range(25)
    ]
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "responses": [
                        {"id": str(i), "status": 200, "body": {"value": []}} for i in range(20)
                    ]
                },
            ),
            FakeResponse(
                200,
                {
                    "responses": [
                        {"id": str(i), "status": 200, "body": {"value": []}}
                        for i in range(20, 25)
                    ]
                },
            ),
        ]
    )
    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    results = client.batch(sub_requests)

    assert len(results) == 25
    assert len(session.calls) == 2  # 25 requests -> chunks of 20 + 5
    first_batch_url, first_batch_body = session.calls[0]
    assert first_batch_url == "https://graph.microsoft.com/v1.0/$batch"
    assert len(first_batch_body["requests"]) == 20
    assert len(session.calls[1][1]["requests"]) == 5


def test_batch_raises_on_outer_batch_post_failure():
    session = FakeSession([FakeResponse(400, {"error": {"code": "InvalidBatch"}})])
    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    try:
        client.batch([{"id": "1", "url": "/groups/group-1/owners?$select=id"}])
    except Exception as exc:
        assert "400" in str(exc)
    else:
        raise AssertionError("expected GraphError when the $batch POST itself fails")
