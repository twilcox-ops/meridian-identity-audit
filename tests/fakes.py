"""Shared fake Graph HTTP primitives used across the test suite.

These stand in for `requests.Response` / `requests.Session` so tests can
drive `GraphClient` (and code built on top of it) without ever making a
real HTTP call. `FakeResponse` is identical everywhere it's used, but
several `FakeSession` shapes exist because different tests assert on
different things: some care only about the URL and query params of GET
pagination, others need every verb tagged and ordered, and onboarding
needs a GET that blows up on contact to prove it's never issued.
"""

from __future__ import annotations


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json_data


class FakeSession:
    """GET-only fake session; records each call as (url, params)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


class FakeSessionAllVerbs:
    """Records every call, across all four verbs, as (verb, url, payload)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []  # (verb, url, payload)

    def get(self, url, headers=None, params=None):
        self.calls.append(("GET", url, params))
        return self._responses.pop(0)

    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, json))
        return self._responses.pop(0)

    def patch(self, url, headers=None, json=None):
        self.calls.append(("PATCH", url, json))
        return self._responses.pop(0)

    def delete(self, url, headers=None):
        self.calls.append(("DELETE", url, None))
        return self._responses.pop(0)


class FakeSessionGetPost:
    """GET+POST fake session; records each call as (url, params_or_body)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # list of (url, params_or_body)

    def get(self, url, headers=None, params=None):
        self.calls.append((url, params))
        return self._responses.pop(0)

    def post(self, url, headers=None, json=None):
        self.calls.append((url, json))
        return self._responses.pop(0)


class FakeSessionPostOnly:
    """POST-only fake session; records each call as (url, json_body)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # (url, json_body)

    def post(self, url, headers=None, json=None):
        self.calls.append((url, json))
        return self._responses.pop(0)


class FakeSessionPostOnlyGuarded(FakeSessionPostOnly):
    """POST-only fake session whose GET raises, proving GET is never used."""

    def get(self, url, headers=None, params=None):  # pragma: no cover - unused here
        raise AssertionError("onboarding should never issue a GET")
