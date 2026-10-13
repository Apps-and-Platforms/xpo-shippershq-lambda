from datetime import datetime
from unittest import mock

import pytest

import bigcommerce_metafields
from bigcommerce_metafields import (
    ACTOR,
    NAMESPACE,
    PERMISSION_SET,
    set_item_carrier_metafields,
    set_wwex_metafields,
    upsert_order_metafields,
)

BASE_URL = "https://api.bigcommerce.com/stores/storehash/v3/orders/42/metafields"


@pytest.fixture
def client():
    """Stand-in for BigCommerceClient: only store_hash and _request are used."""
    fake = mock.Mock()
    fake.store_hash = "storehash"
    fake._request.return_value = {"data": []}
    return fake


def writes(client):
    """Return [(method, url, body)] for every POST/PUT made through the client."""
    return [
        (c.kwargs["method"], c.args[0], c.kwargs["json_body"])
        for c in client._request.call_args_list
        if c.kwargs.get("method") in ("POST", "PUT")
    ]


class TestUpsertOrderMetafields:
    def test_reads_existing_metafields_in_namespace(self, client):
        upsert_order_metafields(client, 42, {})
        client._request.assert_called_once_with(BASE_URL, params={"namespace": NAMESPACE, "limit": 250})

    def test_creates_missing_and_updates_existing(self, client):
        client._request.side_effect = [{"data": [{"key": "existing", "id": 7}]}, {}, {}]
        upsert_order_metafields(client, 42, {"existing": "a", "new": "b"})

        def body(key, value):
            return {"namespace": NAMESPACE, "key": key, "value": value, "permission_set": PERMISSION_SET}

        assert writes(client) == [
            ("PUT", f"{BASE_URL}/7", body("existing", "a")),
            ("POST", BASE_URL, body("new", "b")),
        ]

    def test_values_are_stringified_and_none_becomes_empty(self, client):
        upsert_order_metafields(client, 42, {"n": 5, "none": None})
        assert [w[2]["value"] for w in writes(client)] == ["5", ""]

    @pytest.mark.parametrize("payload", [[], None, {}])
    def test_non_dict_or_empty_existing_payload_creates_all(self, client, payload):
        # _request returns [] for 204 No Content.
        client._request.side_effect = [payload, {}]
        upsert_order_metafields(client, 42, {"k": "v"})
        assert [w[0] for w in writes(client)] == ["POST"]

    def test_request_errors_propagate(self, client):
        client._request.side_effect = [{"data": []}, RuntimeError("boom")]
        with pytest.raises(RuntimeError):
            upsert_order_metafields(client, 42, {"k": "v"})


class TestSetWwexMetafields:
    def test_writes_carrier_code_timestamp_and_actor(self, client):
        set_wwex_metafields(client, 42, "UPS", "GND")
        values = {w[2]["key"]: w[2]["value"] for w in writes(client)}

        assert values["wwex_carrier"] == "UPS"
        assert values["wwex_carrier_code"] == "GND"
        assert values["actor"] == ACTOR
        assert datetime.fromisoformat(values["timestamp"]).tzinfo is not None


class TestSetItemCarrierMetafields:
    @pytest.mark.parametrize("item_carriers", [{}, None])
    def test_no_items_makes_no_requests(self, client, item_carriers):
        set_item_carrier_metafields(client, 42, item_carriers)
        client._request.assert_not_called()

    def test_writes_carrier_and_code_per_sku(self, client):
        set_item_carrier_metafields(client, 42, {"SKU1": ("UPS", "GND"), "SKU2": ("LTL", "LTL1")})
        values = {w[2]["key"]: w[2]["value"] for w in writes(client)}

        # The method-code key is spelled "carrierode_<sku>" in the code (docstring says "carrier_code_<sku>").
        assert values == {
            "carrier_SKU1": "UPS",
            "carrierode_SKU1": "GND",
            "carrier_SKU2": "LTL",
            "carrierode_SKU2": "LTL1",
        }


def test_module_constants():
    assert bigcommerce_metafields.NAMESPACE == "shippingdetails"
    assert bigcommerce_metafields.PERMISSION_SET == "read"
