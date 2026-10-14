"""Create/update BigCommerce order metafields recording the WWEX carrier chosen for an order.

Uses the V3 order metafields API:
    GET  /v3/orders/{order_id}/metafields?namespace=...
    POST /v3/orders/{order_id}/metafields
    PUT  /v3/orders/{order_id}/metafields/{metafield_id}
"""

from datetime import datetime, timezone

from logger import AppLogger

log = AppLogger(__name__)

METAFIELDS_API = "https://api.bigcommerce.com/stores/{store_hash}/v3/orders/{order_id}/metafields"
NAMESPACE = "shippingdetails"
# "read" lets other API accounts read these metafields; only this app can write them.
PERMISSION_SET = "read"
ACTOR = "aws_lambda"


def _get_existing_metafields(client, order_id: int) -> dict:
    """Return {key: metafield_id} for this order's metafields in our namespace."""
    url = METAFIELDS_API.format(store_hash=client.store_hash, order_id=order_id)
    payload = client._request(url, params={"namespace": NAMESPACE, "limit": 250})
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return {field["key"]: field["id"] for field in data}


def upsert_order_metafields(client, order_id: int, values: dict) -> None:
    """Create or update each key/value as a metafield on the order (values are stored as strings)."""
    base_url = METAFIELDS_API.format(store_hash=client.store_hash, order_id=order_id)
    existing = _get_existing_metafields(client, order_id)

    for key, value in values.items():
        body = {
            "namespace": NAMESPACE,
            "key": key,
            "value": "" if value is None else str(value),
            "permission_set": PERMISSION_SET,
        }
        metafield_id = existing.get(key)
        if metafield_id is None:
            log.debug(f"Order #{order_id} | creating metafield {NAMESPACE}.{key}={body['value']!r}")
            client._request(base_url, method="POST", json_body=body)
        else:
            log.debug(f"Order #{order_id} | updating metafield {NAMESPACE}.{key} (id={metafield_id})={body['value']!r}")
            client._request(f"{base_url}/{metafield_id}", method="PUT", json_body=body)


def set_wwex_metafields(client, order_id: int, wwex_carrier: str, wwex_carrier_code: str) -> None:
    """Record the WWEX carrier/code on the BigCommerce order, stamped with the time and actor."""
    upsert_order_metafields(client, order_id, {
        "wwex_carrier": wwex_carrier,
        "wwex_carrier_code": wwex_carrier_code,
        # "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": ACTOR,
    })
    log.info(
        f"Order #{order_id} | BigCommerce metafields set: wwex_carrier='{wwex_carrier}' "
        f"wwex_carrier_code='{wwex_carrier_code}' actor='{ACTOR}'"
    )


def set_item_carrier_metafields(client, order_id: int, item_carriers: dict) -> None:
    """Record each SKU's carrier name and method code on the order as carrier_<sku> / carrier_code_<sku>."""
    if not item_carriers:
        log.info(f"Order #{order_id} | no shipment items found; no per-SKU carrier metafields to set")
        return

    values = {}
    for sku, (carrier_name, carrier_code) in item_carriers.items():
        values[f"carrier_{sku}"] = carrier_name
        values[f"carrier_code_{sku}"] = carrier_code
    upsert_order_metafields(client, order_id, values)
    log.info(f"Order #{order_id} | BigCommerce per-SKU carrier metafields set for {len(item_carriers)} SKU(s): {item_carriers}")
