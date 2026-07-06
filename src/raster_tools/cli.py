"""CLI entry points for raster_tools."""
import os
import sys


def summarize_raster_cli():
    """Entry point for `summarize_raster` command."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, "summarize.py")
    with open(script_path) as f:
        code = compile(f.read(), script_path, "exec")
        exec(code, {"__name__": "__main__"})


if __name__ == "__main__":
    summarize_raster_cli()
