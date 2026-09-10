"""
Regression test for notebooks/simple_disaster_staging.ipynb.

That notebook is the ONE-PASS variant of simple_disaster_template.ipynb: raw
non-COG source -> COG with activation tags -> final location in
nasa-disasters-staging. The two things it exists to guarantee are exactly the two
things a copy-paste edit would quietly undo:

  1. There is no `drcs_activations_new` hop. The whole point is that an operator
     does not run a second notebook afterwards.
  2. The activation event is in the GeoTIFF tags and the S3 prefix, NEVER in the
     filename -- so no output name may carry a YYYYMM_Hazard_Location_ prefix,
     including one the SOURCE arrived with.

The naming config cell itself is covered by test_simple_disaster_naming.py, which
parametrizes over this notebook too. This file pins the config + plumbing around
it. Source-level only: no AWS, no moto, no GDAL.
"""
import os
import re
from pathlib import Path

import pytest

nbformat = pytest.importorskip("nbformat")
pytest.importorskip("shared_utils.file_naming")  # needs the package installed

REPO_ROOT = Path(__file__).resolve().parents[2]
NB_PATH = REPO_ROOT / "notebooks" / "simple_disaster_staging.ipynb"

# The shape this notebook must never emit at the head of an output name.
EVENT_PREFIX_RE = re.compile(r"^\d{6}_[A-Za-z0-9]+_[A-Za-z0-9]+_")


def _cells(cell_type=None):
    nb = nbformat.read(str(NB_PATH), as_version=4)
    return [c for c in nb.cells if cell_type is None or c.cell_type == cell_type]


def _cell_containing(*markers):
    """The single code cell containing every marker string."""
    hits = [c.source for c in _cells("code") if all(m in c.source for m in markers)]
    assert len(hits) == 1, f"expected exactly 1 cell with {markers}, got {len(hits)}"
    return hits[0]


