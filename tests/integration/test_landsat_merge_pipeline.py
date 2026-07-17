"""Integration tests for the Landsat merge path (ls_merge -> gen_merge -> COG).

Synthesizes small multi-scene GeoTIFFs and drives the real merge code, covering:

  - D2: the merged filename's date is the acquisition date for both fresh
        per-scene inputs (LC08_product_YYYYMMDD_HHMMSS_PPPRRR.tif) and
        already-renamed inputs (LC08_product_HHMMSS_PPPRRR_YYYY-MM-DD_day.tif).
  - D3: a re-merge excludes any prior `*_merged*.tif` (it is not folded back in),
        and an empty directory raises a clear error instead of IndexError.
  - End-to-end save: convert_to_cog on the merged TIF yields one valid COG with
        the expected name in both native and EPSG:3857, inputs are not mutated,
        and no temp files are left in /tmp.

Needs rasterio (Python) for ls_merge/gen_merge; the COG steps additionally need
the `rio` CLI and (for the 3857 warp) `gdalwarp`.
"""

import glob
import hashlib
import os
import shutil

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np
from rasterio.crs import CRS
from rasterio.transform import from_bounds

HAS_RIO = shutil.which("rio") is not None
HAS_GDALWARP = shutil.which("gdalwarp") is not None


def _make_scene(path, ox, oy, fill=None, epsg=32610, size=32, span=320):
    """Write a tiny 3-band uint8 GeoTIFF at origin (ox, oy)."""
    if fill is None:
        data = np.random.randint(1, 255, (3, size, size), dtype=np.uint8)
    else:
        data = np.full((3, size, size), fill, dtype=np.uint8)
    transform = from_bounds(ox, oy, ox + span, oy + span, size, size)
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=3,
        dtype="uint8", crs=CRS.from_epsg(epsg), transform=transform, nodata=0,
    ) as dst:
        dst.write(data)


def _digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# --------------------------------------------------------------------------- #
# D2 — merged filename date is the acquisition date, not a positional token
# --------------------------------------------------------------------------- #

def test_fresh_per_scene_merge_name(tmp_path):
    from landsat.landsat89_functions import ls_merge
    d = tmp_path / "trueColor"
    d.mkdir()
    _make_scene(str(d / "LC08_trueColor_20250922_185617_046028.tif"), 500000, 4000000)
    _make_scene(str(d / "LC08_trueColor_20250922_190001_046029.tif"), 500300, 4000000)
    out = ls_merge(str(d))
    # A merged mosaic is day-granularity: only the acquisition DATE is carried
    # into the raw merge name (no time; path/row dropped). rename_with_event
    # appends the _day suffix downstream.
    assert os.path.basename(out) == "LC08_trueColor_merged_2025-09-22.tif"
    assert os.path.exists(out)


def test_renamed_inputs_merge_name_uses_date_not_time(tmp_path):
    """D2 regression: ls_merge must read the acquisition DATE from an already-
    renamed scene (..._HHMMSS_PPPRRR_YYYY-MM-DD_day.tif) via the shared
    file_naming extractor -- neither crashing nor mistaking the time for the date."""
    from landsat.landsat89_functions import ls_merge
    d = tmp_path / "trueColor"
    d.mkdir()
    _make_scene(str(d / "LC08_trueColor_185617_046028_2025-09-22_day.tif"), 500000, 4000000)
    _make_scene(str(d / "LC08_trueColor_190001_046029_2025-09-22_day.tif"), 500300, 4000000)
    out = ls_merge(str(d))
    assert os.path.basename(out) == "LC08_trueColor_merged_2025-09-22.tif"


# --------------------------------------------------------------------------- #
# D3 — re-merge excludes prior merged output; empty dir raises clearly
# --------------------------------------------------------------------------- #

def test_remerge_excludes_prior_merged(tmp_path):
    from landsat.landsat89_functions import ls_merge
    d = tmp_path / "trueColor"
    d.mkdir()
    _make_scene(str(d / "LC08_trueColor_20250922_185617_046028.tif"), 500000, 4000000)
    _make_scene(str(d / "LC08_trueColor_20250922_190001_046029.tif"), 500300, 4000000)
    # A prior merged output far outside the scene footprint. If it were folded
    # back in, the new mosaic's bounds would balloon to include it.
    _make_scene(str(d / "LC08_trueColor_20250922_merged.tif"), 900000, 9000000, fill=222)

    out = ls_merge(str(d))
    assert os.path.basename(out) == "LC08_trueColor_merged_2025-09-22.tif"
    with rasterio.open(out) as src:
        # union of the two real scenes only (x in [500000, 500620]); the far-away
        # prior merged tile at x=900000 must NOT have widened the output.
        assert src.bounds.right < 600000


