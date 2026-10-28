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

def process_landsat_odr_cli():
    """Entry point for the process_landsat_odr command.

    Unlike the two shims above, the STAC pipeline exposes a real
    ``main()`` rather than running at module scope, so this imports and
    calls it instead of exec'ing the file. That also means it is
    reachable by ``tests/integration/test_dispatch_undefined_names.py``
    and by a plain ``import``.
    """
    from landsat.process_landsat_odr import main

    main()


if __name__ == '__main__':
    process_landsat89_cli()
