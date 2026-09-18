"""The contract between what a processor writes and what the uploader publishes.

`test_product_paths.py` covers the destination table in isolation. This file covers
the join: that every directory a processor can actually create is one the uploader
recognizes, and that the merge step's three directory classifications still answer
correctly after the camelCase -> PascalCase rename.

That join is the part with teeth. The upload key is derived from the product
directory on disk, so a processor writing into a directory `product_dirs_for()`
does not know about does not fail -- it silently falls back to the old
`<dest_prefix>/<relpath>` key and the product lands in the wrong place.
"""

import ast
import os
import re

import pytest

from shared_utils.product_paths import (
    PRODUCT_DIRS,
    cloud_mask_dirs,
    composite_dirs,
    index_dirs,
    is_cloud_mask_dir,
    is_composite_dir,
    is_index_dir,
    product_dir,
    product_dirs_for,
)

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")

PROCESSORS = {
    "landsat": os.path.join(SRC, "landsat", "process_landsat89.py"),
    "sentinel2": os.path.join(SRC, "sentinel2", "process_sentinel2.py"),
}
FUNCTION_MODULES = {
    "landsat": os.path.join(SRC, "landsat", "landsat89_functions.py"),
    "sentinel2": os.path.join(SRC, "sentinel2", "sentinel2_functions.py"),
}
FLAT_WRITERS = {
    "satellogic": os.path.join(SRC, "satellogic", "satellogic_v2.py"),
    "umbra": os.path.join(SRC, "umbra", "umbra_v2.py"),
    "capella": os.path.join(SRC, "capella", "capella_v2.py"),
    "iceye": os.path.join(SRC, "iceye", "iceye_v2.py"),
}


def _source(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _product_lookups(path):
    """Every literal ``product_dir(...)`` / ``product_output_dir(...)`` call in a file.

    Returns ``(sensor, product)`` pairs, read off the AST so a renamed literal
    cannot hide from this test the way a regex would let it.
    """
    found = []
    for node in ast.walk(ast.parse(_source(path))):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if name == "product_dir":
            args = node.args
        elif name == "product_output_dir":
            args = node.args[1:]  # (save_location, sensor, product)
        else:
            continue
        if len(args) >= 2 and all(isinstance(a, ast.Constant) for a in args[:2]):
            found.append((args[0].value, args[1].value))

    # satellogic_v2 threads the product through build_output_name(in_file, out,
    # "truecolor") and only then calls product_output_dir(out, "satellogic",
    # product), so the literals are at the build_output_name call sites.
    for node in ast.walk(ast.parse(_source(path))):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "build_output_name"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Constant)
        ):
            found.append(("satellogic", node.args[2].value))
    return found


def _literal_join_segments(path):
    """String literals passed straight into an ``os.path.join(...)`` call.

    A product directory named here is a directory name the destination table does
    not control, which is exactly what this suite exists to prevent.
    """
    segments = set()
    for node in ast.walk(ast.parse(_source(path))):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "join"
            and getattr(getattr(node.func.value, "attr", None), "__str__", str)() == "path"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    segments.add(arg.value)
    return segments


ALL_SOURCES = {**PROCESSORS, **FUNCTION_MODULES, **FLAT_WRITERS}


