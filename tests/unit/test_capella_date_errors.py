"""
Capella ``--date`` failures must say what went wrong.

An operator ran ``process_capella --date 2026092727161906`` for a scene the
vendor had not delivered yet and got ``ValueError: unconverted data remains:
1906`` from deep inside ``strptime``, which said nothing about the real
problem. These tests make sure:

- a malformed ``--date`` raises a ValueError that names the expected format;
- a well-formed ``--date`` with no matching scene raises a FileNotFoundError
  that says the scene may not be delivered yet and lists the nearest dates. It
  used to fall back to the *closest* scene, which silently processed a
  different acquisition;
- an empty bucket raises a clear error instead of ``min() arg is an empty
  sequence``.
"""

import pytest

pytest.importorskip("osgeo.gdal")
pytest.importorskip("rasterio")
pytest.importorskip("scipy")

from capella import capella_v2

SCENE_A = "CAPELLA_C05_SP_GEO_HH_20260418193305_20260418193315"
SCENE_B = "CAPELLA_C05_SP_GEO_HH_20260415225747_20260415225801"
SCENE_BAD = "CAPELLA_BAD"  # unparseable folder must not crash the lookup


def _listing(monkeypatch, *subdirs):
    keys = [f"disasters/{s}/{s}.tif" for s in subdirs]
    monkeypatch.setattr(capella_v2, "retrieve_s3_file_list", lambda b, p: keys)


@pytest.mark.parametrize("bad", [
    "2026092727161906",  # the value from the field report: 16 digits
    "20260418",          # date only
    "2026-04-18 19:33:05",
    "2026041819330",     # 13 digits: strptime alone would accept this one
    "20261318193305",    # month 13
])
def test_malformed_date_names_the_format(monkeypatch, bad):
    _listing(monkeypatch, SCENE_A)
    with pytest.raises(ValueError, match=r"YYYYMMDDHHMMSS") as exc:
        capella_v2.retrieve_capella_resources(bad, "bkt", "disasters")
    assert "unconverted data" not in str(exc.value)


def test_undelivered_scene_raises_with_nearest_dates(monkeypatch):
    _listing(monkeypatch, SCENE_A, SCENE_B, SCENE_BAD)
    with pytest.raises(FileNotFoundError) as exc:
        capella_v2.retrieve_capella_resources("20260927161906", "bkt", "disasters")
    msg = str(exc.value)
    assert "20260927161906" in msg
    assert "not have delivered it yet" in msg
    assert "s3://bkt/disasters/" in msg
    # nearest available dates listed so the operator can pick a real one
    assert "20260418193305" in msg and "20260415225747" in msg


def test_near_miss_does_not_silently_pick_another_scene(monkeypatch):
    _listing(monkeypatch, SCENE_A)
    with pytest.raises(FileNotFoundError):
        capella_v2.retrieve_capella_resources("20260418193306", "bkt", "disasters")


def test_empty_bucket_raises_clear_error(monkeypatch):
    _listing(monkeypatch)
    with pytest.raises(FileNotFoundError, match="No Capella scenes found"):
        capella_v2.retrieve_capella_resources("20260418193305", "bkt", "disasters")


def test_exact_date_still_resolves(monkeypatch):
    _listing(monkeypatch, SCENE_A, SCENE_B, SCENE_BAD)
    tifs = capella_v2.retrieve_capella_resources("20260418193305", "bkt", "disasters")
    assert tifs == [f"s3://bkt/disasters/{SCENE_A}/{SCENE_A}.tif"]


def test_cli_exits_with_message_not_traceback(monkeypatch, tmp_path):
    from capella import process_capella

    meta = tmp_path / "meta.json"
    meta.write_text('{"ACTIVATION_EVENT": "202609_Flood_TX"}')
    _listing(monkeypatch, SCENE_A)
    monkeypatch.setattr("sys.argv", [
        "process_capella", "--date", "20260927161906",
        "--metadata-json", str(meta),
    ])
    with pytest.raises(SystemExit) as exc:
        process_capella.main()
    # sys.exit(str) -> exit status 1 with the message on stderr
    assert str(exc.value.code).startswith("ERROR: No Capella scene for --date 20260927161906")
