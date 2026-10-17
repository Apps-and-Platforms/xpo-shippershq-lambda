from unittest import mock

import pytest
import requests

import bigcommerce_metafields
import orders_sync
from conftest import make_response
from orders_sync import (
    BigCommerceClient,
    TransientProcessingError,
    log_order,
    process_order,
)

ORDER_URL = "https://api.bigcommerce.com/stores/storehash/v2/orders/42"


# --- BigCommerceClient -----------------------------------------------------------------------------


class TestBigCommerceClientInit:
    @pytest.mark.parametrize("missing", ["BC_STORE_HASH", "BC_ACCESS_TOKEN"])
    def test_requires_store_hash_and_token(self, monkeypatch, missing):
        monkeypatch.delenv(missing)
        with pytest.raises(ValueError):
            BigCommerceClient()

    def test_headers_without_client_id(self):
        client = BigCommerceClient()
        assert client.session.headers["X-Auth-Token"] == "bc-token"
        assert "X-Auth-Client" not in client.session.headers

    def test_client_id_header(self, monkeypatch):
        monkeypatch.setenv("BC_CLIENT_ID", "cid")
        assert BigCommerceClient().session.headers["X-Auth-Client"] == "cid"

    def test_explicit_arguments_override_env(self):
        client = BigCommerceClient(store_hash="other", access_token="tok")
        assert client.store_hash == "other"
        assert client.session.headers["X-Auth-Token"] == "tok"


class TestRequest:
    def test_returns_json(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"id": 42})
        assert BigCommerceClient()._request(ORDER_URL, params={"a": 1}) == {"id": 42}
        mock_session_request.assert_called_once_with(
            "GET", ORDER_URL, params={"a": 1}, json=None, timeout=orders_sync.REQUEST_TIMEOUT
        )

    def test_204_returns_empty_list(self, mock_session_request):
        mock_session_request.return_value = make_response(204)
        assert BigCommerceClient()._request(ORDER_URL) == []

    def test_429_waits_for_reset_header_then_retries(self, mock_session_request, monkeypatch):
        sleep = mock.Mock()
        monkeypatch.setattr(orders_sync.time, "sleep", sleep)
        mock_session_request.side_effect = [
            make_response(429, headers={"X-Rate-Limit-Time-Reset-Ms": "2500"}),
            make_response(json_body={"ok": True}),
        ]
        assert BigCommerceClient()._request(ORDER_URL) == {"ok": True}
        sleep.assert_called_once_with(2.5)

    def test_429_until_max_retries_raises(self, mock_session_request):
        mock_session_request.return_value = make_response(429)
        with pytest.raises(RuntimeError):
            BigCommerceClient()._request(ORDER_URL)
        assert mock_session_request.call_count == orders_sync.MAX_RETRIES

    def test_network_error_retries_with_backoff(self, mock_session_request, monkeypatch):
        sleep = mock.Mock()
        monkeypatch.setattr(orders_sync.time, "sleep", sleep)
        mock_session_request.side_effect = [
            requests.exceptions.Timeout(),
            requests.exceptions.ConnectionError(),
            make_response(json_body={"ok": True}),
        ]
        assert BigCommerceClient()._request(ORDER_URL) == {"ok": True}
        assert [c.args[0] for c in sleep.call_args_list] == [2, 4]

    def test_network_error_on_every_attempt_raises(self, mock_session_request):
        mock_session_request.side_effect = requests.exceptions.ConnectionError()
        with pytest.raises(requests.exceptions.ConnectionError):
            BigCommerceClient()._request(ORDER_URL)
        assert mock_session_request.call_count == orders_sync.MAX_RETRIES

    @pytest.mark.parametrize("status", [401, 404, 500])
    def test_http_errors_raise_without_retry(self, mock_session_request, status):
        mock_session_request.return_value = make_response(status, text="err")
        with pytest.raises(requests.exceptions.HTTPError):
            BigCommerceClient()._request(ORDER_URL)
        assert mock_session_request.call_count == 1

    def test_non_json_body_raises(self, mock_session_request):
        mock_session_request.return_value = make_response(200, text="not json")
        with pytest.raises(ValueError):
            BigCommerceClient()._request(ORDER_URL)


