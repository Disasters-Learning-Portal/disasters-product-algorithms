"""Pytest wrapper so the dps/_validate.sh assertions run in the normal suite.

The real assertions live in test_dps_validate.sh (bash `[[ =~ ]]` regex checks
mirror exactly what the DPS run.sh scripts execute). This module just runs that
script and fails if any case failed.
"""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "test_dps_validate.sh")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_dps_validate_sh():
    result = subprocess.run(
        ["bash", SCRIPT], capture_output=True, text=True
    )
    # The script prints "N passed, M failed" and exits non-zero on any failure.
    assert result.returncode == 0, (
        f"dps/_validate.sh assertions failed:\n{result.stdout}\n{result.stderr}"
    )
