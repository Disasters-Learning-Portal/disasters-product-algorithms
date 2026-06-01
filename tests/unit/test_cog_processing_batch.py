"""
Tests for shared_utils.cog_processing.process_batch_s3.
"""

from unittest.mock import patch

import pytest

from shared_utils.cog_processing import process_batch_s3


def test_process_batch_s3_calls_process_single_file_per_item():
    """Each (src, dst) pair triggers exactly one process_single_file call."""
    items = [("src/a.tif", "dst/a.tif"),
             ("src/b.tif", "dst/b.tif"),
             ("src/c.tif", "dst/c.tif")]

    with patch("shared_utils.cog_processing.process_single_file", return_value=True) as mock_psf:
        results = process_batch_s3(
            s3_client="fake-client",
            bucket="my-bucket",
            items=items,
            max_workers=2,
            verbose=False,
        )

    assert results == [True, True, True]
    assert mock_psf.call_count == 3

    called_pairs = {(c.args[2], c.args[3]) for c in mock_psf.call_args_list}
    assert called_pairs == set(items)

    for call in mock_psf.call_args_list:
        assert call.args[0] == "fake-client"
        assert call.args[1] == "my-bucket"
        assert call.kwargs.get("verbose") is False


def test_process_batch_s3_isolates_failures():
    """One failing item is captured as an Exception; siblings still return True."""
    items = [("src/a.tif", "dst/a.tif"),
             ("src/b.tif", "dst/b.tif"),
             ("src/c.tif", "dst/c.tif")]

    def fake_process(_client, _bucket, src, _dst, **_kwargs):
        if src == "src/b.tif":
            raise RuntimeError("boom on b")
        return True

    with patch("shared_utils.cog_processing.process_single_file", side_effect=fake_process):
        results = process_batch_s3("fake", "bucket", items, max_workers=3, verbose=False)

    assert results[0] is True
    assert isinstance(results[1], RuntimeError)
    assert "boom on b" in str(results[1])
    assert results[2] is True


def test_process_batch_s3_preserves_order():
    """Output list aligns with input order regardless of completion order."""
    import time

    items = [(f"src/{i}.tif", f"dst/{i}.tif") for i in range(5)]

    def slow_for_small(_client, _bucket, src, _dst, **_kwargs):
        idx = int(src.split("/")[1].split(".")[0])
        time.sleep(0.02 * (5 - idx))
        return f"done-{idx}"

    with patch("shared_utils.cog_processing.process_single_file", side_effect=slow_for_small):
        results = process_batch_s3("fake", "bucket", items, max_workers=5, verbose=False)

    assert results == [f"done-{i}" for i in range(5)]


def test_process_batch_s3_forwards_kwargs():
    """metadata, nodata, verify etc. are forwarded to process_single_file."""
    items = [("src/a.tif", "dst/a.tif")]
    metadata = {"ACTIVATION_EVENT": "202406_Flood_TX"}

    with patch("shared_utils.cog_processing.process_single_file", return_value=True) as mock_psf:
        process_batch_s3(
            "fake", "bucket", items,
            max_workers=1,
            nodata=-9999,
            verify=False,
            metadata=metadata,
            verbose=False,
        )

    call = mock_psf.call_args_list[0]
    assert call.kwargs.get("nodata") == -9999
    assert call.kwargs.get("verify") is False
    assert call.kwargs.get("metadata") == metadata
    assert call.kwargs.get("verbose") is False


def test_process_batch_s3_empty_input():
    """Empty items list returns empty results, no calls."""
    with patch("shared_utils.cog_processing.process_single_file") as mock_psf:
        results = process_batch_s3("fake", "bucket", [], max_workers=4)

    assert results == []
    assert mock_psf.call_count == 0
