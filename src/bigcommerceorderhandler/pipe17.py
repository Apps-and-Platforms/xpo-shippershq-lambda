"""Pipe17 REST API client.

Credentials are read from environment variables (see .env.example):
    PIPE17_API_KEY   - Pipe17 API key (X-Pipe17-Key)
    PIPE17_API_BASE  - optional, defaults to https://api-v3.pipe17.com/api/v3
"""

import os
import time

import requests
from dotenv import load_dotenv

from logger import AppLogger

load_dotenv()

log = AppLogger(__name__)

PIPE17_API_BASE_DEFAULT = "https://api-v3.pipe17.com/api/v3"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
# Recorded in the "actor" custom field so it's clear which system set the carrier.
ACTOR = "aws_lambda"

# Pipe17 order statuses that come before fulfillment.
DRAFT = "draft"                       # incomplete; needs more data before processing
NEW = "new"                           # initial status when the order is created in Pipe17
ON_HOLD = "onHold"                    # temporarily paused (default 10 minutes)
TO_BE_VALIDATED = "toBeValidated"     # awaiting validation
REVIEW_REQUIRED = "reviewRequired"    # awaiting manual review; this is where we set the carrier

# Statuses that are expected to move on to REVIEW_REQUIRED, so the event should be retried later.
PENDING_STATUSES = frozenset({DRAFT, NEW, ON_HOLD, TO_BE_VALIDATED})


