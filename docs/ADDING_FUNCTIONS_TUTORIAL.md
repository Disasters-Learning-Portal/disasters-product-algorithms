# Tutorial: Adding New Functions and CLIs

This tutorial walks through the full lifecycle of contributing to
`disasters-product-algorithms`: writing a reusable Python function in
`shared_utils/`, exposing it through the package, and wrapping it in a
command-line entry point that operators can run from the terminal.

We will build one concrete example end-to-end: a `summarize_raster`
function that returns min / max / mean / nodata-count for a GeoTIFF,
plus a `summarize_raster` CLI that runs it from the shell.

---

## Part 1 — Write the Python function

### 1.1 Pick the right module

`shared_utils/` follows **single-responsibility** — one concern per file.
Before creating a new file, check whether an existing module is the
natural home:

| Concern                         | Module                       |
| ------------------------------- | ---------------------------- |
| COG creation / validation       | [cog_utils.py](../shared_utils/cog_utils.py), [cog_creator.py](../shared_utils/cog_creator.py) |
| Reprojection                    | [reprojection.py](../shared_utils/reprojection.py) |
| Compression profiles            | [compression.py](../shared_utils/compression.py) |
| S3 upload / download            | [s3_operations.py](../shared_utils/s3_operations.py), [s3utils.py](../shared_utils/s3utils.py) |
| Filename parsing / event naming | [file_naming.py](../shared_utils/file_naming.py) |
| Raster analysis / inspection    | [geotiff_analyzer.py](../shared_utils/geotiff_analyzer.py), [geotools.py](../shared_utils/geotools.py) |
| Logging                         | [log_utils.py](../shared_utils/log_utils.py) |

Our `summarize_raster` is raster inspection — it belongs in
[geotiff_analyzer.py](../shared_utils/geotiff_analyzer.py). If you have
something genuinely new (e.g. STAC catalog generation), create a new
file rather than overloading an existing one.

### 1.2 Write the function

Keep imports at module top, type-hint the signature, and let exceptions
propagate — callers and the main processor handle error formatting.

```python
# shared_utils/geotiff_analyzer.py

from typing import Dict, Optional
import numpy as np
import rasterio


def summarize_raster(
    path: str,
    band: int = 1,
    nodata: Optional[float] = None,
) -> Dict[str, float]:
    """
    Compute basic statistics for a single band of a GeoTIFF.

    Parameters
    ----------
    path : str
        Absolute path to the GeoTIFF.
    band : int
        1-indexed band number (rasterio convention).
    nodata : float, optional
        Override the file's stored nodata value. If None, uses the
        nodata recorded in the dataset metadata.

    Returns
    -------
    dict with keys: min, max, mean, nodata_count, valid_count
    """
    with rasterio.open(path) as src:
        data = src.read(band, masked=False)
        nd = nodata if nodata is not None else src.nodata

    if nd is not None:
        mask = data != nd
        valid = data[mask]
    else:
        valid = data.ravel()

    return {
        "min": float(valid.min()),
        "max": float(valid.max()),
        "mean": float(valid.mean()),
        "nodata_count": int(data.size - valid.size),
        "valid_count": int(valid.size),
    }
```

**Project conventions to follow:**

- **Absolute paths in code**, never relative. The user runs notebooks
  from many working directories.
- **No print statements** in library code — return data, let the CLI
  print. If you need progress output, use the logger from
  [log_utils.py](../shared_utils/log_utils.py).
- **Clean up temp files** in `finally:` blocks. All temp files belong
  in `/tmp`.
- **Don't catch and swallow** rasterio / GDAL errors. They carry real
  diagnostic info; the operator needs to see them.

### 1.3 Export it from the package

Add the function to [shared_utils/__init__.py](../shared_utils/__init__.py)
inside the existing `try / except ImportError` guarded block, so users
who do not have rasterio installed still get a usable package:

```python
# shared_utils/__init__.py

try:
    from shared_utils.geotiff_analyzer import (
        # ...existing exports...
        summarize_raster,
    )
except ImportError:
    pass  # rasterio not available
```

Now any notebook or downstream module can do:

```python
from shared_utils import summarize_raster
stats = summarize_raster("/data/event/scene_B4.tif")
```

### 1.4 Write a test

Add a focused test under [tests/](../tests/). Hit the real path —
write a small fixture GeoTIFF to a temp dir, run the function, assert.
Do not mock rasterio; the COG / GDAL stack is exactly what we want to
exercise.

```python
# tests/test_geotiff_analyzer.py

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shared_utils import summarize_raster


def test_summarize_raster_respects_nodata(tmp_path):
    path = tmp_path / "fixture.tif"
    arr = np.array([[1, 2, -9999], [3, 4, 5]], dtype="int16")
    with rasterio.open(
        path, "w",
        driver="GTiff", height=2, width=3, count=1,
        dtype="int16", nodata=-9999,
        transform=from_origin(0, 2, 1, 1), crs="EPSG:4326",
    ) as dst:
        dst.write(arr, 1)

    stats = summarize_raster(str(path))
    assert stats["nodata_count"] == 1
    assert stats["valid_count"] == 5
    assert stats["min"] == 1
    assert stats["max"] == 5
```

Run with `pytest tests/test_geotiff_analyzer.py -v`.

---

## Part 2 — Wrap it in a CLI

