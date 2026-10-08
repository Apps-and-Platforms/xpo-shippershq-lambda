# Deploying the ShipperHQ BigCommerce order change stack

This guide covers two templates:

- [`iam/sam-deployer-role.yaml`](iam/sam-deployer-role.yaml) creates the deployer role and the function permissions boundary. An admin deploys it once per account and region.
- [`template.yaml`](template.yaml) is the application stack, deployed with `sam deploy` as the deployer role.

The examples use account `020871906410`, region `us-east-2`, user `franconnect-s3-user` and stack `clubpilates-shipperhq`. Run every command from `src/`.

## Step 0: Prerequisites

```bash
cd /Users/JayeshR/Documents/ShippersHQLambda/src
aws --version && sam --version
aws sts get-caller-identity          # confirm which identity and account you're using
```

`sam build` needs Python 3.14 installed locally. If you don't have it, use Docker with `sam build --use-container`, and set `DOCKER_HOST` as in [`help.md`](help.md).

## Step 1: Deploy the deployer role (once per account and region, as an admin)

This needs an identity that can create IAM roles and policies, such as an admin profile.

```bash
aws cloudformation deploy \
  --template-file iam/sam-deployer-role.yaml \
  --stack-name shipperhq-sam-deployer-role \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-2 \
  --profile <admin-profile> \
  --parameter-overrides \
      TrustedPrincipalArns=arn:aws:iam::020871906410:user/franconnect-s3-user \
      RoleName=shipperhq-sam-deployer \
      StackNamePattern='*-shipperhq' \
      BrandNamePattern='*' \
      FunctionPermissionsBoundaryName=shipperhq-lambda-permissions-boundary
```

| Argument | Meaning |
|---|---|
| `--capabilities CAPABILITY_NAMED_IAM` | Required because the template creates a role and a policy with fixed names |
| `TrustedPrincipalArns` | Who may assume the role. To list several, separate them with commas inside quotes: `"arn:...:user/a,arn:...:role/b"` |
| `RoleName` | Name of the deployer role (the default is shown) |
| `StackNamePattern` | Application stack names the role may manage. `*-shipperhq` matches `clubpilates-shipperhq` |
| `BrandNamePattern` | `*` allows every brand. Use e.g. `clubpilates` to limit the role to one brand |
| `FunctionPermissionsBoundaryName` | Must match `PermissionsBoundaryPolicyName` in `template.yaml` (both default to the same name) |

Only `TrustedPrincipalArns` is required; the others are shown with their default values.

Get the role ARN:

```bash
aws cloudformation describe-stacks --stack-name shipperhq-sam-deployer-role --region us-east-2 \
  --query "Stacks[0].Outputs" --output table
```

## Step 2: Set up a CLI profile that assumes the role

Add this to `~/.aws/config`:

```ini
[profile shipperhq-deployer]
role_arn       = arn:aws:iam::020871906410:role/shipperhq-sam-deployer
source_profile = default        # the profile holding franconnect-s3-user's keys
region         = us-east-2
```

Check that it works:

```bash
aws sts get-caller-identity --profile shipperhq-deployer
# Should show: arn:aws:sts::020871906410:assumed-role/shipperhq-sam-deployer/...
```

The role is in the same account and its trust policy names the user directly, so the user doesn't need an extra `sts:AssumeRole` policy.

## Step 3: Build

```bash
sam build                    # or: sam build --use-container
```

This writes the build output to `.aws-sam/build/`.

## Step 4: Deploy the application stack as the deployer role

```bash
sam deploy \
  --profile shipperhq-deployer \
  --region us-east-2 \
  --stack-name clubpilates-shipperhq \
  --s3-prefix clubpilates-shipperhq \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --confirm-changeset \
  --parameter-overrides \
      BrandName=clubpilates \
      BigCommerceEventBusName="aws.partner/bigcommerce.com/36q3qe30nogzkwdzde4tez8c8vvp5im/BigCommerce_YogaSix_Sandbox_EventBridge" \
      SecretAdminPrincipalArn=arn:aws:iam::020871906410:user/franconnect-s3-user \
      PermissionsBoundaryPolicyName=shipperhq-lambda-permissions-boundary
```

