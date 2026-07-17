"""Web Mercator ±85° clip — the `needs_webmerc_clip` True branch + gdalwarp `-te` injection.

The regional (clip-NOT-needed) branch is already exercised by `test_landsat_merge_pipeline`
(a UTM-10N tile well inside the band). This covers the other half: a source whose geographic
latitude footprint exceeds Web Mercator's ±85.05113° valid band. Without the `-te` clamp,
warping such a source to EPSG:3857 either errors ("Point outside of projection domain") or
produces a multi-GB mostly-nodata canvas.

This is a PROJ/GDAL-version-sensitive path (`transform_bounds` for the detect, the warp extent
for the clamp) — exactly the class the GDAL 3.12 / py3.13 validation was about — so it lives in
the GDAL-execution suite.
"""

import shutil

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np
from rasterio.crs import CRS
from rasterio.transform import from_bounds

from shared_utils.reprojection import needs_webmerc_clip, WEBMERC_EXTENT_M

HAS_RIO = shutil.which("rio") is not None
HAS_GDALWARP = shutil.which("gdalwarp") is not None


@pytest.fixture
def high_latitude_geotiff(tmp_path):
    """32x32 uint8 raster in EPSG:4326 spanning lat -88..88 — past the ±85.05° band."""
    path = tmp_path / "highlat_4326.tif"
    data = np.random.randint(1, 255, (1, 32, 32), dtype=np.uint8)
    transform = from_bounds(-10.0, -88.0, 10.0, 88.0, 32, 32)
    with rasterio.open(
        str(path), "w", driver="GTiff", height=32, width=32, count=1,
        dtype="uint8", crs=CRS.from_epsg(4326), transform=transform, nodata=0,
    ) as dst:
        dst.write(data)
    return str(path)


@pytest.fixture
def regional_geotiff(tmp_path):
    """32x32 uint8 raster in EPSG:4326 well inside ±85° (control: clip NOT needed)."""
    path = tmp_path / "regional_4326.tif"
    data = np.random.randint(1, 255, (1, 32, 32), dtype=np.uint8)
    transform = from_bounds(-122.5, 37.5, -122.0, 38.0, 32, 32)
    with rasterio.open(
        str(path), "w", driver="GTiff", height=32, width=32, count=1,
        dtype="uint8", crs=CRS.from_epsg(4326), transform=transform, nodata=0,
    ) as dst:
        dst.write(data)
    return str(path)


# --------------------------------------------------------------------------- #
# needs_webmerc_clip contract (pure rasterio — no gdalwarp/rio CLI needed)
# --------------------------------------------------------------------------- #

def test_true_when_latitude_exceeds_band(high_latitude_geotiff):
    with rasterio.open(high_latitude_geotiff) as src:
        assert needs_webmerc_clip(src, "EPSG:3857") is True


def test_false_for_regional_source(regional_geotiff):
    with rasterio.open(regional_geotiff) as src:
        assert needs_webmerc_clip(src, "EPSG:3857") is False


def test_false_when_target_is_not_webmerc(high_latitude_geotiff):
    # A >±85° source is still not clipped when the target isn't Web Mercator.
    with rasterio.open(high_latitude_geotiff) as src:
        assert needs_webmerc_clip(src, "EPSG:4326") is False


def test_accepts_path_not_just_open_dataset(high_latitude_geotiff):
    # needs_webmerc_clip opens a path transiently — exercise that branch too.
    assert needs_webmerc_clip(high_latitude_geotiff, "EPSG:3857") is True


# --------------------------------------------------------------------------- #
# End-to-end: convert_to_cog auto-detects and injects the -te clamp
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not (HAS_RIO and HAS_GDALWARP), reason="needs rio + gdalwarp")
@pytest.mark.gdal
@pytest.mark.rio_cogeo
def test_convert_to_cog_clamps_high_latitude_to_webmerc_extent(high_latitude_geotiff, tmp_path):
    """A >±85° source warped to EPSG:3857 must yield a valid COG whose bounds are
    clamped to the Web Mercator square (±WEBMERC_EXTENT_M), not diverged toward ±inf."""
    from shared_utils.cog_utils import convert_to_cog, validate_cog

    out = tmp_path / "highlat_3857.tif"
    # clip_to_webmerc defaults to None -> auto-detect via needs_webmerc_clip.
    cog = convert_to_cog(high_latitude_geotiff, output_cog=str(out), dst_crs="EPSG:3857", quiet=True)

    is_valid, _ = validate_cog(cog)
    assert is_valid, "clipped high-latitude output must still be a valid COG"
    with rasterio.open(cog) as src:
        assert src.crs.to_epsg() == 3857
        eps = 1.0  # meters of pixel-alignment slack
        assert src.bounds.left >= -WEBMERC_EXTENT_M - eps
        assert src.bounds.right <= WEBMERC_EXTENT_M + eps
        assert src.bounds.bottom >= -WEBMERC_EXTENT_M - eps
        assert src.bounds.top <= WEBMERC_EXTENT_M + eps
