"""
Tests for the Umbra `list_dates` scene-discovery report.

Pins two things the DPS in-job date-discovery UX depends on:

1. `umbra_v2.report_umbra_scenes()` carries the **scene folder name** through to
   each returned dict (so the report can show which scene a `--date` maps to),
   groups objects by the scene subdir (`parts[2]`, as `retrieve_umbra_resources`
   does), keeps the newest `LastModified` per scene, skips subdirs without a
   parseable acquisition date, and sorts most-recently-delivered first.
2. The list-dates DPS tool (`dps/list_dates/report_dates.py`, `--sensor umbra`)
   prints the aligned table (with a `scene folder` column) AND writes the
   sortable `available_umbra_dates.csv` artifact into `--output` (on DPS that
   dir is uploaded and browsable from the Jobs panel). The per-sensor
   `process_umbra` CLI no longer carries a `--list_dates` flag.
"""

import csv
from datetime import datetime, timezone

import pytest

# umbra_v2 imports the geospatial stack at module load; skip cleanly without it.
pytest.importorskip("osgeo.gdal")
pytest.importorskip("rasterio")
pytest.importorskip("scipy")

from umbra import umbra_v2


def _dt(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# Umbra scene folder is parts[2] under the prefix and starts with the
# acquisition timestamp "%Y-%m-%d-%H-%M-%S" (see retrieve_umbra_resources).
SCENE_A = "2026-04-18-19-33-05_Umbra-05"  # newest delivery
SCENE_B = "2026-04-15-22-57-47_Umbra-05"
SCENE_BAD = "notadate_scene"  # no parseable acquisition token -> skipped


def test_report_umbra_scenes_carries_scene_and_sorts(monkeypatch):
    # Fake S3 listing: scene A has two objects (newest LastModified must win),
    # scene B one object, plus an unparseable subdir that must be dropped.
    fake_pairs = [
        (f"disasters/task1/{SCENE_A}/a1_GEC.tif", _dt(2026, 6, 10, 5, 0, 0)),
        (f"disasters/task1/{SCENE_A}/a2_GEC.tif", _dt(2026, 6, 10, 7, 11, 21)),
        (f"disasters/task2/{SCENE_B}/b1_GEC.tif", _dt(2026, 6, 10, 4, 54, 52)),
        (f"disasters/task3/{SCENE_BAD}/x.tif", _dt(2026, 6, 11, 0, 0, 0)),
    ]
    monkeypatch.setattr(
        umbra_v2, "retrieve_s3_file_list_with_timestamps",
        lambda bucket, prefix: fake_pairs,
    )

    scenes = umbra_v2.report_umbra_scenes()

    # unparseable subdir dropped
    assert len(scenes) == 2
    # every row exposes the scene folder name + copy-ready date
    assert {"date", "scene", "acquired", "added_to_s3"} <= set(scenes[0])
    # sorted newest-delivered first -> scene A on top
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
    # report_dates imports umbra_v2 lazily and calls umbra_v2.report_umbra_scenes,
    # so patch it on the source module.
    monkeypatch.setattr(
        umbra_v2, "report_umbra_scenes",
        lambda *a, **k: scenes,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["report_dates", "--sensor", "umbra", "--output", str(tmp_path)],
    )

    report_dates.main()

    # stdout table includes the new scene-folder column + a copy-ready date
    out = capsys.readouterr().out
    assert "scene folder" in out
    assert "2026-04-18 19:33:05" in out
    assert SCENE_A in out

    # CSV artifact written with header + one row per scene
    csv_path = tmp_path / "available_umbra_dates.csv"
    assert csv_path.exists()
    rows = list(csv.reader(csv_path.open()))
    assert rows[0] == ["date", "scene", "acquired_utc", "added_to_s3_utc"]
    assert rows[1] == ["2026-04-18 19:33:05", SCENE_A,
                       "2026-04-18 19:33:05", "2026-06-10 07:11:21"]
    assert len(rows) == 3  # header + 2 scenes
