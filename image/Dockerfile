FROM mas.maap-project.org/root/maap-workspaces/2i2c/pangeo:v6.0.0

LABEL org.opencontainers.image.source="https://github.com/Disasters-Learning-Portal/disasters-product-algorithms"

USER ${NB_USER}

# === Layer 1: conda env update ===
# Cached on image/environment.yml content. Re-runs ~2-3 min only when env
# changes. The build context is the algorithms repo root, so paths in this
# Dockerfile are relative to that root (not image/).
ADD --chown=${NB_USER}:${NB_USER} image/environment.yml /tmp/environment.yml
RUN conda env update --prefix /srv/conda/envs/notebook --file /tmp/environment.yml \
    && rm /tmp/environment.yml

# === Relax the MAAP extensions' `.lm-Widget { overflow: scroll !important }` ===
# maap-dps-jupyter-extension and maap_algorithms_jupyter_extension both ship
# leftover placeholder CSS from the JupyterLab extension cookiecutter that sets
# `overflow: scroll !important` on `.lm-Widget` -- Lumino's base class, present
# on EVERY widget. `scroll` (not `auto`) paints a scrollbar even with nothing to
# scroll, and `!important` beats Lab's own per-widget overflow, so all of Lab's
# few-px-tall chrome renders as bare scrollbar stepper arrows: the "chevron
# icons everywhere" bug (2i2c-org/infrastructure#8770).
#
# The script rewrites `scroll` -> `auto`; it does NOT delete the declaration.
# An earlier version deleted it and broke the MAAP panels: Lumino's base is
# `overflow: hidden`, and the Submit Jobs form is taller than its panel, so it
# became impossible to scroll to the Submit button. The extensions were relying
# on this rule for their own scrolling -- they just scoped it to every widget in
# the app instead of their own. `auto` keeps a scrollbar exactly where content
# genuinely overflows, so their forms scroll and Lab's chrome stays quiet.
#
# These come from the MAAP base image, so environment.yml can't remove them, and
# disabling the extensions would cost the Register Algorithm / Submit Jobs GUI
# tiles. Runs AFTER Layer 1 in case a conda env update reinstates the files.
# Deliberately NOT --require: if a future base image ships the fix upstream,
# this becomes a no-op instead of a build failure. Idempotent.
# See .clinerules.md rule 39.
COPY --chown=${NB_USER}:${NB_USER} image/scripts/fix_lm_widget_overflow.py \
     /tmp/fix_lm_widget_overflow.py
RUN /srv/conda/envs/notebook/bin/python /tmp/fix_lm_widget_overflow.py \
      --root /srv/conda/envs/notebook/share/jupyter/labextensions \
    && rm /tmp/fix_lm_widget_overflow.py

# === JupyterLab Help-menu customization ===
# Bake the "Disasters Resources" submenu into Help (Report a Bug -> GitHub
# Issues, Community Forum -> GitHub Discussions, Email Support -> mailto).
# Merges into the default Help menu via @jupyterlab/mainmenu-extension
# settings -- no extension, no node build. JupyterLab reads system-wide
# setting overrides from <sys-prefix>/share/jupyter/lab/settings/overrides.json;
# the conda prefix here is /srv/conda/envs/notebook (same as Layer 1). Each
# link uses newBrowserTab (GitHub sends X-Frame-Options: DENY, so the in-lab
# iframe would render a refused page).
COPY --chown=${NB_USER}:${NB_USER} image/overrides.json \
     /srv/conda/envs/notebook/share/jupyter/lab/settings/overrides.json

# === Layer 2: install disasters-product-algorithms from local checkout ===
# Pre-consolidation this was `pip install git+https://...@$ALGORITHMS_REF`
# fetched per-build via a build-arg keyed cache. Post-consolidation the
# algorithms code IS the build context — one repo, one CI flow, no
# cross-repo dispatch. .dockerignore at the repo root keeps the COPY
# tight (no notebooks/, docs/, tests/, .git/ etc.). Re-runs ~30s when
# any algorithms file (matched by .dockerignore rules) changes.
#
# Version handling: pyproject.toml uses setuptools-scm with dynamic
# version derived from git tags. .git/ is excluded from the build
# context (it'd be heavy and cache-busting), so setuptools-scm can't
# discover the tag here. The workflow computes the version once via
# `git describe` (it has .git/ access) and passes it as the build-arg
# below; we then set the canonical PRETEND env var that setuptools-scm
# checks before falling back to .git. Per setuptools-scm docs:
# https://setuptools-scm.readthedocs.io/en/latest/usage/#environment-variables
ARG SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DISASTERS_PRODUCT_ALGORITHMS=0.0.0+local
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DISASTERS_PRODUCT_ALGORITHMS=${SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DISASTERS_PRODUCT_ALGORITHMS}

COPY --chown=${NB_USER}:${NB_USER} . /srv/repo/algorithms
RUN /srv/conda/envs/notebook/bin/pip install --no-deps /srv/repo/algorithms

# Image-test harness inherited from NASA-IMPACT upstream. The script
# references /srv/repo/image-tests/ which doesn't exist in our fork — kept
# as a placeholder for future use rather than deleted, since the upstream
# may eventually populate it via `git subtree pull`.
COPY --chown=${NB_USER}:${NB_USER} image/scripts /srv/repo/scripts
