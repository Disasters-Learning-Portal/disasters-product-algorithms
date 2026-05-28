"""
tools/new_sensor.py — one-shot scaffolder for a new sensor pipeline.

Replaces the manual "cp -r capella/ <sensor>/, edit four files, edit two
pyproject blocks, copy two notebooks, strip leftovers" sequence with one
command:

    python tools/new_sensor.py spire

What it does:
  1. Validates the requested sensor name (Python identifier, lowercase,
     not a reserved repo dir like `tools` / `shared_utils`).
  2. Renders four sensor files under `<sensor>/` from
     tools/_templates/sensor/*.tmpl (string.Template substitution).
  3. Mutates pyproject.toml via tomlkit — appends "<sensor>*" to
     [tool.setuptools.packages.find].include, inserts process_<sensor>
     in [project.scripts] keeping alphabetical key order.
  4. Renders both notebook variants from
     tools/_templates/notebooks/*.tmpl (jupytext-style py:percent source
     parsed into nbformat cells; no jupytext runtime dep required).
  5. Runs tools/check_sensor_consistency.py as a post-condition check.
     Rolls back on failure.

Errors loudly (no partial writes left behind) on:
  - sensor dir already exists,
  - sensor name already wired into pyproject,
  - invalid sensor name (contains `-`, starts with digit, reserved word).

Stdlib + tomlkit + nbformat. No jupytext at runtime.
"""

from __future__ import annotations

import argparse
import shutil
import string
import subprocess
import sys
import tomllib
from pathlib import Path

import nbformat
import tomlkit


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = Path(__file__).resolve().parent / "_templates"
PYPROJECT = REPO_ROOT / "pyproject.toml"
CONSISTENCY_LINT = Path(__file__).resolve().parent / "check_sensor_consistency.py"

# Top-level dirs the scaffolder must never overwrite.
RESERVED_NAMES = {
    "tools", "shared_utils", "notebooks", "docs", "tests",
    "raster_tools", "aws-cost", "ec2", "build", "dist",
    "disasters_product_algorithms.egg-info",
    ".github", ".git", ".claude", ".pytest_cache",
}


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------

def validate_name(name: str) -> None:
    """Raise SystemExit with a useful message if ``name`` is not a legal sensor name."""
    if not name:
        sys.exit("ERROR: sensor name is empty.")
    if not name.isidentifier():
        sys.exit(
            f"ERROR: '{name}' is not a valid Python identifier — sensor "
            f"names must be lowercase ASCII, no hyphens, no leading digit. "
            f"Examples: 'spire', 'iceye', 'planetscope'."
        )
    if name != name.lower():
        sys.exit(f"ERROR: sensor name '{name}' must be lowercase.")
    if name.startswith("_"):
        sys.exit(f"ERROR: sensor name '{name}' must not start with underscore.")
    if name in RESERVED_NAMES:
        sys.exit(
            f"ERROR: '{name}' is a reserved top-level directory in this "
            f"repo. Pick a different sensor name."
        )


def check_doesnt_exist(name: str) -> None:
    """Pre-flight: refuse to clobber an existing sensor."""
    sensor_dir = REPO_ROOT / name
    if sensor_dir.exists():
        sys.exit(
            f"ERROR: {sensor_dir} already exists. Remove it or pick a "
            f"different sensor name. (This script never overwrites.)"
        )

    pyproject_text = PYPROJECT.read_text()
    pyproject = tomllib.loads(pyproject_text)
    scripts = pyproject.get("project", {}).get("scripts", {})
    if f"process_{name}" in scripts:
        sys.exit(
            f"ERROR: [project.scripts].process_{name} already exists in "
            f"pyproject.toml. The sensor appears to be partially wired. "
            f"Revert pyproject.toml or pick a different name."
        )

    include = (
        pyproject.get("tool", {}).get("setuptools", {})
        .get("packages", {}).get("find", {}).get("include", [])
    )
    if f"{name}*" in include:
        sys.exit(
            f"ERROR: '{name}*' is already in "
            f"[tool.setuptools.packages.find].include. The sensor appears "
            f"to be partially wired."
        )


# ----------------------------------------------------------------------------
# Substitution
# ----------------------------------------------------------------------------

