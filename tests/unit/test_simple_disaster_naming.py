"""
Regression test for the simple_disaster_template notebooks' naming config cell.

Notebooks aren't linted or smoke-tested in CI (CLAUDE.md), and this cell is the
one an operator edits per activation — so this test EXTRACTS the real cell and
execs it, pinning the actual notebook code rather than a reimplementation.

What it guards: the cell used to carry 14 identical hand-rolled filename
builders whose date probe was `re.findall(r'\\d{8}')`. An already-stamped vendor
name has no 8-digit run (`2026-08-12T153802Z` is split by the hyphens, and the
time is 6 digits), so every one of them fell through to the "no date found"
branch, `f"{event_name}_{stem}_day.tif"` — which re-prefixed an event the name
already carried and appended `_day` to a name that already ended in a time:

    in : 202607_Fire_OR_SkySat_SR_TrueColor_2026-08-12T153802Z.tif
    out: 202607_Fire_OR_202607_Fire_OR_SkySat_SR_TrueColor_2026-08-12T153802Z_day.tif

The builders are now one delegation to shared_utils.file_naming, which handles
the mixed ISO stamp and is idempotent in both respects.

Pure Python: the cell imports only shared_utils.file_naming (no GDAL/rasterio).
"""
from pathlib import Path

import pytest

nbformat = pytest.importorskip("nbformat")
pytest.importorskip("shared_utils.file_naming")  # needs the package installed

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTEBOOKS = [
    REPO_ROOT / "notebooks" / "simple_disaster_template.ipynb",
    REPO_ROOT / "notebooks" / "simple_disaster_staging.ipynb",
    REPO_ROOT / "notebooks" / "testing-notebooks" / "simple_disaster_template.ipynb",
]

EVENT = "202607_Fire_OR"
# The exact filename from the observed bad run (a SkySat delivery already staged
# under the event, already carrying an ISO-Zulu acquisition stamp).
STAMPED = "202607_Fire_OR_SkySat_SR_TrueColor_2026-08-12T153802Z.tif"


def _naming_cell(nb_path):
    """The config cell that defines CATEGORIZATION_PATTERNS / FILENAME_CREATORS."""
    nb = nbformat.read(str(nb_path), as_version=4)
    cells = [
        c.source for c in nb.cells
        if c.cell_type == "code" and "FILENAME_CREATORS" in c.source
        and "CATEGORIZATION_PATTERNS = {" in c.source
    ]
    assert len(cells) == 1, f"expected exactly 1 naming config cell in {nb_path.name}, got {len(cells)}"
    return cells[0]


def _exec_naming_cell(nb_path):
    namespace = {}
    exec(compile(_naming_cell(nb_path), str(nb_path), "exec"), namespace)
    return namespace


@pytest.fixture(params=NOTEBOOKS, ids=lambda p: p.parent.name + "/" + p.name)
def nb_path(request):
    return request.param


@pytest.fixture
def ns(nb_path):
    return _exec_naming_cell(nb_path)


class TestNamingCellWiring:
    def test_every_category_has_a_builder(self, ns):
        assert set(ns["FILENAME_CREATORS"]) == set(ns["CATEGORIZATION_PATTERNS"])

    def test_every_category_has_an_output_dir(self, ns):
        # A missing entry KeyErrors mid-batch in the plan cell.
        assert set(ns["OUTPUT_DIRS"]) == set(ns["CATEGORIZATION_PATTERNS"])

    def test_builders_delegate_to_the_shared_module(self, ns):
        """No hand-rolled builders: every value comes from shared_utils.file_naming.

        Subset, not equality — the cell documents overriding ONE product by
        assigning a different builder to its key (create_nisar_filename is the
        worked example). What must not reappear is a locally-defined function.
        """
        from shared_utils.file_naming import (
            create_output_filename,
            create_nisar_filename,
        )
        shared = {create_output_filename, create_nisar_filename}
        assert set(ns["FILENAME_CREATORS"].values()) <= shared
        # The default for un-overridden categories is still the shared builder.
        assert create_output_filename in set(ns["FILENAME_CREATORS"].values())

    def test_hand_rolled_builders_are_gone(self, nb_path):
        # The duplicated per-product copies are what drifted from the convention.
        source = _naming_cell(nb_path)
        assert "def create_truecolor_filename" not in source
        assert "def extract_date_from_filename" not in source


