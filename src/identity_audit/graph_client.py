"""Thin Microsoft Graph HTTP client: pagination, throttling, the
single-request verbs (GET-one, POST, PATCH, DELETE) offboarding/onboarding
need, and `$batch` for bundling many independent GETs into one round trip.

Every request is logged with its status code and, for paginated GETs, the
number of items returned - no response bodies, no PII beyond what a log
line needs to say "this call happened and returned N records". HTTP 429 is
handled by waiting exactly as long as the `Retry-After` header says before
retrying the same request - never a fixed sleep, never an immediate retry.
That retry loop is shared across every verb rather than duplicated, and so
is the "log the outcome, raise GraphError on non-2xx" handling for the four
single-request verbs (`_finish`).

The `requests.Session` (and the sleep function) are injectable so tests can
prove the pagination and throttling logic without a real tenant.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import requests

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
BATCH_PATH = "/$batch"

# Graph's hard limit on requests per $batch call - more than this and the
# batch POST itself is rejected, so `batch()` chunks rather than assuming
# the caller already knows to.
MAX_BATCH_REQUESTS = 20

# Only used if Graph sends a 429 with no Retry-After header at all, which
# shouldn't happen in practice - this is a safety net, not the normal path.
_FALLBACK_RETRY_AFTER_SECONDS = 5.0


class GraphError(RuntimeError):
    """Graph responded with a non-2xx, non-429 status code."""


class GraphClient:
    def __init__(
        self,
        access_token: str,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ) -> None:
        self._session = session or requests.Session()
        self._sleep = sleep
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

    def get_pages(
        self, url: str, params: dict[str, Any] | None = None
    ) -> Iterator[list[dict]]:
        """Yield each page's `value` list, following `@odata.nextLink`.

        `params` (e.g. `$filter`, `$top`) is only sent on the first request.
        Graph's `@odata.nextLink` is a complete URL that already encodes the
        paging state, so every subsequent request is a plain GET on it.
        """
        next_url: str | None = url
        next_params = params

        while next_url:
            response = self._request_with_retry(next_url, next_params)
            status = response.status_code

            if status >= 400:
                logger.error("GET %s -> %s", _strip_query(next_url), status)
                raise GraphError(f"Graph request failed with status {status}")

            payload = response.json()
            items = payload.get("value", [])
            logger.info(
                "GET %s -> %s, %d item(s)",
                _strip_query(next_url),
                status,
                len(items),
            )

            yield items

            next_url = payload.get("@odata.nextLink")
            next_params = None

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        """GET a single resource (not a paginated collection) - see `get_pages`
        for anything that returns Graph's `value` array."""
        response = self._with_429_retry(
            lambda: self._session.get(url, headers=self._headers, params=params),
            method="GET",
            url_for_logging=url,
        )
        return self._finish(response, "GET", url)

    def post(self, url: str, json_body: dict[str, Any]) -> requests.Response:
        """POST once (no pagination), honoring 429/Retry-After like `get_pages`."""
        response = self._with_429_retry(
            lambda: self._session.post(url, headers=self._headers, json=json_body),
            method="POST",
            url_for_logging=url,
        )
        return self._finish(response, "POST", url)

    def patch(self, url: str, json_body: dict[str, Any]) -> requests.Response:
        """PATCH once, honoring 429/Retry-After like every other verb here."""
        response = self._with_429_retry(
            lambda: self._session.patch(url, headers=self._headers, json=json_body),
            method="PATCH",
            url_for_logging=url,
        )
        return self._finish(response, "PATCH", url)

    def delete(self, url: str) -> requests.Response:
        """DELETE once, honoring 429/Retry-After like every other verb here."""
        response = self._with_429_retry(
            lambda: self._session.delete(url, headers=self._headers),
            method="DELETE",
            url_for_logging=url,
        )
        return self._finish(response, "DELETE", url)

    def batch(self, sub_requests: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
        """Send GET requests as one or more POSTs to `$batch`, chunked to
        Graph's `MAX_BATCH_REQUESTS`-per-call limit.

        `sub_requests` is a list of `{"id": str, "url": str}`, where `url`
        is relative to the Graph base (e.g. `/groups/{id}/owners?$select=id`)
        - what `$batch` itself expects, unlike every other method here,
        which takes a full URL.

        Returns `{id: {"status": int, "body": dict}}` for every request,
        merged across however many chunked POSTs it took. Correlation is by
        `id`, not response order - Graph does not guarantee sub-responses
        come back in the order the sub-requests were sent.

        A non-2xx sub-response never raises: partial failure inside a batch
        is the normal case this method exists to handle, not exceptional.
        The caller decides what a failed sub-request means. `GraphError` is
        only raised if a batch POST itself fails outright (reused from
        `post()`) - a whole-batch failure is different in kind from one
        sub-request failing.
        """
        results: dict[str, dict[str, Any]] = {}
        chunk_count = 0

        for chunk_start in range(0, len(sub_requests), MAX_BATCH_REQUESTS):
            chunk_count += 1
            chunk = sub_requests[chunk_start : chunk_start + MAX_BATCH_REQUESTS]
            payload = {
                "requests": [
                    {"id": item["id"], "method": "GET", "url": item["url"]}
                    for item in chunk
                ]
            }
            response = self.post(f"{GRAPH_BASE_URL}{BATCH_PATH}", json_body=payload)
            for sub_response in response.json().get("responses", []):
                results[sub_response["id"]] = {
                    "status": sub_response.get("status"),
                    "body": sub_response.get("body") or {},
                }

        logger.info(
            "Batched %d request(s) into %d $batch call(s)",
            len(sub_requests),
            chunk_count,
        )
        return results

    def _finish(self, response: requests.Response, method: str, url: str) -> requests.Response:
        """Shared outcome handling for every single-request verb: log the
        result, raise `GraphError` on a non-2xx response. The caller decides
        whether a failure is fatal or just logged and continued past.
        """
        status = response.status_code
        if status >= 400:
            logger.error("%s %s -> %s", method, _strip_query(url), status)
            raise GraphError(f"Graph request failed with status {status}")
        logger.info("%s %s -> %s", method, _strip_query(url), status)
        return response

    def _request_with_retry(
        self, url: str, params: dict[str, Any] | None
    ) -> requests.Response:
        return self._with_429_retry(
            lambda: self._session.get(url, headers=self._headers, params=params),
            method="GET",
            url_for_logging=url,
        )

    def _with_429_retry(self, request_fn, method: str, url_for_logging: str) -> requests.Response:
        """Call `request_fn()` repeatedly, waiting out every 429 by its Retry-After."""
        while True:
            response = request_fn()
            if response.status_code != 429:
                return response

            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            logger.warning(
                "%s %s -> 429, waiting %ss (Retry-After)",
                method,
                _strip_query(url_for_logging),
                retry_after,
            )
            self._sleep(retry_after)
            # Loop and retry the exact same request - Graph told us to wait,
            # not to change anything about the call.


def _parse_retry_after(header_value: str | None) -> float:
    if header_value is None:
        return _FALLBACK_RETRY_AFTER_SECONDS
    try:
        return max(0.0, float(header_value))
    except ValueError:
        # Retry-After can technically be an HTTP-date; Graph doesn't do
        # that in practice, so fall back rather than parse it.
        return _FALLBACK_RETRY_AFTER_SECONDS


def _strip_query(url: str) -> str:
    """Drop the query string before logging a URL ($filter can carry values)."""
    return url.split("?", 1)[0]
