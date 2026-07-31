"""
Tests for the Satellogic `list_dates` scene-discovery report.

Pins two things the DPS in-job date-discovery UX depends on:

1. `satellogic_v2.report_satellogic_scenes(level)` carries the **scene folder
   name** through to each returned dict, groups objects by the scene subdir
   (`parts[1]`, as `retrieve_satellogic_resources` does), **filters by processing
   level** (`_{level}_` in the folder name), keeps the newest `LastModified` per
   scene, skips subdirs without a parseable acquisition date, and sorts
   most-recently-delivered first.
2. The list-dates DPS tool (`dps/list_dates/report_dates.py`, `--sensor
   satellogic --level L1D`) prints the aligned table (with a `scene folder`
   column) AND writes the sortable `available_satellogic_dates.csv` artifact into
   `--output` (on DPS that dir is uploaded and browsable from the Jobs panel).
   The per-sensor `process_satellogic` CLI no longer carries a `--list_dates` flag.
"""

import csv
from datetime import datetime, timezone

import pytest

# satellogic_v2 imports the geospatial stack at module load; skip without it.
pytest.importorskip("osgeo.gdal")
pytest.importorskip("rasterio")

from satellogic import satellogic_v2


def _dt(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# Satellogic scene folder is parts[1] under the prefix, carries "_{level}_", and
# starts with "%Y%m%d_%H%M%S" (see retrieve_satellogic_resources).
SCENE_A = "20260418_193305_SATL_L1D_29"    # newest L1D delivery
SCENE_B = "20260415_225747_SATL_L1D_11"    # older L1D
SCENE_L1B = "20260420_101010_SATL_L1B_07"  # different level -> excluded for L1D
SCENE_BAD = "notadate_scene_L1D_00"        # right level, unparseable date -> skipped


def test_report_satellogic_scenes_filters_level_carries_scene_and_sorts(monkeypatch):
    # SCENE_L1B has the newest LastModified but the WRONG level -> must be
    # excluded when reporting L1D (proves the level filter, not just a sort).
    fake_pairs = [
        (f"disasters/{SCENE_A}/rasters/a1_analytic.tif", _dt(2026, 6, 10, 5, 0, 0)),
        (f"disasters/{SCENE_A}/rasters/a2_analytic.tif", _dt(2026, 6, 10, 7, 11, 21)),
        (f"disasters/{SCENE_B}/rasters/b1_analytic.tif", _dt(2026, 6, 10, 4, 54, 52)),
        (f"disasters/{SCENE_L1B}/rasters/c1_analytic.tif", _dt(2026, 6, 12, 0, 0, 0)),
        (f"disasters/{SCENE_BAD}/x.tif", _dt(2026, 6, 11, 0, 0, 0)),
    ]
    monkeypatch.setattr(
        satellogic_v2, "retrieve_s3_file_list_with_timestamps",
        lambda bucket, prefix: fake_pairs,
    )

    scenes = satellogic_v2.report_satellogic_scenes("L1D")

    # wrong-level scene + unparseable subdir dropped -> only the two L1D scenes
    assert len(scenes) == 2
    assert all("_L1D_" in s["scene"] for s in scenes)
    assert {"date", "scene", "acquired", "added_to_s3"} <= set(scenes[0])
    # sorted newest-delivered first (among the level-matched scenes)
    assert scenes[0]["scene"] == SCENE_A
    assert scenes[1]["scene"] == SCENE_B
    # --date is the acquisition time formatted exactly as the CLI expects
    assert scenes[0]["date"] == "2026-04-18 19:33:05"
    assert scenes[0]["acquired"] == datetime(2026, 4, 18, 19, 33, 5)
    # newest LastModified across the scene's objects wins
    assert scenes[0]["added_to_s3"] == _dt(2026, 6, 10, 7, 11, 21)


def test_list_dates_prints_table_and_writes_csv(tmp_path, monkeypatch, capsys, report_dates):
    scenes = [
        {"date": "2026-04-18 19:33:05", "scene": SCENE_A,
         "acquired": datetime(2026, 4, 18, 19, 33, 5),
         "added_to_s3": _dt(2026, 6, 10, 7, 11, 21)},
        {"date": "2026-04-15 22:57:47", "scene": SCENE_B,
         "acquired": datetime(2026, 4, 15, 22, 57, 47),
         "added_to_s3": _dt(2026, 6, 10, 4, 54, 52)},
    ]
    # report_dates imports satellogic_v2 lazily and calls
    # satellogic_v2.report_satellogic_scenes(level), so patch it on the source.
    monkeypatch.setattr(
        satellogic_v2, "report_satellogic_scenes",
        lambda *a, **k: scenes,
    )
    # --level is required for satellogic (the report is level-scoped).
    monkeypatch.setattr(
        "sys.argv",
        ["report_dates", "--sensor", "satellogic", "--level", "L1D",
         "--output", str(tmp_path)],
    )

    report_dates.main()

    # stdout table includes the new scene-folder column + a copy-ready date
    out = capsys.readouterr().out
    assert "scene folder" in out
    assert "2026-04-18 19:33:05" in out
    assert SCENE_A in out

    # CSV artifact written with header + one row per scene
    csv_path = tmp_path / "available_satellogic_dates.csv"
    assert csv_path.exists()
    rows = list(csv.reader(csv_path.open()))
    assert rows[0] == ["date", "scene", "acquired_utc", "added_to_s3_utc"]
    assert rows[1] == ["2026-04-18 19:33:05", SCENE_A,
                       "2026-04-18 19:33:05", "2026-06-10 07:11:21"]
    assert len(rows) == 3  # header + 2 scenes
