"""
Tests for Capella/Umbra multi-scene handling.

Both sensors used to emit at most one COG per ``--date``: Capella pooled all
folders sharing a timestamp and ``sigmaCalib`` picked only the first ``_GEO_``
band, while Umbra hard-picked ``selected_subdir[...][0]``. A single acquisition
can appear under several folders (different processing levels -- e.g. ``_GEO_``
vs ``_SLC_``), and -- rarely -- two genuine scenes can share a timestamp.

The fix keeps ``retrieve_*_resources`` returning a flat pooled ``list[str]`` (so
the calib functions and notebooks are unaffected) and adds ``group_*_scenes``
helpers that split the pool into **one group per primary band** (``_GEO_`` for
Capella, ``_gec.tif`` for Umbra). These tests pin:

1. pooling still collects every matching folder's tifs;
2. grouping yields exactly one scene per primary band (unused levels dropped);
3. multiple same-timestamp scenes each become their own group (none dropped);
4. a pool with no primary band groups to nothing (caller raises, not IndexError).
"""

import pytest

# The *_v2 modules import the geospatial stack at load; skip cleanly without it.
pytest.importorskip("osgeo.gdal")
pytest.importorskip("rasterio")
pytest.importorskip("scipy")

from capella import capella_v2
from umbra import umbra_v2


# ---------------------------------------------------------------- Capella ----

# Same acquisition (identical split("_")[5] timestamp) under two levels.
CAP_GEO = "CAPELLA_C18_SM_GEO_HH_20260418193305_20260418193309"
CAP_SLC = "CAPELLA_C18_SM_SLC_HH_20260418193305_20260418193309"
# A second, distinct GEO acquisition that happens to share the timestamp.
CAP_GEO2 = "CAPELLA_C19_SM_GEO_HH_20260418193305_20260418193309"


def _cap_keys(*folders):
    return [f"disasters/{f}/rasters/{f}.tif" for f in folders]


def test_capella_pools_levels_but_groups_to_one_geo(monkeypatch):
    monkeypatch.setattr(
        capella_v2, "retrieve_s3_file_list",
        lambda bucket, prefix: _cap_keys(CAP_GEO, CAP_SLC),
    )

    tifs = capella_v2.retrieve_capella_resources("20260418193305", "bkt", "disasters")
    # both processing levels pooled, real-case keys preserved
    assert len(tifs) == 2
    assert all(t.startswith("s3://bkt/disasters/") for t in tifs)

    scenes = capella_v2.group_capella_scenes(tifs)
    # one GEO band -> one scene; the SLC-only level contributes nothing
    assert len(scenes) == 1
    assert scenes[0] == [t for t in tifs if "_GEO_" in t]


def test_capella_multiple_same_timestamp_scenes_all_kept(monkeypatch):
    monkeypatch.setattr(
        capella_v2, "retrieve_s3_file_list",
        lambda bucket, prefix: _cap_keys(CAP_GEO, CAP_GEO2, CAP_SLC),
    )

    tifs = capella_v2.retrieve_capella_resources("20260418193305", "bkt", "disasters")
    scenes = capella_v2.group_capella_scenes(tifs)

    # both genuine GEO acquisitions become their own group; none dropped
    assert len(scenes) == 2
    assert all(len(g) == 1 and "_GEO_" in g[0] for g in scenes)


def test_group_capella_scenes_empty_without_geo():
    # An SLC-only pool must group to nothing (caller raises instead of IndexError).
    assert capella_v2.group_capella_scenes(["s3://b/disasters/x_SLC_y/x.tif"]) == []


# ------------------------------------------------------------------ Umbra ----

UMB_A = "2026-04-18-19-33-05_UMBRA-05"
UMB_B = "2026-04-18-19-33-05_UMBRA-99"  # distinct scene, same timestamp prefix


def _umb_keys(subdir, *leaves):
    return [f"disasters/x/{subdir}/{leaf}" for leaf in leaves]


def test_umbra_pools_all_subdirs_and_groups_by_gec(monkeypatch):
    keys = (
        _umb_keys(UMB_A, f"{UMB_A}_GEC.tif", f"{UMB_A}_meta.tif")
        + _umb_keys(UMB_B, f"{UMB_B}_gec.tif")
    )
    monkeypatch.setattr(
        umbra_v2, "retrieve_s3_file_list",
        lambda bucket, prefix: keys,
    )

    tifs = umbra_v2.retrieve_umbra_resources("2026-04-18 19:33:05", "bkt", "disasters")
    # all matching subdirs pooled (old code took only the first)
    assert len(tifs) == 3

    scenes = umbra_v2.group_umbra_scenes(tifs)
    # one GEC per scene folder -> two scenes; the non-GEC companion is dropped;
    # matching is case-insensitive (_GEC.tif and _gec.tif both count)
    assert len(scenes) == 2
    assert all(len(g) == 1 and g[0].lower().endswith("_gec.tif") for g in scenes)


def test_group_umbra_scenes_empty_without_gec():
    assert umbra_v2.group_umbra_scenes(["s3://b/disasters/x/y_meta.tif"]) == []
