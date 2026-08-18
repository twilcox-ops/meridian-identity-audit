"""Environment-backed configuration.

Loads the Graph app registration details from environment variables (via
python-dotenv) and never logs or prints them. Fail fast and by name if a
required variable is missing, so a misconfigured `.env` is caught before any
network call is made.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

_REQUIRED_VARS = (
    "GRAPH_TENANT_ID",
    "GRAPH_CLIENT_ID",
    "GRAPH_CERT_PATH",
    "GRAPH_CERT_THUMBPRINT",
)


@dataclass(frozen=True)
class GraphConfig:
    tenant_id: str
    client_id: str
    cert_path: str
    cert_thumbprint: str


def load_graph_config() -> GraphConfig:
    """Load and validate Graph auth settings from the environment.

    Does not read or print `.env` contents beyond what `dotenv` needs to
    populate `os.environ`; values are passed through, never logged.
    """
    load_dotenv()

    missing = [name for name in _REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            f"{', '.join(missing)}. Copy .env.example to .env and fill them in."
        )

    return GraphConfig(
        tenant_id=os.environ["GRAPH_TENANT_ID"],
        client_id=os.environ["GRAPH_CLIENT_ID"],
        cert_path=os.environ["GRAPH_CERT_PATH"],
        cert_thumbprint=os.environ["GRAPH_CERT_THUMBPRINT"],
    )
