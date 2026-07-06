"""Runtime smoke tests for the per-sensor cloud-mask code path.

Why this exists
---------------
The ``lint.yml`` ``cli-smoke`` job only runs ``<cli> --help`` (plus a bare
``import``). ``--help`` is short-circuited by argparse *before* any processing
runs, so a runtime regression inside a product-generation module -- a
``NameError``, or a broken ``if mask is not None: apply_cloud_mask(...)``
block -- is invisible to CI. These tests import the two function modules the
mask logic lives in and actually *execute* ``apply_cloud_mask``, so that class
of regression fails CI.

Scope / known gap
-----------------
``process_landsat89.py`` and ``process_sentinel2.py`` run entirely under
``if __name__ == "__main__":`` (there is no importable ``main()``), so their
argv-driven dispatch loop -- where e.g. ``cloudMask`` is referenced -- is *not*
covered here. Exercising it would need a full synthetic scene fixture
(bands + QA + MTL) or refactoring those scripts to expose ``main()``. This file
covers the importable ``*_functions`` modules, which is where the cloud-mask
edits were made.
"""

import importlib

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np
from rasterio.crs import CRS
from rasterio.transform import from_bounds

# The two modules whose cloud-mask blocks the notebook/DPS refactor touched.
FUNC_MODULES = ["landsat.landsat89_functions", "sentinel2.sentinel2_functions"]

_SIZE = 16
_TRANSFORM = from_bounds(500000, 4000000, 500000 + _SIZE * 30, 4000000 + _SIZE * 30, _SIZE, _SIZE)
_CRS = CRS.from_epsg(32610)


def _real_gdal():
    """True only when the real osgeo.gdal C bindings are present.

    Under tests/conftest.py the module is stubbed when GDAL is absent, so the
    single-band path (which routes through gdal-based dump_geotiff) is skipped
    locally but still runs in CI, where lint.yml installs GDAL from conda.
    """
    try:
        from osgeo import gdal
    except Exception:
        return False
    drv = getattr(gdal, "GetDriverByName", None)
    try:
        return callable(drv) and drv("GTiff") is not None
    except Exception:
        return False


def _write_rgb(path):
    data = np.random.randint(1, 255, (3, _SIZE, _SIZE), dtype=np.uint8)
    with rasterio.open(path, "w", driver="GTiff", height=_SIZE, width=_SIZE, count=3,
                       dtype="uint8", crs=_CRS, transform=_TRANSFORM, nodata=0) as dst:
        dst.write(data)


def _write_index(path):
    data = np.random.uniform(-1.0, 1.0, (1, _SIZE, _SIZE)).astype("float32")
    with rasterio.open(path, "w", driver="GTiff", height=_SIZE, width=_SIZE, count=1,
                       dtype="float32", crs=_CRS, transform=_TRANSFORM, nodata=-9999.0) as dst:
        dst.write(data)


def _write_mask(path):
    m = np.zeros((_SIZE, _SIZE), dtype=np.uint8)
    m[: _SIZE // 2, :] = 1  # top half flagged as cloud
    with rasterio.open(path, "w", driver="GTiff", height=_SIZE, width=_SIZE, count=1,
                       dtype="uint8", crs=_CRS, transform=_TRANSFORM, nodata=0) as dst:
        dst.write(m, 1)


@pytest.mark.parametrize("modname", FUNC_MODULES)
def test_function_module_imports(modname):
    """A syntax/indentation/NameError at module load fails here (`--help` won't)."""
    mod = importlib.import_module(modname)
    assert callable(getattr(mod, "apply_cloud_mask", None))


@pytest.mark.parametrize("modname", FUNC_MODULES)
def test_apply_cloud_mask_threeband_runs(modname, tmp_path):
    """3-band (color) mask path is pure rasterio -> runs everywhere.

    Writes a 4-band RGBA output whose alpha band is zeroed under the cloud.
    """
    mod = importlib.import_module(modname)
    tif = tmp_path / "trueColor" / "prod.tif"
    tif.parent.mkdir(parents=True)
    mask = tmp_path / "cloudMask" / "mask.tif"
    mask.parent.mkdir(parents=True)
    _write_rgb(str(tif))
    _write_mask(str(mask))

    mod.apply_cloud_mask(str(tif), str(mask))

    out = tif.parent / "masked" / "prod_masked.tif"
    assert out.exists(), "masked output was not written"
    with rasterio.open(out) as ds:
        assert ds.count == 4, "expected a 4-band RGBA (alpha) output"
        alpha = ds.read(4)
    assert (alpha == 0).any(), "cloud pixels should be zeroed in the alpha band"
    assert (alpha != 0).any(), "clear pixels should remain in the alpha band"


@pytest.mark.skipif(not _real_gdal(), reason="real osgeo.gdal (dump_geotiff) unavailable; runs in CI where GDAL is installed")
@pytest.mark.parametrize("modname", FUNC_MODULES)
def test_apply_cloud_mask_singleband_runs(modname, tmp_path):
    """1-band (index) mask path -- the branch still used after the refactor."""
    mod = importlib.import_module(modname)
    tif = tmp_path / "NDVI" / "prod.tif"
    tif.parent.mkdir(parents=True)
    mask = tmp_path / "cloudMask" / "mask.tif"
    mask.parent.mkdir(parents=True)
    _write_index(str(tif))
    _write_mask(str(mask))

    mod.apply_cloud_mask(str(tif), str(mask))

    out = tif.parent / "masked" / "prod_masked.tif"
    assert out.exists(), "masked index output was not written"
    with rasterio.open(out) as ds:
        assert ds.count == 1
