"""
CLI entry points for Capella processing.
"""

import os


def process_capella_cli():
    """Entry point for process_capella command."""

    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, "process_capella.py")

    with open(script_path) as f:
        code = compile(f.read(), script_path, "exec")
        exec(code, {"__name__": "__main__"})


if __name__ == "__main__":
    process_capella_cli()