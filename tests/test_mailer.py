"""Tests for report email delivery via Graph's sendMail API.

Mocks the sendMail POST - no real tenant, no real recipient. All email
addresses below are fictional placeholders.
"""

from __future__ import annotations

from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient
from identity_audit.mailer import maybe_send_report_email, send_report_email

from .fakes import FakeResponse, FakeSessionPostOnly as FakeSession


def test_send_report_email_posts_expected_sendmail_payload():
    session = FakeSession([FakeResponse(202)])
    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    send_report_email(
        client,
        subject="Identity Audit: 3 critical, 2 warning, 1 info",
        html_body="<html><body>fictional report body</body></html>",
        sender="digest-sender@example.test",
        recipient="digest-recipient@example.test",
    )

    assert len(session.calls) == 1
    url, payload = session.calls[0]
    assert url == f"{GRAPH_BASE_URL}/users/digest-sender@example.test/sendMail"

    message = payload["message"]
    assert message["subject"] == "Identity Audit: 3 critical, 2 warning, 1 info"
    assert message["body"] == {
        "contentType": "HTML",
        "content": "<html><body>fictional report body</body></html>",
    }
    assert message["toRecipients"] == [
        {"emailAddress": {"address": "digest-recipient@example.test"}}
    ]


def test_maybe_send_report_email_sends_when_configured(monkeypatch):
    monkeypatch.setenv("DIGEST_TO", "digest-recipient@example.test")
    monkeypatch.setenv("DIGEST_FROM", "digest-sender@example.test")

    session = FakeSession([FakeResponse(202)])
    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    sent = maybe_send_report_email(
        client,
        subject="Identity Audit: 0 critical, 0 warning, 0 info",
        html_body="<html></html>",
    )

    assert sent is True
    assert len(session.calls) == 1


def test_maybe_send_report_email_skips_when_not_configured(monkeypatch):
    monkeypatch.delenv("DIGEST_TO", raising=False)
    monkeypatch.delenv("DIGEST_FROM", raising=False)

    session = FakeSession([])
    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    sent = maybe_send_report_email(
        client,
        subject="Identity Audit: 0 critical, 0 warning, 0 info",
        html_body="<html></html>",
    )

    assert sent is False
    assert len(session.calls) == 0


def test_maybe_send_report_email_returns_false_on_send_failure(monkeypatch):
    monkeypatch.setenv("DIGEST_TO", "digest-recipient@example.test")
    monkeypatch.setenv("DIGEST_FROM", "digest-sender@example.test")

    # Mail.Send not yet granted/consented - Graph would 403 this.
    session = FakeSession([FakeResponse(403, {"error": {"code": "Forbidden"}})])
    client = GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)

    sent = maybe_send_report_email(
        client,
        subject="Identity Audit: 0 critical, 0 warning, 0 info",
        html_body="<html></html>",
    )

    assert sent is False
