"""Fetch BigCommerce orders modified in the last N days and log status-significant changes.

Credentials are read from environment variables (see .env.example):
    BC_STORE_HASH    - the store hash, e.g. "abc123"
    BC_ACCESS_TOKEN  - API account access token (X-Auth-Token)
    BC_CLIENT_ID     - optional, only needed for OAuth-style apps
"""

from email.utils import format_datetime, parsedate_to_datetime
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from logger import AppLogger
from pipe17 import REVIEW_REQUIRED, Pipe17Client
from shipperhq import ShipperHQClient, find_shipment_by_detail_name, get_carrier_code, map_to_wwex

load_dotenv()

log = AppLogger(__name__)

API_BASE = "https://api.bigcommerce.com/stores/{store_hash}/v2/orders"
V3_API_BASE = "https://api.bigcommerce.com/stores/{store_hash}/v3/orders"
PAGE_LIMIT = 250
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

# BigCommerce status_id for "Awaiting Fulfillment".
AWAITING_FULFILLMENT_STATUS_ID = 11

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
        if not self.client_id:
            log.warning(
                "BC_CLIENT_ID is not set; 'updated_in_pipe17' metafield ownership checks will not match "
                "any existing metafield."
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
    

    def get_orders_modified_since(self, min_date_modified: int = None, status_id: int = AWAITING_FULFILLMENT_STATUS_ID):
        """Yield orders with date_modified >= `min_date_modified` (Unix timestamp) and the given `status_id`, handling pagination.

        Pass `status_id=None` to fetch orders of any status.
        """
        url = API_BASE.format(store_hash=self.store_hash)
        if min_date_modified is None:
            min_date_modified = (((datetime.now(timezone.utc) - timedelta(days=7))))
        
        page = 1
        total_fetched = 0

        log.info(f"Fetching orders modified since {min_date_modified} with status_id={status_id}")

        while True:
            params = {
                "min_date_modified": format_datetime(min_date_modified),
                "page": page,
                "limit": PAGE_LIMIT,
            }
            if status_id is not None:
                params["status_id"] = status_id
            log.debug(f"Requesting page {page} with params {params}")
            orders = self._request(url, params)

            if not orders:
                log.debug(f"No more orders returned at page {page}; stopping pagination.")
                break

            total_fetched += len(orders)
            log.info(f"Retrieved {len(orders)} orders on page {page} (total so far: {total_fetched})")

            for order in orders:
                yield order

            if len(orders) < PAGE_LIMIT:
                log.debug(f"Page {page} returned {len(orders)} < {PAGE_LIMIT} orders; this is the last page.")
                break
            page += 1

        log.info(f"Finished fetching orders. Total orders modified in range: {total_fetched}")

    def get_order(self, order_id: int):
        """Fetch one BigCommerce order by ID."""
        url = f"{API_BASE.format(store_hash=self.store_hash)}/{order_id}"
        return self._request(url)

    def get_order_metafields(self, order_id: int):
        """Yield all metafields for a given order (V3 API), handling pagination."""
        url = f"{V3_API_BASE.format(store_hash=self.store_hash)}/{order_id}/metafields"
        page = 1

        log.debug(f"Order #{order_id} | fetching metafields")
        while True:
            params = {"page": page, "limit": PAGE_LIMIT}
            response = self._request(url, params)

            data = response.get("data", []) if isinstance(response, dict) else []
            if not data:
                log.debug(f"Order #{order_id} | no metafields on page {page}; stopping pagination.")
                break

            log.debug(f"Order #{order_id} | got {len(data)} metafield(s) on page {page}")

            for metafield in data:
                yield metafield

            pagination = response.get("meta", {}).get("pagination", {})
            total_pages = pagination.get("total_pages", page)
            log.debug(f"Order #{order_id} | metafield page {page}/{total_pages}")
            if page >= total_pages:
                break
            page += 1

    def create_order_metafield(self, order_id: int, key: str, value: str,
                                namespace: str = "Pipe17", permission_set: str = "write"):
        """Create a metafield on the given order (V3 API)."""
        url = f"{V3_API_BASE.format(store_hash=self.store_hash)}/{order_id}/metafields"
        body = {
            "key": key,
            "value": value,
            "namespace": namespace,
            "permission_set": permission_set,
        }
        log.debug(f"Order #{order_id} | creating metafield '{key}'='{value}' (namespace='{namespace}')")
        response = self._request(url, method="POST", json_body=body)
        created = response.get("data") if isinstance(response, dict) else response
        log.info(
            f"Order #{order_id} | created metafield '{key}'='{value}' (namespace='{namespace}', "
            f"id={(created or {}).get('id') if isinstance(created, dict) else 'n/a'})"
        )
        return response.get("data") if isinstance(response, dict) else response

    def update_order_metafield(self, order_id: int, metafield_id: int, value: str):
        """Update the value of an existing metafield on the given order (V3 API)."""
        url = f"{V3_API_BASE.format(store_hash=self.store_hash)}/{order_id}/metafields/{metafield_id}"
        log.debug(f"Order #{order_id} | updating metafield id={metafield_id} to '{value}'")
        response = self._request(url, method="PUT", json_body={"value": value})
        log.info(f"Order #{order_id} | updated metafield id={metafield_id} to '{value}'")
        return response.get("data") if isinstance(response, dict) else response

    def find_pipe17_updated_metafield(self, metafields: list):
        """Return this app's 'updated_in_pipe17' metafield from `metafields`, or None."""
        return next(
            (
                metafield for metafield in metafields
                if metafield.get("key") == "updated_in_pipe17"
                and str(metafield.get("owner_client_id")) == str(self.client_id)
            ),
            None,
        )

    def ensure_pipe17_updated_metafield(self, order_id: int, metafields: list):
        """Ensure the order has an 'updated_in_pipe17' metafield owned by this app; create it if missing."""
        has_flag = any(
            metafield.get("key") == "updated_in_pipe17"
            and str(metafield.get("owner_client_id")) == str(self.client_id)
            for metafield in metafields
        )
        log.debug(f"Order #{order_id} | 'updated_in_pipe17' metafield owned by client_id={self.client_id} present: {has_flag}")
        if has_flag:
            log.debug(f"Order #{order_id} | 'updated_in_pipe17' metafield already present.")
            return None

        return self.create_order_metafield(order_id, "updated_in_pipe17", "created")

    def does_need_pipe17_processing(self, metafields: list):
        """Ensure the order has an 'updated_in_pipe17' metafield owned by this app; create it if missing."""
        has_flag = any(
            metafield.get("key") == "updated_in_pipe17"
            and str(metafield.get("owner_client_id")) == str(self.client_id)
            and metafield.get('value') == 'created'
            for metafield in metafields
        )
        log.debug(f"'updated_in_pipe17' metafield with value 'created' present (needs Pipe17 processing): {has_flag}")

        return has_flag    


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


def log_metafields(order_id: int, metafields: list):
    if not metafields:
        log.debug(f"Order #{order_id} | no metafields")
        return

    log.info(f"Order #{order_id} | {len(metafields)} metafield(s)")
    for metafield in metafields:
        log.debug(
            f"Order #{order_id} | metafield id={metafield.get('id')} "
            f"namespace='{metafield.get('namespace')}' key='{metafield.get('key')}' "
            f"value='{metafield.get('value')}'"
        )


def process_order(days: int = 27, order_id: int = None):
    try:
        client = BigCommerceClient()
    except ValueError as exc:
        log.critical(f"Could not initialize API clients: {exc}. Exiting.")
        raise exc

    event_order = None
    if order_id is not None:
        event_order = client.get_order(order_id)
        if event_order.get("status") != "Awaiting Fulfillment":
            log_order(event_order)
            log.info(
                f"Order #{order_id} | status '{event_order.get('status')}' is not "
                "'Awaiting Fulfillment'; skipping."
            )
            return {"status": "skipped", "order_id": order_id, "order_status": event_order.get("status")}

    try:
        shq_client = ShipperHQClient()
        pipe17_client = Pipe17Client()
    except ValueError as exc:
        log.critical(f"Could not initialize API clients: {exc}. Exiting.")
        raise TransientProcessingError(f"Could not initialize API clients: {exc}")

    stats = {
        "queued": 0, "not_review_required": 0, "no_wwex": 0, "pipe17_missing": 0, "unmapped": 0,
        "updated": 0, "failed": 0,
    }

    order_count = 0
    try:
        orderList = list()

        orders = [event_order] 
        for order in orders:

            order_id = order.get("id")
            if order.get("status") == "Awaiting Fulfillment":
                try:
                    metafields = list(client.get_order_metafields(order_id))
                    log_metafields(order_id, metafields)
                    created_metafield = client.ensure_pipe17_updated_metafield(order_id, metafields)
                    if created_metafield is not None:
                        log.info(
                            f"Order #{order_id} | new order; queued for Pipe17 update "
                            f"(metafield id={created_metafield.get('id')})"
                        )
                        orderList.append((order, created_metafield.get("id")))
                    else:
                        if client.does_need_pipe17_processing(metafields):
                            existing_metafield = client.find_pipe17_updated_metafield(metafields)
                            log.info(
                                f"Order #{order_id} | previously seen but not yet updated in Pipe17; queued "
                                f"(metafield id={existing_metafield.get('id')})"
                            )
                            orderList.append((order, existing_metafield.get("id")))
                        else:
                            log.info(f"Order #{order_id} | already updated in Pipe17; skipping.")

                except requests.exceptions.HTTPError as exc:
                    log.error(f"Failed to sync metafields for order #{order_id}: {exc}")
                    raise RuntimeError(f"Failed to sync metafields for order #{order_id}: {exc}") from exc
            else:
                log.debug(f"Order #{order_id} | status '{order.get('status')}' is not 'Awaiting Fulfillment'; skipping.")

            order_count += 1

        # Only orders that are awaiting review in Pipe17 get their carrier custom fields updated.
        reviewList = list()
        for order, metafield_id in orderList:
            order_id = order.get("id")
            try:
                pipe17_order_id = pipe17_client.get_order_id_by_ext_order_id(ext_order_id=order_id)
            except requests.exceptions.HTTPError as exc:
                stats["failed"] += 1
                log.critical(f"Order #{order_id} | failed to look up Pipe17 order: {exc}")
                raise exc

            if pipe17_order_id is None:
                stats["pipe17_missing"] += 1
                # Pipe17 may lag behind BigCommerce; only escalate once the order has been missing a while.
                date_modified = order.get("date_modified")
                try:
                    order_age = datetime.now(timezone.utc) - parsedate_to_datetime(date_modified)
                except (TypeError, ValueError):
                    order_age = None
                if order_age is not None and order_age < PIPE17_MISSING_GRACE_PERIOD:
                    log.warning(
                        f"Order #{order_id} | no Pipe17 order found yet (modified={date_modified}); skipping."
                    )
                else:
                    log.error(
                        f"Order #{order_id} | no Pipe17 order found (modified={date_modified}); skipping."
                    )
                raise RuntimeError(f"Order #{order_id} | no Pipe17 order found (modified={date_modified})")
            
            if not pipe17_client.is_order_status(pipe17_order_id, REVIEW_REQUIRED):
                stats["not_review_required"] += 1
                log.info(f"Order #{order_id} | Pipe17 order {pipe17_order_id} is not '{REVIEW_REQUIRED}'; skipping.")
                continue
            reviewList.append((order, metafield_id, pipe17_order_id))

        orderList = reviewList

        stats["queued"] = len(orderList)
        log.info(
            f"Scanned {order_count} order(s); {len(orderList)} queued for ShipperHQ/Pipe17 processing: "
            f"{[o.get('id') for o, _, _ in orderList]}"
        )

        for index, (order, metafield_id, pipe17_order_id) in enumerate(orderList, start=1):
            order_id = order.get("id")
            log.info(f"Order #{order_id} | processing {index}/{len(orderList)} (metafield id={metafield_id})")
            try:
                shipments = shq_client.get_advanced_shipment(order_id)
                wwex_shipment = find_shipment_by_detail_name(shipments=shipments, name="World Wide Express 3PL")

                if wwex_shipment is not None:
                    # There should be only one shipment with wwex in order
                    carrier_code, method_code = get_carrier_code(shipment=wwex_shipment)
                    log.info(
                        f"Order #{order_id} | ShipperHQ advanced shipment: {shipments} "
                        f"Carrier Code: {carrier_code} Method Code: {method_code}"
                    )

                    wwex_carrier, wwex_carrier_code = map_to_wwex(carrier_code, method_code)
                    if wwex_carrier is None:
                        stats["unmapped"] += 1
                        log.error(f"Order #{order_id} | skipping Pipe17 update; no WWEX mapping for {carrier_code}/{method_code}")
                        continue

                    log.info(
                        f"Order #{order_id} | Pipe17 orderId={pipe17_order_id}; mapped {carrier_code}/{method_code} "
                        f"-> Carrier='{wwex_carrier}' Code='{wwex_carrier_code}'"
                    )
                    updated = pipe17_client.update_order_custom_fields(
                        pipe17_order_id, carrier=wwex_carrier, carrier_code=wwex_carrier_code
                    )
                    if updated:
                        stats["updated"] += 1
                        client.update_order_metafield(order_id, metafield_id, "updated")
                        log.info(f"Order #{order_id} | sync complete; marked 'updated_in_pipe17'='updated'.")
                    else:
                        stats["failed"] += 1
                        log.error(f"Order #{order_id} | Pipe17 update failed; will retry on next run.")
                else:
                    stats["no_wwex"] += 1
                    log.info(
                        f"Order #{order_id} | no 'World Wide Express 3PL' shipment in ShipperHQ; "
                        f"nothing to update in Pipe17 (will be re-checked next run)."
                    )
                    
                log.info(f"Order #{order_id} | ShipperHQ advanced shipment: {shipments}")
            except requests.exceptions.HTTPError as exc:
                stats["failed"] += 1
                log.error(f"Failed to fetch ShipperHQ advanced shipment for order #{order_id}: {exc}")

    except requests.exceptions.HTTPError as exc:
        log.critical(f"Aborting order sync due to unrecoverable API error: {exc}")
        sys.exit(1)
    except Exception as exc:
        log.critical(f"Unexpected error during order sync ({type(exc).__name__}): {exc}", exc_info=True)
        raise

    log.info(
        f"Order sync complete. Processed {order_count} orders modified "
        f"in the last {days} day(s). Summary: {stats}"
    )
    return {"status": "completed", "order_id": order_id, "stats": stats}

