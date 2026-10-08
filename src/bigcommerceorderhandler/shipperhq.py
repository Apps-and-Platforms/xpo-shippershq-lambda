"""ShipperHQ GraphQL API client and shipment helpers.

Credentials are read from environment variables (see .env.example):
    SHQ_ACCESS_TOKEN - ShipperHQ access token (X-ShipperHQ-Access-Token)
    SHQ_API_BASE     - optional, defaults to https://ovs.shipperhq.com
"""

import os
import time

import requests
from dotenv import load_dotenv

from logger import AppLogger

load_dotenv()

log = AppLogger(__name__)

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

SHQ_API_BASE_DEFAULT = "https://ovs.shipperhq.com"

WWEX_SHQ_CARRIER_CODE = "shqworldwideexpress"

# ShipperHQ methodCode -> (UPS numeric service code, method name) for the WWEX carrier.
WWEX_UPS_METHODS = {
    "70": ("70", "UPS Access Point Economy"),
    "2DA": ("02", "UPS 2nd Day Air"),
    "2DM": ("59", "UPS 2nd Day Air A.M."),
    "3DS": ("12", "UPS 3 Day Select"),
    "GND": ("03", "UPS Ground"),
    "1DA": ("01", "UPS Next Day Air"),
    "1DM": ("14", "UPS Next Day Air Early"),
    "1DP": ("13", "UPS Next Day Air Saver"),
    "STD": ("11", "UPS Standard"),
    "83": ("83", "UPS Today Dedicated Courier"),
    "85": ("85", "UPS Today Express"),
    "86": ("86", "UPS Today Express Saver"),
    "82": ("82", "UPS Today Standard"),
    "WEDP": ("72", "UPS Worldwide Economy DDP"),
    "WEDU": ("17", "UPS Worldwide Economy DDU"),
    "XPD": ("08", "UPS Worldwide Expedited"),
    "XPR": ("07", "UPS Worldwide Express"),
    "96": ("96", "UPS Worldwide Express Freight"),
    "XDM": ("54", "UPS Worldwide Express Plus"),
    "65": ("65", "UPS Worldwide Saver"),
}

# ShipperHQ carrierCode -> (WWEX carrier, methodCode table). To support another carrier,
# add a methods table like WWEX_UPS_METHODS above and register it here. Matched exactly.
WWEX_CARRIER_MAP = {
    WWEX_SHQ_CARRIER_CODE: ("UPS", WWEX_UPS_METHODS),
}

ADVANCED_SHIPMENT_QUERY = """
query Order($orderNumber: String!) {
  viewOrder(orderNumber: $orderNumber) {
    shipments {
      id
      shipmentDetail {
        shipmentId
        name
      }
      carriers {
        carrierDetail {
          carrierCode
          carrierTitle
          carrierType
        }
        methods {
          methodDetails {
            methodCode
            methodTitle
            totalCharges
            currency
            negotiatedRate
          }
          timeInTransitOptions {
            dispatchDate
            deliveryDate
          }
        }
        packages {
          packageDetail {
            declaredValue
            height
            length
            width
            weight
            packingWeight
            packageName
            surchargePrice
          }
          items {
            sku
            qtyPacked
            weightPacked
          }
        }
      }
    }
  }
}
"""