class TestProcessorsOnlyAskForKnownProducts:
    """A processor must never name a product the destination table lacks."""

    @pytest.mark.parametrize("name,path", sorted(ALL_SOURCES.items()))
    def test_every_lookup_is_in_the_table(self, name, path):
        lookups = _product_lookups(path)
        assert lookups, f"{name}: no product lookups found -- did the wiring regress?"
        for sensor, product in lookups:
            assert (sensor, product) in PRODUCT_DIRS, (
                f"{name} asks for {sensor}/{product}, which is not in PRODUCT_DIRS"
            )

    @pytest.mark.parametrize("name,path", sorted(ALL_SOURCES.items()))
    def test_every_resolvable_lookup_is_recognized_by_the_uploader(self, name, path):
        # The whole point: a directory the uploader cannot classify is published
        # to the fallback key, silently, in the wrong place.
        for sensor, product in _product_lookups(path):
            if PRODUCT_DIRS[(sensor, product)] is None:
                continue  # undecided products write flat on purpose
            assert product_dir(sensor, product) in product_dirs_for(sensor)

    @pytest.mark.parametrize("sensor,path", sorted(PROCESSORS.items()))
    def test_processor_covers_the_products_it_advertises(self, sensor, path):
        # Each processor's product directories all route through product_dir().
        looked_up = {p for s, p in _product_lookups(path) if s == sensor}
        assert "cloudMask" in looked_up
        assert "trueColor" in looked_up
        assert "waterExtent" in looked_up


class TestNoRawProductDirectoryLiterals:
    """The camelCase strings must not come back as directory names."""

    # Directories only. These same words are still legitimate as *filename*
    # tokens and as human-readable status labels, so the pattern is anchored to
    # an os.path.join into an output directory.
    JOIN_RE = re.compile(
        r"os\.path\.join\(\s*out_date_dir\s*,\s*['\"]|"
        r"os\.path\.join\(\s*out_dir\s*,\s*date\s*,\s*['\"]"
    )

    @pytest.mark.parametrize("sensor,path", sorted(PROCESSORS.items()))
    def test_no_literal_product_dir_in_a_join(self, sensor, path):
        hits = self.JOIN_RE.findall(_source(path))
        assert not hits, (
            f"{sensor}: {len(hits)} output directory join(s) use a literal instead "
            f"of product_dir(); the uploader keys off these names"
        )

    @pytest.mark.parametrize("sensor,path", sorted(FUNCTION_MODULES.items()))
    def test_cloud_mask_glob_is_not_a_literal(self, sensor, path):
        # These two globs find the merged cloud mask used to mask other products.
        # Left as 'cloudMask' they silently find nothing once the directory is
        # CloudMask, and every product merges unmasked.
        joined = _literal_join_segments(path)
        assert "cloudMask" not in joined, (
            f"{sensor}: 'cloudMask' is still a literal path segment in an "
            f"os.path.join(); it must resolve through product_dir()"
        )
        assert f"product_dir('{sensor}', 'cloudMask')" in _source(path)


class TestMergeStepClassification:
    """The three basename checks the merge step makes, after the rename."""

    def test_cloud_mask_directory_is_detected(self):
        # The original check was `'cloud' in prod_dir` with no case folding.
        # Against '/out/20250414/CloudMask' that is False -- cloud masks would
        # not be merged first, and would then be masked by themselves.
        for sensor in ("landsat", "sentinel2"):
            d = os.path.join("/out", "20250414", product_dir(sensor, "cloudMask"))
            assert is_cloud_mask_dir(sensor, d)
            assert is_cloud_mask_dir(sensor, d + os.sep)  # trailing separator

    @pytest.mark.parametrize("sensor", ["landsat", "sentinel2"])
    def test_non_cloud_products_are_not_mistaken_for_it(self, sensor):
        for product in ("trueColor", "NDVI", "MNDWI"):
            d = os.path.join("/out", "20250414", product_dir(sensor, product))
            assert not is_cloud_mask_dir(sensor, d)

    def test_composites_are_detected_including_the_two_that_were_renamed(self):
        # ColorIR and ShortwaveIR are the ones that broke: the old set held
        # 'colorinfrared'/'shortwaveinfrared', which no longer match the
        # directory basename. A missed composite re-declares nodata=0 on merge.
        for product in ("trueColor", "naturalColor", "colorInfrared", "shortwaveInfrared"):
            d = os.path.join("/out", "20250414", product_dir("sentinel2", product))
            assert is_composite_dir("sentinel2", d), product

    def test_indices_are_detected_including_the_renamed_mndwi(self):
        for product in ("NDVI", "NDWI", "MNDWI", "NBR"):
            d = os.path.join("/out", "20250414", product_dir("sentinel2", product))
            assert is_index_dir("sentinel2", d), product
        assert is_index_dir("landsat", "/out/20250414/" + product_dir("landsat", "EVI"))

    @pytest.mark.parametrize("sensor", ["landsat", "sentinel2"])
    def test_a_composite_is_never_an_index(self, sensor):
        # is_index drives masking, is_composite drives the nodata opt-out; a
        # directory answering yes to both would get contradictory treatment.
        assert not (index_dirs(sensor) & composite_dirs(sensor))
        assert not (index_dirs(sensor) & cloud_mask_dirs(sensor))
        assert not (composite_dirs(sensor) & cloud_mask_dirs(sensor))

    @pytest.mark.parametrize("sensor", ["landsat", "sentinel2"])
    def test_classified_dirs_are_all_real_product_dirs(self, sensor):
        known = product_dirs_for(sensor)
        for group in (index_dirs(sensor), composite_dirs(sensor), cloud_mask_dirs(sensor)):
            assert group <= known


