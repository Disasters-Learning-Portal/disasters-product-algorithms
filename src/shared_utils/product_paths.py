"""Canonical S3 destinations for every published product.

Products are published to a single bucket under a three-level key::

    s3://nasa-disasters-staging/ProgramData/<Sensor>/<Product>/<filename>.tif

There is no event, date, or ``Output/`` level in the key -- the activation event is
carried in the GeoTIFF tags, not in the path. This module hard-codes the ``<Sensor>``
and ``<Product>`` segment for every product so the destination is stated once, in one
place, rather than being inferred from whatever directory a processor happened to write
into.

The strings here were taken from a listing of the live bucket, not from the workflow
code -- the two had drifted apart. Three things to know before editing them:

* **Product directories are PascalCase** (``TrueColor``), but the product token inside
  the *filename* is camelCase and sometimes a different word (``trueColor``,
  ``colorInfrared`` under ``ColorIR/``). Changing a value here does not change any
  filename.
* **Two sensor directories are not spelled the way the code spells them**: the bucket
  uses ``Skysat`` (not ``SkySat``) and ``Iceye`` (not ``ICEYE``).
* **``MNDWI`` publishes to ``mNDWI/``** -- lowercase leading ``m``, matching the
  conventional spelling of the index.

A value of ``None`` means the destination is genuinely undecided and needs a call from
the team; :func:`program_data_prefix` raises :class:`UndecidedProductPath` rather than
guessing. See ``UNDECIDED_NOTES`` for why each one is open.
"""

import os

#: The one bucket every product is published to. Hard-coded here so a notebook or
#: a run.sh never carries its own copy -- the three sensor notebooks had drifted
#: onto two different buckets and two different prefixes.
STAGING_BUCKET = "nasa-disasters-staging"

PROGRAM_DATA_ROOT = "ProgramData"

#: Sensor key -> ``<Sensor>`` directory, spelled as the bucket spells it.
SENSOR_DIRS = {
    "landsat": "Landsat",
    "sentinel2": "Sentinel-2",
    "satellogic": "Satellogic",
    "skysat": "Skysat",
    "umbra": "Umbra",
    "capella": "Capella",
    "iceye": "Iceye",
}

#: ``(sensor, product token) -> <Product>`` directory. The product token is the string
#: the processor already uses internally, so a caller can look up what it has in hand.
#: ``None`` = undecided; see ``UNDECIDED_NOTES``.
PRODUCT_DIRS = {
    # -- Landsat (LC08 / LC09) -------------------------------------------------
    ("landsat", "cloudMask"): "CloudMask",
    ("landsat", "trueColor"): "TrueColor",
    ("landsat", "panchromatic"): "Panchromatic",
    ("landsat", "naturalColor"): "NaturalColor",
    ("landsat", "colorInfrared"): "ColorIR",
    ("landsat", "shortwaveInfrared"): "ShortwaveIR",
    ("landsat", "NDVI"): "NDVI",
    ("landsat", "NDWI"): "NDWI",
    ("landsat", "MNDWI"): "mNDWI",
    ("landsat", "EVI"): "EVI",
    ("landsat", "NBR"): "NBR",
    ("landsat", "waterExtent"): "WaterExtent",
    # -- Sentinel-2 (S2A / S2B / S2C) ------------------------------------------
    ("sentinel2", "cloudMask"): "CloudMask",
    ("sentinel2", "trueColor"): "TrueColor",
    ("sentinel2", "naturalColor"): "NaturalColor",
    ("sentinel2", "colorInfrared"): "ColorIR",
    ("sentinel2", "shortwaveInfrared"): "ShortwaveIR",
    ("sentinel2", "NDVI"): "NDVI",
    ("sentinel2", "NDWI"): "NDWI",
    ("sentinel2", "MNDWI"): "mNDWI",
    ("sentinel2", "NBR"): "NBR",
    # Reachable only through the STAC/ODR workflow; the legacy
    # process_sentinel2 CLI has no EVI product and never creates this directory.
    ("sentinel2", "EVI"): "EVI",
    ("sentinel2", "dNBR"): "dNBR",
    ("sentinel2", "waterExtent"): "WaterExtent",
    # -- Satellogic (product tokens are lowercase in this processor) ------------
    ("satellogic", "truecolor"): "TrueColor",
    ("satellogic", "colorir"): "ColorIR",
    ("satellogic", "ndvi"): "NDVI",
    ("satellogic", "ndwi"): "NDWI",
    ("satellogic", "evi"): "EVI",
    # -- SkySat (no processor in this repo yet; published via the operator
    #    notebooks, which need the same destinations) ---------------------------
    ("skysat", "TrueColor"): "TrueColor",
    ("skysat", "ColorIR"): "ColorIR",
    ("skysat", "NDVI"): "NDVI",
    ("skysat", "NDWI"): "NDWI",
    ("skysat", "EVI"): "EVI",
    # -- SAR -------------------------------------------------------------------
    # Umbra's directory names the delivered processing level (Geocoded Ellipsoid
    # Corrected), not the radiometric product, so it cannot distinguish sigma0 from
    # beta0/gamma0. Only sigma0 has ever been published.
    ("umbra", "sigma0"): "GEC",
    ("umbra", "beta0"): None,
    ("umbra", "gamma0"): None,
    ("capella", "sigma0"): "Backscatter",
    ("iceye", "sigma0"): "Backscatter",
    ("iceye", "sigma0-amp"): "Backscatter",
    ("iceye", "sigma0-dB"): "Backscatter",
}

