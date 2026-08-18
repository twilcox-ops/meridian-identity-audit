"""Entry point for Part A: run every audit check and produce the report.

Each check's raw results are printed to console and also collected into a
shared, severity-ranked `Finding` list that gets written out as an HTML
report and optionally emailed - see `identity_audit.report` for the
severity model and escalation rules.

The detailed per-check console dump (real UPNs, display names) is suppressed
under CI (`CI=true`, set automatically by GitHub Actions) - see
`_print_check_block` below. The full detail is unaffected either way: it's
always in the HTML report and the emailed copy, both gated separately from
console output.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Sequence, TypeVar

from identity_audit.auth import get_access_token
from identity_audit.checks.device_compliance import (
    DEVICE_CHECKIN_STALE_THRESHOLD_DAYS,
    find_noncompliant_or_stale_devices,
)
from identity_audit.checks.guest_accounts import find_guest_accounts
from identity_audit.checks.mfa import find_users_without_mfa
from identity_audit.checks.ownerless_groups import find_ownerless_groups
from identity_audit.checks.privileged_roles import find_privileged_role_holders
from identity_audit.checks.service_principal_credentials import (
    CREDENTIAL_EXPIRY_WARNING_DAYS,
    STATUS_EXPIRED,
    find_expiring_service_principal_credentials,
)
from identity_audit.checks.stale_accounts import (
    STALE_SIGN_IN_THRESHOLD_DAYS,
    find_stale_licensed_users,
)
from identity_audit.ci import is_ci_environment
from identity_audit.config import load_graph_config
from identity_audit.graph_client import GraphClient
from identity_audit.mailer import maybe_send_report_email
from identity_audit.report import (
    build_findings,
    render_html_report,
    summary_line,
    write_report,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _print_check_block(
    header: str, items: Sequence[T], format_item: Callable[[T], str]
) -> None:
    """Print a check's full result block to console - unless running in CI.

    Under CI (`is_ci_environment()`), this whole block - header included -
    is suppressed. The check module's own `logger.info(...)` summary line
    (e.g. "MFA check complete: 16 user(s) without MFA registered") is
    already emitted unconditionally and is safe for a CI log: it's a count,
    not a list of identities. Locally, this prints exactly as it always
    has - header, then one formatted line per item.
    """
    if is_ci_environment():
        return
    print(header)
    for item in items:
        print(format_item(item))


def _format_credential(cred) -> str:
    if cred.status == STATUS_EXPIRED:
        expiry_desc = f"expired {abs(cred.days_until_expiry)} day(s) ago"
    else:
        expiry_desc = f"expires in {cred.days_until_expiry} day(s)"
    return (
        f"  {cred.sp_display_name}  ({cred.app_id})  {cred.credential_type}  "
        f"{expiry_desc}  [{cred.status}]"
    )


def _format_device(device) -> str:
    compliance = "compliant" if device.is_compliant else "non-compliant"
    last_seen = (
        "never"
        if device.days_since_check_in is None
        else f"{device.days_since_check_in} day(s) ago"
    )
    return f"  {device.display_name}  {compliance}  last check-in: {last_seen}"


def main() -> int:
    try:
        config = load_graph_config()
        token = get_access_token(config)
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    client = GraphClient(access_token=token)

    mfa_gaps = find_users_without_mfa(client)
    _print_check_block(
        f"\nUsers without MFA registered ({len(mfa_gaps)}):",
        mfa_gaps,
        lambda user: f"  {user.user_principal_name}  ({user.display_name})",
    )

    stale_users = find_stale_licensed_users(client)
    _print_check_block(
        f"\nLicensed users inactive {STALE_SIGN_IN_THRESHOLD_DAYS}+ days "
        f"({len(stale_users)}):",
        stale_users,
        lambda user: (
            f"  {user.user_principal_name}  ({user.display_name})  "
            f"last sign-in: {user.last_sign_in or 'never signed in'}"
        ),
    )

    guests = find_guest_accounts(client)
    _print_check_block(
        f"\nGuest accounts ({len(guests)}):",
        guests,
        lambda guest: (
            f"  {guest.user_principal_name}  ({guest.display_name})  "
            f"{guest.days_in_tenant} days in tenant"
        ),
    )

    privileged_users = find_privileged_role_holders(client)
    _print_check_block(
        f"\nUsers holding privileged directory roles ({len(privileged_users)}):",
        privileged_users,
        lambda user: (
            f"  {user.user_principal_name}  ({user.display_name})  "
            f"roles: {', '.join(user.roles)}"
        ),
    )

    expiring_credentials = find_expiring_service_principal_credentials(client)
    _print_check_block(
        f"\nService principal credentials expiring within "
        f"{CREDENTIAL_EXPIRY_WARNING_DAYS} days or already expired "
        f"({len(expiring_credentials)}):",
        expiring_credentials,
        _format_credential,
    )

    ownerless_groups = find_ownerless_groups(client)
    _print_check_block(
        f"\nGroups with no owner ({len(ownerless_groups)}):",
        ownerless_groups,
        lambda group: f"  {group.group_display_name}  ({group.group_id})",
    )

    flagged_devices = find_noncompliant_or_stale_devices(client)
    _print_check_block(
        f"\nDevices non-compliant or inactive "
        f"{DEVICE_CHECKIN_STALE_THRESHOLD_DAYS}+ days ({len(flagged_devices)}):",
        flagged_devices,
        _format_device,
    )

    findings = build_findings(
        mfa_gaps=mfa_gaps,
        stale_users=stale_users,
        guests=guests,
        privileged_users=privileged_users,
        expiring_credentials=expiring_credentials,
        ownerless_groups=ownerless_groups,
        flagged_devices=flagged_devices,
    )
    html = render_html_report(findings)
    report_path = write_report(findings, html=html)
    summary = summary_line(findings)
    print(f"\nWrote severity-ranked report to {report_path} ({summary})")

    # Optional final step: gated on DIGEST_TO/DIGEST_FROM being set, so a
    # run with no email configured still succeeds with just the local file.
    email_sent = maybe_send_report_email(
        client, subject=f"Identity Audit: {summary}", html_body=html
    )
    if email_sent:
        print("Sent report email.")
    else:
        print("Report email skipped (see log for why).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
