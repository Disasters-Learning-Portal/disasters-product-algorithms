"""Publish DPS product COGs/PNGs to a MAAP org bucket (e.g. ``nasa-disasters-staging``).

The DPS worker's own IAM role (``dps-verdi-role``) can write ``nasa-disasters`` but
NOT ``nasa-disasters-staging``. MAAP grants a job short-lived credentials for the org
buckets its team was authorized on, via ``maap.aws.workspace_bucket_credentials()``.
This module requests those credentials, confirms the target bucket is writable, and
uploads every product under ``OUT_HOME`` -- keyed by its path relative to ``OUT_HOME``
so same-named COGs in different scene/product subdirs don't overwrite each other
(same rule as ``dps/_finalize.sh``'s operational upload).

maap-py is a DPS-ONLY dependency (pinned in ``dps/environment.yml``; absent from
``pyproject.toml`` / ``image/environment.yml``). ``shared_utils`` is imported broadly
(notebooks, every CLI), so the ``from maap.maap import MAAP`` import is deferred INTO
``_workspace_s3_client`` -- importing this module never requires maap-py; only calling
``upload_dir_to_staging`` (i.e. inside a live DPS job) does. Same lazy pattern as
``dps/_get_secret.py``. Auth is ambient in a DPS job (the wrapper injects ``MAAP_PGT``).
"""

import glob
import os

import boto3


def _join_prefix(*parts):
    """Join S3 key prefix segments, dropping empties and redundant slashes."""
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


def resolve_authorized_path(resp, target_bucket):
    """Return the read_write prefix MAAP granted for ``target_bucket`` (may be "").

    Fails loud (never silently uploads nothing): raises with the full list of what
    WAS authorized so an operator can see the grant is missing or read-only, and
    with the response shape if it isn't the documented ``authorized_s3_paths`` dict.
    """
    if not isinstance(resp, dict):
        raise RuntimeError(
            "unexpected workspace_bucket_credentials() response: "
            f"type={type(resp).__name__} (expected a dict)"
        )
    paths = resp.get("authorized_s3_paths")
    if not isinstance(paths, list):
        raise RuntimeError(
            "workspace_bucket_credentials() response has no 'authorized_s3_paths' list; "
            f"top-level keys={sorted(resp)}"
        )

    matches = [p for p in paths if isinstance(p, dict) and p.get("bucket") == target_bucket]
    writable = [p for p in matches if p.get("access") == "read_write"]
    if writable:
        return writable[0].get("prefix") or ""

    granted = ", ".join(
        f"{p.get('bucket')}({p.get('access')})" for p in paths if isinstance(p, dict)
    ) or "<none>"
    if matches:
        raise RuntimeError(
            f"MAAP granted '{target_bucket}' but only read_only -- cannot upload. "
            f"Authorized paths: {granted}"
        )
    raise RuntimeError(
        f"MAAP workspace credentials do not grant write access to '{target_bucket}'. "
        f"Ask the MAAP / Data Services team to add it to your org's authorized buckets. "
        f"Authorized paths: {granted}"
    )


def iter_upload_keys(out_home, base_prefix, include=None):
    """Yield ``(local_path, s3_key)`` for every product COG/PNG under ``out_home``.

    ``s3_key = base_prefix + relpath(local_path, out_home)`` so the sub-path under
    ``out_home`` is preserved. Mirrors ``dps/_finalize.sh`` (``*.tif`` + ``*.png``).

    ``include`` is an optional predicate taking the local path; only files it
    returns truthy for are yielded. It defaults to None (publish everything),
    which is what a DPS job wants -- ``run.sh`` writes nothing but products into
    ``OUT_HOME``. Operator notebooks share their output tree with intermediates
    (per-tile merge inputs, water-extent scratch), so they pass a predicate.
    """
    base = base_prefix.strip("/")
    files = sorted(
        glob.glob(os.path.join(out_home, "**", "*.tif"), recursive=True)
        + glob.glob(os.path.join(out_home, "**", "*.png"), recursive=True)
    )
    for f in files:
        if include is not None and not include(f):
            continue
        rel = os.path.relpath(f, out_home)
        yield f, (f"{base}/{rel}" if base else rel)


def _workspace_s3_client():
    """Return ``(s3_client, resp)`` from MAAP workspace credentials (lazy maap import)."""
    from maap.maap import MAAP  # lazy: maap-py is a DPS-only dependency

    resp = MAAP().aws.workspace_bucket_credentials()
    creds = resp.get("credentials") if isinstance(resp, dict) else None
    if not isinstance(creds, dict):
        raise RuntimeError(
            "workspace_bucket_credentials() returned no 'credentials' block; "
            f"got type={type(resp).__name__}"
            + (f", keys={sorted(resp)}" if isinstance(resp, dict) else "")
        )
    session = boto3.Session(
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        aws_session_token=creds["aws_session_token"],
    )
    return session.client("s3"), resp


def upload_dir_to_staging(out_home, target_bucket, dest_prefix, include=None):
    """Upload every product under ``out_home`` to ``s3://target_bucket/<prefix>/``.

    ``<prefix>`` = the MAAP-granted read_write prefix for ``target_bucket`` (often "")
    joined with ``dest_prefix`` (e.g. ``dps_output/<activation_event>``). Returns the
    number of files uploaded. Raises on any failure so a ``set -e`` run.sh aborts.

    ``include`` is forwarded to :func:`iter_upload_keys` to skip non-product files;
    None (the default, and what every ``run.sh`` uses) publishes everything.
    """
    s3, resp = _workspace_s3_client()
    entry_prefix = resolve_authorized_path(resp, target_bucket)
    base_prefix = _join_prefix(entry_prefix, dest_prefix)

    n = 0
    for local_path, key in iter_upload_keys(out_home, base_prefix, include=include):
        s3.upload_file(local_path, target_bucket, key)
        print(f"Uploaded: s3://{target_bucket}/{key}")
        n += 1
    print(f"Uploaded {n} file(s) to s3://{target_bucket}/{base_prefix}/")
    return n