#: Why each ``None`` above is still open, so the blank is actionable rather than a gap.
UNDECIDED_NOTES = {
    ("umbra", "beta0"): (
        "Umbra publishes to GEC/, which names a processing level rather than a "
        "radiometric product, so beta0 has no directory that distinguishes it from "
        "sigma0. Either give beta0 its own directory (Beta0/) or move all Umbra "
        "output under product directories the way Capella and Iceye do."
    ),
    ("umbra", "gamma0"): (
        "Same as umbra/beta0: GEC/ cannot distinguish gamma0 from sigma0."
    ),
}


class UndecidedProductPath(Exception):
    """Raised when a product's S3 destination has not been decided yet."""


def product_dir(sensor, product):
    """Return the ``<Product>`` directory for ``(sensor, product)``.

    Raises :class:`KeyError` for an unknown pair and :class:`UndecidedProductPath`
    for one whose destination is still open.
    """
    key = (sensor, product)
    if key not in PRODUCT_DIRS:
        raise KeyError(
            f"no S3 destination recorded for sensor={sensor!r} product={product!r}; "
            f"add it to PRODUCT_DIRS in shared_utils/product_paths.py"
        )
    value = PRODUCT_DIRS[key]
    if value is None:
        raise UndecidedProductPath(
            f"the S3 destination for sensor={sensor!r} product={product!r} is undecided. "
            + UNDECIDED_NOTES.get(key, "")
        )
    return value


def program_data_prefix(sensor, product):
    """Return the full key prefix ``ProgramData/<Sensor>/<Product>`` (no trailing slash).

    >>> program_data_prefix("sentinel2", "colorInfrared")
    'ProgramData/Sentinel-2/ColorIR'
    """
    if sensor not in SENSOR_DIRS:
        raise KeyError(
            f"unknown sensor {sensor!r}; add it to SENSOR_DIRS in "
            f"shared_utils/product_paths.py"
        )
    return f"{PROGRAM_DATA_ROOT}/{SENSOR_DIRS[sensor]}/{product_dir(sensor, product)}"


def is_decided(sensor, product):
    """True if ``(sensor, product)`` is known and has a decided destination."""
    return PRODUCT_DIRS.get((sensor, product)) is not None


def undecided_products():
    """Return the ``(sensor, product)`` pairs still awaiting a destination decision."""
    return sorted(k for k, v in PRODUCT_DIRS.items() if v is None)


def product_dirs_for(sensor):
    """Return the set of decided ``<Product>`` directory names for ``sensor``."""
    return {v for (s, _), v in PRODUCT_DIRS.items() if s == sensor and v is not None}


