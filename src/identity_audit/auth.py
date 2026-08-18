"""Certificate-based app-only auth against Microsoft Graph via MSAL.

No client secret anywhere: the confidential client authenticates with a
private key (`GRAPH_CERT_PATH`) whose matching public certificate was
uploaded to the Entra ID app registration, identified by its SHA-1
thumbprint (`GRAPH_CERT_THUMBPRINT`).
"""

from __future__ import annotations

import logging

import msal

from identity_audit.config import GraphConfig

logger = logging.getLogger(__name__)

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


def _authority(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}"


def get_access_token(config: GraphConfig) -> str:
    """Acquire an app-only Graph access token using the client certificate.

    Raises RuntimeError with MSAL's error/description on failure. Never logs
    the token, the certificate contents, or any config value.
    """
    with open(config.cert_path, "r", encoding="utf-8") as cert_file:
        private_key = cert_file.read()

    app = msal.ConfidentialClientApplication(
        client_id=config.client_id,
        authority=_authority(config.tenant_id),
        client_credential={
            "private_key": private_key,
            "thumbprint": config.cert_thumbprint,
        },
    )

    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)

    if "access_token" not in result:
        error = result.get("error", "unknown_error")
        description = result.get("error_description", "no description")
        logger.error("Token acquisition failed: %s", error)
        raise RuntimeError(f"MSAL token acquisition failed: {error} - {description}")

    logger.info("Acquired Graph access token (app-only, certificate auth)")
    return result["access_token"]
