FROM pangeo/pangeo-notebook:2025.08.14

LABEL org.opencontainers.image.source="https://github.com/Disasters-Learning-Portal/disasters-product-algorithms"

USER ${NB_USER}

# === Layer 1: conda env update ===
# Cached on image/environment.yml content. Re-runs ~2-3 min only when env
# changes. The build context is the algorithms repo root, so paths in this
# Dockerfile are relative to that root (not image/).
ADD --chown=${NB_USER}:${NB_USER} image/environment.yml /tmp/environment.yml
RUN conda env update --prefix /srv/conda/envs/notebook --file /tmp/environment.yml \
    && rm /tmp/environment.yml

# === Layer 2: install disasters-product-algorithms from local checkout ===
# Pre-consolidation this was `pip install git+https://...@$ALGORITHMS_REF`
# fetched per-build via a build-arg keyed cache. Post-consolidation the
# algorithms code IS the build context — one repo, one CI flow, no
# cross-repo dispatch. .dockerignore at the repo root keeps the COPY
# tight (no notebooks/, docs/, tests/, .git/ etc.). Re-runs ~30s when
# any algorithms file (matched by .dockerignore rules) changes.
COPY --chown=${NB_USER}:${NB_USER} . /srv/repo/algorithms
RUN /srv/conda/envs/notebook/bin/pip install --no-deps /srv/repo/algorithms

# Image-test harness inherited from NASA-IMPACT upstream. The script
# references /srv/repo/image-tests/ which doesn't exist in our fork — kept
# as a placeholder for future use rather than deleted, since the upstream
# may eventually populate it via `git subtree pull`.
COPY --chown=${NB_USER}:${NB_USER} image/scripts /srv/repo/scripts
