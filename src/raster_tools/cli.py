"""CLI entry points for raster_tools."""
import os
import sys


def _run_script(name):
    """Execute a sibling script as if it were `__main__`."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, name)
    with open(script_path) as f:
        code = compile(f.read(), script_path, "exec")
        exec(code, {"__name__": "__main__"})


def summarize_raster_cli():
    """Entry point for `summarize_raster` command."""
    _run_script("summarize.py")


def fix_cog_overviews_cli():
    """Entry point for `fix_cog_overviews` command."""
    _run_script("fix_cog_overviews.py")


if __name__ == "__main__":
    summarize_raster_cli()
