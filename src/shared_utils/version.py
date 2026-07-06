"""
Package version + canonical PROCESSOR string for COG metadata.

Single source of truth for "what version of this library wrote this output."
The package version is derived from git tags by setuptools-scm at build time
(see pyproject.toml's [tool.setuptools_scm] block), so the only thing that
ever needs to bump version is a `git tag vX.Y.Z` — not a hand-edited file.

Used by:
  - shared_utils.cog_metadata.detect_activation_event() — stamps PROCESSOR
    into the metadata dict emitted alongside every COG.
  - Operator notebooks (capella, satellogic, umbra, landsat, sentinel2, etc.)
    that build an ACTIVATION_METADATA dict before calling the processor.

Fallback: if the package isn't installed (e.g. notebook run in a fresh kernel
without `pip install -e .`), __version__ falls back to "unknown" and the
PROCESSOR string includes that — so metadata is still well-formed and the
ambiguity is visible rather than hidden behind a hard-coded stale "v1.0".
"""

try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    try:
        __version__ = _pkg_version("disasters-product-algorithms")
    except PackageNotFoundError:
        __version__ = "unknown"
except ImportError:  # pre-3.8 fallback, shouldn't fire on a supported Python
    __version__ = "unknown"

PROCESSOR_STRING = f"NASA Disasters COG Processor v{__version__}"
