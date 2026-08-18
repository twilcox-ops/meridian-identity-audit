"""Part A check: devices that are non-compliant or haven't checked in.

Pulls `displayName`, `isCompliant`, and `approximateLastSignInDateTime` from
`/devices` via `$select` - that last property is Graph's actual name for a
device's last-activity timestamp (there's no separate "last check-in"
field). A device is flagged if either condition holds:

- `isCompliant` is `false` (or `null` - Graph returns `null` rather than
  `true`/`false` for devices with no compliance signal at all, e.g. ones
  never enrolled in Intune; treated as non-compliant here since "unknown
  compliance state" is itself worth flagging, not something to pass
  through as if it were fine).
- `approximateLastSignInDateTime` is more than
  DEVICE_CHECKIN_STALE_THRESHOLD_DAYS old, or missing entirely - a device
  Graph has never seen check in is at least as stale as one that checked in
  90+ days ago.

DEVICE_CHECKIN_STALE_THRESHOLD_DAYS is deliberately a *separate* constant
from `stale_accounts.STALE_SIGN_IN_THRESHOLD_DAYS`, even though both
default to 90 days today. They measure different things - device check-in
vs. user sign-in - and in a real org they'd likely be owned by different
teams with different tolerances; tying them to one shared constant would
make it impossible to tighten either without silently tightening the other.

Requires the `Device.Read.All` application permission - a new grant,
distinct from the six already in use: device objects aren't covered by any
of them. See README Permissions section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient
from identity_audit.graph_dates import parse_graph_datetime

logger = logging.getLogger(__name__)

DEVICES_PATH = "/devices"

# Deliberately separate from stale_accounts.STALE_SIGN_IN_THRESHOLD_DAYS -
# see module docstring for why.
DEVICE_CHECKIN_STALE_THRESHOLD_DAYS = 90

_SELECT_FIELDS = "displayName,isCompliant,approximateLastSignInDateTime"


@dataclass(frozen=True)
class FlaggedDevice:
    display_name: str
    is_compliant: bool
    days_since_check_in: int | None  # None if the device has never checked in


def find_noncompliant_or_stale_devices(
    client: GraphClient,
    page_size: int | None = None,
    now: datetime | None = None,
) -> list[FlaggedDevice]:
    """Return every device that is non-compliant, stale, or both.

    `page_size` sets `$top` on the initial request only, so tests can force
    pagination without a real tenant. `now` is injectable so tests get
    deterministic "N days ago" math instead of depending on wall-clock time.
    """
    reference_time = now or datetime.now(timezone.utc)
    cutoff = reference_time - timedelta(days=DEVICE_CHECKIN_STALE_THRESHOLD_DAYS)

    params: dict[str, object] = {"$select": _SELECT_FIELDS}
    if page_size is not None:
        params["$top"] = page_size

    url = f"{GRAPH_BASE_URL}{DEVICES_PATH}"

    results: list[FlaggedDevice] = []
    for page in client.get_pages(url, params=params):
        for record in page:
            is_compliant = bool(record.get("isCompliant"))
            last_check_in_raw = record.get("approximateLastSignInDateTime")

            if last_check_in_raw is None:
                days_since_check_in = None
                is_stale = True  # never checked in - trivially stale
            else:
                last_check_in = parse_graph_datetime(last_check_in_raw)
                days_since_check_in = (reference_time - last_check_in).days
                is_stale = last_check_in <= cutoff

            if (not is_compliant) or is_stale:
                results.append(
                    FlaggedDevice(
                        display_name=record.get("displayName", ""),
                        is_compliant=is_compliant,
                        days_since_check_in=days_since_check_in,
                    )
                )

    logger.info(
        "Device-compliance check complete: %d device(s) non-compliant or "
        "inactive %d+ days",
        len(results),
        DEVICE_CHECKIN_STALE_THRESHOLD_DAYS,
    )
    return results