class Pipe17Client:
    def __init__(self, api_key=None, api_base=None):
        self.api_key = api_key or os.getenv("PIPE17_API_KEY")
        self.api_base = (api_base or os.getenv("PIPE17_API_BASE", PIPE17_API_BASE_DEFAULT)).rstrip("/")

        if not self.api_key:
            log.critical("Missing Pipe17 credentials (PIPE17_API_KEY env var).")
            raise ValueError("Pipe17 API key is required.")

        self.session = requests.Session()
        self.session.headers.update({
            "X-Pipe17-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        log.debug(f"Initialized Pipe17 client (api_base={self.api_base}, timeout={REQUEST_TIMEOUT}s)")

    def _request(self, path, params=None, method="GET", json_body=None):
        url = f"{self.api_base}/{path.lstrip('/')}"
        for attempt in range(1, MAX_RETRIES + 1):
            log.debug(f"{method} {url} params={params} body={json_body} (attempt {attempt}/{MAX_RETRIES})")
            started = time.monotonic()
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body, timeout=REQUEST_TIMEOUT
                )
            except requests.exceptions.RequestException as exc:
                log.error(
                    f"Network error calling Pipe17 API {method} {url} after {time.monotonic() - started:.2f}s "
                    f"(attempt {attempt}/{MAX_RETRIES}): {type(exc).__name__}: {exc}"
                )
                if attempt == MAX_RETRIES:
                    log.error(f"Giving up on Pipe17 {method} {url} after {MAX_RETRIES} attempts")
                    raise
                log.debug(f"Retrying Pipe17 request in {2 ** attempt}s")
                time.sleep(2 ** attempt)
                continue

            elapsed = time.monotonic() - started
            log.debug(
                f"Pipe17 {method} {url} responded {response.status_code} in {elapsed:.2f}s "
                f"({len(response.content)} bytes)"
            )

            if response.status_code == 429:
                log.warning(
                    f"Rate limited by Pipe17 API on {method} {url}. Retrying in {2 ** attempt}s "
                    f"(attempt {attempt}/{MAX_RETRIES})"
                )
                time.sleep(2 ** attempt)
                continue

            if response.status_code in (401, 403):
                log.critical(
                    f"Pipe17 authentication failed ({response.status_code}) on {method} {url}. "
                    f"Check PIPE17_API_KEY. Body: {response.text[:500]}"
                )
                response.raise_for_status()

            if not response.ok:
                log.error(
                    f"Pipe17 API returned {response.status_code} for {method} {url} "
                    f"(params={params}, body={json_body}): {response.text[:500]}"
                )
                response.raise_for_status()

            if response.status_code == 204 or not response.content:
                log.debug(f"Pipe17 {method} {url} returned an empty body ({response.status_code})")
                return {}

            try:
                payload = response.json()
            except ValueError:
                log.error(f"Pipe17 API returned non-JSON body for {method} {url}: {response.text[:500]}")
                raise
            if not payload.get("success", True):
                log.error(f"Pipe17 API reported failure for {method} {url}: {payload}")
            log.debug(f"Pipe17 {method} {url} payload: {str(payload)[:2000]}")
            return payload

        log.error(f"Exceeded {MAX_RETRIES} retries calling Pipe17 {method} {url}")
        raise RuntimeError("Exceeded max retries calling Pipe17 API.")

    def get_orders_by_ext_order_id(self, ext_order_id):
        """Return all Pipe17 orders whose extOrderId matches `ext_order_id` (e.g. the BigCommerce order id)."""
        orders = []
        page_index = 1

        while True:
            params = {"extOrderId": str(ext_order_id)}
            log.debug(f"Order #{ext_order_id} | looking up Pipe17 orders by extOrderId (page {page_index})")
            payload = self._request("orders", params)
            page_orders = payload.get("orders") or []
            orders.extend(page_orders)

            pagination = payload.get("pagination") or {}
            log.debug(
                f"Order #{ext_order_id} | Pipe17 page {page_index}: {len(page_orders)} order(s), "
                f"pagination={pagination}"
            )
            if pagination.get("last", True) or page_index >= pagination.get("pages", page_index):
                break
            page_index += 1

        log.info(f"Order #{ext_order_id} | found {len(orders)} Pipe17 order(s)")
        return orders

    def get_order_by_ext_order_id(self, ext_order_id):
        """Return the single Pipe17 order for `ext_order_id`, or None if Pipe17 has no such order."""
        orders = self.get_orders_by_ext_order_id(ext_order_id)
        if len(orders) > 1:
            log.warning(
                f"Order #{ext_order_id} | {len(orders)} Pipe17 orders matched; using the first one. "
                f"orderIds={[o.get('orderId') for o in orders]}"
            )
        return orders[0] if orders else None

    def get_order_id_by_ext_order_id(self, ext_order_id):
        """Return Pipe17's internal "orderId" for `ext_order_id`, or None if Pipe17 has no such order."""
        order = self.get_order_by_ext_order_id(ext_order_id)
        if not order:
            log.warning(f"Order #{ext_order_id} | no matching Pipe17 order found.")
            return None

        order_id = order.get("orderId")
        if not order_id:
            log.error(f"Order #{ext_order_id} | Pipe17 order is missing 'orderId': {order}")
            return None

        log.debug(f"Order #{ext_order_id} | Pipe17 orderId={order_id}")
        return order_id

    def get_order(self, order_id):
        """Return the Pipe17 order with internal id `order_id`, or None if it can't be retrieved."""
        log.debug(f"Pipe17 order {order_id} | fetching order details")
        try:
            payload = self._request(f"orders/{order_id}")
        except requests.exceptions.RequestException as exc:
            log.error(f"Pipe17 order {order_id} | failed to fetch order: {type(exc).__name__}: {exc}")
            return None

        order = payload.get("order")
        if not order:
            log.error(
                f"Pipe17 order {order_id} | response has no 'order': "
                f"code={payload.get('code')} message='{payload.get('message')}'"
            )
            return None
        return order

    def get_order_status(self, order_id):
        """Return the status of the Pipe17 order `order_id` (e.g. "reviewRequired"), or None if it can't be read."""
        order = self.get_order(order_id)
        if not order:
            return None
        status = order.get("status")
        log.info(f"Pipe17 order {order_id} | status='{status}'")
        return status

    def is_order_status(self, order_id, expected_status=REVIEW_REQUIRED):
        """Return True if the Pipe17 order `order_id` has status `expected_status` (e.g. "readyForFulfillment")."""
        order = self.get_order(order_id)
        if not order:
            return False

        status = order.get("status")
        matches = status == expected_status
        log.info(
            f"Pipe17 order {order_id} | status='{status}', expected='{expected_status}', match={matches}"
        )
        return matches

    def update_order_custom_fields(self, order_id, item_carriers, order_fields=None):
        """Set carrier custom fields on the Pipe17 order `order_id` in a single PUT.

        `item_carriers` is {sku: carrier details} from shipperhq.get_item_carriers. Each SKU becomes two
        order custom fields: "<sku>_carrier" (its wwex_carrier) and "<sku>_carrier_code" (its method_code),
        plus a single "actor" field, and the order is moved to readyForFulfillment.

        `order_fields` ({name: value}, e.g. order_wwex_carrier / order_wwex_code) are also set as custom
        fields on the order.

        Returns True if Pipe17 reports the update succeeded, False otherwise (including when
        `item_carriers` is empty).
        """
        if not item_carriers:
            log.error(f"Pipe17 order {order_id} | no item carriers; carrier custom fields not set")
            return False

        fields = {}
        for sku, info in item_carriers.items():
            fields[f"carrier_{sku}"] = info.get("wwex_carrier")
            fields[f"carrier_code{sku}"] = info.get("method_code")
        fields["actor"] = ACTOR
        fields.update(order_fields or {})

        body = {"customFields": _custom_fields(fields), "status": "readyForFulfillment"}

        log.info(f"Pipe17 order {order_id} | updating order customFields {fields}")
        try:
            payload = self._request(f"orders/{order_id}", method="PUT", json_body=body)
        except requests.exceptions.RequestException as exc:
            log.error(f"Pipe17 order {order_id} | failed to update carrier fields: {type(exc).__name__}: {exc}")
            return False

        # An empty body (e.g. 204) means the PUT was accepted; otherwise trust the "success" flag.
        success = payload.get("success", True) is True
        if success:
            log.info(f"Pipe17 order {order_id} | updated carrier fields")
        else:
            log.error(
                f"Pipe17 order {order_id} | carrier fields update rejected: "
                f"code={payload.get('code')} message='{payload.get('message')}'"
            )
        return success


def _custom_fields(values):
    """Turn {name: value} into Pipe17's [{"name", "value"}] list; values are sent as strings."""
    return [{"name": name, "value": "" if value is None else str(value)} for name, value in values.items()]