def prefix_for_product_dir(sensor, product_dir_name):
    """Return ``ProgramData/<Sensor>/<product_dir_name>``.

    Used when the product directory name is already in hand (e.g. read off a local
    output path) and does not need to be looked up from a product token.
    """
    if sensor not in SENSOR_DIRS:
        raise KeyError(
            f"unknown sensor {sensor!r}; add it to SENSOR_DIRS in "
            f"shared_utils/product_paths.py"
        )
    return f"{PROGRAM_DATA_ROOT}/{SENSOR_DIRS[sensor]}/{product_dir_name}"


def product_output_dir(save_location, sensor, product, makedirs=True):
    """Return the directory under ``save_location`` that ``product`` is written into.

    Processors write locally into a directory named exactly as the published
    ``<Product>`` segment, so the upload step can key the file by its canonical
    ``ProgramData/<Sensor>/<Product>/`` destination without a second lookup.

    When the product's destination is still undecided the file keeps going to
    ``save_location`` unchanged and a warning names the open decision -- an
    undecided path should block a release, not a running job.
    """
    if not is_decided(sensor, product):
        note = UNDECIDED_NOTES.get((sensor, product), "")
        print(
            f"WARNING: no S3 product directory is decided for {sensor}/{product}; "
            f"writing to {save_location} instead. {note}"
        )
        return save_location
    out = os.path.join(save_location, product_dir(sensor, product))
    if makedirs:
        os.makedirs(out, exist_ok=True)
    return out


# ---------------------------------------------------------------------------
# Directory classification
#
# The merge step asks three questions about a product directory: is it the cloud
# mask (merge it first, and never mask it), is it an index (mask it if -mask was
# passed), is it a composite (keep the 8-bit nodata opt-out). Each processor used
# to answer them by hand off the directory basename -- a substring test for
# 'cloud' and two hard-coded lowercase sets. All three silently drift the moment a
# directory is renamed: 'cloud' in 'CloudMask'.lower() still works, but
# 'colorinfrared' in a literal set does not once the directory is ColorIR.
#
# These derive the answer from PRODUCT_DIRS instead, so a rename updates them.
# ---------------------------------------------------------------------------

#: Product tokens whose output is a spectral index (maskable).
INDEX_PRODUCT_TOKENS = {"NDVI", "NDWI", "MNDWI", "EVI", "NBR"}

#: Product tokens whose output is an 8-bit RGB composite.
COMPOSITE_PRODUCT_TOKENS = {
    "trueColor",
    "naturalColor",
    "shortwaveInfrared",
    "colorInfrared",
}

#: Product tokens whose output is a cloud mask.
CLOUD_MASK_PRODUCT_TOKENS = {"cloudMask"}


def _dirs_for_tokens(sensor, tokens):
    """Directory names for ``tokens`` that ``sensor`` actually publishes."""
    return {
        PRODUCT_DIRS[(sensor, t)]
        for t in tokens
        if PRODUCT_DIRS.get((sensor, t)) is not None
    }


def index_dirs(sensor):
    """Directory names under ``sensor`` holding a spectral index."""
    return _dirs_for_tokens(sensor, INDEX_PRODUCT_TOKENS)


def composite_dirs(sensor):
    """Directory names under ``sensor`` holding an 8-bit RGB composite."""
    return _dirs_for_tokens(sensor, COMPOSITE_PRODUCT_TOKENS)


def cloud_mask_dirs(sensor):
    """Directory names under ``sensor`` holding a cloud mask."""
    return _dirs_for_tokens(sensor, CLOUD_MASK_PRODUCT_TOKENS)


def _basename(path):
    return os.path.basename(os.path.normpath(path))


def is_index_dir(sensor, path):
    """True if ``path``'s leaf directory holds a maskable spectral index."""
    return _basename(path) in index_dirs(sensor)


def is_composite_dir(sensor, path):
    """True if ``path``'s leaf directory holds an 8-bit RGB composite."""
    return _basename(path) in composite_dirs(sensor)


def is_cloud_mask_dir(sensor, path):
    """True if ``path``'s leaf directory holds a cloud mask."""
    return _basename(path) in cloud_mask_dirs(sensor)