def render_template(tmpl_path: Path, mapping: dict) -> str:
    text = tmpl_path.read_text()
    return string.Template(text).substitute(mapping)


def render_sensor_files(name: str, bucket: str) -> dict[Path, str]:
    """Map of destination path -> rendered file content for the four sensor files."""
    mapping = {
        "name": name,
        "Name": name.capitalize(),
        "NAME": name.upper(),
        "bucket": bucket,
    }
    src = TEMPLATES_DIR / "sensor"
    sensor_dir = REPO_ROOT / name
    return {
        sensor_dir / "__init__.py":         render_template(src / "__init__.py.tmpl", mapping),
        sensor_dir / "cli.py":              render_template(src / "cli.py.tmpl", mapping),
        sensor_dir / f"process_{name}.py":  render_template(src / "process_name.py.tmpl", mapping),
        sensor_dir / f"{name}_v2.py":       render_template(src / "name_v2.py.tmpl", mapping),
    }


# ----------------------------------------------------------------------------
# py:percent jupytext source -> nbformat
# ----------------------------------------------------------------------------

def parse_percent_source(text: str) -> list:
    """Parse a jupytext py:percent string into a list of nbformat cells.

    Recognises:
      `# %% [markdown]`  -> markdown cell; subsequent `# ...` lines have the
                            leading `# ` stripped to recover the markdown text.
      `# %%`             -> code cell; subsequent lines are taken verbatim.

    The leading `# ---` jupytext frontmatter block is dropped (we only need
    the cell content; the resulting .ipynb gets its own metadata).
    """
    cells: list = []
    current_kind: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        if current_kind is None:
            return
        # Strip a single trailing blank line, leave intentional blank lines inside.
        while current_lines and current_lines[-1] == "":
            current_lines.pop()
        source = "\n".join(current_lines)
        if current_kind == "markdown":
            cells.append(nbformat.v4.new_markdown_cell(source))
        else:
            cells.append(nbformat.v4.new_code_cell(source))

    in_frontmatter = False
    for raw in text.splitlines():
        # Skip the jupytext YAML header block at the top of the file.
        if raw.startswith("# ---") and current_kind is None and not cells and not in_frontmatter:
            in_frontmatter = True
            continue
        if in_frontmatter:
            if raw.startswith("# ---"):
                in_frontmatter = False
            continue

        if raw.startswith("# %% [markdown]"):
            flush()
            current_kind = "markdown"
            current_lines = []
            continue
        if raw.startswith("# %%"):
            flush()
            current_kind = "code"
            current_lines = []
            continue

        if current_kind == "markdown":
            # Markdown cells store text with the leading "# " stripped.
            if raw.startswith("# "):
                current_lines.append(raw[2:])
            elif raw == "#":
                current_lines.append("")
            else:
                current_lines.append(raw)
        elif current_kind == "code":
            current_lines.append(raw)
        # Lines before any `# %%` marker are ignored.

    flush()
    return cells


def render_notebook(template_path: Path, mapping: dict, dest: Path) -> None:
    raw = render_template(template_path, mapping)
    cells = parse_percent_source(raw)
    nb = nbformat.v4.new_notebook()
    nb.cells = cells
    nb.metadata["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    nb.metadata["language_info"] = {"name": "python"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w") as f:
        nbformat.write(nb, f)


# ----------------------------------------------------------------------------
# pyproject.toml mutation (comment-preserving via tomlkit)
# ----------------------------------------------------------------------------

def update_pyproject(name: str) -> str:
    """Mutate pyproject.toml in place; return the original text for rollback."""
    original = PYPROJECT.read_text()
    doc = tomlkit.parse(original)

    # 1. Append "<name>*" to [tool.setuptools.packages.find].include.
    include = doc["tool"]["setuptools"]["packages"]["find"]["include"]
    include.append(f"{name}*")

    # 2. Insert process_<name> into [project.scripts] in alphabetical order
    #    by key, so reviewers see a clean sorted block.
    scripts = doc["project"]["scripts"]
    new_key = f"process_{name}"
    new_value = f"{name}.cli:process_{name}_cli"

    # tomlkit tables don't expose sorted insertion; rebuild.
    items = [(k, scripts[k]) for k in list(scripts.keys())]
    items.append((new_key, new_value))
    items.sort(key=lambda kv: kv[0])

    # Reset and re-add (preserves the section header / surrounding trivia).
    for k in list(scripts.keys()):
        del scripts[k]
    for k, v in items:
        scripts[k] = v

    PYPROJECT.write_text(tomlkit.dumps(doc))
    return original


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------

