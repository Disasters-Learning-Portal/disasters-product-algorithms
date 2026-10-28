"""The canonical ProgramData destinations, and the key shape they must produce.

These pin the two things that were actually wrong in the field: the product
directory strings (the code had camelCase, the bucket has PascalCase) and the
number of segments in the published key (a template had grown an ``Output/``
level that exists nowhere in the bucket).
"""

import os

import pytest

from shared_utils.product_paths import (
    PRODUCT_DIRS,
    SENSOR_DIRS,
    UNDECIDED_NOTES,
    UndecidedProductPath,
    is_decided,
    prefix_for_product_dir,
    product_dir,
    product_dirs_for,
    product_output_dir,
    program_data_prefix,
    undecided_products,
)
from shared_utils.staging_upload import iter_upload_keys, program_data_key


class TestProductDirectories:
    """The directory strings, spelled as the live bucket spells them."""

    @pytest.mark.parametrize(
        "sensor,product,expected",
        [
            ("landsat", "trueColor", "TrueColor"),
            ("landsat", "colorInfrared", "ColorIR"),
            ("landsat", "naturalColor", "NaturalColor"),
            ("landsat", "cloudMask", "CloudMask"),
            ("landsat", "panchromatic", "Panchromatic"),
            ("landsat", "waterExtent", "WaterExtent"),
            ("sentinel2", "shortwaveInfrared", "ShortwaveIR"),
            ("sentinel2", "colorInfrared", "ColorIR"),
            ("sentinel2", "cloudMask", "CloudMask"),
            ("satellogic", "truecolor", "TrueColor"),
            ("satellogic", "colorir", "ColorIR"),
            ("capella", "sigma0", "Backscatter"),
            ("iceye", "sigma0-dB", "Backscatter"),
            ("umbra", "sigma0", "GEC"),
        ],
    )
    def test_directory_string(self, sensor, product, expected):
        assert product_dir(sensor, product) == expected

    def test_mndwi_keeps_its_lowercase_leading_m(self):
        # The bucket has mNDWI/, not MNDWI/ -- the index's conventional spelling.
        assert product_dir("landsat", "MNDWI") == "mNDWI"
        assert product_dir("sentinel2", "MNDWI") == "mNDWI"

    def test_sensor_dirs_match_the_bucket_spelling(self):
        # Two spellings differ from how the code spells them elsewhere.
        assert SENSOR_DIRS["skysat"] == "Skysat"
        assert SENSOR_DIRS["iceye"] == "Iceye"
        assert SENSOR_DIRS["sentinel2"] == "Sentinel-2"

    def test_unknown_pair_is_a_loud_keyerror(self):
        with pytest.raises(KeyError, match="no S3 destination recorded"):
            product_dir("landsat", "notAProduct")


class TestProgramDataPrefix:
    def test_prefix_shape(self):
        assert (
            program_data_prefix("sentinel2", "colorInfrared")
            == "ProgramData/Sentinel-2/ColorIR"
        )

    @pytest.mark.parametrize("sensor", sorted(SENSOR_DIRS))
    def test_every_decided_product_yields_exactly_three_segments(self, sensor):
        # ProgramData/<Sensor>/<Product> -- no date, no event, and above all no
        # Output/ level, which a notebook template had grown and which exists in
        # no object in the bucket.
        for (s, product), value in PRODUCT_DIRS.items():
            if s != sensor or value is None:
                continue
            parts = program_data_prefix(s, product).split("/")
            assert len(parts) == 3, parts
            assert parts[0] == "ProgramData"
            assert "Output" not in parts

    def test_unknown_sensor_is_a_loud_keyerror(self):
        with pytest.raises(KeyError, match="unknown sensor"):
            program_data_prefix("landsat8", "trueColor")

    def test_prefix_for_product_dir_round_trips(self):
        assert (
            prefix_for_product_dir("landsat", product_dir("landsat", "MNDWI"))
            == "ProgramData/Landsat/mNDWI"
        )


