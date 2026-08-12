# =============================================================================
# Self-contained DPS runtime image for disasters-product-algorithms.
# =============================================================================
# Purpose: an OGC Application Package container that runs the DPS sensor CLIs
# (process_capella, process_umbra, process_satellogic, process_landsat89,
# process_sentinel2) via the per-sensor dps/<sensor>/run.sh scripts.
#
# This image BAKES what dps/capella/build-env.sh does at MAAP build time:
#   1. create the `disasters_dps` conda env from dps/environment.yml, and
#   2. `pip install` this repo into that env so the `process_*` console
#      scripts are on PATH for run.sh's `conda run --name disasters_dps`.
#
# It is deliberately LEAN (miniforge, not Pangeo). The heavy Pangeo hub image
# (image/Dockerfile) is for JupyterHub, NOT for a headless DPS worker.
#
# Layout mirrors MAAP's OGC/CWL model: MAAP clones the repo to /app/<repo> and
# runs baseCommand = disasters-product-algorithms/dps/<sensor>/run.sh from /app.
# We therefore place the code at /app/disasters-product-algorithms so a run.sh
# baked into this image resolves ../_validate.sh, ../_finalize.sh, and
# ../environment.yml exactly as it would in a live DPS clone.
#
# BUILD (context = repo root; -f selects this Dockerfile so BuildKit uses the
# sibling dps/Dockerfile.dockerignore instead of the repo-root .dockerignore
# -- the root one EXCLUDES dps/, which would silently drop every run.sh):
#
#   docker build -f dps/Dockerfile \
#     --build-arg SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DISASTERS_PRODUCT_ALGORITHMS=$(git describe --tags) \
#     -t disasters-dps:local .
# =============================================================================

# TEST IMAGE: use :latest so the first build is guaranteed to pull (the pinned
# tag couldn't be verified from the dev sandbox). Once the CI build succeeds,
# PIN to the exact version `latest` resolved to (see the build log's FROM line)
# for reproducibility — tags are <miniforge-version>-<build>, e.g. 24.11.3-2.
FROM condaforge/miniforge3:latest

LABEL org.opencontainers.image.source="https://github.com/Disasters-Learning-Portal/disasters-product-algorithms"
LABEL org.opencontainers.image.description="DPS worker image for NASA Disasters product algorithms (process_* CLIs)"

SHELL ["/bin/bash", "-lc"]

# --- setuptools-scm version handling -------------------------------------
# pyproject.toml derives its version from git tags via setuptools-scm, but the
# build context has no .git/ (excluded by dps/Dockerfile.dockerignore -- heavy
# and cache-busting). Without a tag to discover, `pip install` of the repo
# hard-fails. Set the canonical PRETEND env var setuptools-scm checks BEFORE it
# reaches for .git/. The build workflow passes the real computed version here;
# the default is only a build-time fallback so a local `docker build` succeeds.
ARG SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DISASTERS_PRODUCT_ALGORITHMS=0.0.0+dps
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DISASTERS_PRODUCT_ALGORITHMS=${SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DISASTERS_PRODUCT_ALGORITHMS}

# --- Layer 1: create the `disasters_dps` conda env -----------------------
# Cached on dps/environment.yml content. The env NAME (`disasters_dps`) is
# defined INSIDE that yml (`name: disasters_dps`) and is load-bearing: every
# dps/*/run.sh calls `conda run --name disasters_dps process_<sensor>`. Do NOT
# rename it, and do NOT pass `-n <name>` here -- `conda env update -f` (exactly
# as build-env.sh does) honors the yml's own `name:` and creates that named env.
COPY dps/environment.yml /tmp/dps-environment.yml
RUN conda env update -f /tmp/dps-environment.yml \
    && rm -f /tmp/dps-environment.yml \
    && conda clean -afy

# --- Layer 2: bake the repo code + install the console scripts -----------
# COPY the whole (dockerignore-filtered) context to /app/disasters-product-algorithms.
# dps/Dockerfile.dockerignore keeps exactly what `pip install .` needs
# (pyproject.toml, README.md, src/) plus the dps/ runtime scripts, and drops the
# heavy/dev-only trees. Re-runs when any kept file changes.
WORKDIR /app
COPY . /app/disasters-product-algorithms

