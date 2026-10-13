"""Shared fixtures for the bigcommerceorderhandler unit tests.

The handler modules import each other as top-level modules (as they do inside the Lambda package),
so the package directory is put on sys.path. Every outbound HTTP call and sleep is blocked so a test
can never reach BigCommerce, ShipperHQ or Pipe17 with real credentials from a local .env.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest
import requests

HANDLER_DIR = Path(__file__).resolve().parents[1] / "src" / "bigcommerceorderhandler"
sys.path.insert(0, str(HANDLER_DIR))

# Keep AppLogger's rotating log file out of the source tree; must be set before the modules are imported.
os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="bc-handler-test-logs-")

_CREDENTIAL_KEYS = (
    "BC_STORE_HASH",
    "BC_ACCESS_TOKEN",
    "BC_CLIENT_ID",
    "SHQ_ACCESS_TOKEN",
    "SHQ_API_BASE",
    "PIPE17_API_KEY",
    "PIPE17_API_BASE",
    "ORDER_SYNC_SECRET_ARN",
)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch):
    """Clear credentials (including any loaded from a local .env) and set fake ones."""
    for key in _CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BC_STORE_HASH", "storehash")
    monkeypatch.setenv("BC_ACCESS_TOKEN", "bc-token")
    monkeypatch.setenv("SHQ_ACCESS_TOKEN", "shq-token")
    monkeypatch.setenv("PIPE17_API_KEY", "pipe17-key")


@pytest.fixture(autouse=True)
def _block_network_and_sleep(monkeypatch):
    def _no_network(*args, **kwargs):
        raise AssertionError(f"Unmocked HTTP call: {args} {kwargs}")

    monkeypatch.setattr(requests.Session, "request", _no_network)
    monkeypatch.setattr("time.sleep", lambda seconds: None)


def make_response(status_code=200, json_body=None, text=None, headers=None):
    """Build a real requests.Response so .ok / .raise_for_status() / .json() behave as in production."""
    response = requests.Response()
    response.status_code = status_code
    response.headers.update(headers or {})
    if json_body is not None:
        response._content = json.dumps(json_body).encode()
    else:
        response._content = (text or "").encode()
    response.url = "https://example.test/"
    return response


@pytest.fixture
def mock_session_request(monkeypatch):
    """Replace requests.Session.request with a Mock; set .side_effect / .return_value per test."""
    request = mock.Mock(name="Session.request")
    monkeypatch.setattr(requests.Session, "request", lambda self, *a, **kw: request(*a, **kw))
    return request
