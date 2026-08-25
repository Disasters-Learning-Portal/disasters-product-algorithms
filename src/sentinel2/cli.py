"""
CLI entry points for Sentinel-2 processing.
"""

from sentinel2.process_sentinel2 import main as process_sentinel2_main


def process_sentinel2_cli():
    """Entry point for the process_sentinel2 command."""
    process_sentinel2_main()


if __name__ == "__main__":
    process_sentinel2_cli()
