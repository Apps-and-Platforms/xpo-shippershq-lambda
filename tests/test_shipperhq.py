import pytest
import requests

import shipperhq
from conftest import make_response
from shipperhq import (
    CarrierMappingError,
    MissingCarrierMethodsError,
    MissingCarriersError,
    MissingMethodCodeError,
    ShipperHQClient,
    UnknownCarrierError,
    find_shipment_by_detail_name,
    get_carrier_code,
    get_carrier_codes,
    get_item_carriers,
    get_order_wwex_fields,
    map_to_wwex,
)


def carrier(code, method_code="GND", skus=(), title="Carrier"):
    return {
        "carrierDetail": {"carrierCode": code, "carrierTitle": title},
        "methods": [{"methodDetails": {"methodCode": method_code, "methodTitle": "Method"}}] if method_code else [],
        "packages": [{"items": [{"sku": sku} for sku in skus]}],
    }


def shipment(name, *carriers, shipment_id="s1"):
    return {"id": shipment_id, "shipmentDetail": {"name": name}, "carriers": list(carriers)}


def item_info(carrier_code, wwex_carrier, method_code, carrier_title="Carrier", method_title="Method"):
    return {
        "carrier_code": carrier_code,
        "wwex_carrier": wwex_carrier,
        "method_code": method_code,
        "carrier_title": carrier_title,
        "method_title": method_title,
    }


# --- ShipperHQClient -------------------------------------------------------------------------------


class TestShipperHQClientInit:
    def test_requires_access_token(self, monkeypatch):
        monkeypatch.delenv("SHQ_ACCESS_TOKEN")
        with pytest.raises(ValueError):
            ShipperHQClient()

    def test_uses_env_and_default_base(self):
        client = ShipperHQClient()
        assert client.access_token == "shq-token"
        assert client.api_base == shipperhq.SHQ_API_BASE_DEFAULT
        assert client.session.headers["X-ShipperHQ-Access-Token"] == "shq-token"
        assert client.session.headers["X-ShipperHQ-Scope"] == "LIVE"

    def test_explicit_arguments_override_env(self, monkeypatch):
        monkeypatch.setenv("SHQ_API_BASE", "https://env.test")
        client = ShipperHQClient(access_token="explicit", api_base="https://arg.test")
        assert client.access_token == "explicit"
        assert client.api_base == "https://arg.test"


class TestGraphql:
    def test_returns_data(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"data": {"x": 1}})
        assert ShipperHQClient()._graphql("query", {"a": 1}) == {"x": 1}
        method, url = mock_session_request.call_args.args
        assert method == "POST"
        assert url == shipperhq.SHQ_API_BASE_DEFAULT
        assert mock_session_request.call_args.kwargs["json"] == {"query": "query", "variables": {"a": 1}}

    def test_graphql_errors_still_return_data(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"data": None, "errors": [{"message": "bad"}]})
        assert ShipperHQClient()._graphql("query") is None

    def test_retries_on_429_then_succeeds(self, mock_session_request):
        mock_session_request.side_effect = [make_response(429), make_response(json_body={"data": {"ok": True}})]
        assert ShipperHQClient()._graphql("query") == {"ok": True}
        assert mock_session_request.call_count == 2

    def test_gives_up_after_max_retries_on_429(self, mock_session_request):
        mock_session_request.return_value = make_response(429)
        with pytest.raises(RuntimeError):
            ShipperHQClient()._graphql("query")
        assert mock_session_request.call_count == shipperhq.MAX_RETRIES

    def test_retries_network_errors_then_raises(self, mock_session_request):
        mock_session_request.side_effect = requests.exceptions.ConnectionError("down")
        with pytest.raises(requests.exceptions.ConnectionError):
            ShipperHQClient()._graphql("query")
        assert mock_session_request.call_count == shipperhq.MAX_RETRIES

    @pytest.mark.parametrize("status", [401, 500])
    def test_http_errors_raise(self, mock_session_request, status):
        mock_session_request.return_value = make_response(status, text="nope")
        with pytest.raises(requests.exceptions.HTTPError):
            ShipperHQClient()._graphql("query")
        assert mock_session_request.call_count == 1

    def test_non_json_body_raises(self, mock_session_request):
        mock_session_request.return_value = make_response(200, text="<html>")
        with pytest.raises(ValueError):
            ShipperHQClient()._graphql("query")


class TestGetAdvancedShipment:
    def test_returns_first_orders_shipments(self, mock_session_request):
        shipments = [shipment("World Wide Express 3PL")]
        mock_session_request.return_value = make_response(
            json_body={"data": {"viewOrder": [{"shipments": shipments}, {"shipments": []}]}}
        )
        assert ShipperHQClient().get_advanced_shipment(123) == shipments
        assert mock_session_request.call_args.kwargs["json"]["variables"] == {"orderNumber": "123"}

    def test_empty_view_order_fails_assertion(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"data": {"viewOrder": []}})
        with pytest.raises(AssertionError):
            ShipperHQClient().get_advanced_shipment(123)

    def test_missing_view_order_raises(self, mock_session_request):
        # viewOrder=None currently surfaces as a TypeError from len(None).
        mock_session_request.return_value = make_response(json_body={"data": None})
        with pytest.raises(TypeError):
            ShipperHQClient().get_advanced_shipment(123)


# --- shipment helpers ------------------------------------------------------------------------------


class TestFindShipmentByDetailName:
    def test_returns_first_match(self):
        first = shipment("World Wide Express 3PL", shipment_id="a")
        second = shipment("World Wide Express 3PL", shipment_id="b")
        assert find_shipment_by_detail_name([shipment("Other"), first, second]) is first

    def test_custom_name(self):
        target = shipment("Warehouse B")
        assert find_shipment_by_detail_name([target], name="Warehouse B") is target

    @pytest.mark.parametrize("shipments", [None, [], [shipment("Other")], [{"id": "x", "shipmentDetail": None}]])
    def test_returns_none_when_absent(self, shipments):
        assert find_shipment_by_detail_name(shipments) is None


