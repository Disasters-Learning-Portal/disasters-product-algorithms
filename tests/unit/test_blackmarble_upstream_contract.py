"""The pinned upstream `blackmarble` must still offer what dps/blackmarble/run.sh relies on.

run.sh calls upstream's `blackmarble` console script directly (no shim since issue #186)
and ALWAYS passes `--viirs-product <short name>`, taking the short name from
dps/blackmarble/platform.sh. That only works while three things stay true of the pin in
dps/environment.yml:

  1. the CLI accepts `--viirs-product`;
  2. upstream's product table names exactly the products platform.sh can select, with the
     same mission-start dates run.sh enforces as its date floor (and the same Suomi-NPP
     sunset it warns about);
  3. the product passed in is the one actually SEARCHED for. This is the failure that
     matters: if the flag stopped reaching the CMR query, every job would still exit 0 --
     on the wrong satellite, under the other product's tags.

Upstream is pip-installed only into the DPS worker env (`disasters_dps`), never into CI's
pytest env, so this module skips where it is absent. Nothing here touches the network:
earthaccess is stubbed (the pattern upstream's own tests/acquire/test_viirs_product.py uses).
"""
import os
import shutil
import subprocess
from datetime import datetime

import pytest

blackmarble = pytest.importorskip(
    "blackmarble", reason="upstream blackmarble is only installed in the disasters_dps DPS worker env"
)
from blackmarble.acquire import viirs  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLATFORM_SH = os.path.join(REPO_ROOT, "dps", "blackmarble", "platform.sh")
VALIDATE_SH = os.path.join(REPO_ROOT, "dps", "_validate.sh")

BBOX = (-122.55, 37.69, -122.32, 37.81)
PLATFORMS = ("snpp", "noaa20")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def platform_sh(expr):
    """Evaluate a bash expression with platform.sh sourced exactly as run.sh sources it."""
    result = subprocess.run(
        ["bash", "-c", f'source "{VALIDATE_SH}"; source "{PLATFORM_SH}"; {expr}'],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"{expr!r} failed: {result.stderr}"
    return result.stdout.strip()


def short_name(platform):
    return platform_sh(f'bm_platform_short_name "{platform}"')


def test_cli_accepts_viirs_product():
    """The exact flag run.sh passes. Typer reports an unknown option as a usage error."""
    from typer.testing import CliRunner

    from blackmarble.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "--viirs-product" in result.output


def test_upstream_offers_exactly_the_products_platform_sh_selects():
    assert set(viirs.VIIRS_PRODUCTS) == {short_name(p) for p in PLATFORMS}


@pytest.mark.parametrize("platform", PLATFORMS)
def test_mission_start_matches_the_run_sh_date_floor(platform):
    product = viirs.VIIRS_PRODUCTS[short_name(platform)]
    floor = platform_sh(f'bm_platform_min_date "{platform}"')
    assert f"{product.start_date:%Y-%m-%d}" == floor


def test_suomi_npp_sunset_matches_the_run_sh_warning():
    snpp = viirs.VIIRS_PRODUCTS[short_name("snpp")]
    assert snpp.end_date is not None
    assert f"{snpp.end_date:%Y-%m-%d}" == platform_sh('printf %s "$BM_SNPP_SUNSET_DATE"')


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_requested_product_is_the_one_searched(platform, monkeypatch, tmp_path):
    """download_viirs(product=X) must send short_name=X to CMR -- for BOTH products.

    Parametrized over both so a regression to "always the default" (VJ146A2) is caught on
    the Suomi-NPP case, where it would otherwise be invisible.
    """
    calls = []
    monkeypatch.setattr(viirs.earthaccess, "login", lambda *a, **k: None)
    monkeypatch.setattr(viirs.earthaccess, "search_data", lambda **kw: calls.append(kw) or [])
    monkeypatch.setattr(viirs.earthaccess, "download", lambda *a, **k: [])

    product = short_name(platform)
    viirs.download_viirs(datetime(2023, 6, 15), BBOX, tmp_path, product=product)

    assert len(calls) == 1
    assert calls[0]["short_name"] == product
    assert calls[0]["version"] == "2"
