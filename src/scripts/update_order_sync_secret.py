"""Write the order-sync Secrets Manager secret from a .env file.

Keys come from .env.example; values come from the .env file, falling back to
the .env.example default. The full SecretString is always rewritten (no merge)
so the result does not depend on who can read the current value.

The secret is looked up from the stack's BigCommerceOrderSyncSecretArn output
(or passed with --secret-id).

Usage:
    python scripts/update_order_sync_secret.py --stack-name clubpilates-shipperhq
    python scripts/update_order_sync_secret.py --stack-name clubpilates-shipperhq --env-file .env.clubpilates --dry-run
"""

import argparse
import json
import sys
from pathlib import Path

HANDLER_DIR = Path(__file__).resolve().parent.parent / "bigcommerceorderhandler"

# Must match _REQUIRED_CREDENTIALS in bigcommerceorderhandler/app.py.
REQUIRED_KEYS = ("BC_STORE_HASH", "BC_ACCESS_TOKEN", "PIPE17_API_KEY", "SHQ_ACCESS_TOKEN")
SECRET_OUTPUT_KEY = "BigCommerceOrderSyncSecretArn"


def parse_env_file(path):
    values = {}
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def build_secret(example_path, env_path):
    defaults = parse_env_file(example_path)
    overrides = parse_env_file(env_path)

    unknown = sorted(set(overrides) - set(defaults))
    if unknown:
        print(f"Ignoring keys not in {example_path.name}: {', '.join(unknown)}", file=sys.stderr)

    secret = {key: overrides.get(key, default) for key, default in defaults.items()}
    missing = [key for key in REQUIRED_KEYS if not secret.get(key)]
    if missing:
        raise ValueError(f"{env_path} is missing values for required keys: {', '.join(missing)}")
    return secret


def mask(value):
    return "<empty>" if not value else "****" + value[-4:] if len(value) > 8 else "****"


def secret_arn_from_stack(session, stack_name):
    stacks = session.client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"]
    for output in stacks[0].get("Outputs", []):
        if output["OutputKey"] == SECRET_OUTPUT_KEY:
            return output["OutputValue"]
    raise ValueError(f"Stack {stack_name} has no {SECRET_OUTPUT_KEY} output; is it fully deployed?")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--stack-name", help=f"Deployed stack; the secret ARN is read from its {SECRET_OUTPUT_KEY} output.")
    target.add_argument("--secret-id", help="Secret name or ARN (e.g. the BigCommerceOrderSyncSecretArn stack output).")
    parser.add_argument("--env-file", type=Path, default=HANDLER_DIR / ".env", help="File with the real values.")
    parser.add_argument("--example-file", type=Path, default=HANDLER_DIR / ".env.example")
    parser.add_argument("--region")
    parser.add_argument("--profile")
    parser.add_argument("--dry-run", action="store_true", help="Show the keys that would be written, then exit.")
    args = parser.parse_args()

    try:
        secret = build_secret(args.example_file, args.env_file)
    except (OSError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    secret_id = args.secret_id
    if args.stack_name:
        try:
            secret_id = secret_arn_from_stack(session, args.stack_name)
        except Exception as error:
            parser.exit(1, f"error: could not read secret ARN from stack {args.stack_name}: {error}\n")

    print(f"Secret: {secret_id}")
    for key, value in secret.items():
        print(f"  {key} = {mask(value)}")
    if args.dry_run:
        print("Dry run: nothing written.")
        return

    response = session.client("secretsmanager").put_secret_value(
        SecretId=secret_id, SecretString=json.dumps(secret)
    )
    print(f"Wrote version {response['VersionId']} to {response['ARN']}")


if __name__ == "__main__":
    main()
