from unittest import mock

import pytest
import requests

import orders_sync
from conftest import make_response
from orders_sync import (
    AWAITING_SHIPMENT_STATUS_ID,
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
        mock_session_request.return_value = make_response(json_body={"id": 42, "status": "Awaiting Shipment"})
        assert BigCommerceClient().update_order_status(42, 9)["status"] == "Awaiting Shipment"
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


@pytest.fixture
def bc_client(monkeypatch):
    client = mock.Mock(spec=BigCommerceClient)
    client.get_order.return_value = {"id": 42, "status": "Awaiting Fulfillment"}
    monkeypatch.setattr(orders_sync, "BigCommerceClient", mock.Mock(return_value=client))
    return client


@pytest.fixture
def shq_client(monkeypatch):
    client = mock.Mock()
    client.get_advanced_shipment.return_value = WWEX_SHIPMENTS
    monkeypatch.setattr(orders_sync, "ShipperHQClient", mock.Mock(return_value=client))
    return client


@pytest.fixture
def metafields(monkeypatch):
    fakes = mock.Mock()
    monkeypatch.setattr(orders_sync, "set_item_carrier_metafields", fakes.set_item_carrier_metafields)
    monkeypatch.setattr(orders_sync, "set_wwex_metafields", fakes.set_wwex_metafields)
    return fakes


class TestProcessOrder:
    def test_happy_path_sets_metafields_and_status(self, bc_client, shq_client, metafields):
        assert process_order(42) == {"status": "updated", "order_id": 42}

        shq_client.get_advanced_shipment.assert_called_once_with(42)
        metafields.set_item_carrier_metafields.assert_called_once_with(bc_client, 42, {"SKU1": ("UPS", "GND")})
        metafields.set_wwex_metafields.assert_called_once_with(bc_client, 42, "UPS", "GND")
        bc_client.update_order_status.assert_called_once_with(42, AWAITING_SHIPMENT_STATUS_ID)

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

    def test_shipperhq_http_error_returns_failed(self, bc_client, shq_client, metafields):
        shq_client.get_advanced_shipment.side_effect = requests.exceptions.HTTPError("500")
        assert process_order(42) == {"status": "failed", "order_id": 42}
        bc_client.update_order_status.assert_not_called()

    def test_no_wwex_shipment_still_sets_item_metafields_and_status(self, bc_client, shq_client, metafields):
        other = [dict(WWEX_SHIPMENTS[0], shipmentDetail={"name": "Warehouse"})]
        shq_client.get_advanced_shipment.return_value = other

        assert process_order(42) == {"status": "updated", "order_id": 42}
        metafields.set_item_carrier_metafields.assert_called_once()
        metafields.set_wwex_metafields.assert_not_called()
        bc_client.update_order_status.assert_called_once_with(42, AWAITING_SHIPMENT_STATUS_ID)

    def test_no_shipments_still_updates_status(self, bc_client, shq_client, metafields):
        shq_client.get_advanced_shipment.return_value = None
        assert process_order(42)["status"] == "updated"
        metafields.set_item_carrier_metafields.assert_called_once_with(bc_client, 42, {})

    def test_unmapped_carrier_returns_unmapped_without_status_change(self, bc_client, shq_client, metafields):
        shipments = [dict(WWEX_SHIPMENTS[0])]
        shipments[0]["carriers"] = [dict(WWEX_SHIPMENTS[0]["carriers"][0], carrierDetail={"carrierCode": "fedex"})]
        shq_client.get_advanced_shipment.return_value = shipments

        result = process_order(42)
        assert result["status"] == "unmapped"
        assert "fedex" in result["error"]
        bc_client.update_order_status.assert_not_called()

    def test_wwex_shipment_without_carriers_returns_unmapped(self, bc_client, shq_client, metafields):
        shq_client.get_advanced_shipment.return_value = [dict(WWEX_SHIPMENTS[0], carriers=[])]
        assert process_order(42)["status"] == "unmapped"
        metafields.set_wwex_metafields.assert_not_called()

    def test_bigcommerce_http_error_during_sync_raises(self, bc_client, shq_client, metafields):
        metafields.set_item_carrier_metafields.side_effect = requests.exceptions.HTTPError("422")
        with pytest.raises(requests.exceptions.HTTPError):
            process_order(42)

    def test_status_update_failure_raises(self, bc_client, shq_client, metafields):
        bc_client.update_order_status.side_effect = requests.exceptions.HTTPError("500")
        with pytest.raises(requests.exceptions.HTTPError):
            process_order(42)

    def test_unexpected_error_raises(self, bc_client, shq_client, metafields):
        metafields.set_wwex_metafields.side_effect = KeyError("boom")
        with pytest.raises(KeyError):
            process_order(42)
