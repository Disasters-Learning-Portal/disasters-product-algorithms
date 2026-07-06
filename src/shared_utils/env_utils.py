"""Load secrets from a git-ignored ``.env.local`` file into ``os.environ``.

Keeps credentials (e.g. Copernicus COP_USER/COP_PASS) out of notebooks and
source. Notebooks call :func:`load_env_local` and then read ``os.environ``.
"""
import os


def load_env_local(filename=".env.local", required=None, start_dir=None):
    """Load ``KEY=VALUE`` pairs from the nearest ``filename`` into ``os.environ``.

    Searches ``start_dir`` (default: current working directory) and walks up
    toward the filesystem root until it finds ``filename``. Existing environment
    variables are NOT overwritten -- an already-exported var wins over the file.
    Blank lines and ``#`` comments are ignored; surrounding quotes on values are
    stripped.

    Args:
        filename: name of the dotenv file to look for (default ``".env.local"``).
        required: optional iterable of keys that must be set after loading; if
            any are missing, raises ``ValueError`` naming them.
        start_dir: directory to begin the upward search from (default: cwd).

    Returns:
        The path of the file that was loaded, or ``None`` if none was found.

    Raises:
        ValueError: if ``required`` is given and any key is still unset.
    """
    start = os.path.abspath(start_dir or os.getcwd())
    found = None
    d = start
    while True:
        candidate = os.path.join(d, filename)
        if os.path.isfile(candidate):
            with open(candidate) as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
            found = candidate
            break
        parent = os.path.dirname(d)
        if parent == d:  # reached filesystem root
            break
        d = parent

    if required:
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise ValueError(
                f"Missing required env var(s) {missing}. Create a '{filename}' "
                f"file (see '.env.local.example' at the repo root) with these "
                f"set, or export them as environment variables. "
                f"Searched upward from {start}."
            )
    return found
