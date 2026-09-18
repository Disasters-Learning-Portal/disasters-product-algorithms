"""``tools/pyspectral_selfcheck.py`` gates BOTH image builds (``image/Dockerfile``
and ``dps/Dockerfile``), so a crash in the script itself blocks every rebuild.

Its network guard replaced ``socket.socket`` with a function and only THEN
imported ``requests``. That import is the first thing in the process to load
``ssl``, which does ``class SSLSocket(socket)`` -- subclassing a function:

    TypeError: function() argument 'code' must be code, not str

Not an ``ImportError``, so it escaped the guard's ``except`` and the dev hub
image failed every build that reached the step.

Runs in a SUBPROCESS: the guard patches ``socket`` process-wide, and the script
as a whole needs pyspectral's baked-in data, so only ``_block_network`` is
lifted out (by AST) and executed in a clean interpreter.
"""

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "pyspectral_selfcheck.py"


def _block_network_source():
    tree = ast.parse(SCRIPT.read_text())
    (func,) = [n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "_block_network"]
    return ast.unparse(func)


def _run(body):
    code = _block_network_source() + "\n" + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


def test_guard_installs_in_a_clean_interpreter():
    pytest.importorskip("requests")

    result = _run("""
        import sys
        assert "ssl" not in sys.modules, "precondition: ssl must not be preloaded"
        _block_network()
        print("installed")
    """)

    assert result.returncode == 0, result.stderr
    assert "installed" in result.stdout


def test_guard_still_blocks_the_network():
    """Moving the import must not weaken the guard it exists to install."""
    pytest.importorskip("requests")

    result = _run("""
        import socket
        _block_network()
        import requests

        blocked = 0
        for call in (
            lambda: socket.socket(),
            lambda: socket.create_connection(("example.com", 443)),
            lambda: requests.get("https://example.com"),
            lambda: requests.Session().get("https://example.com"),
        ):
            try:
                call()
            except RuntimeError as e:
                assert "attempted to use the network" in str(e)
                blocked += 1
        print(f"blocked={blocked}")
    """)

    assert result.returncode == 0, result.stderr
    assert "blocked=4" in result.stdout
