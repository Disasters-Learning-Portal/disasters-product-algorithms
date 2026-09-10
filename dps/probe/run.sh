#!/usr/bin/env -S bash --login
set -euo pipefail
# dps/probe/run.sh -- TEMPORARY IAM diagnostic. DELETE AFTER USE.
#
# Answers two questions that cannot be answered from outside a DPS job:
#   1. What AWS identity does a DPS worker actually run as? (docs/DPS.md claims
#      dps-verdi-role in acct 884094767067 -- documentation, never observed.)
#   2. Is that identity allowed to call secretsmanager:GetSecretValue at all?
#
# For (2) the secret does NOT need to exist. AWS distinguishes an identity-policy
# denial ("... because no identity-based policy allows the secretsmanager:
# GetSecretValue action") from a missing resource (ResourceNotFoundException), so
# pointing --secret_arn at a made-up ARN in your own account is decisive:
#   * "no identity-based policy allows"  -> the worker role cannot read secrets;
#     only MAAP can change that. Stop -- don't build the cross-account KMS setup.
#   * ResourceNotFoundException / other  -> the action is permitted; proceed to
#     create the CMK + secret + resource policy, then re-run with the real ARN.
#
# NEVER prints a secret value -- only OK / the error dict. Exits 0 in every case
# so a denial is a RESULT, not a failed job.

mkdir -p output

SECRET_ARN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --secret_arn) SECRET_ARN="$2"; shift 2;;
    *) echo "WARN: ignoring unrecognized arg: $1" >&2; shift;;
  esac
done

# strip stray quotes an operator may paste around the ARN
SECRET_ARN="${SECRET_ARN//[\"\']/}"

conda run --live-stream --name disasters_dps python - "${SECRET_ARN}" <<'PY'
import sys

import boto3
import botocore

REGION = "us-west-2"

try:
    ident = boto3.client("sts", region_name=REGION).get_caller_identity()
    print(f"DPS IDENTITY Arn     : {ident.get('Arn')}")
    print(f"DPS IDENTITY Account : {ident.get('Account')}")
except Exception as e:  # noqa: BLE001 -- diagnostic; report anything
    print(f"DPS IDENTITY: FAILED -- {e}")

arn = sys.argv[1].strip()
if not arn:
    print("SECRETS MANAGER: skipped (no --secret_arn given)")
    raise SystemExit(0)

print(f"SECRETS MANAGER: probing {arn}")
try:
    boto3.client("secretsmanager", region_name=REGION).get_secret_value(SecretId=arn)
    # Value deliberately NOT printed -- the job log is not a secret store.
    print("SECRETS MANAGER: OK (value read; not printed)")
except botocore.exceptions.ClientError as e:
    err = e.response.get("Error", {})
    print(f"SECRETS MANAGER: DENIED/ERROR code={err.get('Code')}")
    print(f"SECRETS MANAGER: message={err.get('Message')}")
except Exception as e:  # noqa: BLE001
    print(f"SECRETS MANAGER: unexpected error -- {type(e).__name__}: {e}")
PY

echo "Probe complete."
