import json
import os
from unittest import mock

import pytest

import app
import orders_sync

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:order-sync"
VALID_SECRET = {
    "BC_STORE_HASH": "hash",
    "BC_ACCESS_TOKEN": "token",
    "PIPE17_API_KEY": "key",
    "SHQ_ACCESS_TOKEN": "shq",
}


def eventbridge_event(order_id=42):
    return {"detail-type": "order.updated", "detail": {"data": {"id": order_id}}}


def sqs_record(body, message_id="m1", receive_count="1"):
    return {
        "messageId": message_id,
        "body": body if isinstance(body, str) else json.dumps(body),
        "attributes": {"ApproximateReceiveCount": receive_count},
    }


@pytest.fixture(autouse=True)
def _reset_cached_credentials(monkeypatch):
    monkeypatch.setattr(app, "_credentials", None)
    # _load_credentials writes into os.environ directly; restore it afterwards.
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def secrets_client(monkeypatch):
    monkeypatch.setenv("ORDER_SYNC_SECRET_ARN", SECRET_ARN)
    client = mock.Mock()
    client.get_secret_value.return_value = {"SecretString": json.dumps(VALID_SECRET)}
    boto_client = mock.Mock(return_value=client)
    monkeypatch.setattr(app.boto3, "client", boto_client)
    return client


@pytest.fixture
def process_order(monkeypatch):
    fake = mock.Mock(return_value={"status": "updated", "order_id": 42})
    monkeypatch.setattr(orders_sync, "process_order", fake)
    return fake


# --- _load_credentials -----------------------------------------------------------------------------


class TestLoadCredentials:
    def test_requires_secret_arn(self):
        with pytest.raises(RuntimeError):
            app._load_credentials()

    def test_loads_allowed_keys_into_environment(self, secrets_client):
        secrets_client.get_secret_value.return_value = {
            "SecretString": json.dumps({**VALID_SECRET, "SHQ_API_BASE": "https://shq.test", "UNEXPECTED": "x", "BC_CLIENT_ID": 5})
        }
        app._load_credentials()

        secrets_client.get_secret_value.assert_called_once_with(SecretId=SECRET_ARN)
        assert os.environ["BC_STORE_HASH"] == "hash"
        assert os.environ["SHQ_API_BASE"] == "https://shq.test"
        assert os.environ["BC_CLIENT_ID"] == "5"
        assert "UNEXPECTED" not in os.environ

    def test_caches_secret_across_calls(self, secrets_client):
        app._load_credentials()
        os.environ["BC_STORE_HASH"] = "changed"
        app._load_credentials()

        secrets_client.get_secret_value.assert_called_once()
        assert os.environ["BC_STORE_HASH"] == "hash"

    @pytest.mark.parametrize("secret", [{}, {"SecretString": ""}, {"SecretBinary": b"x"}])
    def test_requires_secret_string(self, secrets_client, secret):
        secrets_client.get_secret_value.return_value = secret
        with pytest.raises(ValueError, match="SecretString"):
            app._load_credentials()

    def test_requires_json_object(self, secrets_client):
        secrets_client.get_secret_value.return_value = {"SecretString": "[1, 2]"}
        with pytest.raises(ValueError, match="JSON object"):
            app._load_credentials()

    def test_invalid_json_raises(self, secrets_client):
        secrets_client.get_secret_value.return_value = {"SecretString": "{not json"}
        with pytest.raises(json.JSONDecodeError):
            app._load_credentials()

    @pytest.mark.parametrize("missing", list(VALID_SECRET))
    def test_requires_each_credential(self, secrets_client, missing):
        secrets_client.get_secret_value.return_value = {"SecretString": json.dumps({**VALID_SECRET, missing: ""})}
        with pytest.raises(ValueError, match=missing):
            app._load_credentials()


# --- _process_event --------------------------------------------------------------------------------


class TestProcessEvent:
    @pytest.mark.parametrize("raw_id,expected", [(42, 42), ("42", 42)])
    def test_processes_valid_order_id(self, secrets_client, process_order, raw_id, expected):
        result = app._process_event(eventbridge_event(raw_id))
        process_order.assert_called_once_with(order_id=expected)
        assert result == {"order_id": expected, "result": process_order.return_value}

    @pytest.mark.parametrize(
        "event",
        [{}, {"detail": None}, {"detail": {"data": {}}}, {"detail": "x"}],
    )
    def test_rejects_missing_order_id(self, process_order, event):
        with pytest.raises(ValueError, match="detail.data.id"):
            app._process_event(event)
        process_order.assert_not_called()

    @pytest.mark.parametrize("raw_id", [0, -1, "0", "-1", "abc", "4.2", 4.2, True, None, ""])
    def test_rejects_invalid_order_id(self, process_order, raw_id):
        with pytest.raises(ValueError, match="positive integer"):
            app._process_event(eventbridge_event(raw_id))
        process_order.assert_not_called()

    def test_validates_before_loading_credentials(self, process_order, monkeypatch):
        load = mock.Mock()
        monkeypatch.setattr(app, "_load_credentials", load)
        with pytest.raises(ValueError):
            app._process_event(eventbridge_event("bad"))
        load.assert_not_called()

    def test_credential_errors_propagate(self, process_order):
        # ORDER_SYNC_SECRET_ARN is not set.
        with pytest.raises(RuntimeError):
            app._process_event(eventbridge_event())
        process_order.assert_not_called()


# --- lambda_handler --------------------------------------------------------------------------------


class TestLambdaHandler:
    def test_bare_eventbridge_event_is_processed_directly(self, secrets_client, process_order):
        assert app.lambda_handler(eventbridge_event(), None)["order_id"] == 42

    def test_bare_event_errors_propagate(self, process_order):
        with pytest.raises(ValueError):
            app.lambda_handler({"detail": {}}, None)

    def test_sqs_batch_all_succeed(self, secrets_client, process_order):
        event = {"Records": [sqs_record(eventbridge_event(1), "m1"), sqs_record(eventbridge_event(2), "m2", "3")]}
        assert app.lambda_handler(event, None) == {"batchItemFailures": []}
        assert [c.kwargs["order_id"] for c in process_order.call_args_list] == [1, 2]

    def test_sqs_reports_only_failed_records(self, secrets_client, process_order):
        process_order.side_effect = [{"status": "updated"}, orders_sync.TransientProcessingError(), {"status": "updated"}]
        event = {
            "Records": [
                sqs_record(eventbridge_event(1), "m1"),
                sqs_record(eventbridge_event(2), "m2"),
                sqs_record(eventbridge_event(3), "m3"),
            ]
        }
        assert app.lambda_handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "m2"}]}

    @pytest.mark.parametrize("body", ["{not json", {"detail": {}}, eventbridge_event("abc")])
    def test_sqs_bad_messages_are_reported_as_failures(self, secrets_client, process_order, body):
        event = {"Records": [sqs_record(body, "bad")]}
        assert app.lambda_handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
        process_order.assert_not_called()

    def test_empty_sqs_batch(self, process_order):
        assert app.lambda_handler({"Records": []}, None) == {"batchItemFailures": []}
