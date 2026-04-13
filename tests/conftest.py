import pytest
import sys
import types
import numpy as np
import os

# Ensure osgeo is available as a stub so that shared_utils.__init__ can
# import geotools without the GDAL C library being present.  This must
# happen before any shared_utils import.
if "osgeo" not in sys.modules:
    try:
        import osgeo  # noqa: F401
    except ImportError:
        _osgeo = types.ModuleType("osgeo")
        for _sub in ("osr", "gdal", "gdalconst"):
            _mod = types.ModuleType(f"osgeo.{_sub}")
            setattr(_osgeo, _sub, _mod)
            sys.modules[f"osgeo.{_sub}"] = _mod
        sys.modules["osgeo"] = _osgeo

# Only import rasterio if available
rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_bounds
from rasterio.crs import CRS


@pytest.fixture
def uint8_geotiff(tmp_path):
    """64x64 uint8 3-band RGB GeoTIFF in EPSG:32610, nodata=0"""
    path = tmp_path / "test_rgb.tif"
    data = np.random.randint(1, 255, (3, 64, 64), dtype=np.uint8)
    transform = from_bounds(500000, 4000000, 500640, 4000640, 64, 64)
    with rasterio.open(
        str(path), 'w', driver='GTiff',
        height=64, width=64, count=3, dtype='uint8',
        crs=CRS.from_epsg(32610), transform=transform, nodata=0
    ) as dst:
        dst.write(data)
    return str(path)


@pytest.fixture
def float32_geotiff(tmp_path):
    """64x64 float32 1-band GeoTIFF in EPSG:32610, nodata=-9999"""
    path = tmp_path / "test_float.tif"
    data = np.random.uniform(0.0, 1.0, (1, 64, 64)).astype(np.float32)
    data[0, 0:5, 0:5] = -9999.0  # Some nodata pixels
    transform = from_bounds(500000, 4000000, 500640, 4000640, 64, 64)
    with rasterio.open(
        str(path), 'w', driver='GTiff',
        height=64, width=64, count=1, dtype='float32',
        crs=CRS.from_epsg(32610), transform=transform, nodata=-9999.0
    ) as dst:
        dst.write(data)
    return str(path)


@pytest.fixture
def int16_geotiff(tmp_path):
    """64x64 int16 1-band GeoTIFF in EPSG:4326, nodata=-9999"""
    path = tmp_path / "test_int16.tif"
    data = np.random.randint(-100, 100, (1, 64, 64), dtype=np.int16)
    transform = from_bounds(-122.5, 37.5, -122.0, 38.0, 64, 64)
    with rasterio.open(
        str(path), 'w', driver='GTiff',
        height=64, width=64, count=1, dtype='int16',
        crs=CRS.from_epsg(4326), transform=transform, nodata=-9999
    ) as dst:
        dst.write(data)
    return str(path)


@pytest.fixture
def categorical_geotiff(tmp_path):
    """64x64 uint8 1-band with 'mask' in filename, nodata=255"""
    path = tmp_path / "test_cloud_mask.tif"
    data = np.random.choice([0, 1, 255], size=(1, 64, 64)).astype(np.uint8)
    transform = from_bounds(500000, 4000000, 500640, 4000640, 64, 64)
    with rasterio.open(
        str(path), 'w', driver='GTiff',
        height=64, width=64, count=1, dtype='uint8',
        crs=CRS.from_epsg(32610), transform=transform, nodata=255
    ) as dst:
        dst.write(data)
    return str(path)


@pytest.fixture
def epsg4326_geotiff(tmp_path):
    """64x64 uint8 3-band already in EPSG:4326"""
    path = tmp_path / "test_4326.tif"
    data = np.random.randint(1, 255, (3, 64, 64), dtype=np.uint8)
    transform = from_bounds(-122.5, 37.5, -122.0, 38.0, 64, 64)
    with rasterio.open(
        str(path), 'w', driver='GTiff',
        height=64, width=64, count=3, dtype='uint8',
        crs=CRS.from_epsg(4326), transform=transform, nodata=0
    ) as dst:
        dst.write(data)
    return str(path)