class TestUndecidedDestinations:
    """A blank destination must be flagged, never guessed."""

    def test_umbra_beta_and_gamma_are_the_open_decisions(self):
        assert undecided_products() == [("umbra", "beta0"), ("umbra", "gamma0")]

    @pytest.mark.parametrize("product", ["beta0", "gamma0"])
    def test_resolving_an_undecided_product_raises(self, product):
        assert not is_decided("umbra", product)
        with pytest.raises(UndecidedProductPath, match="undecided"):
            program_data_prefix("umbra", product)

    def test_every_undecided_product_explains_itself(self):
        # A blank with no note is just a gap; the note is what makes it actionable.
        for key in undecided_products():
            assert UNDECIDED_NOTES.get(key), key

    def test_undecided_product_still_writes_somewhere(self, tmp_path, capsys):
        # An undecided path should block a release, not a running job.
        out = product_output_dir(str(tmp_path), "umbra", "beta0")
        assert out == str(tmp_path)
        assert "no S3 product directory is decided" in capsys.readouterr().out

    def test_decided_product_gets_its_directory_created(self, tmp_path):
        out = product_output_dir(str(tmp_path), "umbra", "sigma0")
        assert out == os.path.join(str(tmp_path), "GEC")
        assert os.path.isdir(out)


class TestUploadKeys:
    """The date level is local-only; it must not reach S3."""

    def _tree(self, root, rels):
        for rel in rels:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        return str(root)

    def test_date_level_is_dropped_from_the_key(self, tmp_path):
        out_home = self._tree(
            tmp_path, ["20250414/TrueColor/LC08_trueColor_merged_2025-04-14_day.tif"]
        )
        keys = [k for _, k in iter_upload_keys(out_home, "ignored", sensor="landsat")]
        assert keys == [
            "ProgramData/Landsat/TrueColor/LC08_trueColor_merged_2025-04-14_day.tif"
        ]

    def test_masked_subdir_flattens_into_the_product_directory(self, tmp_path):
        out_home = self._tree(
            tmp_path, ["20250414/TrueColor/masked/LC08_trueColor_masked_2025-04-14_day.tif"]
        )
        keys = [k for _, k in iter_upload_keys(out_home, "ignored", sensor="landsat")]
        assert keys == [
            "ProgramData/Landsat/TrueColor/LC08_trueColor_masked_2025-04-14_day.tif"
        ]

    def test_every_key_has_exactly_four_segments(self, tmp_path):
        out_home = self._tree(
            tmp_path,
            [
                "20250414/TrueColor/a.tif",
                "20250414/mNDWI/b.tif",
                "20250414/ColorIR/masked/c.tif",
            ],
        )
        for _, key in iter_upload_keys(out_home, "ignored", sensor="landsat"):
            assert key.split("/")[:2] == ["ProgramData", "Landsat"]
            assert len(key.split("/")) == 4, key

    def test_unrecognized_file_falls_back_and_says_so(self, tmp_path, capsys):
        # Losing a product silently would be worse than publishing it to the old key.
        out_home = self._tree(tmp_path, ["20250414/scratch/intermediate.tif"])
        keys = [k for _, k in iter_upload_keys(out_home, "dps_output/EVT", sensor="landsat")]
        assert keys == ["dps_output/EVT/20250414/scratch/intermediate.tif"]
        assert "not under a known landsat product directory" in capsys.readouterr().out

    def test_without_a_sensor_the_old_relpath_key_is_unchanged(self, tmp_path):
        out_home = self._tree(tmp_path, ["20250414/TrueColor/a.tif"])
        keys = [k for _, k in iter_upload_keys(out_home, "dps_output/EVT")]
        assert keys == ["dps_output/EVT/20250414/TrueColor/a.tif"]

    def test_program_data_key_returns_none_when_unrecognized(self, tmp_path):
        out_home = self._tree(tmp_path, ["20250414/scratch/x.tif"])
        f = os.path.join(out_home, "20250414", "scratch", "x.tif")
        assert program_data_key(f, out_home, "landsat") is None

    def test_product_dirs_for_is_the_decided_set(self):
        assert product_dirs_for("capella") == {"Backscatter"}
        assert product_dirs_for("umbra") == {"GEC"}
