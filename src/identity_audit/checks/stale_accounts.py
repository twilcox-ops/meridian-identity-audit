"""Part A check: licensed users who haven't signed in for 90+ days.

Pulls `userPrincipalName`, `displayName`, `signInActivity`, and
`assignedLicenses` from `/users` via `$select`. Graph's `/users` endpoint
doesn't support a simple, always-consistent server-side `$filter` for either
condition this check needs:

- `signInActivity/lastSignInDateTime` filtering exists, but only through the
  advanced-query mechanics (`ConsistencyLevel: eventual` + `$count=true`),
  and is explicitly eventually consistent - replication lag on exactly the
  property a "90+ days stale" report depends on is the wrong trade for a
  first version of this check.
- "Has at least one license" has no direct `$filter` shortcut at all; Graph
  only supports filtering `assignedLicenses` by a specific SKU ID, not
  "non-empty".

So both conditions are evaluated client-side after a plain `$select` pull.
If tenant size makes that too slow later, the advanced-query filter on
`signInActivity` is the first thing to revisit.

Requires the `AuditLog.Read.All` and `User.Read.All` application
permissions (per Graph's `signInActivity` resource docs) - see README
Permissions section.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient

logger = logging.getLogger(__name__)

USERS_PATH = "/users"

# Named per the requirement, not a magic number scattered through the logic.
STALE_SIGN_IN_THRESHOLD_DAYS = 90

_SELECT_FIELDS = "userPrincipalName,displayName,signInActivity,assignedLicenses"

# Graph sometimes returns lastSignInDateTime with 7 fractional-second digits
# (100-ns ticks as decimals); datetime.fromisoformat only accepts up to 6.
_EXCESS_FRACTIONAL_SECONDS_RE = re.compile(r"(\.\d{6})\d+")


@dataclass(frozen=True)
class StaleLicensedUser:
    user_principal_name: str
    display_name: str
    last_sign_in: str | None  # raw Graph ISO 8601 timestamp, or None if never signed in


def find_stale_licensed_users(
    client: GraphClient,
    page_size: int | None = None,
    now: datetime | None = None,
) -> list[StaleLicensedUser]:
    """Return licensed users inactive for STALE_SIGN_IN_THRESHOLD_DAYS+ days.

    A user who has never signed in at all - Graph reports this as a missing
    `signInActivity` or a missing `lastSignInDateTime` within it, not as a
    date - counts as stale too: there's no sign-in within the window because
    there's no sign-in at all.

    `page_size` sets `$top` on the initial request only, so tests can force
    pagination without a real tenant. `now` is injectable so tests get
    deterministic "N days ago" math instead of depending on wall-clock time.
    """
    reference_time = now or datetime.now(timezone.utc)
    cutoff = reference_time - timedelta(days=STALE_SIGN_IN_THRESHOLD_DAYS)

    params: dict[str, object] = {"$select": _SELECT_FIELDS}
    if page_size is not None:
        params["$top"] = page_size

    url = f"{GRAPH_BASE_URL}{USERS_PATH}"

    results: list[StaleLicensedUser] = []
    for page in client.get_pages(url, params=params):
        for record in page:
            if not record.get("assignedLicenses"):
                continue  # "still holds a license" means a non-empty assignedLicenses

            last_sign_in_raw = (record.get("signInActivity") or {}).get(
                "lastSignInDateTime"
            )
            if last_sign_in_raw is None:
                is_stale = True  # never signed in - trivially 90+ days stale
            else:
                is_stale = _parse_graph_datetime(last_sign_in_raw) <= cutoff

            if is_stale:
                results.append(
                    StaleLicensedUser(
                        user_principal_name=record.get("userPrincipalName", ""),
                        display_name=record.get("displayName", ""),
                        last_sign_in=last_sign_in_raw,
                    )
                )

    logger.info(
        "Stale-account check complete: %d licensed user(s) inactive %d+ days",
        len(results),
        STALE_SIGN_IN_THRESHOLD_DAYS,
    )
    return results


def _parse_graph_datetime(value: str) -> datetime:
    """Parse a Graph ISO 8601 timestamp into an aware UTC datetime."""
    value = value.replace("Z", "+00:00")
    value = _EXCESS_FRACTIONAL_SECONDS_RE.sub(r"\1", value)
    return datetime.fromisoformat(value)
