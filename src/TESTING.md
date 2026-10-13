# Running the unit tests

The unit tests cover the order change function in [`bigcommerceorderhandler/`](bigcommerceorderhandler/). They live in [`tests/`](../tests/) at the repo root, outside the function's `CodeUri`, so they are not deployed with the Lambda.

Unlike [DEPLOYMENT.md](DEPLOYMENT.md), run every command here from the **repo root** (`ShippersHQLambda/`), not `src/`.

## Step 1: Set up a virtual environment (once)

You need Python 3.14, the same version as the Lambda runtime.

```bash
cd /Users/JayeshR/Documents/ShippersHQLambda
python3.14 -m venv .venv                     # skip if .venv already exists
.venv/bin/pip install -r requirements-dev.txt
```

[`requirements-dev.txt`](../requirements-dev.txt) installs the function's own requirements plus `boto3` and `pytest`.

## Step 2: Run the tests

```bash
.venv/bin/python -m pytest
```

A passing run ends with a line like `163 passed in 0.26s`.

To activate the venv first and then run `pytest` without the path, use `source .venv/bin/activate`.

### Run a subset

```bash
.venv/bin/python -m pytest tests/test_orders_sync.py                 # one file
.venv/bin/python -m pytest tests/test_app.py::TestLambdaHandler      # one class
.venv/bin/python -m pytest -k ProcessOrder                           # tests whose names match
.venv/bin/python -m pytest -v                                        # list each test
.venv/bin/python -m pytest -x                                        # stop at the first failure
```

### Run from VS Code

Open the Testing panel (beaker icon), choose **Configure Python Tests**, then pick **pytest** and the repo root folder. VS Code reads [`pytest.ini`](../pytest.ini) and finds the tests.

## What each test file covers

| File | Module under test |
| --- | --- |
| [`tests/test_app.py`](../tests/test_app.py) | `app.py`: loading the Secrets Manager secret, checking the order ID, reporting failed SQS records for retry |
| [`tests/test_orders_sync.py`](../tests/test_orders_sync.py) | `orders_sync.py`: BigCommerce client retries and errors, every result of `process_order` |
| [`tests/test_shipperhq.py`](../tests/test_shipperhq.py) | `shipperhq.py`: GraphQL client, shipment and carrier helpers, WWEX mapping |
| [`tests/test_bigcommerce_metafields.py`](../tests/test_bigcommerce_metafields.py) | `bigcommerce_metafields.py`: creating and updating order metafields |
| [`tests/test_pipe17.py`](../tests/test_pipe17.py) | `pipe17.py`: Pipe17 client (no longer called, but kept) |
| [`tests/test_logger.py`](../tests/test_logger.py) | `logger.py`: handlers, levels, local log file |

## How the tests stay offline

[`tests/conftest.py`](../tests/conftest.py) sets this up for every test:

- **No real API calls.** Every HTTP request is blocked. A test that makes a request it hasn't mocked fails with `Unmocked HTTP call`. This matters because `bigcommerceorderhandler/.env` may hold real credentials, and the modules load it when imported.
- **Fake credentials.** `BC_*`, `SHQ_*` and `PIPE17_*` are replaced with test values, and `ORDER_SYNC_SECRET_ARN` is unset. Tests that need Secrets Manager mock `boto3`.
- **No waiting.** `time.sleep` is disabled, so retry and backoff tests run instantly.
- **No log files in the repo.** Logs go to a temporary directory, not `bigcommerceorderhandler/logs/`.

No AWS credentials, network access or Docker are needed.

## Writing new tests

- Add a `tests/test_<module>.py` file. Import the module by its plain name (`import orders_sync`); `conftest.py` puts `bigcommerceorderhandler/` on the import path, matching how the Lambda imports it.
- To fake an HTTP response, use the `mock_session_request` fixture with `make_response` from `conftest`:

  ```python
  from conftest import make_response

  def test_get_order(mock_session_request):
      mock_session_request.return_value = make_response(json_body={"id": 42})
      assert BigCommerceClient().get_order(42) == {"id": 42}
  ```

  Use `side_effect=[...]` to return a different response on each call, such as a 429 followed by a 200.
- To test `process_order` without HTTP, replace the clients on the module with `monkeypatch.setattr(orders_sync, "ShipperHQClient", ...)`, as the fixtures in `test_orders_sync.py` do.

Run the tests before `sam build` and `sam deploy` ([DEPLOYMENT.md](DEPLOYMENT.md), Step 3).
