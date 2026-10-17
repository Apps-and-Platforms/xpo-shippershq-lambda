"""Tests for the Pipe17 client used by process_order."""

import pytest
import requests

import pipe17
from conftest import make_response
from pipe17 import PENDING_STATUSES, REVIEW_REQUIRED, Pipe17Client

API = pipe17.PIPE17_API_BASE_DEFAULT


class TestInit:
    def test_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("PIPE17_API_KEY")
        with pytest.raises(ValueError):
            Pipe17Client()

    def test_strips_trailing_slash_from_base(self):
        assert Pipe17Client(api_base="https://p17.test/api/").api_base == "https://p17.test/api"

    def test_headers(self):
        assert Pipe17Client().session.headers["X-Pipe17-Key"] == "pipe17-key"


class TestRequest:
    def test_builds_url_from_path(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"success": True})
        assert Pipe17Client()._request("/orders") == {"success": True}
        assert mock_session_request.call_args.args == ("GET", f"{API}/orders")

    def test_empty_body_returns_empty_dict(self, mock_session_request):
        mock_session_request.return_value = make_response(204)
        assert Pipe17Client()._request("orders") == {}

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_http_errors_raise(self, mock_session_request, status):
        mock_session_request.return_value = make_response(status, text="err")
        with pytest.raises(requests.exceptions.HTTPError):
            Pipe17Client()._request("orders")

    def test_429_retries_then_gives_up(self, mock_session_request):
        mock_session_request.return_value = make_response(429)
        with pytest.raises(RuntimeError):
            Pipe17Client()._request("orders")
        assert mock_session_request.call_count == pipe17.MAX_RETRIES

    def test_network_error_retries_then_raises(self, mock_session_request):
        mock_session_request.side_effect = requests.exceptions.ConnectionError()
        with pytest.raises(requests.exceptions.ConnectionError):
            Pipe17Client()._request("orders")
        assert mock_session_request.call_count == pipe17.MAX_RETRIES


class TestOrderLookup:
    def test_paginates_until_last_page(self, mock_session_request):
        mock_session_request.side_effect = [
            make_response(json_body={"orders": [{"orderId": "a"}], "pagination": {"last": False, "pages": 2}}),
            make_response(json_body={"orders": [{"orderId": "b"}], "pagination": {"last": True, "pages": 2}}),
        ]
        assert Pipe17Client().get_orders_by_ext_order_id(42) == [{"orderId": "a"}, {"orderId": "b"}]
        assert mock_session_request.call_args.kwargs["params"] == {"extOrderId": "42"}

    def test_single_page_without_pagination(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"orders": []})
        assert Pipe17Client().get_orders_by_ext_order_id(42) == []
        assert mock_session_request.call_count == 1

    @pytest.mark.parametrize(
        "orders,expected",
        [([], None), ([{"orderId": "a"}, {"orderId": "b"}], "a"), ([{"status": "new"}], None)],
    )
    def test_get_order_id_by_ext_order_id(self, mock_session_request, orders, expected):
        mock_session_request.return_value = make_response(json_body={"orders": orders})
        assert Pipe17Client().get_order_id_by_ext_order_id(42) == expected


class TestOrderStatus:
    def test_get_order_status(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"order": {"status": REVIEW_REQUIRED}})
        assert Pipe17Client().get_order_status("p1") == REVIEW_REQUIRED
        assert mock_session_request.call_args.args == ("GET", f"{API}/orders/p1")

    def test_get_order_returns_none_on_http_error(self, mock_session_request):
        mock_session_request.return_value = make_response(404, text="missing")
        assert Pipe17Client().get_order("p1") is None
        assert Pipe17Client().get_order_status("p1") is None

    def test_get_order_returns_none_without_order_key(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"code": 404, "message": "nope"})
        assert Pipe17Client().get_order("p1") is None

    @pytest.mark.parametrize("status,expected", [(REVIEW_REQUIRED, True), ("new", False)])
    def test_is_order_status(self, mock_session_request, status, expected):
        mock_session_request.return_value = make_response(json_body={"order": {"status": status}})
        assert Pipe17Client().is_order_status("p1") is expected

    def test_pending_statuses_exclude_review_required(self):
        assert REVIEW_REQUIRED not in PENDING_STATUSES


ITEM_CARRIERS = {
    "SKU1": {
        "carrier_code": "shqworldwideexpress",
        "wwex_carrier": "UPS",
        "method_code": "GND",
        "carrier_title": "Title",
        "method_title": "Method",
    },
    "SKU2": {
        "carrier_code": "shqworldwideexpressfreight",
        "wwex_carrier": "LTL",
        "method_code": "LTL1",
        "carrier_title": "Title",
        "method_title": "Method",
    },
}


def put_body(mock_session_request):
    put = [c for c in mock_session_request.call_args_list if c.args[0] == "PUT"]
    assert len(put) == 1
    assert put[0].args == ("PUT", f"{API}/orders/p1")
    return put[0].kwargs["json"]


class TestUpdateCustomFields:
    def test_sets_carrier_fields_per_sku_in_one_put(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"success": True})
        assert Pipe17Client().update_order_custom_fields("p1", ITEM_CARRIERS) is True

        assert put_body(mock_session_request) == {
            "customFields": [
                {"name": "carrier_SKU1", "value": "UPS"},
                {"name": "carrier_codeSKU1", "value": "GND"},
                {"name": "carrier_SKU2", "value": "LTL"},
                {"name": "carrier_codeSKU2", "value": "LTL1"},
                {"name": "actor", "value": "aws_lambda"},
            ],
            "status": "readyForFulfillment",
        }
        # Only the PUT is sent; the order is not fetched first.
        assert mock_session_request.call_count == 1

    def test_missing_values_sent_as_empty_strings(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"success": True})
        assert Pipe17Client().update_order_custom_fields(
            "p1", {"SKU1": {"wwex_carrier": "UPS", "method_code": None}}
        ) is True

        assert put_body(mock_session_request)["customFields"] == [
            {"name": "carrier_SKU1", "value": "UPS"},
            {"name": "carrier_codeSKU1", "value": ""},
            {"name": "actor", "value": "aws_lambda"},
        ]

    def test_sets_order_fields_alongside_sku_fields(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"success": True})
        order_fields = {"order_wwex_carrier": "UPS", "order_wwex_code": "GND"}
        assert Pipe17Client().update_order_custom_fields("p1", ITEM_CARRIERS, order_fields) is True

        body = put_body(mock_session_request)
        assert body["customFields"][-2:] == [
            {"name": "order_wwex_carrier", "value": "UPS"},
            {"name": "order_wwex_code", "value": "GND"},
        ]
        assert len(body["customFields"]) == 7
        assert body["status"] == "readyForFulfillment"

    @pytest.mark.parametrize("item_carriers", [None, {}])
    def test_no_item_carriers_fails_without_request(self, mock_session_request, item_carriers):
        assert Pipe17Client().update_order_custom_fields(
            "p1", item_carriers, {"order_wwex_carrier": "UPS"}
        ) is False
        mock_session_request.assert_not_called()

    @pytest.mark.parametrize(
        "response,expected",
        [
            (make_response(204), True),
            (make_response(json_body={"success": False, "code": 400}), False),
            (make_response(500, text="err"), False),
        ],
    )
    def test_result(self, mock_session_request, response, expected):
        mock_session_request.return_value = response
        assert Pipe17Client().update_order_custom_fields("p1", ITEM_CARRIERS) is expected
