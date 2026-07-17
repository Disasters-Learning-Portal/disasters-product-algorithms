"""Regression tests for Landsat tile-vs-merge output filenames.

Covers the shared rename helpers used by both the tile (per-scene) and merge
(mosaic) paths of the Landsat pipeline:

  - get_final_filename  -- predicts the post-rename name (used for skip checks)
  - rename_with_event   -- performs the actual rename

These two MUST stay in lockstep: the skip check is meaningless if it predicts a
different name than the rename produces. The merged-product cases are the D4
regression: a Sentinel-2 merged file used to drop its product token because the
old code hard-coded `parts[1] if date_index > 1 else parts[2]`.

Pure-Python (no GDAL); rename_with_event only needs a file to exist on disk.
"""

import os
import pytest

from shared_utils.cog_utils import get_final_filename, rename_with_event

# (input filename, expected final basename) for individual per-scene products.
# Individual scenes carry a real acquisition time, so they end in a full ISO 8601
# Zulu datetime (`...T HH:MM:SS Z`) -- `_day` is reserved for merged mosaics and
# time-less products. The date+time pair is relocated to the end; every other
# token (product, path/row, tile) is preserved in order.
INDIVIDUAL_CASES = [
    ("LC08_trueColor_20250922_185617_046028.tif",
     "LC08_trueColor_046028_2025-09-22T18:56:17Z.tif"),
    ("LC09_NDVI_20250101_120000_012034.tif",
     "LC09_NDVI_012034_2025-01-01T12:00:00Z.tif"),
    ("S2B_MSIL2A_colorInfrared_20251111_161419_T17RLN.tif",
     "S2B_MSIL2A_colorInfrared_T17RLN_2025-11-11T16:14:19Z.tif"),
    # waterExtent carries an NSTD token and has NO acquisition time, so it stays
    # on the day-granularity `_day` form (there is no HH:MM:SS to build a Z stamp).
    ("LC08_waterExtent_NSTD_1_5_20250922.tif",
     "LC08_waterExtent_NSTD_1_5_2025-09-22_day.tif"),
]

# (input filename, expected final basename) for merged mosaics.
MERGED_CASES = [
    ("LC08_trueColor_20250922_merged.tif",
     "LC08_trueColor_merged_2025-09-22_day.tif"),
    ("LC09_NDWI_20250101_merged.tif",
     "LC09_NDWI_merged_2025-01-01_day.tif"),
    # D4 regression: multi-token sensor id must keep the real product token.
    ("S2B_MSIL2A_colorInfrared_20251111_merged.tif",
     "S2B_MSIL2A_colorInfrared_merged_2025-11-11_day.tif"),
]

ALL_CASES = INDIVIDUAL_CASES + MERGED_CASES


@pytest.mark.parametrize("name,expected", ALL_CASES)
def test_get_final_filename_predicts_expected(name, expected):
    assert os.path.basename(get_final_filename(f"/out/{name}", "202509_Flood_X")) == expected


@pytest.mark.parametrize("name,expected", ALL_CASES)
def test_rename_with_event_produces_expected(name, expected, tmp_path):
    src = tmp_path / name
    src.write_bytes(b"dummy")
    result = rename_with_event(str(src), "202509_Flood_X", quiet=True)
    assert os.path.exists(result)
    assert os.path.basename(result) == expected


@pytest.mark.parametrize("name,_expected", ALL_CASES)
def test_predictor_and_rename_stay_in_lockstep(name, _expected, tmp_path):
    """get_final_filename must predict exactly what rename_with_event does."""
    predicted = os.path.basename(get_final_filename(f"/anything/{name}", "EVT"))
    src = tmp_path / name
    src.write_bytes(b"dummy")
    actual = os.path.basename(rename_with_event(str(src), "EVT", quiet=True))
    assert predicted == actual


def test_sentinel2_merged_keeps_product_token():
    """D4: the product token must survive on a merged multi-token sensor id."""
    out = get_final_filename("/x/S2B_MSIL2A_colorInfrared_20251111_merged.tif", "EVT")
    assert "colorInfrared" in os.path.basename(out)


def test_event_name_never_prefixed():
    """Event prefix was dropped 2026-06-16; it must not reappear in any name."""
    for name, _ in ALL_CASES:
        assert "202509_Flood_X" not in os.path.basename(
            get_final_filename(f"/x/{name}", "202509_Flood_X")
        )
