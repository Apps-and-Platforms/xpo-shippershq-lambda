"""Sync one BigCommerce order's WWEX carrier/method from ShipperHQ to Pipe17.

Credentials are read from environment variables (see .env.example):
    BC_STORE_HASH    - the store hash, e.g. "abc123"
    BC_ACCESS_TOKEN  - API account access token (X-Auth-Token)
    BC_CLIENT_ID     - optional, only needed for OAuth-style apps
"""

from email.utils import parsedate_to_datetime
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from bigcommerce_metafields import record_pipe17_status
from logger import AppLogger
from pipe17 import PENDING_STATUSES, REVIEW_REQUIRED, Pipe17Client
from shipperhq import ShipperHQClient, CarrierMappingError, get_item_carriers, get_order_wwex_fields

load_dotenv()

log = AppLogger(__name__)

API_BASE = "https://api.bigcommerce.com/stores/{store_hash}/v2/orders"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

# BigCommerce built-in order status IDs (https://developer.bigcommerce.com/docs/rest-management/orders/order-status).
AWAITING_SHIPMENT_STATUS_ID = 9

# Orders missing from Pipe17 for less than this long are logged as a warning, not an error.
PIPE17_MISSING_GRACE_PERIOD = timedelta(days=7)

# Order statuses worth surfacing louder than a routine INFO line.
STATUS_LOG_LEVELS = {
    "Cancelled": "warning",
    "Declined": "warning",
    "Refunded": "warning",
    "Partially Refunded": "warning",
    "Disputed": "error",
    "Manual Verification Required": "warning",
}


class TransientProcessingError(Exception):
    """Raised when a temporary error occurs and the event should be reprocessed."""
    def __init__(self, message="Transient error encountered. Retrying event."):
        self.message = message
        super().__init__(self.message)


