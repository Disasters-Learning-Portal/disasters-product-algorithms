"""
Regression tests for the two lean "publish to nasa-disasters-staging" notebooks:

    notebooks/tools/drcs_new_transfer.ipynb   sensor-first tree, event = filename prefix
    notebooks/tools/drcs_transfer.ipynb       event folder, raw non-COG deliveries

Both end in the same place (ProgramData/<product>/Output/, event in the tags, not
the name) and share their process/verify cells. Notebooks aren't importable, so the
find + plan cells are EXTRACTED and exec'd against a moto-mocked bucket -- that pins
the real code the operator runs, not a reimplementation. Offline: no GDAL, no S3.
"""
import os
import re
from pathlib import Path

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
nbformat = pytest.importorskip("nbformat")
pytest.importorskip("shared_utils.file_naming")  # needs the package installed

from moto import mock_aws

REPO_ROOT = Path(__file__).resolve().parents[2]
NB_NEW = REPO_ROOT / "notebooks" / "tools" / "drcs_new_transfer.ipynb"
NB_RAW = REPO_ROOT / "notebooks" / "tools" / "drcs_transfer.ipynb"
EVENT_PREFIX_RE = re.compile(r"^\d{6}_[A-Za-z0-9]+_[A-Za-z0-9]+_")


def _cell(nb_path, *markers):
    nb = nbformat.read(str(nb_path), as_version=4)
    hits = [c.source for c in nb.cells
            if c.cell_type == "code" and all(m in c.source for m in markers)]
    assert len(hits) == 1, f"{nb_path.name}: expected 1 cell with {markers}, got {len(hits)}"
    return hits[0]


def _exec(nb_path, ns, *markers):
    exec(compile(_cell(nb_path, *markers), f"{nb_path.name}:{markers[0]}", "exec"), ns)
    return ns


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")


def _config(nb_path):
    ns = _exec(nb_path, {}, "# ---- INPUTS ----")
    return _exec(nb_path, ns, "ACTIVATION_METADATA = {")


@pytest.mark.parametrize("nb_path", [NB_NEW, NB_RAW], ids=lambda p: p.stem)
class TestSharedContract:
    def test_destination_is_program_data_output(self, nb_path):
        cfg = _config(nb_path)
        assert cfg["DESTINATION_BUCKET"] == "nasa-disasters-staging"
        assert cfg["DESTINATION_TEMPLATE"].format(product="NISAR") == "ProgramData/NISAR/Output"
        assert cfg["SOURCE_BUCKET"] == "nasa-disasters"

    def test_metadata_carries_event_source_processor(self, nb_path):
        cfg = _config(nb_path)
        md = cfg["ACTIVATION_METADATA"]
        assert md["ACTIVATION_EVENT"] == cfg["EVENT_NAME"]
        assert md["SOURCE"] == cfg["SOURCE"]
        assert md["PROCESSOR"].startswith("NASA Disasters COG Processor")

    def test_process_cell_delegates_to_main_processor_with_tags(self, nb_path):
        cell = _cell(nb_path, "def _process(item):")
        assert "from shared_utils.main_processor import convert_to_cog" in cell
        assert "metadata=ACTIVATION_METADATA" in cell
        assert "target_crs=None" in cell
        assert "manual_nodata=NODATA" in cell
        # Write preflight runs before the batch, not inside the worker.
        assert cell.index("can_write_to_bucket(") < cell.index("def _process(item):")
        # An existing destination object is a skip, not a failure.
        assert "isinstance(out, FileExistsError)" in cell

    def test_nodata_input_spells_out_the_three_way_contract(self, nb_path):
        """`NODATA = None` alone reads as "no nodata", which is the OPPOSITE of what
        convert_to_cog does with it (inherit / auto-detect). The value that declares
        none -- and strips an inherited 0 off 8-bit imagery -- is False. Both must
        be spelled out where the operator sets it."""
        cell = _cell(nb_path, "# ---- INPUTS ----")
        assert "NODATA = None" in cell
        assert "NODATA = False" in cell
        assert "STRIP" in cell
        assert "manual_nodata=NODATA" in _cell(nb_path, "def _process(item):")

    def test_main_processor_forwards_nodata_by_identity(self, nb_path):
        """The notebooks hand NODATA to main_processor.convert_to_cog(manual_nodata=),
        which must pass it to cog_utils untouched -- a truthiness check there would
        turn False (declare none) into None (inherit)."""
        import inspect
        from shared_utils import main_processor
        src = inspect.getsource(main_processor.convert_to_cog)
        assert "nodata=manual_nodata," in src
        assert re.search(r"if\s+(not\s+)?manual_nodata\b", src) is None
        assert "manual_nodata or" not in src and "or manual_nodata" not in src

    def test_verify_cell_checks_every_required_tag(self, nb_path):
        cell = _cell(nb_path, "REQUIRED_TAGS = (")
        for tag in ("ACTIVATION_EVENT", "YEAR_MONTH", "HAZARD", "LOCATION", "SOURCE", "PROCESSOR"):
            assert tag in cell
        assert "raise RuntimeError" in cell