class TestNamingCellOutputs:
    def test_already_named_source_is_unchanged(self, ns):
        build = ns["FILENAME_CREATORS"]["trueColor"]
        assert build(STAMPED, EVENT) == STAMPED

    def test_no_doubled_event_prefix(self, ns):
        build = ns["FILENAME_CREATORS"]["trueColor"]
        assert build(STAMPED, EVENT).count(EVENT) == 1

    def test_no_day_suffix_on_an_iso_zulu_name(self, ns):
        build = ns["FILENAME_CREATORS"]["trueColor"]
        assert not build(STAMPED, EVENT).endswith("_day.tif")

    def test_vendor_raw_name_still_gets_event_and_day(self, ns):
        build = ns["FILENAME_CREATORS"]["trueColor"]
        assert build("SkySat_SR_TrueColor_20260812.tif", EVENT) == \
            "202607_Fire_OR_SkySat_SR_TrueColor_2026-08-12_day.tif"

    def test_batch_is_idempotent(self, ns):
        """Re-running the notebook over its own output must be a no-op."""
        build = ns["FILENAME_CREATORS"]["trueColor"]
        for name in (STAMPED, "SkySat_SR_TrueColor_20260812.tif", "tc_20260812.tif"):
            once = build(name, EVENT)
            assert build(once, EVENT) == once


class TestNisarCategoryWiring:
    """The 'nisar' category routes interferometric PAIR products through
    create_nisar_filename instead of the single-date shared builder.

    NISAR GUNW names carry two dates (reference + secondary). The shared builder
    relocates only the first, promoting the REFERENCE date to the canonical
    trailing slot and stranding the secondary date mid-name, unhyphenated:

        NISAR_D54_GUNW_20260617_20260629_unw_..._cm.tif
          -> <EVENT>_NISAR_D54_GUNW_20260629_unw_..._cm_2026-06-17_day.tif
    """

    EVENT = "202606_Earthquake_Venezuela"
    PAIRS = [
        ("NISAR_D54_GUNW_20260617_20260629_unw_delon_deRamp_maskWater_cm.tif",
         "202606_Earthquake_Venezuela_NISAR_D54_GUNW_unw_delon_deRamp_maskWater_cm"
         "_2026-06-17_2026-06-29_day.tif"),
        ("NISAR_GUNW_20260613_20260625_unw_delon_deRamp_maskWater_cm-crop2.tif",
         "202606_Earthquake_Venezuela_NISAR_GUNW_unw_delon_deRamp_maskWater_cm-crop2"
         "_2026-06-13_2026-06-25_day.tif"),
        ("NISAR_GUNW_20260613_20260625_unw_delon_deRamp_maskWater_cm.tif",
         "202606_Earthquake_Venezuela_NISAR_GUNW_unw_delon_deRamp_maskWater_cm"
         "_2026-06-13_2026-06-25_day.tif"),
        ("NISAR_GUNW_A61_20260618_20260630_unw_delon_deRamp_maskWater_cm_clipped.tif",
         "202606_Earthquake_Venezuela_NISAR_GUNW_A61_unw_delon_deRamp_maskWater_cm_clipped"
         "_2026-06-18_2026-06-30_day.tif"),
    ]

    @pytest.fixture
    def nisar_ns(self, ns, nb_path):
        if "nisar" not in ns["CATEGORIZATION_PATTERNS"]:
            pytest.skip(f"{nb_path.name} carries no 'nisar' category")
        return ns

    def test_nisar_uses_the_pair_builder(self, nisar_ns):
        from shared_utils.file_naming import create_nisar_filename
        assert nisar_ns["FILENAME_CREATORS"]["nisar"] is create_nisar_filename

    def test_nisar_pattern_matches_the_delivered_names(self, nisar_ns):
        import re as _re
        pattern = nisar_ns["CATEGORIZATION_PATTERNS"]["nisar"]
        for src, _ in self.PAIRS:
            assert _re.search(pattern, src, _re.IGNORECASE), src

    def test_nisar_wins_over_the_optical_categories(self, nisar_ns):
        """First match wins, so a NISAR name must not be siphoned elsewhere."""
        import re as _re
        for src, _ in self.PAIRS:
            matched = next(
                cat for cat, pat in nisar_ns["CATEGORIZATION_PATTERNS"].items()
                if _re.search(pat, src, _re.IGNORECASE)
            )
            assert matched == "nisar", f"{src} categorized as {matched}"

    @pytest.mark.parametrize("src,expected", PAIRS)
    def test_both_dates_survive_in_order(self, nisar_ns, src, expected):
        build = nisar_ns["FILENAME_CREATORS"]["nisar"]
        assert build(src, self.EVENT) == expected

    def test_output_keys_stay_distinct(self, nisar_ns):
        """The crop/clip variants must not collapse onto one S3 key."""
        build = nisar_ns["FILENAME_CREATORS"]["nisar"]
        names = [build(src, self.EVENT) for src, _ in self.PAIRS]
        assert len(set(names)) == len(names)

    def test_nisar_batch_is_idempotent(self, nisar_ns):
        build = nisar_ns["FILENAME_CREATORS"]["nisar"]
        for src, _ in self.PAIRS:
            once = build(src, self.EVENT)
            assert build(once, self.EVENT) == once

    def test_nisar_has_an_output_dir_and_a_nodata_entry(self, nisar_ns):
        assert nisar_ns["OUTPUT_DIRS"]["nisar"]
        # 0 cm displacement is real data — it must never be the sentinel.
        assert nisar_ns["NODATA_VALUES"]["nisar"] != 0
