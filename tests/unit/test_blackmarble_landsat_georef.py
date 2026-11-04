"""Black Marble's Landsat mosaic must put pixels where they belong (issue #186).

THE DEFECT THIS GUARDS
----------------------
Before veda-black-marble 114fa7f, upstream's `process_landsat_date` reprojected each band
tile into a WINDOW-sized array using the full-MOSAIC transform, then pasted the array into
the mosaic at the window offset -- applying the offset twice. With `MARGIN_PIXELS = 120`
that put every published product ~3.6 km north-west of truth (.clinerules.md rule 46).

It is the nastiest class of defect to catch downstream: the COG's transform is correct, its
bounds match the requested bbox, `cog_validate` passes, and in a viewer the layer lands over
the right city. Only the pixel CONTENT is displaced. Upstream fixed it in 114fa7f, and we
used to carry a monkeypatch shim for it; the shim is gone, so the only offline proof that
the PINNED upstream places pixels correctly is the placement test below.

It drives upstream's REAL `process_landsat_date` (not a re-implementation of its window
arithmetic -- a copy of the arithmetic would keep passing if upstream regressed) over two
synthetic UTM tiles carrying markers at known San Francisco coordinates, and asserts every
marker lands within TOLERANCE_M of truth. Modelled on upstream's own
tests/prepare/test_landsat_georef.py.
"""
import os

import numpy as np
import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(
    REPO_ROOT, "tests", "fixtures", "blackmarble_sf_misregistered_crop.tif"
)

BBOX = (-122.55, 37.69, -122.32, 37.81)
UTM = "EPSG:32610"
CLEAR_QA = 21824  # Landsat C2 QA_PIXEL: clear, low confidence for everything
FILL_QA = 1  # Landsat C2 QA_PIXEL bit 0: designated fill

# Each tile carries data only on its own side of SPLIT_LON, so the mosaic must take content
# from BOTH -- the multi-tile path is where the defect lived. The other side is flagged FILL
# in QA_PIXEL, exactly as a real Landsat scene flags its off-swath area. (Upstream's own
# version of this test leaves QA "clear" everywhere and relies on a bare 0 sample acting as
# nodata; under the GDAL in dps/environment.yml the warp rewrites that 0 to 1.4e-45 "to
# avoid being treated as NoData", the first tile then occupies the whole mosaic, and the
# second tile's markers never land -- a fixture artefact, not a placement result.)
SPLIT_LON = -122.457
MARKERS = {
    "tile_a": {"FerryBldg": (-122.3937, 37.7955), "TwinPeaks": (-122.4477, 37.7544)},
    "tile_b": {"OceanBeach": (-122.5107, 37.7600), "Presidio": (-122.4662, 37.7989)},
}

# The defect is measured in kilometres (~3600 m, or off the grid entirely), so the tolerance
# can be generous and still be decisive.
TOLERANCE_M = 200.0


def _write_tile(directory, name, markers, value0, east):
    """A synthetic Landsat tile: UTM 10N, 30 m, one labelled 5x5 blob per marker."""
    import rasterio
    from pyproj import Transformer
    from rasterio.transform import from_bounds

    fwd = Transformer.from_crs("EPSG:4326", UTM, always_xy=True)
    x0, y1 = fwd.transform(-122.80, 38.00)
    x1, y0 = fwd.transform(-122.10, 37.50)
    width, height = int((x1 - x0) / 30), int((y1 - y0) / 30)
    transform = from_bounds(x0, y0, x1, y1, width, height)

    band = np.full((height, width), 10000, dtype=np.uint16)
    qa = np.full((height, width), CLEAR_QA, dtype=np.uint16)
    split_col = int((~transform * fwd.transform(SPLIT_LON, 37.75))[0])
    empty = np.s_[:, :split_col] if east else np.s_[:, split_col:]
    band[empty] = 0
    qa[empty] = FILL_QA

    labels = {}
    for i, (marker, (lon, lat)) in enumerate(markers.items()):
        value = value0 + 1000 * i
        col, row = (~transform) * fwd.transform(lon, lat)
        band[int(row) - 2:int(row) + 3, int(col) - 2:int(col) + 3] = value
        labels[float(value)] = (marker, lon, lat)

    profile = {
        "driver": "GTiff", "dtype": "uint16", "width": width, "height": height,
        "count": 1, "crs": UTM, "transform": transform,
    }
    band_path = os.path.join(directory, f"{name}_SR_B5.TIF")
    qa_path = os.path.join(directory, f"{name}_QA_PIXEL.TIF")
    with rasterio.open(band_path, "w", **profile) as dst:
        dst.write(band, 1)
    with rasterio.open(qa_path, "w", **profile) as dst:
        dst.write(qa, 1)
    return band_path, qa_path, labels