class TestSensorFirstTree:
    """drcs_activations_new/<Sensor>/<product>/<EVENT>_<stem>.tif"""

    KEYS = [
        "drcs_activations_new/Sentinel-2/colorIR/202409_Hurricane_Helene_S2A_colorInfrared_161001_T16RFT_2024-09-22_day.tif",
        "drcs_activations_new/Sentinel-2/colorIR/202409_hurricane_helene_S2B_colorInfrared_160939_T16RFU_2024-09-27_day.tif",
        "drcs_activations_new/Sentinel-2/colorIR/202312_Flood_NewEngland_S2B_colorInfrared_T18TYN_2023-12-20_day.tif",
        "drcs_activations_new/Sentinel-2/colorIR/202309_Hurricane_Idalia_pre_event_S2A_colorInfrared_T16RGS_2023-07-20_day.tif",
        "drcs_activations_new/Sentinel-2/NDVI/202409_Hurricane_Helene_S2A_NDVI_161001_T16RFT_2024-09-22_day.tif",
        "drcs_activations_new/Sentinel-2/colorIR/202409_Hurricane_Helene_notes.txt",
        "drcs_activations_new/Landsat/trueColor/202409_Hurricane_Helene_LC08_trueColor_2024-09-30_day.tif",
    ]

    def _seed(self):
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="nasa-disasters",
                         CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        for i, k in enumerate(self.KEYS):
            s3.put_object(Bucket="nasa-disasters", Key=k, Body=b"x" * (i + 1))

    def test_find_is_scoped_to_sensor_product_and_event(self, aws_env):
        with mock_aws():
            self._seed()
            ns = _config(NB_NEW)
            ns.update(EVENT_NAME="202409_Hurricane_Helene", SENSOR="Sentinel-2", PRODUCT="colorIR")
            _exec(NB_NEW, ns, "get_paginator('list_objects_v2')")
            names = [os.path.basename(k) for k, _ in ns["found"]]
            assert names == [
                "202409_Hurricane_Helene_S2A_colorInfrared_161001_T16RFT_2024-09-22_day.tif",
                "202409_hurricane_helene_S2B_colorInfrared_160939_T16RFU_2024-09-27_day.tif",
            ], "other events, the .txt, and other product folders must be excluded"

    def test_find_across_the_whole_tree(self, aws_env):
        with mock_aws():
            self._seed()
            ns = _config(NB_NEW)
            ns.update(EVENT_NAME="202409_Hurricane_Helene", SENSOR=None, PRODUCT=None)
            _exec(NB_NEW, ns, "get_paginator('list_objects_v2')")
            folders = sorted({os.path.dirname(k) for k, _ in ns["found"]})
            assert folders == ["drcs_activations_new/Landsat/trueColor",
                               "drcs_activations_new/Sentinel-2/NDVI",
                               "drcs_activations_new/Sentinel-2/colorIR"]
            assert len(ns["found"]) == 4

    def test_plan_strips_event_and_keeps_source_product_folder(self, aws_env):
        with mock_aws():
            self._seed()
            ns = _config(NB_NEW)
            ns.update(EVENT_NAME="202409_Hurricane_Helene", SENSOR=None, PRODUCT=None)
            _exec(NB_NEW, ns, "get_paginator('list_objects_v2')")
            _exec(NB_NEW, ns, "plan = []")
            dests = sorted(p["dest"] for p in ns["plan"])
            assert dests == [
                "ProgramData/NDVI/Output/S2A_NDVI_161001_T16RFT_2024-09-22_day.tif",
                "ProgramData/colorIR/Output/S2A_colorInfrared_161001_T16RFT_2024-09-22_day.tif",
                "ProgramData/colorIR/Output/S2B_colorInfrared_160939_T16RFU_2024-09-27_day.tif",
                "ProgramData/trueColor/Output/LC08_trueColor_2024-09-30_day.tif",
            ]
            for p in ns["plan"]:
                assert not EVENT_PREFIX_RE.match(p["new_name"])
                assert p["dest"] == f"{p['dest_prefix']}/{p['new_name']}"

    def test_empty_search_reports_the_prefix(self, aws_env, capsys):
        with mock_aws():
            self._seed()
            ns = _config(NB_NEW)
            ns.update(EVENT_NAME="202501_Fire_CA", SENSOR=None, PRODUCT=None)
            _exec(NB_NEW, ns, "get_paginator('list_objects_v2')")
            assert ns["found"] == []
            assert "aws s3 ls s3://nasa-disasters/drcs_activations_new/" in capsys.readouterr().out


