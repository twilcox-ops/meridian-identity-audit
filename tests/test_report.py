"""Tests for severity-ranked report generation.

Covers the two things this step needs proven: severity grouping/counts are
correct given small fake findings, and the cross-check escalation rule
(e.g. a privileged-role holder who's also missing MFA) actually escalates.
No Graph calls, no real tenant. All names below are fictional placeholders.
"""

from __future__ import annotations

from identity_audit.checks.mfa import UserMfaStatus
from identity_audit.checks.privileged_roles import PrivilegedUser
from identity_audit.report import (
    CRITICAL,
    INFO,
    WARNING,
    Finding,
    build_findings,
    group_by_severity,
    render_html_report,
    summarize_counts,
    summary_line,
)


def _fake_findings() -> list[Finding]:
    return [
        Finding(severity=CRITICAL, category="No MFA registered", subject="a@example.test (A)"),
        Finding(severity=CRITICAL, category="No MFA registered", subject="b@example.test (B)"),
        Finding(severity=CRITICAL, category="No MFA registered", subject="c@example.test (C)"),
        Finding(
            severity=WARNING,
            category="Group with no owner",
            subject="Team Fictional (grp-1)",
        ),
        Finding(
            severity=WARNING,
            category="Group with no owner",
            subject="Team Placeholder (grp-2)",
        ),
        Finding(
            severity=INFO,
            category="Guest account",
            subject="guest@example.test (Fictional Guest)",
        ),
    ]


def test_group_by_severity_and_counts():
    findings = _fake_findings()

    grouped = group_by_severity(findings)
    assert [f.subject for f in grouped[CRITICAL]] == [
        "a@example.test (A)",
        "b@example.test (B)",
        "c@example.test (C)",
    ]
    assert len(grouped[WARNING]) == 2
    assert len(grouped[INFO]) == 1

    counts = summarize_counts(findings)
    assert counts == {CRITICAL: 3, WARNING: 2, INFO: 1}
    assert summary_line(findings) == "3 critical, 2 warning, 1 info"


def test_render_html_report_includes_summary_and_all_categories():
    findings = _fake_findings()
    html = render_html_report(findings)

    assert "3 critical, 2 warning, 1 info" in html
    assert "No MFA registered" in html
    assert "Group with no owner" in html
    assert "Guest account" in html
    assert "a@example.test (A)" in html


def test_render_html_report_handles_no_findings():
    html = render_html_report([])

    assert "0 critical, 0 warning, 0 info" in html
    assert "No findings." in html


def test_build_findings_escalates_privileged_role_holder_missing_mfa():
    mfa_gaps = [
        UserMfaStatus(
            user_principal_name="admin.fictional@example.test",
            display_name="Fictional Admin",
        )
    ]
    privileged_users = [
        PrivilegedUser(
            user_principal_name="admin.fictional@example.test",
            display_name="Fictional Admin",
            roles=("Global Administrator",),
        ),
        PrivilegedUser(
            user_principal_name="other.admin@example.test",
            display_name="Other Admin",
            roles=("User Administrator",),
        ),
    ]

    findings = build_findings(
        mfa_gaps=mfa_gaps,
        stale_users=[],
        guests=[],
        privileged_users=privileged_users,
        expiring_credentials=[],
        ownerless_groups=[],
        flagged_devices=[],
    )

    privileged_findings = {
        f.subject: f for f in findings if f.category == "Privileged role holder"
    }

    escalated = privileged_findings["admin.fictional@example.test (Fictional Admin)"]
    assert escalated.severity == CRITICAL
    assert "no MFA registered" in escalated.detail

    not_escalated = privileged_findings["other.admin@example.test (Other Admin)"]
    assert not_escalated.severity == WARNING
