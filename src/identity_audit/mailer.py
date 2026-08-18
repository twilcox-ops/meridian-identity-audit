"""Sends the severity-ranked HTML report via Microsoft Graph's sendMail API.

Uses Graph's `POST /users/{sender}/sendMail` rather than a separate SMTP or
email-service dependency, so delivery stays inside the same app-only auth
and permission model as every read check, through the same `GraphClient`
(its 429/Retry-After handling applies here too, even though a single send
has nothing to paginate).

Requires the `Mail.Send` application permission - a new grant, and the
first *write-capable* permission in this project; every permission granted
so far has been read-only (`*.Read.*`). None of them cover sending mail -
it's a completely separate resource (Exchange mail, not the directory).
Worth noting for the least-privilege story: Graph's app-only `Mail.Send` by
default lets the app send as *any* mailbox in the tenant, not just the one
configured as `DIGEST_FROM` - Exchange Online's `ApplicationAccessPolicy`
can scope that down to a single mailbox, but that's an Exchange admin
action outside what this repo's code can configure. See README Permissions
section.
"""

from __future__ import annotations

import logging
import os

from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient, GraphError

logger = logging.getLogger(__name__)


def send_report_email(
    client: GraphClient,
    subject: str,
    html_body: str,
    sender: str,
    recipient: str,
) -> None:
    """Send `html_body` as an HTML email from `sender` to `recipient`.

    Raises `GraphError` (propagated from `GraphClient.post`) on a non-2xx
    response - `maybe_send_report_email` is what decides whether that's
    fatal or just logged and skipped; this function always tries to send.
    """
    url = f"{GRAPH_BASE_URL}/users/{sender}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        },
        "saveToSentItems": "false",
    }
    client.post(url, json_body=payload)


def maybe_send_report_email(client: GraphClient, subject: str, html_body: str) -> bool:
    """Send the report email if DIGEST_TO and DIGEST_FROM are both set.

    This is the gate that makes email delivery optional: an environment
    that only wants the local HTML file just leaves those two blank (as
    `.env.example` does by default) and this quietly no-ops. Only reads
    already-loaded environment variables - no `.env` file access here.

    Returns True if the email was sent, False if it was skipped (not
    configured) or failed to send (logged either way, never raised further -
    a failed email should not take down an otherwise-successful audit run).
    """
    recipient = os.environ.get("DIGEST_TO")
    sender = os.environ.get("DIGEST_FROM")

    if not recipient or not sender:
        logger.info(
            "Skipping report email: DIGEST_TO and/or DIGEST_FROM are not "
            "set in the environment"
        )
        return False

    try:
        send_report_email(
            client, subject=subject, html_body=html_body, sender=sender, recipient=recipient
        )
    except GraphError as exc:
        logger.error("Report email send failed, continuing without it: %s", exc)
        return False

    logger.info("Report email sent successfully")
    return True
