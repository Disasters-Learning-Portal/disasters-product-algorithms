"""
CLI entry points for Umbra processing.
"""

import os


def process_umbra_cli():
    """Entry point for process_umbra command."""
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, "process_umbra.py")

    with open(script_path) as f:
        code = compile(f.read(), script_path, "exec")
        exec(code, {"__name__": "__main__"})


if __name__ == "__main__":
    process_umbra_cli()