class ShipperHQClient:
    def __init__(self, access_token=None, api_base=None):
        self.access_token = access_token or os.getenv("SHQ_ACCESS_TOKEN")
        self.api_base = api_base or os.getenv("SHQ_API_BASE", SHQ_API_BASE_DEFAULT)

        if not self.access_token:
            log.critical("Missing ShipperHQ credentials (SHQ_ACCESS_TOKEN env var).")
            raise ValueError("ShipperHQ access token is required.")

        self.session = requests.Session()
        self.session.headers.update({
            "X-ShipperHQ-Access-Token": self.access_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-ShipperHQ-Scope":"LIVE"
        })

        log.debug(f"Initialized ShipperHQ client (api_base={self.api_base}, scope=LIVE, timeout={REQUEST_TIMEOUT}s)")

    def _graphql(self, query, variables=None):
        for attempt in range(1, MAX_RETRIES + 1):
            log.debug(f"POST {self.api_base} (GraphQL) variables={variables} (attempt {attempt}/{MAX_RETRIES})")
            started = time.monotonic()
            try:
                response = self.session.post(
                    self.api_base,
                    json={"query": query, "variables": variables or {}},
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.exceptions.RequestException as exc:
                log.error(
                    f"Network error calling ShipperHQ API after {time.monotonic() - started:.2f}s "
                    f"(attempt {attempt}/{MAX_RETRIES}, variables={variables}): {type(exc).__name__}: {exc}"
                )
                if attempt == MAX_RETRIES:
                    log.error(f"Giving up on ShipperHQ API after {MAX_RETRIES} attempts (variables={variables})")
                    raise
                log.debug(f"Retrying ShipperHQ request in {2 ** attempt}s")
                time.sleep(2 ** attempt)
                continue

            elapsed = time.monotonic() - started
            log.debug(
                f"ShipperHQ responded {response.status_code} in {elapsed:.2f}s "
                f"({len(response.content)} bytes, variables={variables})"
            )

            if response.status_code == 429:
                log.warning(
                    f"Rate limited by ShipperHQ API. Retrying in {2 ** attempt}s (attempt {attempt}/{MAX_RETRIES})"
                )
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 401:
                log.critical("ShipperHQ authentication failed (401). Check SHQ_ACCESS_TOKEN.")
                response.raise_for_status()

            if not response.ok:
                log.error(f"ShipperHQ API returned {response.status_code}: {response.text[:500]}")
                response.raise_for_status()

            try:
                payload = response.json()
            except ValueError:
                log.error(f"ShipperHQ API returned non-JSON body (status {response.status_code}): {response.text[:500]}")
                raise
            if payload.get("errors"):
                log.error(f"ShipperHQ API returned GraphQL errors for variables={variables}: {payload['errors']}")
            log.debug(f"ShipperHQ GraphQL data for variables={variables}: {payload.get('data')}")
            return payload.get("data")

        log.error(f"Exceeded {MAX_RETRIES} retries calling ShipperHQ API (variables={variables})")
        raise RuntimeError("Exceeded max retries calling ShipperHQ API.")

    def get_advanced_shipment(self, order_id):
        """Fetch ShipperHQ's Advanced Shipment details for the order, using order_id as the order number."""
        log.debug(f"Order #{order_id} | fetching ShipperHQ advanced shipment")
        data = self._graphql(ADVANCED_SHIPMENT_QUERY, {"orderNumber": str(order_id)})
        order = (data or {}).get("viewOrder")

        if not order:
            log.warning(f"Order #{order_id} | ShipperHQ viewOrder returned no data (data={data})")

        assert(len(order) > 0 )

        shipments = order[0].get("shipments") if order else None
        if len(order) > 1:
            log.warning(f"Order #{order_id} | ShipperHQ returned {len(order)} viewOrder entries; using the first one.")
        log.debug(f"Order #{order_id} | ShipperHQ returned {len(shipments or [])} shipment(s)")
        return shipments


def find_shipment_by_detail_name(shipments: list, name: str = "World Wide Express 3PL"):
    """Return the first shipment whose shipmentDetail.name matches `name`, or None if not found."""
    if not shipments:
        log.debug(f"No shipments to search for shipmentDetail.name='{name}'")
        return None

    names = []
    for shipment in shipments:
        detail = shipment.get("shipmentDetail") or {}
        names.append(detail.get("name"))
        if detail.get("name") == name:
            log.debug(f"Found shipment id={shipment.get('id')} with shipmentDetail.name='{name}'")
            return shipment

    log.info(f"No shipment with shipmentDetail.name='{name}' among {len(shipments)} shipment(s); names seen: {names}")
    return None


def get_carrier_codes(shipment: dict) -> list:
    """Return the carrierCode for every carrier attached to `shipment`."""
    if not shipment:
        return []

    codes = []
    for carrier in shipment.get("carriers") or []:
        detail = carrier.get("carrierDetail") or {}
        code = detail.get("carrierCode")
        if code:
            codes.append(code)

    return codes


def get_carrier_code(shipment: dict):
    """Return (carrierCode, methodCode) for the first carrier on `shipment` and its first method.

    Either value is None if the shipment has no carriers, or the first carrier has no methods.
    """
    carriers = (shipment or {}).get("carriers") or []
    if not carriers:
        log.warning(f"Shipment id={(shipment or {}).get('id')} has no carriers; cannot determine carrier/method code")
        return None, None

    if len(carriers) > 1:
        log.debug(f"Shipment id={shipment.get('id')} has {len(carriers)} carriers; using the first one")

    carrier = carriers[0]
    carrier_code = (carrier.get("carrierDetail") or {}).get("carrierCode")

    methods = carrier.get("methods") or []
    method_details = (methods[0].get("methodDetails") if methods else None) or {}
    if isinstance(method_details, list):
        method_details = method_details[0] if method_details else {}
    method_code = method_details.get("methodCode")

    if not methods:
        log.warning(f"Shipment id={shipment.get('id')} carrier '{carrier_code}' has no methods")
    log.debug(
        f"Shipment id={shipment.get('id')} resolved carrierCode='{carrier_code}' methodCode='{method_code}' "
        f"(methodTitle='{method_details.get('methodTitle')}')"
    )
    return carrier_code, method_code


def map_to_wwex(carrier_code, method_code):
    """Return (wwex_carrier, wwex_carrier_code) for a ShipperHQ carrier/method, or (None, None) if unmapped."""
    carrier_mapping = WWEX_CARRIER_MAP.get(carrier_code)
    if carrier_mapping is None:
        log.warning(f"No WWEX mapping for carrierCode='{carrier_code}' (known: {list(WWEX_CARRIER_MAP)})")
        return None, None

    wwex_carrier, methods = carrier_mapping
    method = methods.get(method_code)
    if method is None:
        log.warning(f"No WWEX mapping for carrierCode='{carrier_code}' methodCode='{method_code}'")
        return None, None

    numeric_code, _name = method
    log.debug(
        f"Mapped ShipperHQ carrierCode='{carrier_code}' methodCode='{method_code}' "
        f"-> WWEX carrier='{wwex_carrier}' code='{numeric_code}' ({_name})"
    )
    return wwex_carrier, numeric_code