class TestClassificationMatchesThePreviousBehaviour:
    """Proof the rename did not change which products get masked or opted out.

    These are the literal sets the two processors carried before the destination
    table existed, expressed as the product tokens they stood for.
    """

    def test_landsat_index_set_is_unchanged(self):
        assert index_dirs("landsat") == {
            product_dir("landsat", p) for p in ("NDVI", "NDWI", "MNDWI", "EVI", "NBR")
        }

    def test_sentinel2_index_set_covers_the_legacy_four(self):
        # The legacy process_sentinel2 CLI masks exactly these four. EVI is also
        # in the set because the STAC/ODR workflow can produce it, but that CLI
        # never creates an EVI directory, so the extra entry is unreachable there
        # and masking behaviour is unchanged.
        legacy = {product_dir("sentinel2", p) for p in ("NDVI", "NDWI", "MNDWI", "NBR")}
        assert legacy <= index_dirs("sentinel2")
        assert index_dirs("sentinel2") - legacy == {product_dir("sentinel2", "EVI")}
        assert "EVI" not in {p for s, p in _product_lookups(PROCESSORS["sentinel2"])}

    def test_sentinel2_composite_set_is_unchanged(self):
        assert composite_dirs("sentinel2") == {
            product_dir("sentinel2", p)
            for p in ("trueColor", "naturalColor", "shortwaveInfrared", "colorInfrared")
        }


class TestMergeStepUsesTheSharedClassifiers:
    """Pin the call sites, not just the helpers.

    A test of `is_cloud_mask_dir()` alone still passes if a processor goes back to
    testing the basename by hand -- which is how the original bug looked:
    `'cloud' in prod_dir`, no case folding, False against `.../CloudMask`.
    """

    @pytest.mark.parametrize("sensor,path", sorted(PROCESSORS.items()))
    def test_cloud_mask_is_found_through_the_classifier(self, sensor, path):
        src = _source(path)
        assert f"is_cloud_mask_dir('{sensor}', prod_dir)" in src
        assert "'cloud' in" not in src, (
            f"{sensor}: a hand-rolled substring test for the cloud mask directory "
            f"is back; it does not survive a rename or a case change"
        )

    @pytest.mark.parametrize("sensor,path", sorted(PROCESSORS.items()))
    def test_index_check_goes_through_the_classifier(self, sensor, path):
        src = _source(path)
        assert f"is_index_dir('{sensor}', prod_dir)" in src
        assert "lower() in {" not in src, (
            f"{sensor}: a hard-coded lowercase directory set is back"
        )

    def test_composite_check_goes_through_the_classifier(self):
        src = _source(PROCESSORS["sentinel2"])
        assert "is_composite_dir('sentinel2', prod_dir)" in src
        assert "COMPOSITE_PRODUCT_DIRS" not in src