def test_empty_dir_raises_filenotfound(tmp_path):
    from landsat.landsat89_functions import ls_merge
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(FileNotFoundError):
        ls_merge(str(d))


def test_multicrs_merge_does_not_mutate_inputs(tmp_path):
    """A CRS-mismatched scene must be reprojected to a temp file, never in place.

    Reopening the source path in 'w' mode used to overwrite the operator's
    native-CRS input scene, permanently destroying it.
    """
    from landsat.landsat89_functions import ls_merge
    import glob as _glob

    d = tmp_path / "mNDWI"
    d.mkdir()
    a = str(d / "LC08_mNDWI_20250922_185617_046028.tif")
    b = str(d / "LC08_mNDWI_20250922_190001_046029.tif")
    c = str(d / "LC08_mNDWI_20250922_190500_046030.tif")
    _make_scene(a, 500000, 4000000, epsg=32610, size=16, span=160)
    _make_scene(b, 500160, 4000000, epsg=32610, size=16, span=160)
    _make_scene(c, 500000, 4000000, epsg=32611, size=16, span=160)  # different UTM zone
    before = {p: _digest(p) for p in (a, b, c)}

    out = ls_merge(str(d))

    # Every source scene is byte-identical after the merge.
    assert {p: _digest(p) for p in (a, b, c)} == before
    assert os.path.exists(out)
    # The reprojection temp dir is cleaned up.
    assert not _glob.glob("/tmp/ls_merge_reproj_*")


# --------------------------------------------------------------------------- #
# End-to-end: merged TIF -> COG save, in both native and Web Mercator
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not HAS_RIO, reason="rio CLI not available")
@pytest.mark.rio_cogeo
def test_merge_then_cog_native_preserves_inputs_and_cleans_tmp(tmp_path):
    from landsat.landsat89_functions import ls_merge
    from shared_utils.cog_utils import convert_to_cog, validate_cog

    d = tmp_path / "trueColor"
    d.mkdir()
    s1 = str(d / "LC08_trueColor_20250922_185617_046028.tif")
    s2 = str(d / "LC08_trueColor_20250922_190001_046029.tif")
    _make_scene(s1, 500000, 4000000)
    _make_scene(s2, 500300, 4000000)
    before = {s1: _digest(s1), s2: _digest(s2)}

    merged = ls_merge(str(d))
    cog = convert_to_cog(merged, dst_crs=None, quiet=True)  # native: no warp

    assert os.path.basename(cog) == "LC08_trueColor_merged_2025-09-22.tif"
    is_valid, _ = validate_cog(cog)
    assert is_valid
    # Source scenes must not be mutated by the merge (same-CRS path).
    assert {s1: _digest(s1), s2: _digest(s2)} == before
    # No temp residue from the COG step.
    assert not glob.glob("/tmp/*.cog.tmp.tif")
    assert not glob.glob("/tmp/*.warped.tmp.tif")


@pytest.mark.skipif(not (HAS_RIO and HAS_GDALWARP), reason="needs rio + gdalwarp")
@pytest.mark.gdal
@pytest.mark.rio_cogeo
def test_merge_then_cog_webmercator(tmp_path):
    from landsat.landsat89_functions import ls_merge
    from shared_utils.cog_utils import convert_to_cog, validate_cog

    d = tmp_path / "NDVI"
    d.mkdir()
    _make_scene(str(d / "LC08_NDVI_20250922_185617_046028.tif"), 500000, 4000000)
    _make_scene(str(d / "LC08_NDVI_20250922_190001_046029.tif"), 500300, 4000000)

    merged = ls_merge(str(d))
    cog = convert_to_cog(merged, dst_crs="EPSG:3857", quiet=True)

    is_valid, _ = validate_cog(cog)
    assert is_valid
    with rasterio.open(cog) as src:
        assert src.crs.to_epsg() == 3857
