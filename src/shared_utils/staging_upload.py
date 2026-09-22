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

TWO ENTRY POINTS, ONE KEYING. Both consume ``iter_upload_keys``, so they produce
identical keys; they differ only in whose credentials do the PutObject:

* ``upload_dir_to_staging`` -- MAAP workspace credentials. The DPS path, and the only
  one that works there: ``dps-verdi-role`` cannot write ``nasa-disasters-staging``.
* ``upload_dir_ambient`` -- the default boto3 credential chain, plus a per-prefix write
  preflight. The NOTEBOOK/hub path. maap-py needs ``MAAP_PGT``, which the DPS wrapper
  injects and the hub never does, so the MAAP call 401s for every hub operator.

The choice is an explicit function name rather than a flag on purpose: a notebook reader
can see which identity is about to write. Implicit credential fallback is what made
``s3_operations.initialize_s3_client`` unusable (it silently used ambient credentials
while reporting success), and this module should not reintroduce it.
"""

import glob
import os
import posixpath

import boto3

from shared_utils.product_paths import prefix_for_product_dir, product_dirs_for


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


def program_data_key(local_path, out_home, sensor):
    """Return the canonical ``ProgramData/<Sensor>/<Product>/<file>`` key, or None.

    The product directory is read off the local output path: processors write into
    ``<out_home>/<date>/<Product>/`` (and ``.../<Product>/masked/``), so the nearest
    enclosing directory whose name is a known product for ``sensor`` names the
    product. The ``<date>`` level is deliberately dropped -- it groups scenes for
    merging on disk and has no place in the published key.

    Returns None when no product directory is recognized, which lets the caller
    fall back rather than inventing a destination.
    """
    rel = os.path.relpath(local_path, out_home)
    parts = rel.split(os.sep)[:-1]  # directories only
    known = product_dirs_for(sensor)
    for part in reversed(parts):
        if part in known:
            prefix = prefix_for_product_dir(sensor, part)
            return f"{prefix}/{os.path.basename(local_path)}"
    return None


def iter_upload_keys(out_home, base_prefix, include=None, sensor=None):
    """Yield ``(local_path, s3_key)`` for every product COG/PNG under ``out_home``.

    With ``sensor`` set, each file is keyed by its canonical published destination,
    ``ProgramData/<Sensor>/<Product>/<filename>`` (see :func:`program_data_key`), and
    ``base_prefix`` is not used. A file whose product directory is not recognized
    falls back to the ``base_prefix``-relative key and is reported, so nothing is
    silently dropped or published somewhere unintended.

    With ``sensor`` unset (the default), ``s3_key = base_prefix + relpath(local_path,
    out_home)`` so the sub-path under ``out_home`` is preserved. Mirrors
    ``dps/_finalize.sh`` (``*.tif`` + ``*.png``).

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
        if sensor is not None:
            key = program_data_key(f, out_home, sensor)
            if key is not None:
                yield f, key
                continue
            print(
                f"WARNING: {os.path.relpath(f, out_home)} is not under a known "
                f"{sensor} product directory; falling back to the relative key."
            )
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


def upload_dir_to_staging(out_home, target_bucket, dest_prefix, include=None, sensor=None):
    """Upload every product under ``out_home`` to ``s3://target_bucket/<prefix>/``.

    ``<prefix>`` = the MAAP-granted read_write prefix for ``target_bucket`` (often "")
    joined with ``dest_prefix`` (e.g. ``dps_output/<activation_event>``). Returns the
    number of files uploaded. Raises on any failure so a ``set -e`` run.sh aborts.

    ``include`` is forwarded to :func:`iter_upload_keys` to skip non-product files;
    None (the default, and what every ``run.sh`` uses) publishes everything.

    ``sensor`` is likewise forwarded: set it to publish to the canonical
    ``ProgramData/<Sensor>/<Product>/`` destination instead of mirroring the local
    tree under ``dest_prefix``.
    """
    s3, resp = _workspace_s3_client()
    entry_prefix = resolve_authorized_path(resp, target_bucket)
    base_prefix = _join_prefix(entry_prefix, dest_prefix)

    pairs = iter_upload_keys(out_home, base_prefix, include=include, sensor=sensor)
    return _upload_pairs(s3, target_bucket, pairs, base_prefix)