# Install the repo into the `disasters_dps` env so the `process_*` console
# scripts land on that env's PATH (this is exactly what build-env.sh does).
# --no-deps is MANDATORY: every runtime dep already comes from conda (Layer 1).
# Letting pip resolve pyproject.toml's [project.dependencies] here risks pulling
# a manylinux GDAL/rasterio wheel that mismatches the conda-provided libgdal --
# the exact dylib-coupling failure CLAUDE.md "Critical Constraints" warns
# against. Same pattern as image/Dockerfile's Layer 2 install.
RUN conda run --live-stream --name disasters_dps pip install --no-deps --no-cache-dir \
        /app/disasters-product-algorithms \
    && conda clean -afy

# run.sh scripts must be executable (git mode may not survive a plain COPY of a
# non-git context; make it explicit and idempotent).
RUN chmod +x /app/disasters-product-algorithms/dps/*/run.sh \
             /app/disasters-product-algorithms/dps/*.sh 2>/dev/null || true

# --- build-time smoke check ----------------------------------------------
# Fail the build EARLY (not at DPS job time) if the env name drifted from
# dps/environment.yml's `name: disasters_dps`, or if any `process_*` entry point
# is broken. `conda run --name disasters_dps` is the exact invocation run.sh
# uses, so this proves the runtime contract holds. --help exits 0 without doing
# real work; the import line proves every sensor package + shared_utils resolves.
# The last two lines cover the UPSTREAM blackmarble package (pip-installed from
# github.com/NASA-IMPACT/veda-black-marble by dps/environment.yml, not vendored here):
# the import proves the VCS install landed with its WRS2 package data, and the --help
# proves its console script + pip-resolved deps (typer/earthaccess/osmnx/pystac-client/
# obstore) all resolved. This is the only build-time gate on that install.
#
# The final loop gates the conda-forge GDAL DRIVER PLUGINS. GDAL 3.9+ ships most
# non-core drivers as separate `libgdal-*` conda packages that a bare `gdal` install
# does NOT pull in, and nothing above notices: `blackmarble --help` and every
# `process_* --help` pass fine without them, so a missing driver only surfaces at DPS
# job time as `plugin gdal_<DRV>.so is not available`. That has now bitten twice --
# JP2OpenJPEG (Sentinel-2 .jp2 bands, silent empty-success) and HDF5 (Black Marble
# VIIRS .h5 granules, permanentFail). Assert on the plugin FILE, not on
# `gdal.GetDriverByName("HDF5")`: GDAL's deferred plugin loading registers a PROXY
# driver even when the .so is absent (that proxy is what emits the "could have been
# recognized by driver HDF5, but plugin ... is not available" message), so
# GetDriverByName returns non-None either way and gates nothing. The glob tolerates
# any version suffix conda-forge adds. Adding a driver dep to dps/environment.yml?
# Add it here too.
RUN conda run --name disasters_dps process_capella --help >/dev/null \
    && conda run --name disasters_dps python -c \
        "import shared_utils, capella, umbra, satellogic, landsat, sentinel2" \
    && conda run --name disasters_dps python -c "import blackmarble" \
    && conda run --name disasters_dps blackmarble --help >/dev/null \
    && conda run --name disasters_dps bash -c '\
        for drv in HDF5 JP2OpenJPEG; do \
          ls "$CONDA_PREFIX"/lib/gdalplugins/gdal_${drv}*.so >/dev/null 2>&1 \
            || { echo "MISSING GDAL plugin gdal_${drv}.so - add the matching libgdal-* package to dps/environment.yml"; exit 1; }; \
        done'

# MAAP's OGC runtime invokes the CWL baseCommand
# (disasters-product-algorithms/dps/<sensor>/run.sh) from /app. Match that CWD.
WORKDIR /app

# The OGC/CWL execution layer overrides the command with the run.sh path at job
# submission time, so CMD is only for interactive debugging. bash (not a specific
# sensor's run.sh) because there is no single default sensor.
CMD ["bash"]
