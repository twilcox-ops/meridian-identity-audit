"""Severity-ranked HTML report for Part A findings.

Turns each check's raw results into a shared `Finding` list, ranks them by
severity, and renders one self-contained HTML file grouped by severity with
summary counts at the top. No email, no scheduling - this module only
produces the file; those pieces land separately once this one is solid.

## Severity model (the reasoning behind it, not a silent decision)

Three levels, in descending order of urgency:

- **CRITICAL** - either an active exposure right now (no MFA on an account
  that can be phished or password-sprayed straight into a takeover), or a
  credential that has already failed (expired, not just expiring) rather
  than one there's still time to rotate.
- **WARNING** - a governance/hygiene gap that increases risk or blast
  radius but isn't itself an active compromise: a stale-but-licensed
  account nobody's using, a group nobody owns, a non-compliant device, a
  credential that's *about* to expire (there's still a window to act), or
  simply holding a privileged role (expected for some people - not
  inherently bad on its own).
- **INFO** - visibility, not a failure. A guest account existing at all is
  normal for most orgs; this check is about knowing they're there and for
  how long, not flagging every guest as a problem. A device that's
  compliant but just hasn't phoned home in a while defaults here too,
  since "compliant and idle" is a much smaller concern than
  "non-compliant," and conflating the two would bury the devices that
  actually need attention under ones that probably don't.

## Escalation: when one finding makes another worse

A flat per-check severity misses the case that matters most in a real
incident - the same identity showing up in more than one check at once.
Three escalation rules, each a known identity-security anti-pattern, all
escalating the *privileged-role* finding to CRITICAL (that check is the
natural hub for this: holding elevated access is what turns an ordinary
gap into an urgent one):

1. Privileged role holder + no MFA registered - an admin account reachable
   without a second factor.
2. Privileged role holder + guest account - an external identity holding
   elevated internal access.
3. Privileged role holder + stale-but-licensed (inactive 90+ days) - a
   dormant admin credential is a classic target: nobody's watching it, but
   it still works.

Other plausible overlaps (e.g. correlating a non-compliant device to the
user signed into it) aren't implemented, because the checks as built don't
carry the data needed to correlate them - the device check has no
per-user linkage. Noted here rather than silently skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path

from identity_audit.checks.device_compliance import FlaggedDevice
from identity_audit.checks.guest_accounts import GuestAccount
from identity_audit.checks.mfa import UserMfaStatus
from identity_audit.checks.ownerless_groups import OwnerlessGroup
from identity_audit.checks.privileged_roles import PrivilegedUser
from identity_audit.checks.service_principal_credentials import (
    STATUS_EXPIRED,
    ExpiringCredential,
)
from identity_audit.checks.stale_accounts import StaleLicensedUser

logger = logging.getLogger(__name__)

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

# Display order, most urgent first - drives both grouping and the summary line.
SEVERITY_ORDER = (CRITICAL, WARNING, INFO)
_SEVERITY_LABELS = {CRITICAL: "Critical", WARNING: "Warning", INFO: "Info"}

DEFAULT_REPORT_PATH = Path("reports") / "audit-report.html"


@dataclass(frozen=True)
class Finding:
    severity: str  # CRITICAL | WARNING | INFO
    category: str
    subject: str
    detail: str = ""


def build_findings(
    mfa_gaps: list[UserMfaStatus],
    stale_users: list[StaleLicensedUser],
    guests: list[GuestAccount],
    privileged_users: list[PrivilegedUser],
    expiring_credentials: list[ExpiringCredential],
    ownerless_groups: list[OwnerlessGroup],
    flagged_devices: list[FlaggedDevice],
) -> list[Finding]:
    """Normalize every check's raw results into one severity-ranked list.

    Escalation needs to see across checks (e.g. "is this privileged user
    also in the MFA-gap list?"), so it happens here rather than inside any
    individual check - each check module stays focused on its own Graph
    call and knows nothing about the others.
    """
    mfa_gap_upns = {u.user_principal_name for u in mfa_gaps}
    guest_upns = {g.user_principal_name for g in guests}
    stale_upns = {u.user_principal_name for u in stale_users}

    findings: list[Finding] = []

    for user in mfa_gaps:
        findings.append(
            Finding(
                severity=CRITICAL,
                category="No MFA registered",
                subject=f"{user.user_principal_name} ({user.display_name})",
            )
        )

    for user in stale_users:
        last_seen = user.last_sign_in or "never signed in"
        findings.append(
            Finding(
                severity=WARNING,
                category="Inactive 90+ days, still licensed",
                subject=f"{user.user_principal_name} ({user.display_name})",
                detail=f"last sign-in: {last_seen}",
            )
        )

    for guest in guests:
        findings.append(
            Finding(
                severity=INFO,
                category="Guest account",
                subject=f"{guest.user_principal_name} ({guest.display_name})",
                detail=f"{guest.days_in_tenant} days in tenant",
            )
        )

    for user in privileged_users:
        reasons = []
        if user.user_principal_name in mfa_gap_upns:
            reasons.append("no MFA registered")
        if user.user_principal_name in guest_upns:
            reasons.append("guest account")
        if user.user_principal_name in stale_upns:
            reasons.append("inactive 90+ days")

        severity = CRITICAL if reasons else WARNING
        detail = f"roles: {', '.join(user.roles)}"
        if reasons:
            detail += f" - ALSO: {', '.join(reasons)}"

        findings.append(
            Finding(
                severity=severity,
                category="Privileged role holder",
                subject=f"{user.user_principal_name} ({user.display_name})",
                detail=detail,
            )
        )

    for cred in expiring_credentials:
        if cred.status == STATUS_EXPIRED:
            severity = CRITICAL
            category = "Service principal credential expired"
            expiry_desc = f"expired {abs(cred.days_until_expiry)} day(s) ago"
        else:
            severity = WARNING
            category = "Service principal credential expiring soon"
            expiry_desc = f"expires in {cred.days_until_expiry} day(s)"

        findings.append(
            Finding(
                severity=severity,
                category=category,
                subject=f"{cred.sp_display_name} ({cred.app_id})",
                detail=f"{cred.credential_type} credential - {expiry_desc}",
            )
        )

    for group in ownerless_groups:
        findings.append(
            Finding(
                severity=WARNING,
                category="Group with no owner",
                subject=f"{group.group_display_name} ({group.group_id})",
            )
        )

    for device in flagged_devices:
        last_seen = (
            "never"
            if device.days_since_check_in is None
            else f"{device.days_since_check_in} day(s) ago"
        )
        if not device.is_compliant:
            severity = WARNING
            category = "Non-compliant device"
        else:
            severity = INFO
            category = "Device inactive 90+ days"

        findings.append(
            Finding(
                severity=severity,
                category=category,
                subject=device.display_name,
                detail=f"last check-in: {last_seen}",
            )
        )

    logger.info("Report build complete: %d finding(s)", len(findings))
    return findings


def group_by_severity(findings: list[Finding]) -> dict[str, list[Finding]]:
    """Bucket findings by severity, in SEVERITY_ORDER."""
    grouped: dict[str, list[Finding]] = {sev: [] for sev in SEVERITY_ORDER}
    for finding in findings:
        grouped.setdefault(finding.severity, []).append(finding)
    return grouped


def summarize_counts(findings: list[Finding]) -> dict[str, int]:
    """Return {severity: count} for every severity in SEVERITY_ORDER."""
    return {sev: len(items) for sev, items in group_by_severity(findings).items()}


def summary_line(findings: list[Finding]) -> str:
    """E.g. "3 critical, 5 warning, 12 info" - used in both the HTML and the console."""
    counts = summarize_counts(findings)
    return ", ".join(
        f"{counts[sev]} {_SEVERITY_LABELS[sev].lower()}" for sev in SEVERITY_ORDER
    )


def render_html_report(
    findings: list[Finding], generated_at: datetime | None = None
) -> str:
    """Render findings into one self-contained HTML page, grouped by severity."""
    generated_at = generated_at or datetime.now().astimezone()
    grouped = group_by_severity(findings)

    sections_html = "\n".join(
        _render_section(sev, grouped[sev]) for sev in SEVERITY_ORDER if grouped[sev]
    )
    if not sections_html:
        sections_html = '<p class="empty">No findings.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Identity Posture Audit Report</title>
<style>
{_CSS}
</style>
</head>
<body>
<h1>Identity Posture Audit Report</h1>
<p class="generated-at">Generated {escape(generated_at.isoformat(timespec="seconds"))}</p>
<p class="summary">{escape(summary_line(findings))}</p>
{sections_html}
</body>
</html>
"""


def _render_section(severity: str, findings: list[Finding]) -> str:
    rows = "\n".join(_render_row(f) for f in findings)
    return f"""<section class="severity-{severity}">
<h2>{_SEVERITY_LABELS[severity]} ({len(findings)})</h2>
<table>
<thead><tr><th>Category</th><th>Subject</th><th>Detail</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</section>"""


def _render_row(finding: Finding) -> str:
    return (
        "<tr>"
        f"<td>{escape(finding.category)}</td>"
        f"<td>{escape(finding.subject)}</td>"
        f"<td>{escape(finding.detail)}</td>"
        "</tr>"
    )


_CSS = """
body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { margin-bottom: 0.25rem; }
.generated-at { color: #666; margin-top: 0; }
.summary { font-weight: 600; font-size: 1.1rem; margin-bottom: 2rem; }
section { margin-bottom: 2rem; }
h2 { border-bottom: 2px solid #ccc; padding-bottom: 0.25rem; }
.severity-critical h2 { border-color: #b00020; color: #b00020; }
.severity-warning h2 { border-color: #b25f00; color: #b25f00; }
.severity-info h2 { border-color: #1a5fb4; color: #1a5fb4; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #eee; }
th { background: #f5f5f5; }
.empty { color: #666; }
"""


def write_report(
    findings: list[Finding],
    path: Path = DEFAULT_REPORT_PATH,
    html: str | None = None,
) -> Path:
    """Write the HTML report to `path`, creating parent dirs as needed.

    Pass `html` (e.g. from a `render_html_report(findings)` call the caller
    already made, to also email the same content) to avoid rendering twice -
    rendering again would carry a slightly different `generated_at`
    timestamp than what was emailed. Renders internally if omitted.
    """
    if html is None:
        html = render_html_report(findings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    logger.info("Wrote severity-ranked report to %s (%d finding(s))", path, len(findings))
    return path
