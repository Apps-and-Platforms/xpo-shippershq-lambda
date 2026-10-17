from unittest import mock

import requests

import bigcommerce_metafields
from bigcommerce_metafields import record_pipe17_status, upsert_order_metafield
from conftest import make_response
from orders_sync import BigCommerceClient

METAFIELDS_URL = "https://api.bigcommerce.com/stores/storehash/v3/orders/42/metafields"


class TestUpsertOrderMetafield:
    def test_creates_when_missing(self, mock_session_request):
        mock_session_request.side_effect = [make_response(json_body={"data": []}), make_response(json_body={})]
        upsert_order_metafield(BigCommerceClient(), 42, "pipe17_update_status", "updated")

        get, post = mock_session_request.call_args_list
        assert get.args == ("GET", METAFIELDS_URL)
        assert get.kwargs["params"] == {"namespace": "shippingdetails", "key": "pipe17_update_status"}
        assert post.args == ("POST", METAFIELDS_URL)
        assert post.kwargs["json"] == {
            "namespace": "shippingdetails",
            "key": "pipe17_update_status",
            "value": "updated",
            "permission_set": "read",
        }

    def test_updates_when_present(self, mock_session_request):
        mock_session_request.side_effect = [
            make_response(json_body={"data": [{"id": 7, "key": "pipe17_update_status"}]}),
            make_response(json_body={}),
        ]
        upsert_order_metafield(BigCommerceClient(), 42, "pipe17_update_status", "failed")

        put = mock_session_request.call_args_list[1]
        assert put.args == ("PUT", f"{METAFIELDS_URL}/7")
        assert put.kwargs["json"]["value"] == "failed"


class TestRecordPipe17Status:
    def test_writes_status_metafield(self, monkeypatch):
        upsert = mock.Mock()
        monkeypatch.setattr(bigcommerce_metafields, "upsert_order_metafield", upsert)
        client = mock.Mock()
        record_pipe17_status(client, 42, "updated")
        upsert.assert_called_once_with(client, 42, "pipe17_update_status", "updated")

    def test_request_error_is_logged_not_raised(self, monkeypatch):
        upsert = mock.Mock(side_effect=requests.exceptions.HTTPError("500"))
        monkeypatch.setattr(bigcommerce_metafields, "upsert_order_metafield", upsert)
        record_pipe17_status(mock.Mock(), 42, "failed")
