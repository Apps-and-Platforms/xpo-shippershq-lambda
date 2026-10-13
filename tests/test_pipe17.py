"""Pipe17 is no longer called by process_order, but the client is kept for possible future use."""

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


class TestUpdateCustomFields:
    def test_sends_custom_fields_and_status(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"success": True})
        assert Pipe17Client().update_order_custom_fields("p1", "UPS", "GND") is True

        assert mock_session_request.call_args.args == ("PUT", f"{API}/orders/p1")
        body = mock_session_request.call_args.kwargs["json"]
        fields = {f["name"]: f["value"] for f in body["customFields"]}
        assert fields["Carrier"] == "UPS"
        assert fields["Code"] == "GND"
        assert fields["Modified_By"] == "AWS_Lambda"
        assert fields["Timestamp"].endswith("UTC")
        assert body["status"] == "readyForFulfillment"

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
        assert Pipe17Client().update_order_custom_fields("p1", "UPS", "GND") is expected
