"""
Regression test for notebooks/bake_event_metadata.ipynb — the S3 bulk
activation-event metadata baker.

Notebooks aren't importable modules, so this test EXTRACTS the notebook's code
cells and execs them against a moto-mocked S3 bucket. It therefore pins the REAL
notebook code (scan/classify, in-place rename+delete, idempotency, CSV bulk-fix),
not a reimplementation — if someone edits the notebook and breaks the contract,
this fails.

Offline-only: uses `moto` (mock_aws). GDAL /vsis3 can't be intercepted by moto
(it patches botocore, not libcurl), so we point GDAL at a dead endpoint to force
`read_event_s3` down its boto3 fallback — which is the same tag-reading path that
runs in production when /vsis3 is unavailable, and the authoritative idempotency
check lives inside `bake_s3` from the downloaded bytes regardless.
"""
import csv
from pathlib import Path

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
pytest.importorskip("osgeo")                      # notebook -> shared_utils.cog_metadata imports osgeo.gdal
pytest.importorskip("shared_utils.cog_metadata")  # needs the package installed (pip install -e .)
nbformat = pytest.importorskip("nbformat")

from moto import mock_aws
from rasterio.io import MemoryFile

REPO_ROOT = Path(__file__).resolve().parents[2]
NB_PATH = REPO_ROOT / "notebooks" / "bake_event_metadata.ipynb"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "gaia_atlanta_sample.tif"

BUCKET = "test-disasters-bucket"
PREFIX = "drcs_activations_new/202510_Flood_AK/s2/"

# The notebook's 5 code cells, in order.
CFG, SCAN, REPORT, EXECUTE, BULKFIX = [
    c.source for c in nbformat.read(str(NB_PATH), as_version=4).cells if c.cell_type == "code"
]

FIXTURE_BYTES = FIXTURE.read_bytes()
with rasterio.open(str(FIXTURE)) as _src:
    ORIG_PIXELS = _src.read()

ALL_NAMES = [
    "202510_Flood_AK_gaia_2025-01-11.tif",  # matched -> gaia_2025-01-11.tif
    "randomscene.tif",                      # no event pattern
    "202511_Fire_CA_dup.tif",               # both strip to dup.tif -> collision
    "202512_Storm_TX_dup.tif",
]
MATCHED_KEY = PREFIX + "202510_Flood_AK_gaia_2025-01-11.tif"
NEW_KEY = PREFIX + "gaia_2025-01-11.tif"
RND_KEY = PREFIX + "randomscene.tif"
DUPA_KEY = PREFIX + "202511_Fire_CA_dup.tif"
DUPB_KEY = PREFIX + "202512_Storm_TX_dup.tif"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    # moto creds + force GDAL /vsis3 to fail fast (connection refused) so
    # read_event_s3 exercises its boto3 fallback under moto.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_S3_ENDPOINT", "localhost:1")   # GDAL var; boto3/moto ignore it
    monkeypatch.setenv("AWS_HTTPS", "NO")
    monkeypatch.setenv("AWS_VIRTUAL_HOSTING", "FALSE")
    monkeypatch.setenv("GDAL_HTTP_MAX_RETRY", "0")
    monkeypatch.setenv("GDAL_HTTP_CONNECTTIMEOUT", "1")


def _run(cells, dry, csv_path):
    """Exec the given notebook code cells in a fresh namespace, overriding config."""
    ns = {}
    exec(CFG, ns)
    ns["INPUT"] = "s3://{}/{}".format(BUCKET, PREFIX)
    ns["DRY_RUN"] = dry
    ns["CSV_PATH"] = str(csv_path)
    ns["SCAN_WORKERS"] = 2
    ns["PROCESS_WORKERS"] = 2
    for c in cells:
        exec(c, ns)
    return ns


def _seed(s3):
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    for n in ALL_NAMES:
        s3.put_object(Bucket=BUCKET, Key=PREFIX + n, Body=FIXTURE_BYTES)