class TestOrderEndpoints:
    def test_get_order(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"id": 42})
        assert BigCommerceClient().get_order(42) == {"id": 42}
        assert mock_session_request.call_args.args == ("GET", ORDER_URL)

    def test_update_order_status(self, mock_session_request):
        mock_session_request.return_value = make_response(json_body={"id": 42, "status": "Awaiting Fulfillment"})
        assert BigCommerceClient().update_order_status(42, 9)["status"] == "Awaiting Fulfillment"
        assert mock_session_request.call_args.args == ("PUT", ORDER_URL)
        assert mock_session_request.call_args.kwargs["json"] == {"status_id": 9}


# --- log_order -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,level",
    [("Awaiting Fulfillment", "info"), ("Cancelled", "warning"), ("Disputed", "error"), (None, "info")],
)
def test_log_order_level_depends_on_status(monkeypatch, status, level):
    fake_log = mock.Mock()
    monkeypatch.setattr(orders_sync, "log", fake_log)
    order = {"id": 42}
    if status is not None:
        order["status"] = status
    log_order(order)
    getattr(fake_log, level).assert_called_once()


# --- process_order ---------------------------------------------------------------------------------

WWEX_SHIPMENTS = [
    {
        "id": "s1",
        "shipmentDetail": {"name": "World Wide Express 3PL"},
        "carriers": [
            {
                "carrierDetail": {"carrierCode": "shqworldwideexpress"},
                "methods": [{"methodDetails": {"methodCode": "GND"}}],
                "packages": [{"items": [{"sku": "SKU1"}]}],
            }
        ],
    }
]

SKU1_CARRIER = {
    "carrier_code": "shqworldwideexpress",
    "wwex_carrier": "UPS",
    "method_code": "GND",
    "carrier_title": None,
    "method_title": None,
}


@pytest.fixture
def bc_client(monkeypatch):
    client = mock.Mock(spec=BigCommerceClient)
    client.get_order.return_value = {"id": 42, "status": "Awaiting Fulfillment"}
    monkeypatch.setattr(orders_sync, "BigCommerceClient", mock.Mock(return_value=client))
    return client


@pytest.fixture(autouse=True)
def upsert_metafield(monkeypatch):
    upsert = mock.Mock()
    monkeypatch.setattr(bigcommerce_metafields, "upsert_order_metafield", upsert)
    return upsert


@pytest.fixture
def shq_client(monkeypatch):
    client = mock.Mock()
    client.get_advanced_shipment.return_value = WWEX_SHIPMENTS
    monkeypatch.setattr(orders_sync, "ShipperHQClient", mock.Mock(return_value=client))
    return client


@pytest.fixture
def pipe17_client(monkeypatch):
    client = mock.Mock()
    client.get_order_id_by_ext_order_id.return_value = "p1"
    client.get_order_status.return_value = orders_sync.REVIEW_REQUIRED
    client.update_order_custom_fields.return_value = True
    monkeypatch.setattr(orders_sync, "Pipe17Client", mock.Mock(return_value=client))
    return client


