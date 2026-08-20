"""One-off diagnostic: fire rapid concurrent GETs at a lightweight Graph
endpoint until a real HTTP 429 comes back, to prove `GraphClient`'s
Retry-After handling works against real throttling - not just the mocked
`requests.Session` in `tests/test_graph_client.py`.

Not part of the check suite. Every request goes through `GraphClient.get()`
exactly as the checks/onboarding/offboarding code paths do; this script
doesn't reimplement or bypass `_with_429_retry` in `graph_client.py`, it
just generates enough concurrent traffic to trigger a 429 from Graph and
then observes the outcome. Because `GraphClient.get()` already retries
internally, a throttled call still returns 200 by the time it comes back -
so a `logging.Handler` is attached here purely to *watch* the WARNING-level
"-> 429, waiting Xs (Retry-After)" line `_with_429_retry` already logs, and
record its timestamp and Retry-After value without touching graph_client.py
itself. The INFO-level line for the retry that follows (`GET ... -> 200`)
is emitted by the same code, unaltered.

Requests target `GET /users?$top=1` - already covered by `User.ReadWrite.All`
(granted for onboarding/offboarding), so no new permission grant is needed,
and `$top=1` keeps each response body tiny.

Usage:
    .venv/Scripts/python.exe scripts/trigger_throttle.py [--burst-size N] [--max-bursts N]
"""

from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from identity_audit.auth import get_access_token
from identity_audit.config import load_graph_config
from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient, GraphError

LOG_PATH = "logs/throttle-trigger.log"
USERS_PATH = "/users"

logger = logging.getLogger(__name__)


class _ThrottleWatcher(logging.Handler):
    """Observes graph_client's own 429 WARNING log lines (emitted by
    `_with_429_retry`) and records each one. Purely a listener on the
    existing logging call - graph_client.py is never touched.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.events: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name != "identity_audit.graph_client":
            return
        if "-> 429" not in record.getMessage():
            return
        method, url, retry_after = record.args  # matches _with_429_retry's logger.warning(...) call
        self.events.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "url": url,
                "retry_after_seconds": retry_after,
            }
        )


def _configure_logging() -> _ThrottleWatcher:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8"),
        ],
    )
    watcher = _ThrottleWatcher()
    logging.getLogger("identity_audit.graph_client").addHandler(watcher)
    return watcher


def _fire_one(client: GraphClient, index: int) -> tuple[int, int, float]:
    """A single GET through the normal client.get() path. If Graph
    throttles it, `_with_429_retry` waits out Retry-After and retries
    internally before returning - so the return value here is always the
    eventual success, and any 429 is only visible via the watcher.
    """
    url = f"{GRAPH_BASE_URL}{USERS_PATH}"
    started = time.monotonic()
    response = client.get(url, params={"$top": "1"})
    elapsed = time.monotonic() - started
    return index, response.status_code, elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--burst-size", type=int, default=150, help="Concurrent GET /users requests per burst"
    )
    parser.add_argument(
        "--max-bursts", type=int, default=6, help="Stop after this many bursts if no 429 seen"
    )
    parser.add_argument(
        "--pool-size",
        type=int,
        default=15,
        help="Reused keep-alive connections shared by all worker threads",
    )
    args = parser.parse_args()

    watcher = _configure_logging()

    config = load_graph_config()
    token = get_access_token(config)

    # Opening a fresh TCP+TLS connection per request is the bottleneck in
    # this environment (each new connection got visibly slower under
    # concurrency - the classic backoff signature of a host-level cap on
    # new outbound connections), not Graph itself. Capping the pool at a
    # modest size and setting pool_block=True makes every worker thread
    # queue for one of a small set of *reused* keep-alive connections
    # instead of opening a new one - this is just session configuration on
    # the injected requests.Session GraphClient already accepts for this
    # purpose; graph_client.py's retry logic is untouched.
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=1, pool_maxsize=args.pool_size, pool_block=True
    )
    session.mount("https://", adapter)
    client = GraphClient(access_token=token, session=session)

    for burst in range(1, args.max_bursts + 1):
        if watcher.events:
            break
        logger.info(
            "Burst %d/%d: firing %d concurrent GET %s requests",
            burst, args.max_bursts, args.burst_size, USERS_PATH,
        )
        with ThreadPoolExecutor(max_workers=args.burst_size) as pool:
            futures = [
                pool.submit(_fire_one, client, i) for i in range(args.burst_size)
            ]
            for future in as_completed(futures):
                try:
                    index, status, elapsed = future.result()
                    logger.info(
                        "  request #%d finished -> %s in %.2fs (after any internal retries)",
                        index, status, elapsed,
                    )
                except GraphError as exc:
                    logger.error("  request failed: %s", exc)

    print("\n" + "=" * 60)
    if watcher.events:
        print(f"Captured {len(watcher.events)} real 429 response(s) from Graph:\n")
        for event in watcher.events:
            print(
                f"  {event['timestamp']}  {event['method']} {event['url']} "
                f"-> 429, Retry-After: {event['retry_after_seconds']}s"
            )
        print(
            f"\nFull timestamped sequence (429 + the retry that followed it) "
            f"is in {LOG_PATH}"
        )
    else:
        print(
            f"No 429 observed after {args.max_bursts} burst(s) of "
            f"{args.burst_size} requests - tenant's throttling threshold "
            f"wasn't reached. Try a larger --burst-size or --max-bursts."
        )
    print("=" * 60)


if __name__ == "__main__":
    main()