[`samconfig.toml`](samconfig.toml) already contains everything, including `profile = "shipperhq-deployer"`, so `sam deploy` alone is equivalent.

| Argument | Meaning |
|---|---|
| `--stack-name` | Must match `StackNamePattern`. The `stack_name` in the `global` section of `samconfig.toml` (`lambda-bc-changeorder-stack`) does **not** match, but the `deploy` section's value takes precedence |
| `--resolve-s3` | Uses or creates the `aws-sam-cli-managed-default` artifact bucket |
| `--capabilities CAPABILITY_IAM` | Needed because SAM generates the function's role |
| `--confirm-changeset` | Shows the change set and asks before applying it. Drop it for CI |
| `BrandName` | Prefix for every resource name (function, rule, secret) |
| `BigCommerceEventBusName` | Name of the partner event bus, in the same account and region |
| `SecretAdminPrincipalArn` | The user or role allowed to read and manage the secret value. For SSO, use the role ARN |
| `PermissionsBoundaryPolicyName` | Leave as the default unless you renamed the boundary in step 1 |

**Alternative (option B):** keep your own credentials and let CloudFormation use the role by adding `--role-arn arn:aws:iam::020871906410:role/shipperhq-sam-deployer` and dropping `--profile shipperhq-deployer`. Your user then also needs `iam:PassRole` on the deployer role, plus permissions for the S3 upload and the change set.

## Step 5: Fill in the secret

The stack creates the secret with empty values. The deployer role can write the secret but can't read it, and the script only writes, so it works with either profile:

```bash
python scripts/update_order_sync_secret.py --stack-name clubpilates-shipperhq \
  --env-file bigcommerceorderhandler/.env --region us-east-2 --profile shipperhq-deployer --dry-run
# Check the listed keys, then run it again without --dry-run
```

## Step 6: Check the deployment

```bash
aws cloudformation describe-stacks --stack-name clubpilates-shipperhq --profile shipperhq-deployer \
  --query "Stacks[0].[StackStatus,Outputs]" --output json

# Confirm the function role has the boundary
ROLE=$(aws cloudformation describe-stacks --stack-name clubpilates-shipperhq --profile shipperhq-deployer \
  --query "Stacks[0].Outputs[?OutputKey=='BigCommerceOrderChangeFunctionV1IamRole'].OutputValue" --output text)
aws iam get-role --role-name "${ROLE##*/}" --profile shipperhq-deployer \
  --query "Role.PermissionsBoundary"
```

## Adding another brand

1. Repeat step 4 with a new `--stack-name <brand>-shipperhq`, `--s3-prefix`, `BrandName` and event bus.
2. Repeat step 5 for the new stack.

Step 1 doesn't need to change if `BrandNamePattern='*'`.

## Tearing down

```bash
sam delete --stack-name clubpilates-shipperhq --region us-east-2 --profile shipperhq-deployer
# The deployer role can also force-delete the secret if its name is still reserved:
aws secretsmanager delete-secret --secret-id clubpilates/shipperhq/bigcommerce-order-sync \
  --force-delete-without-recovery --region us-east-2 --profile shipperhq-deployer
# Delete the deployer role last, as admin, once no application stacks remain
aws cloudformation delete-stack --stack-name shipperhq-sam-deployer-role --region us-east-2 --profile <admin-profile>
```

## Troubleshooting

- **`AccessDenied` on `iam:CreateRole` or `PutRolePolicy`:** the boundary name in `template.yaml` doesn't match the one in the deployer stack, or the stack name doesn't match `*-shipperhq`.
- **The function gets AccessDenied at runtime:** it needs something the boundary doesn't allow. Add that permission to the boundary policy in `iam/sam-deployer-role.yaml` and redeploy step 1.
- **The secret name already exists:** an earlier secret is still pending deletion. Run the force-delete command from the teardown section.
- **`AccessDenied` on `s3:PutObject` naming `user/franconnect-s3-user`:** `sam deploy` ran as your user rather than the deployer role. Check that the `shipperhq-deployer` profile from step 2 exists and that `aws sts get-caller-identity --profile shipperhq-deployer` shows the assumed role.