class TestProcessOrder:
    def test_happy_path_sets_pipe17_fields_without_status_change(
        self, bc_client, shq_client, pipe17_client, upsert_metafield
    ):
        assert process_order(42) == {"status": "updated", "order_id": 42}
        assert upsert_metafield.call_args_list == [
            mock.call(bc_client, 42, "pipe17_update_status", "processing"),
            mock.call(bc_client, 42, "pipe17_update_status", "updated"),
        ]

        shq_client.get_advanced_shipment.assert_called_once_with(42)
        pipe17_client.update_order_custom_fields.assert_called_once_with(
            "p1", {"SKU1": SKU1_CARRIER}, {"order_wwex_carrier": "UPS", "order_wwex_code": "GND"}
        )
        bc_client.update_order_status.assert_not_called()

    def test_bigcommerce_client_init_failure_raises(self, monkeypatch):
        monkeypatch.setattr(orders_sync, "BigCommerceClient", mock.Mock(side_effect=ValueError("no creds")))
        with pytest.raises(ValueError):
            process_order(42)

    @pytest.mark.parametrize("status", ["Pending", "Shipped", "Cancelled", None])
    def test_skips_orders_not_awaiting_fulfillment(self, bc_client, shq_client, status):
        bc_client.get_order.return_value = {"id": 42, "status": status}
        assert process_order(42) == {"status": "skipped", "order_id": 42, "order_status": status}
        shq_client.get_advanced_shipment.assert_not_called()
        bc_client.update_order_status.assert_not_called()

    def test_shipperhq_client_init_failure_is_transient(self, bc_client, monkeypatch):
        monkeypatch.setattr(orders_sync, "ShipperHQClient", mock.Mock(side_effect=ValueError("no token")))
        with pytest.raises(TransientProcessingError):
            process_order(42)

    def test_shipperhq_http_error_returns_failed(self, bc_client, shq_client, pipe17_client):
        shq_client.get_advanced_shipment.side_effect = requests.exceptions.HTTPError("500")
        assert process_order(42) == {"status": "failed", "order_id": 42}
        bc_client.update_order_status.assert_not_called()

    def test_shipment_name_does_not_matter(self, bc_client, shq_client, pipe17_client):
        other = [dict(WWEX_SHIPMENTS[0], shipmentDetail={"name": "Warehouse"})]
        shq_client.get_advanced_shipment.return_value = other

        assert process_order(42) == {"status": "updated", "order_id": 42}
        pipe17_client.update_order_custom_fields.assert_called_once_with(
            "p1", {"SKU1": SKU1_CARRIER}, {"order_wwex_carrier": "UPS", "order_wwex_code": "GND"}
        )

    def test_no_shipments_still_updates_status(self, bc_client, shq_client, pipe17_client):
        shq_client.get_advanced_shipment.return_value = None
        assert process_order(42)["status"] == "updated"
        pipe17_client.update_order_custom_fields.assert_called_once_with("p1", {}, {})

    def test_unmapped_carrier_returns_unmapped_without_status_change(self, bc_client, shq_client, pipe17_client):
        shipments = [dict(WWEX_SHIPMENTS[0])]
        shipments[0]["carriers"] = [dict(WWEX_SHIPMENTS[0]["carriers"][0], carrierDetail={"carrierCode": "fedex"})]
        shq_client.get_advanced_shipment.return_value = shipments

        result = process_order(42)
        assert result["status"] == "unmapped"
        assert "fedex" in result["error"]
        bc_client.update_order_status.assert_not_called()

    def test_carrier_without_method_code_returns_unmapped(self, bc_client, shq_client, pipe17_client):
        carrier = dict(WWEX_SHIPMENTS[0]["carriers"][0], methods=[])
        shq_client.get_advanced_shipment.return_value = [dict(WWEX_SHIPMENTS[0], carriers=[carrier])]
        assert process_order(42)["status"] == "unmapped"
        pipe17_client.update_order_custom_fields.assert_not_called()

    def test_pipe17_lookup_http_error_raises(self, bc_client, shq_client, pipe17_client):
        pipe17_client.get_order_id_by_ext_order_id.side_effect = requests.exceptions.HTTPError("500")
        with pytest.raises(requests.exceptions.HTTPError):
            process_order(42)

    def test_pipe17_update_failure_returns_failed_without_status_change(
        self, bc_client, shq_client, pipe17_client, upsert_metafield
    ):
        pipe17_client.update_order_custom_fields.return_value = False
        assert process_order(42) == {"status": "failed", "order_id": 42}
        assert upsert_metafield.call_args_list[-1] == mock.call(bc_client, 42, "pipe17_update_status", "failed")
        bc_client.update_order_status.assert_not_called()

    def test_metafield_error_is_logged_not_raised(self, bc_client, shq_client, pipe17_client, upsert_metafield):
        upsert_metafield.side_effect = requests.exceptions.HTTPError("500")
        assert process_order(42)["status"] == "updated"

    def test_only_processing_written_when_pipe17_not_review_required(
        self, bc_client, shq_client, pipe17_client, upsert_metafield
    ):
        pipe17_client.get_order_status.return_value = "readyForFulfillment"
        assert process_order(42)["status"] == "not_review_required"
        upsert_metafield.assert_called_once_with(bc_client, 42, "pipe17_update_status", "processing")

    def test_unexpected_error_raises(self, bc_client, shq_client, pipe17_client):
        pipe17_client.update_order_custom_fields.side_effect = KeyError("boom")
        with pytest.raises(KeyError):
            process_order(42)
