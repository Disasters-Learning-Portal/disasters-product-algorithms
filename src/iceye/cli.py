"""
CLI entry points for ICEYE processing.
"""

import os


def process_iceye_cli():
    """Entry point for process_iceye command."""

    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, "process_iceye.py")

    with open(script_path) as f:
        code = compile(f.read(), script_path, "exec")
        exec(code, {"__name__": "__main__"})


if __name__ == "__main__":
    process_iceye_cli()