@pytest.fixture(scope="module")
def config():
    """The INPUTS cell, exec'd. Pure Python -- it only assigns constants."""
    namespace = {}
    exec(compile(_cell_containing("DESTINATION_BUCKET = 'nasa-disasters-staging'", "NAME_EVENT_PREFIX"),
                 str(NB_PATH), "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def naming():
    """The categorization/naming config cell, exec'd."""
    namespace = {}
    exec(compile(_cell_containing("FILENAME_CREATORS", "CATEGORIZATION_PATTERNS = {"),
                 str(NB_PATH), "exec"), namespace)
    return namespace


class TestDestination:
    def test_writes_to_the_staging_bucket(self, config):
        assert config["DESTINATION_BUCKET"] == "nasa-disasters-staging"

    def test_reads_from_the_raw_activation_tree(self, config):
        assert config["SOURCE_BUCKET"] == "nasa-disasters"
        assert config["GEOTIFF_DIR"] == "drcs_activations"

    def test_destination_template_is_program_data_output(self, config):
        assert config["DESTINATION_TEMPLATE"].format(product="NISAR") == \
            "ProgramData/NISAR/Output"

    def test_no_intermediate_hop_in_any_code_cell(self):
        """`drcs_activations_new` is the thing this notebook deletes.

        Markdown may still name it -- the header cell explains how this template
        differs from simple_disaster_template.ipynb -- but no code may.
        """
        offenders = [
            i for i, c in enumerate(_cells())
            if c.cell_type == "code" and "drcs_activations_new" in c.source
        ]
        assert not offenders, f"drcs_activations_new survives in code cells {offenders}"

    def test_uploads_through_a_boto3_client_not_the_aws_cli(self):
        """Destination writes go through upload_to_s3 with a boto3 client.

        Ambient credentials on purpose (2026-08-26): on the Disasters hub the pod
        already assumes disasters-prod, so anyone with hub access can publish.
        The previous initialize_s3_client route tried an STS assume-role from
        aws_credentials.py -- a gitignored file absent from every fresh pod -- and
        then fell through to these same ambient credentials silently.
        """
        cell = _cell_containing("def _process(item):")
        assert "upload_to_s3(" in cell
        assert "boto3.client('s3')" in cell
        assert "aws', 's3', 'cp', f's3://{DESTINATION_BUCKET}" not in cell

    def test_upload_errors_are_not_swallowed(self):
        """upload_to_s3 gates every print, including the real boto3 error, on
        `verbose`. With verbose=False an AccessDenied surfaced only as the
        generic "upload failed" and the cause was lost."""
        cell = _cell_containing("def _process(item):")
        # Assert on the CALL, not the cell -- the comment above it names
        # verbose=False to explain what went wrong before.
        call = next(
            ln for ln in cell.splitlines()
            if "upload_to_s3(" in ln and not ln.lstrip().startswith("#")
        )
        assert "verbose=True" in call, f"upload_to_s3 call not verbose: {call.strip()}"

    def test_write_access_is_preflighted_before_any_conversion(self):
        """These scenes take minutes each and _process deletes its /tmp files in
        `finally`, so discovering AccessDenied at the upload throws away the whole
        batch. head_bucket/fsspec both succeed read-only, so probe a real write."""
        cell = _cell_containing("def _process(item):")
        assert "can_write_to_bucket(" in cell
        # Must run before the batch, not inside the worker.
        assert cell.index("can_write_to_bucket(") < cell.index("def _process(item):")


class TestEventStaysOutOfTheFilename:
    def test_name_event_prefix_is_empty(self, config):
        assert config["NAME_EVENT_PREFIX"] == ""

    def test_no_builder_adds_a_prefix(self, naming):
        """Every category, called with '', must emit an unprefixed name."""
        from shared_utils.file_naming import strip_event_prefix  # noqa: F401  (import guard)

        sources = {
            "nisar": "NISAR_D54_GUNW_20260617_20260629_unw_deIon_cm.tif",
            "trueColor": "SkySat_SR_TrueColor_20260812.tif",
            "ndvi": "Sentinel2_NDVI_20260101.tif",
        }
        for category, builder in naming["FILENAME_CREATORS"].items():
            source = sources.get(category, f"Sensor_{category}_20260101.tif")
            out = builder(source, "")
            assert not EVENT_PREFIX_RE.match(out), f"{category} emitted {out}"

    def test_a_source_that_arrives_event_prefixed_is_stripped(self, naming):
        """The notebook runs strip_event_prefix() before the builder.

        Without it the canonical-stem short-circuit in create_output_filename
        returns an already-stamped vendor name verbatim -- prefix and all.
        """
        from shared_utils.file_naming import strip_event_prefix

        event = "202606_Earthquake_Venezuela"
        cases = [
            # (source name, category)
            ("202606_Earthquake_Venezuela_NISAR_D54_GUNW_20260617_20260629_unw_cm.tif", "nisar"),
            ("202607_Fire_OR_SkySat_SR_TrueColor_2026-08-12T153802Z.tif", "trueColor"),
            ("202606_earthquake_venezuela_Sentinel2_NDVI_20260101.tif", "ndvi"),
        ]
        for source, category in cases:
            out = naming["FILENAME_CREATORS"][category](
                strip_event_prefix(os.path.basename(source), event), ""
            )
            assert not EVENT_PREFIX_RE.match(out), f"{source} -> {out}"

    def test_plan_cell_calls_the_stripper_with_event_name(self):
        """EVENT_NAME must be passed, not just the generic shape match.

        It is the only thing that can strip an event whose location token
        contains underscores (202508_Flood_New_Mexico).
        """
        cell = _cell_containing("processing_plan = []", "strip_event_prefix")
        assert "strip_event_prefix(filename, EVENT_NAME)" in cell
        assert "FILENAME_CREATORS[category](source_stem, NAME_EVENT_PREFIX)" in cell


class TestTempFilePaths:
    """Nothing adds an event prefix any more, so `new_name` can equal `filename`.

    `/tmp/{filename}` as the download target and `/tmp/{new_name}` as the COG
    target would then be ONE path, and convert_to_cog would read and write it.
    The same collision hits two sources that share a basename across the four
    worker threads. Both are namespaced by the plan index.
    """

    def test_plan_items_carry_an_index(self):
        assert "'idx': len(processing_plan)" in _cell_containing("processing_plan = []",
                                                                 "strip_event_prefix")

    def test_temp_paths_are_namespaced_by_index(self):
        cell = _cell_containing("def _process(item):")
        assert "local_input = f\"/tmp/{item['idx']}_src_{filename}\"" in cell
        assert "local_cog = f\"/tmp/{item['idx']}_cog_{new_name}\"" in cell

    def test_download_and_cog_targets_differ_for_an_identity_rename(self, naming):
        """The concrete case: a source already in canonical, unprefixed form."""
        from shared_utils.file_naming import strip_event_prefix

        filename = "SkySat_SR_TrueColor_2026-08-12_day.tif"
        new_name = naming["FILENAME_CREATORS"]["trueColor"](
            strip_event_prefix(filename, "202606_Earthquake_Venezuela"), ""
        )
        assert new_name == filename, "precondition: this source renames to itself"
        idx = 0
        assert f"/tmp/{idx}_src_{filename}" != f"/tmp/{idx}_cog_{new_name}"


class TestActivationTags:
    def test_metadata_dict_is_built_from_event_name(self):
        cell = _cell_containing('"ACTIVATION_EVENT": EVENT_NAME')
        assert "PROCESSOR_STRING" in cell

    def test_metadata_reaches_convert_to_cog(self):
        """With the event out of the filename, the tags are the only record of it."""
        cell = _cell_containing("metadata=ACTIVATION_METADATA")
        assert "convert_to_cog(" in cell

    def test_verify_cell_checks_every_required_tag(self):
        cell = _cell_containing("REQUIRED_TAGS = (")
        for tag in ("ACTIVATION_EVENT", "YEAR_MONTH", "HAZARD", "LOCATION",
                    "SOURCE", "PROCESSOR"):
            assert tag in cell


def test_every_code_cell_compiles():
    for i, c in enumerate(_cells("code")):
        compile(c.source, f"{NB_PATH.name}:cell{i}", "exec")
