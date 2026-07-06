"""
CLI entry points for landsat-product-algorithm package.
"""
import sys
import os


def process_landsat89_cli():
    """Entry point for process_landsat89 command."""
    # Execute the process_landsat89.py script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, 'process_landsat89.py')

    with open(script_path) as f:
        code = compile(f.read(), script_path, 'exec')
        exec(code, {'__name__': '__main__'})


if __name__ == '__main__':
    process_landsat89_cli()
