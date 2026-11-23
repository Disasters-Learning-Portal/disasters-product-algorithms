"""tools/check_sensor_consistency.py: the notebook rules, including retirement.

The lint runs in CI and as a pre-commit hook, but nothing exercised its notebook
logic against anything except the live tree -- so a rule could only ever be seen
passing. These build small notebook trees under tmp_path and point the tool at
them, which is the only way to watch each rule FAIL.

What changed and why this file exists: a workflow that a newer pipeline has
superseded is retired to notebooks/legacy/ instead of being deleted. The old
rule ("notebooks/<sensor>_workflow.ipynb must exist") would have forced the
choice between deleting a working fallback and renaming the new notebook over
the old name.
"""
import importlib.util
import json
import os

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TOOL = os.path.join(REPO_ROOT, "tools", "check_sensor_consistency.py")

SENSORS = ["capella", "landsat", "sentinel2"]


def _notebook(path, cell0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "cells": [{"cell_type": "markdown", "metadata": {}, "source": [cell0]}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 4,
    }))


@pytest.fixture
def lint(tmp_path, monkeypatch):
    """The tool, loaded by path, rooted at an empty notebooks/ tree."""
    spec = importlib.util.spec_from_file_location("_check_sensor_consistency", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    nb = tmp_path / "notebooks"
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "NOTEBOOKS_DIR", nb)
    monkeypatch.setattr(mod, "TESTING_NOTEBOOKS_DIR", nb / "testing-notebooks")
    monkeypatch.setattr(mod, "LEGACY_NOTEBOOKS_DIR", nb / "legacy")
    mod.nb = nb
    return mod


def _check(lint, sensor="landsat"):
    return lint.check_notebook_conformance(sensor, SENSORS)


class TestCurrentWorkflow:
    def test_the_classic_pair_passes(self, lint):
        _notebook(lint.nb / "landsat_workflow.ipynb", "# Landsat workflow")
        _notebook(lint.nb / "testing-notebooks" / "landsat_workflow.ipynb", "# Landsat (dev)")
        assert _check(lint) == []

    def test_no_notebook_at_all_fails(self, lint):
        _notebook(lint.nb / "testing-notebooks" / "landsat_workflow.ipynb", "# Landsat (dev)")
        failures = _check(lint)
        assert len(failures) == 1
        assert "no matching notebook at notebooks/landsat_workflow.ipynb" in failures[0]

    def test_missing_testing_twin_fails(self, lint):
        _notebook(lint.nb / "landsat_workflow.ipynb", "# Landsat workflow")
        failures = _check(lint)
        assert len(failures) == 1
        assert "testing notebook" in failures[0]


class TestRetiredWorkflow:
    def test_retired_with_a_successor_passes(self, lint):
        _notebook(lint.nb / "legacy" / "landsat_workflow.ipynb", "# Landsat workflow")
        _notebook(lint.nb / "landsat_odr_workflow.ipynb", "# Landsat STAC workflow")
        _notebook(lint.nb / "testing-notebooks" / "landsat_workflow.ipynb", "# Landsat (dev)")
        assert _check(lint) == []

    def test_retired_without_a_successor_fails(self, lint):
        """The whole point of the rule: a sensor never ends up with only a
        retired notebook."""
        _notebook(lint.nb / "legacy" / "landsat_workflow.ipynb", "# Landsat workflow")
        _notebook(lint.nb / "testing-notebooks" / "landsat_workflow.ipynb", "# Landsat (dev)")
        failures = _check(lint)
        assert len(failures) == 1
        assert "only retired notebooks" in failures[0]
        assert "notebooks/legacy/landsat_workflow.ipynb" in failures[0]

    def test_a_copy_left_at_the_top_level_fails(self, lint):
        _notebook(lint.nb / "landsat_workflow.ipynb", "# Landsat workflow")
        _notebook(lint.nb / "legacy" / "landsat_workflow.ipynb", "# Landsat workflow")
        _notebook(lint.nb / "testing-notebooks" / "landsat_workflow.ipynb", "# Landsat (dev)")
        failures = _check(lint)
        assert len(failures) == 1
        assert "exists both at" in failures[0]

    def test_cell0_is_still_checked_on_a_retired_notebook(self, lint):
        _notebook(lint.nb / "legacy" / "landsat_workflow.ipynb", "# Capella workflow")
        _notebook(lint.nb / "landsat_odr_workflow.ipynb", "# Landsat STAC workflow")
        _notebook(lint.nb / "testing-notebooks" / "landsat_workflow.ipynb", "# Landsat (dev)")
        failures = _check(lint)
        assert any("does not mention the sensor name 'landsat'" in f for f in failures)
        assert any("mentions another sensor" in f and "capella" in f for f in failures)

    def test_cell0_is_checked_on_the_successor(self, lint):
        """Before this change a <sensor>_odr_workflow.ipynb was never looked at."""
        _notebook(lint.nb / "legacy" / "landsat_workflow.ipynb", "# Landsat workflow")
        _notebook(lint.nb / "landsat_odr_workflow.ipynb", "# Sentinel-2 STAC workflow")
        _notebook(lint.nb / "testing-notebooks" / "landsat_workflow.ipynb", "# Landsat (dev)")
        failures = _check(lint)
        assert any("landsat_odr_workflow.ipynb" in f and "sentinel-2" in f for f in failures)


class TestOwnership:
    @pytest.mark.parametrize("name,owner", [
        ("landsat_workflow.ipynb", "landsat"),
        ("landsat_odr_workflow.ipynb", "landsat"),
        ("sentinel2_odr_workflow.ipynb", "sentinel2"),
        ("capella_workflow.ipynb", "capella"),
        ("product_roundtrip_smoketest.ipynb", None),   # not a workflow notebook
        ("umbra_workflow.ipynb", None),                # no such sensor in SENSORS
    ])
    def test_owner_of(self, lint, name, owner):
        assert lint.owner_of(name, SENSORS) == owner

    def test_longest_sensor_name_wins(self, lint):
        """`landsat_tm_workflow.ipynb` belongs to a `landsat_tm` package when one
        exists, not to `landsat` as a variant."""
        sensors = ["landsat", "landsat_tm"]
        assert lint.owner_of("landsat_tm_workflow.ipynb", sensors) == "landsat_tm"
        assert lint.owner_of("landsat_odr_workflow.ipynb", sensors) == "landsat"

    def test_another_sensors_notebook_is_not_counted_as_a_successor(self, lint):
        _notebook(lint.nb / "legacy" / "landsat_workflow.ipynb", "# Landsat workflow")
        _notebook(lint.nb / "sentinel2_odr_workflow.ipynb", "# Sentinel-2 STAC workflow")
        _notebook(lint.nb / "testing-notebooks" / "landsat_workflow.ipynb", "# Landsat (dev)")
        assert any("only retired notebooks" in f for f in _check(lint))


def test_the_real_tree_passes():
    """The repo itself, through the tool's own entry point."""
    spec = importlib.util.spec_from_file_location("_check_sensor_consistency_live", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main() == 0