def _upload_pairs(s3, target_bucket, pairs, base_prefix):
    """Upload ``(local_path, key)`` pairs with a caller-supplied client. Returns the count."""
    n = 0
    for local_path, key in pairs:
        s3.upload_file(local_path, target_bucket, key)
        print(f"Uploaded: s3://{target_bucket}/{key}")
        n += 1
    # base_prefix is "" whenever sensor= keyed the files by their canonical
    # ProgramData destination, which is the notebook case -- don't print "bucket//".
    dest = f"s3://{target_bucket}/{base_prefix}/" if base_prefix else f"s3://{target_bucket}/"
    print(f"Uploaded {n} file(s) to {dest}")
    return n


def upload_dir_ambient(out_home, target_bucket, dest_prefix="", include=None,
                       sensor=None, preflight=True):
    """Upload every product under ``out_home`` using AMBIENT AWS credentials.

    Identical keying to :func:`upload_dir_to_staging` -- both consume
    :func:`iter_upload_keys`, so a notebook run and a DPS run land on the same keys --
    but with a plain ``boto3.client('s3')`` (the default credential chain) instead of
    MAAP workspace credentials.

    **This is the notebook / hub path.** maap-py authenticates from the ``MAAP_PGT``
    environment variable, which the DPS wrapper injects and the Disasters hub never
    does (it is per-user and secret, so it cannot be baked into the image, and the
    ``maapToken`` in MAAP Settings lives in the JupyterLab frontend SettingRegistry --
    a notebook kernel never sees it). ``upload_dir_to_staging`` therefore raises
    ``HTTPError: 401`` for every hub operator. On the hub the pod already assumes
    ``disasters-prod``, so ambient credentials are both the working path and the one
    any hub user gets -- the same choice ``notebooks/tools/simple_disaster_staging.ipynb``
    and the two ``*_transfer`` notebooks make against this bucket.

    DPS keeps :func:`upload_dir_to_staging`, where ``dps-verdi-role`` genuinely cannot
    write ``nasa-disasters-staging`` and ``MAAP_PGT`` genuinely is present.

    With ``preflight`` (the default) a real zero-byte ``PutObject`` is attempted under
    every distinct destination prefix before anything is uploaded, and a failure raises.
    Grants on this bucket are **per prefix**, and ``head_bucket`` succeeds for a
    read-only identity -- so a cheap probe is the only thing that actually proves the
    write, and failing up front beats dying part-way through the loop.
    """
    # Imported here, not at module scope, to keep this module's import graph flat --
    # s3_operations pulls in fsspec, which nothing else in staging_upload needs.
    from shared_utils.s3_operations import can_write_to_bucket

    s3 = boto3.client("s3")
    base_prefix = _join_prefix(dest_prefix)

    # Materialized once: the preflight needs every destination prefix up front, and
    # re-running iter_upload_keys would glob the tree a second time.
    pairs = list(iter_upload_keys(out_home, base_prefix, include=include, sensor=sensor))

    if preflight and pairs:
        for prefix in sorted({posixpath.dirname(key) for _, key in pairs}):
            ok, detail = can_write_to_bucket(s3, target_bucket, prefix, verbose=False)
            if not ok:
                raise RuntimeError(
                    f"cannot write to s3://{target_bucket}/{prefix}/ with the current "
                    f"(ambient) AWS credentials: {detail}. Nothing was uploaded. On the "
                    f"Disasters hub, check the pod's identity with "
                    f"boto3.client('sts').get_caller_identity() -- it should be disasters-prod."
                )

    return _upload_pairs(s3, target_bucket, pairs, base_prefix)
