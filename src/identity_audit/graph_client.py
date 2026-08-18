"""Thin Microsoft Graph HTTP client: pagination and throttling, nothing else.

Every request is logged with its status code and the number of items
returned - no response bodies, no PII beyond what a log line needs to say
"this call happened and returned N records". HTTP 429 is handled by waiting
exactly as long as the `Retry-After` header says before retrying the same
request - never a fixed sleep, never an immediate retry.

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

    def _request_with_retry(
        self, url: str, params: dict[str, Any] | None
    ) -> requests.Response:
        while True:
            response = self._session.get(url, headers=self._headers, params=params)
            if response.status_code != 429:
                return response

            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            logger.warning(
                "GET %s -> 429, waiting %ss (Retry-After)",
                _strip_query(url),
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
