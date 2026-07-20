# dps/_env.sh -- shared runtime AWS env for DPS run scripts that READ the CSDA
# vendor buckets (capella / umbra / satellogic / list_dates).
#
# WHY: a DPS worker runs as `dps-verdi-role` (account 884094767067). That role
# can WRITE to nasa-disasters (the _finalize.sh upload path uses ambient creds
# via plain boto3), but it has NO s3:ListBucket / read on the cross-account CSDA
# vendor buckets (csdap-capellaspace-delivery, csda-data-vendor-umbra,
# csda-data-vendor-satellogic) -- so an unassisted `--list_dates` or fetch dies
# with `AccessDenied ... s3:ListBucket`.
#
# The hub reads those same buckets as the `disasters-prod` role (that is the
# identity a hub notebook resolves to). DPS must ASSUME that role for reads.
# shared_utils.s3utils._read_session() already does exactly this: if READ_ROLE_ARN
# is set it assumes that role for every vendor READ (list + download); unset, it
# falls back to ambient creds -- which is correct on the hub (already
# disasters-prod) but AccessDenied on DPS. Setting the env var here is the whole
# fix. No ExternalId is needed: disasters-prod trusts dps-verdi-role directly, so
# READ_ROLE_EXTERNAL_ID stays unset.
#
# Scope: ONLY the vendor-read path (_read_session) is affected. The upload path
# (upload_file_to_s3 -> plain boto3.client) is untouched and stays on
# dps-verdi-role. Landsat/Sentinel-2 don't read CSDA buckets, so they don't
# source this file.
#
# `:-` keeps an operator-provided override winning over this default.
export READ_ROLE_ARN="${READ_ROLE_ARN:-arn:aws:iam::515966502221:role/disasters-prod}"