def _exists(s3, key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


def _obytes(s3, key):
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _tags(b):
    with MemoryFile(b) as m, m.open() as s_:
        return s_.tags()


def _pixels(b):
    with MemoryFile(b) as m, m.open() as s_:
        return s_.read()


def _plain_geotiff():
    # A non-COG GeoTIFF (untiled, no overviews) -> cog_validate returns False. Returns (bytes, data).
    from rasterio.transform import from_bounds
    from rasterio.crs import CRS
    data = np.random.RandomState(0).uniform(0, 1, (1, 1024, 1024)).astype("float32")
    prof = dict(driver="GTiff", height=1024, width=1024, count=1, dtype="float32",
                crs=CRS.from_epsg(3857), transform=from_bounds(0, 0, 1024, 1024, 1024, 1024),
                nodata=-9999.0, tiled=False, compress="deflate")
    with MemoryFile() as mf:
        with mf.open(**prof) as dst:
            dst.write(data)
        return mf.read(), data


@mock_aws
def test_scan_classifies_and_dry_run_is_readonly(tmp_path):
    s3 = boto3.client("s3", region_name="us-west-2")
    _seed(s3)
    ns = _run([SCAN, REPORT, EXECUTE], dry=True, csv_path=tmp_path / "needs_event.csv")

    assert {m["new_key"] for m in ns["matched"]} == {NEW_KEY}
    assert ns["matched"][0]["event"] == "202510_Flood_AK"
    reasons = {u["key"]: u["reason"] for u in ns["unmatched"]}
    assert reasons[RND_KEY] == "no_event_pattern"
    assert reasons[DUPA_KEY] == "name_collision"
    assert reasons[DUPB_KEY] == "name_collision"
    assert ns["already_baked"] == []
    assert ns["matched"][0]["is_cog"] is True   # gaia fixture is already a valid COG
    # dry run must touch nothing in S3
    assert ns["results"] == []
    assert not _exists(s3, NEW_KEY)
    assert _exists(s3, MATCHED_KEY)
    assert (tmp_path / "needs_event.csv").exists()


@mock_aws
def test_execute_bakes_renames_and_keeps_cog_identical(tmp_path):
    s3 = boto3.client("s3", region_name="us-west-2")
    _seed(s3)
    ns = _run([SCAN, REPORT, EXECUTE], dry=False, csv_path=tmp_path / "needs_event.csv")

    assert [r["status"] for r in ns["results"]] == ["success"]
    assert _exists(s3, NEW_KEY)              # renamed (prefix stripped)
    assert not _exists(s3, MATCHED_KEY)      # original deleted
    b = _obytes(s3, NEW_KEY)
    t = _tags(b)
    assert t["ACTIVATION_EVENT"] == "202510_Flood_AK"
    assert t["YEAR_MONTH"] == "202510" and t["HAZARD"] == "Flood" and t["LOCATION"] == "AK"
    with MemoryFile(b) as m, m.open() as s_:
        assert str(s_.crs) == "EPSG:3857"
        assert s_.dtypes == ("float32",)
        assert s_.nodatavals == (-9999.0,)
    assert np.array_equal(_pixels(b), ORIG_PIXELS)   # pixels bit-identical
    from shared_utils.cog_metadata import validate_cog_in_memory
    assert validate_cog_in_memory(b, "n.tif")[0]     # still a valid COG
    # unmatched + collision objects left untouched
    assert _exists(s3, RND_KEY) and _exists(s3, DUPA_KEY) and _exists(s3, DUPB_KEY)


@mock_aws
def test_idempotent_rerun_skips_already_baked(tmp_path):
    s3 = boto3.client("s3", region_name="us-west-2")
    _seed(s3)
    csvp = tmp_path / "needs_event.csv"
    _run([SCAN, REPORT, EXECUTE], dry=False, csv_path=csvp)          # first pass bakes gaia
    ns = _run([SCAN, REPORT, EXECUTE], dry=False, csv_path=csvp)     # second pass

    assert NEW_KEY in {a["key"] for a in ns["already_baked"]}
    assert all(m["new_key"] != NEW_KEY for m in ns["matched"])
    assert np.array_equal(_pixels(_obytes(s3, NEW_KEY)), ORIG_PIXELS)


@mock_aws
def test_bulk_fix_bakes_in_place_from_csv(tmp_path):
    s3 = boto3.client("s3", region_name="us-west-2")
    _seed(s3)
    csvp = tmp_path / "needs_event.csv"
    _run([SCAN, REPORT, EXECUTE], dry=False, csv_path=csvp)          # scan writes CSV, bakes gaia

    rows = list(csv.DictReader(open(csvp, newline="")))
    for r in rows:
        if r["filename"] == "randomscene.tif":
            r["ACTIVATION_EVENT"] = "202601_Quake_PR"
    with open(csvp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["bucket", "key", "filename", "reason", "cog", "ACTIVATION_EVENT"])
        w.writeheader()
        w.writerows(rows)

    ns = _run([SCAN, REPORT, EXECUTE, BULKFIX], dry=False, csv_path=csvp)

    assert [r["status"] for r in ns["fix_results"]] == ["success"]
    assert _exists(s3, RND_KEY)                                      # key unchanged (event in metadata only)
    rb = _obytes(s3, RND_KEY)
    assert _tags(rb)["ACTIVATION_EVENT"] == "202601_Quake_PR"
    assert np.array_equal(_pixels(rb), ORIG_PIXELS)
    from shared_utils.cog_metadata import validate_cog_in_memory
    assert validate_cog_in_memory(rb, "r.tif")[0]
    # blank-event dup rows were skipped, not baked
    assert _tags(_obytes(s3, DUPA_KEY)).get("ACTIVATION_EVENT") is None


@mock_aws
def test_non_cog_input_flagged_and_converted(tmp_path):
    from shared_utils.cog_metadata import validate_cog_in_memory
    plain_bytes, data = _plain_geotiff()
    assert not validate_cog_in_memory(plain_bytes, "p.tif")[0]   # precondition: input is NOT a COG
    s3 = boto3.client("s3", region_name="us-west-2")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    src_key = PREFIX + "202510_Flood_AK_plain_2025-01-11.tif"
    new_key = PREFIX + "plain_2025-01-11.tif"
    s3.put_object(Bucket=BUCKET, Key=src_key, Body=plain_bytes)

    # dry scan flags it as not-a-COG
    dns = _run([SCAN, REPORT, EXECUTE], dry=True, csv_path=tmp_path / "c.csv")
    assert [m["is_cog"] for m in dns["matched"]] == [False]

    # execute converts it to a COG, in place (renamed, old key deleted)
    ns = _run([SCAN, REPORT, EXECUTE], dry=False, csv_path=tmp_path / "c.csv")
    assert [r["status"] for r in ns["results"]] == ["success"]
    assert ns["results"][0]["converted"] is True
    assert _exists(s3, new_key) and not _exists(s3, src_key)
    b = _obytes(s3, new_key)
    assert validate_cog_in_memory(b, "n.tif")[0]                 # output is now a valid COG
    assert _tags(b)["ACTIVATION_EVENT"] == "202510_Flood_AK"
    assert np.array_equal(_pixels(b), data)                      # pixels bit-identical
