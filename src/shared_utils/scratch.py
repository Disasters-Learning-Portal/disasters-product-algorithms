"""
scratch.py

Throwaway local directories: where raw vendor scenes are downloaded, and the
operator notebooks' per-run OUTPUT_DIR.

Both exist because /tmp on a hub pod filled up run after run. The vendor CLIs
downloaded every raw scene into a shared, hardcoded ``/tmp/s3_temp`` that
nothing ever deleted, and the workflow notebooks kept every earlier run's COGs
in OUTPUT_DIR -- which their upload cell then re-published. See
``.clinerules.md`` rule 61.
"""

import os
import shutil
import tempfile
from contextlib import contextmanager


@contextmanager
def download_scratch(prefix):
    """Yield a fresh directory for one run's raw downloads; always delete it.

    The removal is in ``finally``, so it runs on success, on an exception and
    on Ctrl-C (``KeyboardInterrupt`` is not an ``Exception``). Only a SIGKILL
    can leave it behind, and then the random name keeps it from ever being
    mistaken for a cache.
    """
    path = tempfile.mkdtemp(prefix=f"{prefix}_dl_")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _temp_roots():
    return {os.path.realpath(tempfile.gettempdir()), os.path.realpath("/tmp")}


def reset_output_dir(path):
    """Empty ``path`` (creating it if needed) so a run holds only its own products.

    Refuses anything that is not strictly INSIDE a temp directory -- the temp
    root itself included -- because an operator who pointed OUTPUT_DIR at a
    real folder must never have it wiped. The path is resolved first, so a
    symlink under /tmp that points elsewhere is refused too.
    """
    real = os.path.realpath(path)
    roots = _temp_roots()
    if not any(real != root and real.startswith(root + os.sep) for root in roots):
        raise ValueError(
            f"Refusing to clear OUTPUT_DIR={path!r} (resolves to {real!r}): it is "
            f"not inside a temp directory ({', '.join(sorted(roots))}). Point "
            f"OUTPUT_DIR at a subfolder of /tmp, or empty it yourself."
        )

    if os.path.isdir(real):
        n_files, n_bytes = 0, 0
        for dirpath, _dirs, files in os.walk(real):
            for name in files:
                n_files += 1
                try:
                    n_bytes += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        shutil.rmtree(real)
        print(f"Cleared OUTPUT_DIR {path}: removed {n_files} file(s), "
              f"{n_bytes / 1024**2:.1f} MB from earlier runs.")
    os.makedirs(real, exist_ok=True)
    return path