def rollback(written_paths: list[Path], pyproject_original: str | None) -> None:
    """Best-effort cleanup if the post-condition check fails."""
    for p in written_paths:
        if p.exists():
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    if pyproject_original is not None:
        PYPROJECT.write_text(pyproject_original)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scaffold a new sensor pipeline.",
        epilog=(
            "Example: python tools/new_sensor.py spire\n"
            "         python tools/new_sensor.py spire --bucket csda-spire-delivery"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("name", help="Sensor name (lowercase identifier, e.g. 'spire').")
    parser.add_argument(
        "--bucket", default=None,
        help="Default S3 bucket. Defaults to '<name>-data-bucket'.",
    )
    parser.add_argument(
        "--description", default=None,
        help="One-line notebook description. Defaults to a generic blurb.",
    )
    args = parser.parse_args()

    name = args.name
    bucket = args.bucket or f"{name}-data-bucket"
    description = args.description or (
        f"This notebook demonstrates the complete workflow for retrieving "
        f"and processing {name.capitalize()} imagery using the "
        f"`disasters-product-algorithms` package."
    )

    validate_name(name)
    check_doesnt_exist(name)

    written: list[Path] = []
    pyproject_original: str | None = None

    try:
        # 1. Sensor package files.
        sensor_dir = REPO_ROOT / name
        sensor_dir.mkdir()
        written.append(sensor_dir)
        for dest, content in render_sensor_files(name, bucket).items():
            dest.write_text(content)
            print(f"  Created {dest.relative_to(REPO_ROOT)}")

        # 2. pyproject.toml mutations.
        pyproject_original = update_pyproject(name)
        print(f"  Updated pyproject.toml: added '{name}*' to "
              f"[tool.setuptools.packages.find].include")
        print(f"  Updated pyproject.toml: added "
              f"process_{name} = \"{name}.cli:process_{name}_cli\" "
              f"to [project.scripts]")

        # 3. Notebooks.
        mapping = {
            "name": name,
            "Name": name.capitalize(),
            "NAME": name.upper(),
            "bucket": bucket,
            "description": description,
        }

        nb_main = REPO_ROOT / "notebooks" / f"{name}_workflow.ipynb"
        nb_test = REPO_ROOT / "notebooks" / "testing-notebooks" / f"{name}_workflow.ipynb"
        render_notebook(TEMPLATES_DIR / "notebooks" / "workflow.py.tmpl", mapping, nb_main)
        written.append(nb_main)
        print(f"  Created {nb_main.relative_to(REPO_ROOT)}")
        render_notebook(TEMPLATES_DIR / "notebooks" / "testing_workflow.py.tmpl", mapping, nb_test)
        written.append(nb_test)
        print(f"  Created {nb_test.relative_to(REPO_ROOT)}")

        # 4. Post-condition: the consistency lint must now pass.
        print()
        print("Running tools/check_sensor_consistency.py ...")
        result = subprocess.run(
            [sys.executable, str(CONSISTENCY_LINT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if result.returncode != 0:
            raise RuntimeError(
                "Post-condition check FAILED — the scaffold left the repo "
                "in an inconsistent state. Rolling back."
            )

    except Exception as exc:  # noqa: BLE001 — broad on purpose for rollback
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        rollback(written, pyproject_original)
        print("Rolled back all scaffolder changes.", file=sys.stderr)
        return 1

    print()
    print("Next steps:")
    print(f"  1. Edit {name}/{name}_v2.py — implement "
          f"retrieve_{name}_resources(), sigmaCalib(), apply_filter().")
    print( "  2. Add conda deps to dev-conda-deps.txt if needed.")
    print(f"  3. git add -A && git commit -m \"feat({name}): scaffold new sensor\"")
    print(f"  4. Open PR: feat/{name} -> dev")
    return 0


if __name__ == "__main__":
    sys.exit(main())
