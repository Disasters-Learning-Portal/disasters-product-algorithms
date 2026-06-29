#!/usr/bin/env python
"""Register the disasters-product-algorithms DPS algorithms with MAAP.

Run from a MAAP ADE workspace where maap-py is installed and the MAAP API host
is configured (the default ``MAAP()`` constructor picks it up):

    python dps/register_algorithms.py                 # register all
    python dps/register_algorithms.py landsat         # register one
    python dps/register_algorithms.py landsat sentinel2

Each sensor's manifest is dps/<sensor>/algorithm_config.yaml. The version, repo
URL, build/run commands and inputs are declared there -- this script only walks
the folders and calls register_algorithm_from_yaml_file().
"""
import sys
from pathlib import Path

from maap.maap import MAAP

DPS_DIR = Path(__file__).resolve().parent
SENSORS = ["landsat", "sentinel2"]


def main(argv):
    targets = argv[1:] or SENSORS
    maap = MAAP()
    for sensor in targets:
        cfg = DPS_DIR / sensor / "algorithm_config.yaml"
        if not cfg.exists():
            print(f"SKIP {sensor}: {cfg} not found")
            continue
        print(f"Registering {sensor} from {cfg} ...")
        resp = maap.register_algorithm_from_yaml_file(str(cfg))
        print(getattr(resp, "text", resp))


if __name__ == "__main__":
    main(sys.argv)
