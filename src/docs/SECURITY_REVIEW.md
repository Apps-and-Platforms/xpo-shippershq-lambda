# Security Review: ShipperHQ BigCommerce Order Change Lambda

| | |
|---|---|
| **Stack template** | [`src/template.yaml`](../template.yaml) (AWS SAM) |
| **Deployer / IAM template** | [`src/iam/sam-deployer-role.yaml`](../iam/sam-deployer-role.yaml) |
| **Function code** | [`src/bigcommerceorderhandler/`](../bigcommerceorderhandler/) |
| **Secret tooling** | [`src/scripts/update_order_sync_secret.py`](../scripts/update_order_sync_secret.py) |
| **Runtime** | Python 3.14, x86_64, AWS Lambda |
| **Prepared** | 2026-10-19 |

---

## 1. System overview

The stack processes BigCommerce order events and updates matching orders in Pipe17 with carrier data from ShipperHQ.

```
BigCommerce ──(partner event bus)──► EventBridge Rule ──► SQS Queue ──► Lambda ──HTTPS──► BigCommerce API
                                           │                 │                    ├─HTTPS──► ShipperHQ API
                                           └──(undeliverable)┴─(500 failures)─► SQS DLQ  └─HTTPS──► Pipe17 API
                                                                                  │
                                                       Secrets Manager secret ◄───┘ (GetSecretValue at runtime)
```

**Resources created per brand (`BrandName`)**

| Resource | Name | Purpose |
|---|---|---|
| `AWS::SecretsManager::Secret` | `<brand>/shipperhq/bigcommerce-order-sync` | Third-party API credentials |
| `AWS::SecretsManager::ResourcePolicy` | – | Restricts who can read the secret |
| `AWS::Serverless::Function` | `<brand>-bigcommerce-order-change-v1` | Order processing |
| IAM role (SAM-generated) | `<stack>-BigCommerceOrderChangeFunctio-<suffix>` | Function execution role |
| `AWS::SQS::Queue` | `<brand>-bigcommerce-order-change` | Buffer and retry |
| `AWS::SQS::Queue` | `<brand>-bigcommerce-order-change-dlq` | Dead letter queue |
| `AWS::SQS::QueuePolicy` | – | Restricts who can send to the queues |
| `AWS::Events::Rule` | `<brand>-bigcommerce-order-change-all` | Routes partner bus events to SQS |

**Data handled:** BigCommerce order data (order IDs, status, line items/SKUs, shipping details, which may include customer names and addresses), ShipperHQ shipment/carrier data, Pipe17 order records. No payment card data is requested by the function.

**Credentials handled:** `BC_ACCESS_TOKEN`, `BC_CLIENT_ID`, `BC_STORE_HASH`, `SHQ_ACCESS_TOKEN`, `PIPE17_API_KEY`.

---

## 2. Security controls

### 2.1 Secrets management

