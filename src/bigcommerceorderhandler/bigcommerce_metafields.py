"""Create/update BigCommerce order metafields recording the outcome of the order sync.

Uses the V3 order metafields API:
    GET  /v3/orders/{order_id}/metafields?namespace=...&key=...
    POST /v3/orders/{order_id}/metafields
    PUT  /v3/orders/{order_id}/metafields/{metafield_id}
"""

import requests

from logger import AppLogger

log = AppLogger(__name__)

METAFIELDS_API = "https://api.bigcommerce.com/stores/{store_hash}/v3/orders/{order_id}/metafields"
METAFIELD_NAMESPACE = "shippingdetails"
# "read" lets other API accounts read these metafields; only this app can write them.
METAFIELD_PERMISSION_SET = "read"
# Metafield recording the progress of the Pipe17 carrier update ("processing" / "updated" / "failed").
PIPE17_STATUS_METAFIELD_KEY = "pipe17_update_status"
PIPE17_STATUS_PROCESSING = "processing"
PIPE17_STATUS_UPDATED = "updated"
PIPE17_STATUS_FAILED = "failed"


def upsert_order_metafield(client, order_id: int, key: str, value):
    """Create the order metafield `key` in our namespace, or update it if it already exists."""
    url = METAFIELDS_API.format(store_hash=client.store_hash, order_id=order_id)
    payload = client._request(url, params={"namespace": METAFIELD_NAMESPACE, "key": key})
    existing = payload.get("data", []) if isinstance(payload, dict) else []
    body = {
        "namespace": METAFIELD_NAMESPACE,
        "key": key,
        "value": "" if value is None else str(value),
        "permission_set": METAFIELD_PERMISSION_SET,
    }
    if existing:
        metafield_id = existing[0]["id"]
        log.debug(f"Order #{order_id} | updating metafield {METAFIELD_NAMESPACE}.{key} (id={metafield_id})={body['value']!r}")
        client._request(f"{url}/{metafield_id}", method="PUT", json_body=body)
    else:
        log.debug(f"Order #{order_id} | creating metafield {METAFIELD_NAMESPACE}.{key}={body['value']!r}")
        client._request(url, method="POST", json_body=body)
    log.info(f"Order #{order_id} | BigCommerce metafield {METAFIELD_NAMESPACE}.{key}='{body['value']}'")


def record_pipe17_status(client, order_id: int, status: str):
    """Record the Pipe17 update outcome on the BigCommerce order.

    Failures are logged, not raised: by now Pipe17 may already be updated, and a retry would skip the order.
    """
    try:
        upsert_order_metafield(client, order_id, PIPE17_STATUS_METAFIELD_KEY, status)
    except requests.exceptions.RequestException as exc:
        log.error(
            f"Order #{order_id} | failed to record Pipe17 status '{status}' in BigCommerce metafield: "
            f"{type(exc).__name__}: {exc}"
        )
