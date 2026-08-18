"""Part A check: service principal credentials nearing expiry.

Pulls `displayName`, `appId`, `passwordCredentials`, and `keyCredentials`
from `/servicePrincipals` via `$select`. A service principal can hold
multiple credentials of each type at once (e.g. mid-rotation), and each
credential carries its own `endDateTime` - this check evaluates every
credential independently rather than the service principal as a whole.

Design decision (flagged to and confirmed by the project owner, not picked
silently): an already-expired credential is *included* in this check's
results rather than dropped, tagged `status="expired"` (vs.
`"expiring_soon"` for credentials still inside the warning window) so the
eventual severity-ranked report can treat "already expired" as strictly
worse than "expiring in N days" instead of losing that distinction by
collapsing both into one undifferentiated list. `days_until_expiry` is
negative for already-expired credentials.

Requires the `Application.Read.All` application permission - a new grant,
distinct from `User.Read.All` / `AuditLog.Read.All` / `Reports.Read.All` /
`RoleManagement.Read.Directory` already in use: service principal
credential metadata isn't covered by any of those. See README Permissions
section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient
from identity_audit.graph_dates import parse_graph_datetime

logger = logging.getLogger(__name__)

SERVICE_PRINCIPALS_PATH = "/servicePrincipals"

# Named per the requirement, not a magic number scattered through the logic.
CREDENTIAL_EXPIRY_WARNING_DAYS = 30

_SELECT_FIELDS = "displayName,appId,passwordCredentials,keyCredentials"

STATUS_EXPIRING_SOON = "expiring_soon"
STATUS_EXPIRED = "expired"

# (result label, Graph property name) for the two credential collections -
# same shape, different property, so both are walked with one loop.
_CREDENTIAL_FIELDS = (("password", "passwordCredentials"), ("key", "keyCredentials"))


@dataclass(frozen=True)
class ExpiringCredential:
    sp_display_name: str
    app_id: str
    credential_type: str  # "password" | "key"
    days_until_expiry: int  # negative if already expired
    status: str  # STATUS_EXPIRING_SOON | STATUS_EXPIRED


def find_expiring_service_principal_credentials(
    client: GraphClient,
    page_size: int | None = None,
    now: datetime | None = None,
) -> list[ExpiringCredential]:
    """Return every service principal credential due to expire within
    CREDENTIAL_EXPIRY_WARNING_DAYS, plus every credential already expired.

    `page_size` sets `$top` on the initial request only, so tests can force
    pagination without a real tenant. `now` is injectable so tests get
    deterministic day-count math instead of depending on wall-clock time.
    """
    reference_time = now or datetime.now(timezone.utc)
    warning_cutoff = reference_time + timedelta(days=CREDENTIAL_EXPIRY_WARNING_DAYS)

    params: dict[str, object] = {"$select": _SELECT_FIELDS}
    if page_size is not None:
        params["$top"] = page_size

    url = f"{GRAPH_BASE_URL}{SERVICE_PRINCIPALS_PATH}"

    results: list[ExpiringCredential] = []
    for page in client.get_pages(url, params=params):
        for record in page:
            display_name = record.get("displayName", "")
            app_id = record.get("appId", "")

            for credential_type, field_name in _CREDENTIAL_FIELDS:
                for credential in record.get(field_name) or []:
                    end_date_time_raw = credential.get("endDateTime")
                    if not end_date_time_raw:
                        continue  # no expiry to evaluate

                    expires_at = parse_graph_datetime(end_date_time_raw)
                    if expires_at > warning_cutoff:
                        continue  # not nearing expiry yet

                    days_until_expiry = (expires_at - reference_time).days
                    status = (
                        STATUS_EXPIRED
                        if expires_at <= reference_time
                        else STATUS_EXPIRING_SOON
                    )

                    results.append(
                        ExpiringCredential(
                            sp_display_name=display_name,
                            app_id=app_id,
                            credential_type=credential_type,
                            days_until_expiry=days_until_expiry,
                            status=status,
                        )
                    )

    logger.info(
        "Service-principal credential check complete: %d credential(s) "
        "expiring within %d days or already expired",
        len(results),
        CREDENTIAL_EXPIRY_WARNING_DAYS,
    )
    return results
