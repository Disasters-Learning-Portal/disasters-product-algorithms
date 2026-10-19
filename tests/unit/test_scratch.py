"""shared_utils.scratch: per-run download dirs and the notebook OUTPUT_DIR reset."""

import os
import tempfile

import pytest

from shared_utils.scratch import download_scratch, reset_output_dir


class TestDownloadScratch:
    def test_removed_after_normal_exit(self):
        with download_scratch("t") as d:
            open(os.path.join(d, "raw.tif"), "wb").write(b"x")
            assert os.path.isdir(d)
        assert not os.path.exists(d)

    def test_removed_after_exception(self):
        with pytest.raises(RuntimeError):
            with download_scratch("t") as d:
                open(os.path.join(d, "raw.tif"), "wb").write(b"x")
                raise RuntimeError("boom")
        assert not os.path.exists(d)

    def test_removed_after_keyboard_interrupt(self):
        # KeyboardInterrupt is not an Exception -- an `except Exception` cleanup
        # would miss it, which is how Ctrl-C used to leak temp files.
        with pytest.raises(KeyboardInterrupt):
            with download_scratch("t") as d:
                open(os.path.join(d, "raw.tif"), "wb").write(b"x")
                raise KeyboardInterrupt
        assert not os.path.exists(d)

    def test_each_run_gets_its_own_dir(self):
        with download_scratch("t") as a, download_scratch("t") as b:
            assert a != b
            assert os.path.basename(a).startswith("t_dl_")


@pytest.fixture
def tmp_subdir():
    d = tempfile.mkdtemp(prefix="reset_output_test_")
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


class TestResetOutputDir:
    def test_empties_existing_dir(self, tmp_subdir):
        os.makedirs(os.path.join(tmp_subdir, "Backscatter"))
        open(os.path.join(tmp_subdir, "Backscatter", "old.tif"), "wb").write(b"x" * 10)
        open(os.path.join(tmp_subdir, "old.png"), "wb").write(b"x")
        reset_output_dir(tmp_subdir)
        assert os.path.isdir(tmp_subdir)
        assert os.listdir(tmp_subdir) == []

    def test_creates_missing_dir(self, tmp_subdir):
        target = os.path.join(tmp_subdir, "new_output")
        reset_output_dir(target)
        assert os.path.isdir(target)

    def test_accepts_slash_tmp_subdir(self):
        # The notebooks' defaults (/tmp/s3_temp, /tmp/skysat_output) live under
        # /tmp, which on macOS is NOT tempfile.gettempdir().
        target = "/tmp/reset_output_dir_test_accepts"
        try:
            reset_output_dir(target)
            assert os.path.isdir(target)
        finally:
            os.rmdir(target)

    @pytest.mark.parametrize("bad", [
        "/tmp", "/tmp/", tempfile.gettempdir(),
        os.path.expanduser("~/reset_output_dir_must_refuse"), "/",
    ])
    def test_refuses_outside_or_root(self, bad):
        with pytest.raises(ValueError, match="Refusing to clear"):
            reset_output_dir(bad)

    def test_refuses_symlink_escaping_tmp(self, tmp_subdir, tmp_path_factory):
        # A path that *looks* like it is under /tmp but resolves elsewhere.
        outside = os.path.expanduser("~")
        link = os.path.join(tmp_subdir, "link_to_home")
        os.symlink(outside, link)
        with pytest.raises(ValueError, match="Refusing to clear"):
            reset_output_dir(link)
        assert os.path.isdir(outside)
