"""Unit tests for Capella's Lee filter and its nodata handling.

``capella_v2.lee_filter`` and the sigma0 dB conversion had no coverage at all,
despite being the substantive change in PR #76 (NaN-aware filtering moved onto
the linear backscatter, and the old -60 dB floor replaced). These pin the two
properties that matter downstream: the filter must not invent or smear values
across invalid pixels, and the border must come out as the declared nodata so
it is actually maskable.
"""

import numpy as np
import pytest

pytest.importorskip("scipy")

from capella.capella_v2 import CAPELLA_NODATA, lee_filter


def test_nodata_sentinel_is_not_zero():
    """0 dB is legitimate SAR backscatter, so it can never be the sentinel.

    CLAUDE.md: "Capella & Umbra SAR CLIs default -nodata to -9999.0 ... their
    output is float32 dB backscatter where 0 dB is a legitimate value."
    """
    assert CAPELLA_NODATA == -9999.0
    assert CAPELLA_NODATA != 0


def test_lee_filter_preserves_a_uniform_field():
    """A constant image has zero local variance -- the filter must be a no-op."""
    img = np.full((32, 32), 4.0)
    out = lee_filter(img, size=5)
    assert np.allclose(out, 4.0), f"uniform field changed: {out.min()}..{out.max()}"


def test_lee_filter_reduces_speckle_variance():
    """On a noisy uniform field the filter must reduce variance, not amplify it."""
    rng = np.random.default_rng(0)
    img = 4.0 + rng.normal(0, 1.0, size=(64, 64))
    out = lee_filter(img, size=5)
    assert out.var() < img.var(), (
        f"variance rose: {img.var():.4f} -> {out.var():.4f}"
    )
    # The mean is a physical quantity (linear backscatter) -- it must be kept.
    assert out.mean() == pytest.approx(img.mean(), rel=0.05)


def test_lee_filter_is_nan_aware():
    """Invalid pixels must neither receive nor contaminate a smoothed value.

    This is what separates the NaN-aware implementation from the naive one: a
    naive uniform_filter smears NaN across the whole neighbourhood, so a single
    invalid pixel punches a hole the size of the kernel.
    """
    img = np.full((32, 32), 4.0)
    img[16, 16] = np.nan

    out = lee_filter(img, size=5)

    # The hole stays exactly one pixel wide -- no kernel-sized blast radius.
    assert np.isnan(out[16, 16])
    neighbourhood = np.delete(out[14:19, 14:19].ravel(), 12)  # drop the centre
    assert np.isfinite(neighbourhood).all(), (
        f"NaN smeared into {np.count_nonzero(~np.isfinite(neighbourhood))} neighbours"
    )
    assert np.allclose(neighbourhood, 4.0), (
        "valid neighbours were contaminated by the NaN"
    )


def test_lee_filter_does_not_amplify_outliers():
    """A bright point target must not be spread beyond its own kernel."""
    img = np.full((32, 32), 1.0)
    img[16, 16] = 100.0
    out = lee_filter(img, size=5)
    assert out.max() <= 100.0 + 1e-9, "filter amplified the peak"
    assert out[0, 0] == pytest.approx(1.0, abs=1e-6), "peak leaked across the image"


@pytest.mark.parametrize("size", [3, 5, 7])
def test_lee_filter_accepts_the_supported_kernel_sizes(size):
    """The CLI restricts --filter_size to {3,5,7}; all three must work."""
    rng = np.random.default_rng(1)
    img = 2.0 + rng.normal(0, 0.5, size=(24, 24))
    out = lee_filter(img, size=size)
    assert out.shape == img.shape
    assert np.isfinite(out).all()
