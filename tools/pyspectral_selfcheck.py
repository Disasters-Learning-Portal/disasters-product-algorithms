"""
Build-time gate: pyspectral's Rayleigh correction works OFFLINE.

Why this exists
---------------
`sentinel2.sentinel2_odr_functions.get_rayleigh_correction` raises when
pyspectral is unavailable, rather than returning 0 and shipping
uncorrected top-of-atmosphere data under a product name that claims
correction. That makes the correction a hard dependency of the L1C path
-- and installing the conda package is NOT sufficient to satisfy it.

`Rayleigh(platform, "msi")` needs two data sets that the package does
not ship:

  * the RSR file  rsr_msi_<platform>.h5
  * the Rayleigh LUT  <aerosol_type>/rayleigh_lut_us-standard.h5

Both are fetched from the internet on first use unless
`download_from_internet: False` is set in the pyspectral config. A DPS
worker has no outbound network, so an image that installs pyspectral but
skips the data provisioning fails at ACTIVATION time, in the middle of a
disaster response, with a confusing network error.

This script blocks the network and then exercises the real code path, so
that failure surfaces as a red image build instead.

Run it from the Dockerfile after provisioning the data:

    python tools/pyspectral_selfcheck.py

Exit code 0 = the offline setup is good. Non-zero = the image is broken.
"""

import sys


def _block_network():
    """Make any outbound request raise, so a latent fetch is loud."""
    import socket

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "pyspectral attempted to use the network. Its RSR/LUT data "
            "is not fully provisioned, or download_from_internet is not "
            "False in the pyspectral config."
        )

    socket.socket = _blocked
    socket.create_connection = _blocked

    try:
        import requests

        requests.get = _blocked
        requests.Session.request = _blocked
    except ImportError:
        pass


# Must run before pyspectral is imported.
_block_network()

import numpy as np  # noqa: E402
from pyspectral.rayleigh import Rayleigh  # noqa: E402


# Every Sentinel-2 platform the pipeline maps in
# _PLATFORM_TO_PYSPECTRAL. S2C is included deliberately: it has been
# operational since 2025, its RSRs genuinely differ from S2A's (B02 at
# 486.0 nm vs 489.8 nm), and the pipeline refuses to substitute another
# platform's response.
PLATFORMS = ("Sentinel-2A", "Sentinel-2B", "Sentinel-2C")

# The bands whose effective wavelength falls inside the LUT's 400-800 nm
# grid, i.e. the ones the pipeline will actually ask for.
BANDS = ("B01", "B02", "B03", "B04", "B05", "B06", "B07")


def main():
    sun_zenith = np.asarray(45.0)
    sat_zenith = np.asarray(0.0)
    azidiff = np.asarray(0.0)

    failures = []

    for platform in PLATFORMS:

        try:
            rayleigh = Rayleigh(platform, "msi")
        except Exception as exc:  # noqa: BLE001 - report, don't mask
            failures.append(f"{platform}: Rayleigh() failed: {exc!r}")
            continue

        for band in BANDS:

            try:
                value = float(
                    np.asarray(
                        rayleigh.get_reflectance(
                            sun_zenith, sat_zenith, azidiff, band
                        )
                    ).ravel()[0]
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    f"{platform} {band}: get_reflectance failed: {exc!r}"
                )
                continue

            # pyspectral returns PERCENT (0-100). A physically sane
            # Rayleigh reflectance at 45 deg sun zenith is a few
            # percent in the blue, less in the red -- never 0, never
            # saturated.
            if not (0.0 < value < 100.0):
                failures.append(
                    f"{platform} {band}: implausible reflectance {value}"
                )

        print(f"  ok: {platform}")

    if failures:
        print("\npyspectral offline self-check FAILED:\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("\npyspectral offline self-check OK "
          f"({len(PLATFORMS)} platforms x {len(BANDS)} bands, no network).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
