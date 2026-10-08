"""Process BigCommerce order-update events from EventBridge."""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REQUIRED_CREDENTIALS = (
    "BC_STORE_HASH",
    "BC_ACCESS_TOKEN",
    "PIPE17_API_KEY",
    "SHQ_ACCESS_TOKEN",
)
_ALLOWED_SECRET_KEYS = set(_REQUIRED_CREDENTIALS) | {
    "BC_CLIENT_ID",
    "LOG_LEVEL",
    "PIPE17_API_BASE",
    "SHQ_API_BASE",
}
_credentials = None


def _load_credentials():
    global _credentials
    if _credentials is None:
        secret_arn = os.getenv("ORDER_SYNC_SECRET_ARN")
        if not secret_arn:
            raise RuntimeError("ORDER_SYNC_SECRET_ARN must be configured for the Lambda.")

        secret = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
        if not secret.get("SecretString"):
            raise ValueError("The order-sync Secrets Manager secret must contain a JSON SecretString.")
        values = json.loads(secret["SecretString"])
        if not isinstance(values, dict):
            raise ValueError("The order-sync secret must be a JSON object.")

        missing = [key for key in _REQUIRED_CREDENTIALS if not values.get(key)]
        if missing:
            raise ValueError(f"The order-sync secret is missing required keys: {', '.join(missing)}")
        _credentials = {key: str(value) for key, value in values.items() if key in _ALLOWED_SECRET_KEYS}

    os.environ.update(_credentials)


def lambda_handler(event, context):
    logger.info("Received EventBridge event: %s", json.dumps(event, separators=(",", ":")))
    try:
        raw_order_id = event["detail"]["data"]["id"]
    except (KeyError, TypeError) as error:
        raise ValueError("Expected a BigCommerce order ID at detail.data.id.") from error
    if isinstance(raw_order_id, bool):
        raise ValueError("BigCommerce order ID must be a positive integer.")
    if isinstance(raw_order_id, int):
        order_id = raw_order_id
    elif isinstance(raw_order_id, str) and raw_order_id.isdecimal():
        order_id = int(raw_order_id)
    else:
        raise ValueError("BigCommerce order ID must be a positive integer.")
    if order_id <= 0:
        raise ValueError("BigCommerce order ID must be a positive integer.")

    _load_credentials()
    os.environ.setdefault("LOG_DIR", "/tmp/logs")
    from orders_sync import process_order as process_order

    result = process_order(order_id=order_id)
    logger.info("BigCommerce order %s processing result: %s", order_id, json.dumps(result, default=str))
    return {"order_id": order_id, "result": result}