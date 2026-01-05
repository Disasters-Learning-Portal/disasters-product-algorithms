"""
CLI entry points for sentinel processing.
"""
import sys
import os


def process_sentinel2_cli():
    """Entry point for process_sentinel2 command."""
    # Execute the process_sentinel2.py script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, 'process_sentinel2.py')

    with open(script_path) as f:
        code = compile(f.read(), script_path, 'exec')
        exec(code, {'__name__': '__main__'})


def download_sentinel2_cli():
    """Entry point for download_sentinel2 command."""
    # Execute the download_sentinel2.py script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, 'download_sentinel2.py')

    with open(script_path) as f:
        code = compile(f.read(), script_path, 'exec')
        exec(code, {'__name__': '__main__'})


if __name__ == '__main__':
    process_sentinel2_cli()
