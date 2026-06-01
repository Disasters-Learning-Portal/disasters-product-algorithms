"""
Tests for shared_utils.version.

Pins the contract that:
  1. __version__ is a non-empty string (so notebooks / PROCESSOR strings
     never embed `None` or empty into COG metadata).
  2. PROCESSOR_STRING starts with the canonical prefix and contains the
     resolved __version__.
  3. The PackageNotFoundError fallback path resolves to "unknown" rather
     than raising on import — important for fresh-kernel notebook runs
     where the package isn't pip-installed yet.
"""

import importlib
import sys

import pytest


class TestVersionModule:
    """Pin shared_utils.version's public contract."""

    def test_version_is_non_empty_string(self):
        from shared_utils import version as version_mod
        assert isinstance(version_mod.__version__, str)
        assert version_mod.__version__ != ""

    def test_processor_string_contains_prefix_and_version(self):
        from shared_utils import version as version_mod
        assert version_mod.PROCESSOR_STRING.startswith(
            "NASA Disasters COG Processor v"
        )
        # The version segment after the prefix must be exactly __version__
        suffix = version_mod.PROCESSOR_STRING[len("NASA Disasters COG Processor v"):]
        assert suffix == version_mod.__version__

    def test_package_not_found_falls_back_to_unknown(self, monkeypatch):
        """When importlib.metadata.version raises PackageNotFoundError
        (e.g. fresh kernel without `pip install -e .`), __version__ should
        be 'unknown' and PROCESSOR_STRING should reflect that — not crash,
        not embed an empty string."""
        import importlib.metadata as _md

        def _raise(_name):
            raise _md.PackageNotFoundError("disasters-product-algorithms")

        monkeypatch.setattr(_md, "version", _raise)

        # Drop any cached copy of shared_utils.version so the module-level
        # try/except actually re-runs against the patched importlib.metadata.
        sys.modules.pop("shared_utils.version", None)

        reloaded = importlib.import_module("shared_utils.version")
        try:
            assert reloaded.__version__ == "unknown"
            assert reloaded.PROCESSOR_STRING == "NASA Disasters COG Processor vunknown"
        finally:
            # Restore an un-patched version for downstream tests.
            sys.modules.pop("shared_utils.version", None)
            importlib.import_module("shared_utils.version")