def test_pinned_upstream_places_landsat_pixels_within_tolerance(tmp_path):
    """Every marker lands within TOLERANCE_M of its true coordinate in the band mosaic."""
    pytest.importorskip("blackmarble", reason="upstream blackmarble not installed in this env")
    from pyproj import Transformer

    from blackmarble.crs import create_processing_grid
    from blackmarble.prepare import process_landsat_date

    band_a, qa_a, labels_a = _write_tile(str(tmp_path), "tile_a", MARKERS["tile_a"], 20000, east=True)
    band_b, qa_b, labels_b = _write_tile(str(tmp_path), "tile_b", MARKERS["tile_b"], 30000, east=False)
    labels = {**labels_a, **labels_b}

    crs, transform, shape = create_processing_grid(BBOX, 30.0)
    result = process_landsat_date(
        bands_data={
            "QA_PIXEL": ([qa_a, qa_b], [], [], []),
            "B5": ([band_a, band_b], [], [], []),
        },
        bbox=BBOX,
        processing_crs=crs,
        reference_transform=transform,
        reference_shape=shape,
        apply_qa_mask=True,
        dilate_pixels=0,
    )
    mosaic, profile = result["B5"]
    assert profile["transform"] == transform, "the frame itself must be the processing grid"

    back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    errors = {}
    for value, (marker, true_lon, true_lat) in labels.items():
        hits = np.argwhere(np.isclose(mosaic, value))
        assert hits.size, f"{marker} is missing from the mosaic (shifted off the grid?)"
        row, col = hits.mean(axis=0)
        lon, lat = back.transform(*(transform * (col + 0.5, row + 0.5)))
        dx = (lon - true_lon) * 111_320.0 * np.cos(np.radians(true_lat))
        dy = (lat - true_lat) * 110_900.0
        errors[marker] = float(np.hypot(dx, dy))

    assert set(errors) == {"FerryBldg", "TwinPeaks", "OceanBeach", "Presidio"}
    misplaced = {m: round(e) for m, e in errors.items() if e >= TOLERANCE_M}
    assert not misplaced, (
        f"markers misplaced by more than {TOLERANCE_M:.0f} m (metres): {misplaced}. "
        "Has the veda-black-marble pin in dps/environment.yml regressed below 114fa7f?"
    )


def test_fixture_documents_the_shipped_defect():
    """A crop of a real DPS-published product from BEFORE the upstream fix, kept as evidence.

    Deliberately asserts only on things that make it a USEFUL reference, not on the
    misregistration itself -- the crop is frozen evidence, and a test that asserted "these
    pixels are wrong" would have to be deleted the day we regenerate it.
    """
    rasterio = pytest.importorskip("rasterio")

    assert os.path.exists(FIXTURE), "the reference crop is missing"
    assert os.path.getsize(FIXTURE) < 500_000, "fixtures must stay small (<500 KB)"

    with rasterio.open(FIXTURE) as src:
        # The unnamed per-bbox local Albers upstream builds -- no EPSG authority code.
        assert src.crs.to_epsg() is None
        assert src.crs.to_dict()["proj"] == "aea"
        assert src.count == 3 and src.dtypes[0] == "uint8"
        tags = src.tags()
        assert tags["ACTIVATION_EVENT"] == "202601_KyleWx_US"
        assert "georeferencing defect" in tags["FIXTURE_NOTE"]
