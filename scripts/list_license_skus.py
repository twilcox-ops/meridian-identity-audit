"""One-off utility: list every license SKU in the tenant.

Not part of the check suite - this exists purely to find a real `skuId` to
pass to `onboard-user --license-sku-id` when testing onboarding.py against
a real tenant, since SKU IDs are opaque GUIDs with no way to guess them.

Requires the `Organization.Read.All` application permission for
`GET /subscribedSkus` - not currently granted for any Part A check or for
onboarding; flag before adding it in Entra. `Directory.Read.All` is the
broader alternative Graph also accepts.

Usage:
    .venv/Scripts/python.exe scripts/list_license_skus.py
"""

from __future__ import annotations

import json
import logging

import requests

from identity_audit.auth import get_access_token
from identity_audit.config import load_graph_config
from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient, GraphError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

SUBSCRIBED_SKUS_PATH = "/subscribedSkus"
_SELECT_FIELDS = "skuId,skuPartNumber"


def _print_error_body(token: str, url: str, params: dict) -> None:
    """Re-issue the call directly (bypassing GraphClient, which discards
    the response body on error) to print Graph's JSON error - "error.code"
    and "error.message" usually name the exact cause. Same diagnostic
    pattern used earlier to debug the MFA check's 403.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    response = requests.get(url, headers=headers, params=params)
    print(f"\nGET {SUBSCRIBED_SKUS_PATH} -> {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        body = {"raw_text": response.text}
    print(json.dumps(body, indent=2))


def main() -> None:
    config = load_graph_config()
    token = get_access_token(config)
    client = GraphClient(access_token=token)

    url = f"{GRAPH_BASE_URL}{SUBSCRIBED_SKUS_PATH}"
    params = {"$select": _SELECT_FIELDS}

    try:
        skus: list[dict] = []
        for page in client.get_pages(url, params=params):
            skus.extend(page)
    except GraphError:
        _print_error_body(token, url, params)
        raise

    if not skus:
        print("No subscribed SKUs found in this tenant.")
        return

    part_number_width = max(
        (len(sku.get("skuPartNumber", "")) for sku in skus),
        default=0,
    )
    part_number_width = max(part_number_width, len("SKU PART NUMBER"))

    header = f"{'SKU PART NUMBER'.ljust(part_number_width)}  SKU ID"
    print(f"\n{header}")
    print("-" * len(header))
    for sku in skus:
        part_number = sku.get("skuPartNumber", "")
        sku_id = sku.get("skuId", "")
        print(f"{part_number.ljust(part_number_width)}  {sku_id}")


if __name__ == "__main__":
    main()
