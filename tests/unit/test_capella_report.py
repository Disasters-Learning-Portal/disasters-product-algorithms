"""
Tests for the Capella `list_dates` scene-discovery report.

Pins two things the DPS in-job date-discovery UX depends on:

1. `capella_v2.report_capella_scenes()` carries the **scene folder name**
   through to each returned dict (added so the report can show which scene a
   `--date` maps to), keeps the newest `LastModified` per scene, skips subdirs
   without a parseable acquisition date, and sorts most-recently-delivered first.
2. The list-dates DPS tool (`dps/list_dates/report_dates.py`, `--sensor capella`)
   prints the aligned table (with a `scene folder` column) AND writes the
   sortable `available_capella_dates.csv` artifact into `--output` (on DPS that
   dir is uploaded and browsable from the Jobs panel). The per-sensor
   `process_capella` CLI no longer carries a `--list_dates` flag.
"""

import csv
from datetime import datetime, timezone

import pytest

# capella_v2 imports the geospatial stack at module load; skip cleanly without it.
pytest.importorskip("osgeo.gdal")
pytest.importorskip("rasterio")
pytest.importorskip("scipy")

from capella import capella_v2


def _dt(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# Two real-shape Capella scene folders (acquisition date is split("_")[5]).
SCENE_A = "CAPELLA_C05_SP_GEO_HH_20260418193305_20260418193315"  # newest delivery
SCENE_B = "CAPELLA_C05_SP_GEO_HH_20260415225747_20260415225801"
SCENE_BAD = "CAPELLA_BAD"  # no parseable acquisition token -> skipped


def test_report_capella_scenes_carries_scene_and_sorts(monkeypatch):
    # Fake S3 listing: scene A has two objects (newest LastModified must win),
    # scene B one object, plus an unparseable subdir that must be dropped.
    fake_pairs = [
        (f"disasters/{SCENE_A}/rasters/a1.tif", _dt(2026, 6, 10, 5, 0, 0)),
        (f"disasters/{SCENE_A}/rasters/a2.tif", _dt(2026, 6, 10, 7, 11, 21)),
        (f"disasters/{SCENE_B}/rasters/b1.tif", _dt(2026, 6, 10, 4, 54, 52)),
        (f"disasters/{SCENE_BAD}/x.tif", _dt(2026, 6, 11, 0, 0, 0)),
    ]
    monkeypatch.setattr(
        capella_v2, "retrieve_s3_file_list_with_timestamps",
        lambda bucket, prefix: fake_pairs,
    )

    scenes = capella_v2.report_capella_scenes()

    # unparseable subdir dropped
    assert len(scenes) == 2
    # every row exposes the scene folder name + copy-ready date
    assert {"date", "scene", "acquired", "added_to_s3"} <= set(scenes[0])
    # sorted newest-delivered first -> scene A on top
    assert scenes[0]["scene"] == SCENE_A
    assert scenes[1]["scene"] == SCENE_B
    # --date is parsed from the folder's 6th token
    assert scenes[0]["date"] == "20260418193305"
    assert scenes[0]["acquired"] == datetime(2026, 4, 18, 19, 33, 5)
    # newest LastModified across the scene's objects wins
    assert scenes[0]["added_to_s3"] == _dt(2026, 6, 10, 7, 11, 21)


def test_list_dates_prints_table_and_writes_csv(tmp_path, monkeypatch, capsys, report_dates):
    scenes = [
        {"date": "20260418193305", "scene": SCENE_A,
         "acquired": datetime(2026, 4, 18, 19, 33, 5),
         "added_to_s3": _dt(2026, 6, 10, 7, 11, 21)},
        {"date": "20260415225747", "scene": SCENE_B,
         "acquired": datetime(2026, 4, 15, 22, 57, 47),
         "added_to_s3": _dt(2026, 6, 10, 4, 54, 52)},
    ]
    # report_dates imports capella_v2 lazily and calls
    # capella_v2.report_capella_scenes, so patch it on the source module.
    monkeypatch.setattr(
        capella_v2, "report_capella_scenes",
        lambda *a, **k: scenes,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["report_dates", "--sensor", "capella", "--output", str(tmp_path)],
    )

    report_dates.main()

    # stdout table includes the new scene-folder column + a copy-ready date
    out = capsys.readouterr().out
    assert "scene folder" in out
    assert "20260418193305" in out
    assert SCENE_A in out

    # CSV artifact written with header + one row per scene
    csv_path = tmp_path / "available_capella_dates.csv"
    assert csv_path.exists()
    rows = list(csv.reader(csv_path.open()))
    assert rows[0] == ["date", "scene", "acquired_utc", "added_to_s3_utc"]
    assert rows[1] == ["20260418193305", SCENE_A,
                       "2026-04-18 19:33:05", "2026-06-10 07:11:21"]
    assert len(rows) == 3  # header + 2 scenes