| Control | Where | Detail |
|---|---|---|
| Credentials stored in AWS Secrets Manager | `template.yaml` → `BigCommerceOrderSyncSecret` | All third-party credentials live in one secret. The template ships only empty placeholders; real values are written after deploy. |
| No credentials in source, template or Lambda config | `template.yaml` → function `Environment` | The only environment variables are `BRAND_NAME` and `ORDER_SYNC_SECRET_ARN` (the secret's ARN, not its value). Credentials never appear in the Lambda console, `GetFunctionConfiguration` output or CloudFormation parameters. |
| Encryption at rest | Secrets Manager default | Secret is encrypted with the AWS-managed `aws/secretsmanager` KMS key. |
| Resource policy: explicit deny on read | `BigCommerceOrderSyncSecretPolicy` → `DenyReadExceptFunctionRoleAndSecretAdmin` | `secretsmanager:GetSecretValue` is **denied to every principal** except the function's execution role and the named `SecretAdminPrincipalArn`, using `aws:PrincipalArn`. The explicit deny overrides any broad identity policy (for example an account admin with `secretsmanager:*` on `*`). |
| Public policy blocked | `BigCommerceOrderSyncSecretPolicy` → `BlockPublicPolicy: true` | Secrets Manager rejects any resource policy change that would grant broad (public or cross-account) access. |
| Named secret administrator | `SecretAdminPrincipalArn` parameter | A single, explicit IAM user or role administers the secret. The parameter is validated by `AllowedPattern` to be an IAM user/role ARN in a 12-digit account. |
| Secret-scoped function access | function `Policies` → `AWSSecretsManagerGetSecretValuePolicy` | The function's identity policy grants `GetSecretValue` on this one secret ARN only. |
| Key allowlist on load | `app.py` → `_ALLOWED_SECRET_KEYS` | Only known keys from the secret are loaded into the process; unexpected keys are dropped, so the secret cannot inject arbitrary environment variables (for example `PYTHONPATH`, `AWS_*`). |
| Fail-closed validation | `app.py` → `_load_credentials()` | The function refuses to run if the ARN is unset, the `SecretString` is empty or not a JSON object, or any of the four required credentials is missing. |
| Secret loaded at runtime, cached in memory | `app.py` → `_credentials` | Read once per execution environment; never written to disk. |
| Safe secret update tooling | `scripts/update_order_sync_secret.py` | Writes values from a local, git-ignored `.env`; prints only **masked** values (`****` + last 4 chars); supports `--dry-run`; validates required keys before writing; rewrites the full secret rather than merging, so the operator does not need read access to the existing value. |
| `.env` files excluded from git | `.gitignore` | `.env`, `credentials/` and `src/bigcommerceorderhandler/credentials` are ignored; only `.env.example` (empty values) is committed. |
| Secret removed with stack | `DeletionPolicy: Delete`, `UpdateReplacePolicy: Delete` | Credentials do not linger in the account after the stack is deleted or the secret is replaced. |

### 2.2 IAM: least privilege and privilege-escalation prevention

**Function execution role**

| Control | Detail |
|---|---|
| Permissions boundary on the function role | `Globals.Function.PermissionsBoundary` attaches the customer-managed policy `shipperhq-lambda-permissions-boundary` (created by `iam/sam-deployer-role.yaml`). Whatever SAM or a future change attaches to the role, its effective permissions can never exceed the boundary. |
| Boundary contents are narrowly scoped | The boundary allows only: (1) `logs:CreateLogGroup/CreateLogStream/PutLogEvents` on the function's own log group; (2) `secretsmanager:GetSecretValue/DescribeSecret` on `<brand>/shipperhq/bigcommerce-order-sync-*`; (3) SQS receive/delete/visibility/attributes on the `<brand>-bigcommerce-order-change` queue. This caps the `Resource: "*"` in the AWS-managed `AWSLambdaBasicExecutionRole` and `AWSLambdaSQSQueueExecutionRole` policies SAM attaches. |
| No write access to AWS data stores | The function cannot write to S3, DynamoDB, other queues, other secrets, or IAM. All outbound effects go to the three third-party APIs. |

**Deployer role (`shipperhq-sam-deployer`)**

| Control | Detail |
|---|---|
| Separate, dedicated deployment role | Deployments run via the `shipperhq-deployer` profile assuming this role, not an admin identity. Trusted principals are an explicit list (`TrustedPrincipalArns`); users get only `sts:AssumeRole` on this one role (`DeployerUserAssumeRolePolicy`, `deployer-user-assume-role-policy.json`). |
| Short sessions | `MaxSessionDuration: 3600` (1 hour). |
| Resource-scoped permissions | Every write action is scoped to name patterns: stacks `*-shipperhq` (plus the SAM CLI artifact stack), functions `<brand>-bigcommerce-order-change-v1`, queues `<brand>-bigcommerce-order-change[-dlq]`, secrets `<brand>/shipperhq/bigcommerce-order-sync-*`, rules `<brand>-bigcommerce-order-change-all`, SAM artifact buckets `aws-sam-cli-managed-default-samclisourcebucket-*`. `Resource: "*"` is used only for read/list/validate actions that do not support resource-level permissions. |
| Privilege escalation blocked by boundary condition | `iam:CreateRole`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy` and `iam:PutRolePermissionsBoundary` are allowed only when `iam:PermissionsBoundary` equals the function boundary. The deployer cannot create an unbounded role or remove the boundary, so it cannot grant itself or a function more than the boundary allows. |
| Managed policy attach allowlist | `iam:AttachRolePolicy/DetachRolePolicy` are allowed only for `AWSLambdaBasicExecutionRole` and `AWSLambdaSQSQueueExecutionRole`, and only on roles carrying the boundary. `AdministratorAccess` or any other policy cannot be attached. |
| `iam:PassRole` restricted | Allowed only for stack roles and only when `iam:PassedToService` is `lambda.amazonaws.com`. |
| Event source mappings restricted | `lambda:CreateEventSourceMapping` is conditioned on `lambda:FunctionArn` matching the order change function. |
| No delete/modify of unrelated IAM | IAM actions are limited to roles named `<stack-name-pattern>-*`. No user, group or policy management beyond the deployer's own stack. |

**CloudFormation guardrails**

| Control | Detail |
|---|---|
| Change sets reviewed before deploy | `samconfig.toml` → `confirm_changeset = true`: every deployment shows the change set and requires manual confirmation. |
| Only `CAPABILITY_IAM` | Not `CAPABILITY_NAMED_IAM`; the stack cannot create arbitrarily named IAM resources. |
| Template linting | `samconfig.toml` → `[default.validate.parameters] lint = true` runs cfn-lint on `sam validate`. |

### 2.3 Event ingestion and messaging

| Control | Where | Detail |
|---|---|---|
| Events only from the BigCommerce partner bus | `BigCommerceOrderChangeRule.EventBusName` | The rule is attached to the dedicated `aws.partner/bigcommerce.com/...` partner event source bus, which only BigCommerce (the partner) can publish to. No public HTTP endpoint, API Gateway or Function URL is exposed. |
| No inbound network surface | template | The function has no URL, no API Gateway trigger and no resource-based permission for external callers; its only trigger is the SQS event source mapping. |
| Queue send restricted to this rule | `BigCommerceOrderChangeQueuePolicy` | Only `events.amazonaws.com` may `sqs:SendMessage`, and only when `aws:SourceArn` equals this rule's ARN (confused-deputy protection). Other EventBridge rules, in this or another account, cannot write to the queue or DLQ. |
| Encryption at rest | both queues → `SqsManagedSseEnabled: true` | Messages are encrypted at rest with SQS-managed keys. |
| Encryption in transit | AWS service endpoints | EventBridge → SQS → Lambda traffic stays on AWS service endpoints over TLS. |
| Bounded retries and DLQ | queue `RedrivePolicy`, rule `DeadLetterConfig` | Failed messages retry every 60 s (visibility timeout) up to 500 receives, then move to the DLQ. Events EventBridge cannot deliver go straight to the DLQ. Nothing is silently dropped; the DLQ keeps messages for the 14-day maximum for investigation. |
| Per-message failure isolation | function event `BatchSize: 1`, `ReportBatchItemFailures` | One bad message cannot cause others to be reprocessed or lost. |
| Bounded execution | `Globals.Function.Timeout: 30` | Function runtime is capped; visibility timeout (60 s) is greater than the function timeout, preventing duplicate concurrent processing of one message. |

### 2.4 Application code

| Control | Where | Detail |
|---|---|---|
| Strict input validation | `app.py` → `_process_event()` | The only value taken from the event is `detail.data.id`. It must be a positive integer (or decimal-digit string); booleans, negative numbers, floats and other strings are rejected **before** credentials are loaded or any API is called. Because the order ID is used to build API URL paths, this prevents path/URL injection into BigCommerce, Pipe17 and ShipperHQ requests. |
| TLS-only outbound calls | `orders_sync.py`, `bigcommerce_metafields.py`, `pipe17.py`, `shipperhq.py` | All API base URLs are `https://`. `requests` verifies TLS certificates by default and no code disables verification (`verify=False` is not used). |
| Hard-coded BigCommerce host | `orders_sync.py`, `bigcommerce_metafields.py` | `https://api.bigcommerce.com/...` is a constant; the store hash is the only variable part. |
| Overridable API bases come only from the protected secret | `SHQ_API_BASE`, `PIPE17_API_BASE` | These can be changed only by principals able to write the secret (the secret admin / deployer), not by event content. |
| Request timeouts | `REQUEST_TIMEOUT = 30` in each client | Every outbound HTTP call has a timeout, so a slow or hostile endpoint cannot hang the function. |
| Bounded retries with backoff and rate-limit handling | `orders_sync.py` | Retries are capped (`MAX_RETRIES`), use exponential backoff and honour BigCommerce's `X-Rate-Limit-Time-Reset-Ms`. |
| Credentials sent only in headers | API clients | Tokens are sent as HTTP headers (e.g. `X-Auth-Token`), never in URLs or query strings, so they do not appear in logged URLs. |
| Credentials not logged | `orders_sync.py`, `logger.py` | Client initialisation logs whether a client ID is *set*, not its value. Tokens are never passed to the logger. |
| No local log files in Lambda | `logger.py` → `RUNNING_IN_LAMBDA` | In Lambda, logs go only to stdout → CloudWatch Logs. The rotating file handler is used only for local runs. |
| Configurable log level, INFO default | `LOG_LEVEL` in secret | Verbose DEBUG logging (which includes request parameters and truncated response bodies) is off by default. |
| Minimal dependencies | `requirements.txt` | Only `requests` and `python-dotenv`; `boto3` comes from the Lambda runtime. |
| Unit tests | `tests/` | Test suite covers the handler, logger and each API client. |

### 2.5 Resource isolation and governance

| Control | Detail |
|---|---|
| Parameter validation | `BrandName` must match `^[a-zA-Z0-9-]{1,30}$`; `SecretAdminPrincipalArn` must match an IAM user/role ARN pattern; `BigCommerceEventBusName` must be non-empty. This prevents malformed or wildcard values from widening resource names or policies. |
| Brand-prefixed resource names | Every resource name starts with `BrandName`, so multiple brands deployed in one account are isolated, and the deployer role and permissions boundary can scope access with `BrandNamePattern`. |
| Tagging | Function, secret and queues carry a `Brand` tag for ownership, cost allocation and tag-based access control. |
| Documented intent | Key resources include `com.aws.cloudformation.Context` metadata stating their purpose and the invariants they must keep (e.g. who may read the secret, retry timing). |

---

## 3. Items for reviewer attention

These are known gaps or decisions reviewers may want to weigh. None expose credentials or provide an inbound attack path, but each is worth an explicit decision.

| # | Area | Observation | Suggested action |
|---|---|---|---|
| 1 | Logging / PII | `app.py` logs the full EventBridge event at INFO, and `orders_sync.py` logs ShipperHQ shipment data and Pipe17 fields at INFO. At DEBUG, request bodies and up to 2,000 chars of responses are logged. These can contain customer names and addresses. | Confirm acceptable for the data classification, or reduce to IDs at INFO. Keep `LOG_LEVEL=INFO` in production. |
| 2 | Log retention | No `AWS::Logs::LogGroup` is defined, so the Lambda-created log group defaults to **never expire**. | Add a log group with `RetentionInDays` (and optionally a KMS key). |
| 3 | Encryption keys | Secret and queues use AWS-managed keys, not customer-managed KMS keys. | Acceptable for most policies; use a CMK if key-level audit/revocation is required. |
| 4 | Secret rotation | No automatic rotation is configured (third-party tokens are rotated manually). | Document a manual rotation schedule; use `update_order_sync_secret.py` to rotate. |
| 5 | Secret admin scope | `SecretAdminPrincipalArn` gets `secretsmanager:*` (including delete). `samconfig.toml` currently sets it to an IAM **user**, which implies long-lived access keys. | Prefer an SSO/IAM role; enforce MFA on the user if a user is kept. |
| 6 | Transport enforcement on SQS | Queue policy has no `Deny` on `aws:SecureTransport = false`. AWS services already use TLS. | Optional hardening: add a deny-non-TLS statement. |
| 7 | Event filter | Rule pattern `source: [{prefix: ""}]` forwards **every** event on the partner bus; non-order events are rejected by input validation and end in the DLQ after retries. | Optionally narrow the pattern to order event types to reduce noise and processing. |
| 8 | Deployer trust policy | The `cloudformation.amazonaws.com` trust statement has no `aws:SourceAccount` condition. | Add `aws:SourceAccount: ${AWS::AccountId}`. |
| 9 | Deployer scope default | `BrandNamePattern` defaults to `*`, so the deployer can manage stacks for every brand in the account. | Set per brand if brands have different owners. |
| 10 | Committed identifiers | `samconfig.toml` and `iam/deployer-user-assume-role-policy.json` contain the AWS account ID, an IAM user name and the BigCommerce store hash/event bus name. These are identifiers, not credentials. | Confirm acceptable for repository visibility. |
| 11 | Tracked log file | `src/bigcommerceorderhandler/logs/bigcommerce.log` is committed to git despite the `.gitignore` entry (it was added before the ignore rule). | Review its contents and `git rm --cached` it. |
| 12 | Concurrency / tracing | No reserved concurrency limit or X-Ray tracing is set. | Optional: set `ReservedConcurrentExecutions` to cap third-party API usage; enable tracing if needed for audit. |
| 13 | Network | Function runs outside a VPC with public internet egress (needed to reach the three SaaS APIs). | Acceptable; a VPC with NAT and egress filtering is possible if required by policy. |

---

## 4. Deployment and operations checklist

1. Deploy `iam/sam-deployer-role.yaml` once per account/region with an admin principal (creates the deployer role and the function permissions boundary).
2. Deploy the application stack with `sam deploy` using the `shipperhq-deployer` profile; review the change set before confirming.
3. Write credentials with `python scripts/update_order_sync_secret.py --stack-name <brand>-shipperhq` (use `--dry-run` first). Never commit `.env`.
4. Verify only the function role and secret admin can read the secret: `aws secretsmanager get-resource-policy --secret-id <arn>`.
5. Monitor the DLQ (`BigCommerceOrderChangeDLQUrl` output) and CloudWatch Logs for failures.