class TestEventFolderTree:
    """drcs_activations/<EVENT>/<SUB_PRODUCT>/<stem>.tif"""

    KEYS = [
        "drcs_activations/202604_Typhoon_Sinlaku/Blackmarble/blackmarble_VNP46A2_2026-04-18_day.tif",
        "drcs_activations/202604_Typhoon_Sinlaku/Blackmarble/202604_Typhoon_Sinlaku_blackmarble_BRDF_2026-03_monthly.tif",
        "drcs_activations/202604_Typhoon_Sinlaku/Blackmarble/NISAR_D54_GUNW_20260617_20260629_unw_cm.TIF",
        "drcs_activations/202604_Typhoon_Sinlaku/Blackmarble/readme.txt",
        "drcs_activations/202604_Typhoon_Sinlaku/nisar/other_product.tif",
        "drcs_activations/202606_Earthquake_Venezuela/Blackmarble/blackmarble_VNP46A2_2026-06-01_day.tif",
    ]

    def _seed(self):
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="nasa-disasters",
                         CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        for i, k in enumerate(self.KEYS):
            s3.put_object(Bucket="nasa-disasters", Key=k, Body=b"x" * (i + 1))

    def test_find_lists_only_this_event_and_sub_product(self, aws_env):
        with mock_aws():
            self._seed()
            ns = _config(NB_RAW)
            ns.update(EVENT_NAME="202604_Typhoon_Sinlaku", SUB_PRODUCT="Blackmarble")
            _exec(NB_RAW, ns, "get_paginator('list_objects_v2')")
            names = [os.path.basename(k) for k, _ in ns["found"]]
            assert names == [
                "202604_Typhoon_Sinlaku_blackmarble_BRDF_2026-03_monthly.tif",
                "NISAR_D54_GUNW_20260617_20260629_unw_cm.TIF",   # uppercase .TIF kept, real key case
                "blackmarble_VNP46A2_2026-04-18_day.tif",
            ]

    def test_plan_strips_a_source_prefix_and_keeps_both_pair_dates(self, aws_env):
        with mock_aws():
            self._seed()
            ns = _config(NB_RAW)
            ns.update(EVENT_NAME="202604_Typhoon_Sinlaku", SUB_PRODUCT="Blackmarble", PRODUCT="BlackMarble")
            _exec(NB_RAW, ns, "get_paginator('list_objects_v2')")
            _exec(NB_RAW, ns, "plan = []")
            dests = sorted(p["dest"] for p in ns["plan"])
            assert dests == [
                "ProgramData/BlackMarble/Output/NISAR_D54_GUNW_unw_cm_2026-06-17_2026-06-29_day.tif",
                "ProgramData/BlackMarble/Output/blackmarble_BRDF_2026-03_monthly_day.tif",
                "ProgramData/BlackMarble/Output/blackmarble_VNP46A2_2026-04-18_day.tif",
            ]
            for p in ns["plan"]:
                assert not EVENT_PREFIX_RE.match(p["new_name"])

    def test_duplicate_output_names_are_refused(self, aws_env):
        """main_processor scratches to /tmp/cog_<new_name>; two items with one name
        would race under the thread pool."""
        with mock_aws():
            s3 = boto3.client("s3")
            s3.create_bucket(Bucket="nasa-disasters",
                             CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
            for k in ("drcs_activations/202604_Typhoon_Sinlaku/Blackmarble/a/x_20260418.tif",
                      "drcs_activations/202604_Typhoon_Sinlaku/Blackmarble/b/x_20260418.tif"):
                s3.put_object(Bucket="nasa-disasters", Key=k, Body=b"x")
            ns = _config(NB_RAW)
            ns.update(EVENT_NAME="202604_Typhoon_Sinlaku", SUB_PRODUCT="Blackmarble")
            _exec(NB_RAW, ns, "get_paginator('list_objects_v2')")
            with pytest.raises(RuntimeError, match="Duplicate output name"):
                _exec(NB_RAW, ns, "plan = []")
