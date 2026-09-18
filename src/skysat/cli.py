"""
CLI entry points for Skysat processing.
"""

import os


def process_skysat_cli():
    """Entry point for process_skysat command."""
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, "process_skysat.py")

    with open(script_path) as f:
        code = compile(f.read(), script_path, "exec")
        exec(code, {"__name__": "__main__"})


if __name__ == "__main__":
    process_skysat_cli()