class TestGetCarrierCodes:
    def test_collects_codes_skipping_blanks(self):
        s = shipment("x", carrier("a"), {"carrierDetail": {}}, {"carrierDetail": None}, carrier("b"))
        assert get_carrier_codes(s) == ["a", "b"]

    @pytest.mark.parametrize("s", [None, {}, {"carriers": None}])
    def test_empty(self, s):
        assert get_carrier_codes(s) == []


class TestGetCarrierCode:
    def test_returns_first_carrier_and_method(self):
        s = shipment("x", carrier("shqworldwideexpress", "GND"), carrier("other", "AIR"))
        assert get_carrier_code(s) == ("shqworldwideexpress", "GND")

    def test_method_details_as_list(self):
        c = carrier("shqworldwideexpress")
        c["methods"] = [{"methodDetails": [{"methodCode": "2DA"}, {"methodCode": "NDA"}]}]
        assert get_carrier_code(shipment("x", c)) == ("shqworldwideexpress", "2DA")

    def test_method_without_code_returns_none(self):
        c = carrier("shqworldwideexpress")
        c["methods"] = [{"methodDetails": None}]
        assert get_carrier_code(shipment("x", c)) == ("shqworldwideexpress", None)

    @pytest.mark.parametrize("s", [None, {}, shipment("x")])
    def test_no_carriers(self, s):
        with pytest.raises(MissingCarriersError):
            get_carrier_code(s)

    def test_no_methods(self):
        with pytest.raises(MissingCarrierMethodsError):
            get_carrier_code(shipment("x", carrier("shqworldwideexpress", method_code=None)))


class TestMapToWwex:
    @pytest.mark.parametrize(
        "carrier_code,expected",
        [("shqworldwideexpress", "UPS"), ("shqworldwideexpressfreight", "LTL")],
    )
    def test_known_carriers(self, carrier_code, expected):
        assert map_to_wwex(carrier_code, "GND") == (expected, "GND")

    @pytest.mark.parametrize("carrier_code", ["fedex", "SHQWORLDWIDEEXPRESS", None])
    def test_unknown_carrier(self, carrier_code):
        with pytest.raises(UnknownCarrierError):
            map_to_wwex(carrier_code, "GND")

    @pytest.mark.parametrize("method_code", [None, ""])
    def test_missing_method_code(self, method_code):
        with pytest.raises(MissingMethodCodeError):
            map_to_wwex("shqworldwideexpress", method_code)

    def test_errors_share_base_class(self):
        for error in (UnknownCarrierError, MissingMethodCodeError, MissingCarriersError, MissingCarrierMethodsError):
            assert issubclass(error, CarrierMappingError)


class TestGetItemCarriers:
    def test_maps_every_sku(self):
        shipments = [
            shipment("a", carrier("shqworldwideexpress", "GND", skus=["SKU1", "SKU2"])),
            shipment("b", carrier("shqworldwideexpressfreight", "LTL1", skus=["SKU3"])),
        ]
        assert get_item_carriers(shipments) == {
            "SKU1": item_info("shqworldwideexpress", "UPS", "GND"),
            "SKU2": item_info("shqworldwideexpress", "UPS", "GND"),
            "SKU3": item_info("shqworldwideexpressfreight", "LTL", "LTL1"),
        }

    def test_later_carrier_wins_for_duplicate_sku(self):
        shipments = [
            shipment("a", carrier("shqworldwideexpress", "GND", skus=["SKU1"])),
            shipment("b", carrier("shqworldwideexpressfreight", "LTL1", skus=["SKU1"])),
        ]
        assert get_item_carriers(shipments) == {"SKU1": item_info("shqworldwideexpressfreight", "LTL", "LTL1")}

    def test_skips_items_without_sku(self):
        c = carrier("shqworldwideexpress", "GND", skus=["SKU1"])
        c["packages"][0]["items"].append({"qtyPacked": 1})
        assert get_item_carriers([shipment("a", c)]) == {"SKU1": item_info("shqworldwideexpress", "UPS", "GND")}

    @pytest.mark.parametrize("shipments", [None, [], [shipment("a")]])
    def test_empty(self, shipments):
        assert get_item_carriers(shipments) == {}

    def test_unmapped_carrier_raises(self):
        with pytest.raises(UnknownCarrierError):
            get_item_carriers([shipment("a", carrier("fedex", "GROUND", skus=["SKU1"]))])

    def test_missing_method_code_raises(self):
        with pytest.raises(MissingMethodCodeError):
            get_item_carriers([shipment("a", carrier("shqworldwideexpress", method_code=None, skus=["SKU1"]))])


class TestGetOrderWwexFields:
    def test_uses_first_wwex_item(self):
        item_carriers = {
            "SKU1": {"carrier_code": "other", "method_code": "X"},
            "SKU2": {"carrier_code": "shqworldwideexpressfreight", "method_code": "LTL1"},
            "SKU3": {"carrier_code": "shqworldwideexpress", "method_code": "GND"},
        }
        assert get_order_wwex_fields(item_carriers) == {
            "order_wwex_carrier": "LTL",
            "order_wwex_code": "LTL1",
        }

    @pytest.mark.parametrize("item_carriers", [None, {}, {"SKU1": {"carrier_code": "other", "method_code": "X"}}])
    def test_no_wwex_item(self, item_carriers):
        assert get_order_wwex_fields(item_carriers) == {}