class BigCommerceClient:
    def __init__(self, store_hash=None, access_token=None, client_id=None):
        self.store_hash = store_hash or os.getenv("BC_STORE_HASH")
        self.access_token = access_token or os.getenv("BC_ACCESS_TOKEN")
        self.client_id = client_id or os.getenv("BC_CLIENT_ID")

        if not self.store_hash or not self.access_token:
            log.critical("Missing BigCommerce credentials (BC_STORE_HASH / BC_ACCESS_TOKEN env vars).")
            raise ValueError("BigCommerce store hash and access token are required.")

        self.session = requests.Session()
        self.session.headers.update({
            "X-Auth-Token": self.access_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if self.client_id:
            self.session.headers["X-Auth-Client"] = self.client_id

        log.debug(
            f"Initialized BigCommerce client for store {self.store_hash} "
            f"(client_id {'set' if self.client_id else 'NOT set'}, timeout={REQUEST_TIMEOUT}s)"
        )

    def _request(self, url, params=None, method="GET", json_body=None):
        for attempt in range(1, MAX_RETRIES + 1):
            log.debug(f"{method} {url} params={params} body={json_body} (attempt {attempt}/{MAX_RETRIES})")
            started = time.monotonic()
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body, timeout=REQUEST_TIMEOUT
                )
            except requests.exceptions.RequestException as exc:
                log.error(
                    f"Network error calling BigCommerce API {method} {url} after {time.monotonic() - started:.2f}s "
                    f"(attempt {attempt}/{MAX_RETRIES}): {type(exc).__name__}: {exc}"
                )
                if attempt == MAX_RETRIES:
                    log.error(f"Giving up on BigCommerce {method} {url} after {MAX_RETRIES} attempts")
                    raise
                log.debug(f"Retrying BigCommerce request in {2 ** attempt}s")
                time.sleep(2 ** attempt)
                continue

            elapsed = time.monotonic() - started
            log.debug(
                f"BigCommerce {method} {url} responded {response.status_code} in {elapsed:.2f}s "
                f"({len(response.content)} bytes, rate-limit remaining="
                f"{response.headers.get('X-Rate-Limit-Requests-Left', 'n/a')})"
            )

            if response.status_code == 429:
                retry_after_ms = int(response.headers.get("X-Rate-Limit-Time-Reset-Ms", 1000))
                retry_after = retry_after_ms / 1000
                log.warning(
                    f"Rate limited by BigCommerce API on {method} {url}. Retrying in {retry_after:.1f}s "
                    f"(attempt {attempt}/{MAX_RETRIES})"
                )
                time.sleep(retry_after)
                continue

            if response.status_code == 401:
                log.critical(
                    f"BigCommerce authentication failed (401) on {method} {url}. "
                    f"Check BC_ACCESS_TOKEN / BC_STORE_HASH. Body: {response.text[:500]}"
                )
                response.raise_for_status()

            if response.status_code == 204:
                log.debug(f"BigCommerce {method} {url} returned 204 No Content")
                return []

            if not response.ok:
                log.error(
                    f"BigCommerce API returned {response.status_code} for {method} {url} "
                    f"(params={params}, body={json_body}): {response.text[:500]}"
                )
                response.raise_for_status()

            try:
                payload = response.json()
            except ValueError:
                log.error(f"BigCommerce API returned non-JSON body for {method} {url}: {response.text[:500]}")
                raise
            log.debug(f"BigCommerce {method} {url} payload: {str(payload)[:2000]}")
            return payload

        log.error(f"Exceeded {MAX_RETRIES} retries calling BigCommerce {method} {url}")
        raise RuntimeError("Exceeded max retries calling BigCommerce API.")
    

    def get_order(self, order_id: int):
        """Fetch one BigCommerce order by ID."""
        url = f"{API_BASE.format(store_hash=self.store_hash)}/{order_id}"
        return self._request(url)

    def update_order_status(self, order_id: int, status_id: int):
        """Set the BigCommerce order's status by status ID."""
        url = f"{API_BASE.format(store_hash=self.store_hash)}/{order_id}"
        order = self._request(url, method="PUT", json_body={"status_id": status_id})
        log.info(f"Order #{order_id} | BigCommerce status changed to '{order.get('status')}' (status_id={status_id})")
        return order


def log_order(order: dict):
    order_id = order.get("id")
    status = order.get("status", "Unknown")
    total = order.get("total_inc_tax")
    date_modified = order.get("date_modified")
    customer_id = order.get("customer_id")

    level_name = STATUS_LOG_LEVELS.get(status, "info")
    log_fn = getattr(log, level_name)

    log_fn(
        f"Order #{order_id} | status='{status}' | customer_id={customer_id} "
        f"| total={total} | modified={date_modified}"
    )


def process_order(order_id: int):
    """Copy the WWEX carrier/method from ShipperHQ onto the order's Pipe17 custom fields."""
    try:
        client = BigCommerceClient()
    except ValueError as exc:
        log.critical(f"Could not initialize API clients: {exc}. Exiting.")
        raise exc

    order = client.get_order(order_id)
    if order.get("status") != "Awaiting Fulfillment":
        log_order(order)
        log.info(f"Order #{order_id} | status '{order.get('status')}' is not 'Awaiting Fulfillment'; skipping.")
        return {"status": "skipped", "order_id": order_id, "order_status": order.get("status")}

    try:
        shq_client = ShipperHQClient()
        pipe17_client = Pipe17Client()
    except ValueError as exc:
        log.critical(f"Could not initialize API clients: {exc}. Exiting.")
        raise TransientProcessingError(f"Could not initialize API clients: {exc}")
 
    try:
        try:
            pipe17_order_id = pipe17_client.get_order_id_by_ext_order_id(ext_order_id=order_id)
        except requests.exceptions.HTTPError as exc:
            log.critical(f"Order #{order_id} | failed to look up Pipe17 order: {exc}")
            raise exc

        if pipe17_order_id is None:
            # Pipe17 may lag behind BigCommerce; only escalate once the order has been missing a while.
            date_modified = order.get("date_modified")
            try:
                order_age = datetime.now(timezone.utc) - parsedate_to_datetime(date_modified)
            except (TypeError, ValueError):
                order_age = None
            if order_age is not None and order_age < PIPE17_MISSING_GRACE_PERIOD:
                log.warning(f"Order #{order_id} | no Pipe17 order found yet (modified={date_modified}); skipping.")
            else:
                log.error(f"Order #{order_id} | no Pipe17 order found (modified={date_modified}); skipping.")
            raise RuntimeError(f"Order #{order_id} | no Pipe17 order found (modified={date_modified})")

        record_pipe17_status(client, order_id, "processing")

        # Only orders that are awaiting review in Pipe17 get their carrier custom fields updated.
        # Orders still in an earlier status are retried until they reach review; anything else is final.
        pipe17_status = pipe17_client.get_order_status(pipe17_order_id)
        if pipe17_status is None:
            raise TransientProcessingError(f"Order #{order_id} | could not read Pipe17 order {pipe17_order_id} status")
        if pipe17_status in PENDING_STATUSES:
            log.info(
                f"Order #{order_id} | Pipe17 order {pipe17_order_id} is '{pipe17_status}', "
                f"not yet '{REVIEW_REQUIRED}'; retrying later."
            )
            raise TransientProcessingError(
                f"Order #{order_id} | Pipe17 order {pipe17_order_id} still '{pipe17_status}'"
            )
        if pipe17_status != REVIEW_REQUIRED:
            log.info(f"Order #{order_id} | Pipe17 order {pipe17_order_id} is '{pipe17_status}', not '{REVIEW_REQUIRED}'; skipping.")
            return {"status": "not_review_required", "order_id": order_id, "pipe17_status": pipe17_status}

        try:
            shipments = shq_client.get_advanced_shipment(order_id)
        except requests.exceptions.HTTPError as exc:
            log.error(f"Failed to fetch ShipperHQ advanced shipment for order #{order_id}: {exc}")
            return {"status": "failed", "order_id": order_id}
        log.info(f"Order #{order_id} | ShipperHQ advanced shipment: {shipments}")

        item_carriers = get_item_carriers(shipments)
        log.info(f"Order #{order_id} | carriers by SKU: {item_carriers}")
        order_fields = get_order_wwex_fields(item_carriers)
        log.info(f"Order #{order_id} | order WWEX fields: {order_fields}")

        updated = pipe17_client.update_order_custom_fields(pipe17_order_id, item_carriers, order_fields)
        record_pipe17_status(client, order_id, "updated" if updated else "failed")
        if not updated:
            log.error(f"Order #{order_id} | Pipe17 update failed.")
            return {"status": "failed", "order_id": order_id}

        # client.update_order_status(order_id, AWAITING_SHIPMENT_STATUS_ID)

    except requests.exceptions.HTTPError as exc:
        log.critical(f"Aborting order sync due to unrecoverable API error: {exc}")
        raise exc
    except TransientProcessingError:
        raise
    except CarrierMappingError as exc:
        # Retrying won't fix bad ShipperHQ carrier data, so fail without retrying the SQS message.
        log.error(f"Order #{order_id} | carrier mapping failed ({type(exc).__name__}); not retrying: {exc}")
        return {"status": "unmapped", "order_id": order_id, "error": str(exc)}
    except Exception as exc:
        log.critical(f"Unexpected error during order sync ({type(exc).__name__}): {exc}", exc_info=True)
        raise

    log.info(f"Order #{order_id} | sync complete.")
    return {"status": "updated", "order_id": order_id}