The project's CLI pattern is consistent across sensors: each
top-level package (`landsat/`, `sentinel2/`, `satellogic/`, `umbra/`)
ships a thin `cli.py` that execs a `process_*.py` script. Standalone
utilities can live in `shared_utils/scripts/` or a new top-level
package — we'll use the latter so the entry point is short and
obvious to operators.

### 2.1 Create the script package

```
disasters-product-algorithms/
└── raster_tools/
    ├── __init__.py        # empty
    ├── cli.py             # entry point wrapper
    └── summarize.py       # actual script
```

### 2.2 The argparse script

[raster_tools/summarize.py](../raster_tools/summarize.py):

```python
#!/usr/bin/env python
"""summarize.py — print min/max/mean/nodata-count for a GeoTIFF."""

import argparse
import json
import sys

from shared_utils import summarize_raster


def main():
    parser = argparse.ArgumentParser(
        description="Print summary statistics for a GeoTIFF band.",
        usage="summarize_raster INPUT [-b BAND] [-n NODATA] [--json]",
    )
    parser.add_argument("input", help="Path to the input GeoTIFF.")
    parser.add_argument("-b", "--band", type=int, default=1,
                        help="1-indexed band number (default: 1).")
    parser.add_argument("-n", "--nodata", type=float, default=None,
                        help="Override nodata value (default: from file).")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of human-readable text.")
    args = parser.parse_args()

    stats = summarize_raster(args.input, band=args.band, nodata=args.nodata)

    if args.json:
        json.dump(stats, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for k, v in stats.items():
            print(f"{k:>14}: {v}")


if __name__ == "__main__":
    main()
```

**Argparse conventions used elsewhere in this repo** (see
[landsat/process_landsat89.py](../landsat/process_landsat89.py)):

- Positional `input` first.
- Short flags use a single dash and lowercase (`-p`, `-date`, `-tile`)
  rather than `--long-form`. The `--json` exception above is reasonable
  for a new util, but match existing style if you extend an existing
  sensor pipeline.
- Defaults are explicit. No silent fallbacks.
- Help strings describe units and accepted values, not just the name.

### 2.3 The thin `cli.py` wrapper

[raster_tools/cli.py](../raster_tools/cli.py) — mirrors the existing
pattern in [landsat/cli.py](../landsat/cli.py):

```python
"""CLI entry points for raster_tools."""
import os
import sys


def summarize_raster_cli():
    """Entry point for `summarize_raster` command."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, "summarize.py")
    with open(script_path) as f:
        code = compile(f.read(), script_path, "exec")
        exec(code, {"__name__": "__main__"})


if __name__ == "__main__":
    summarize_raster_cli()
```

Why this exec-wrapper pattern instead of just calling `main()`? It is
how every other CLI in this repo is structured (see
[landsat/cli.py](../landsat/cli.py),
[sentinel2/cli.py](../sentinel2/cli.py)) — keeping it consistent means
operators can debug any CLI with the same mental model.

---

## Part 3 — Register the CLI

### 3.1 Add the entry point to `pyproject.toml`

[pyproject.toml](../pyproject.toml), `[project.scripts]` section:

```toml
[project.scripts]
process_landsat89 = "landsat.cli:process_landsat89_cli"
process_sentinel2 = "sentinel2.cli:process_sentinel2_cli"
download_sentinel2 = "sentinel2.cli:download_sentinel2_cli"
process_satellogic = "satellogic.cli:process_satellogic_cli"
process_umbra = "umbra.cli:process_umbra_cli"
summarize_raster  = "raster_tools.cli:summarize_raster_cli"   # <-- new
```

And include the new package in the `setuptools` find list:

```toml
[tool.setuptools.packages.find]
include = ["landsat*", "sentinel2*", "satellogic*", "umbra*",
           "shared_utils*", "raster_tools*"]
```

### 3.2 Reinstall the package

Entry points are wired at install time. After editing `pyproject.toml`
you **must** reinstall:

```bash
pip install -e .
```

The `-e` (editable) flag means subsequent edits to `.py` files are
picked up without re-installing — but adding or renaming an entry
point requires a fresh `pip install -e .`.

### 3.3 Verify

```bash
summarize_raster --help
summarize_raster /data/event/scene_B4.tif
summarize_raster /data/event/scene_B4.tif --json
```

If the command is "not found", confirm the active environment is the
one you ran `pip install -e .` in, and that `pip show
disasters-product-algorithms` reports the editable install location
matching this repo.

---

## Part 4 — Checklist before opening a PR

- [ ] Function lives in the right `shared_utils/` module (or new
      single-responsibility file).
- [ ] Type hints on the public signature; docstring with
      Parameters / Returns.
- [ ] Exported in [shared_utils/__init__.py](../shared_utils/__init__.py)
      inside an `ImportError`-guarded block.
- [ ] Pytest covering the happy path + at least one edge case
      (nodata, empty band, wrong dtype).
- [ ] CLI script under a top-level package with `cli.py` +
      `process_*.py` / `<name>.py`.
- [ ] Entry point added to `[project.scripts]` and package included
      in `[tool.setuptools.packages.find]`.
- [ ] `pip install -e .` re-run; `<command> --help` works.
- [ ] Public API entry added to
      [docs/SHARED_UTILS_API.md](SHARED_UTILS_API.md) so the next
      contributor finds it.

That last item is the easiest one to skip and the one operators will
thank you for most.
