FROM pangeo/pangeo-notebook:2025.08.14

LABEL org.opencontainers.image.source="https://github.com/nasa-impact/pangeo-notebook-veda-image"

USER ${NB_USER}

ADD environment.yml environment.yml

# Accept GitHub PAT as build argument and configure git temporarily
ARG GH_PAT

# === Layer 1: conda env update ===
# Cached on environment.yml contents. Re-runs only when env.yml changes.
# Does NOT install disasters-product-algorithms (see env.yml comment).
RUN if [ -n "$GH_PAT" ]; then \
        git config --global url."https://${GH_PAT}@github.com/".insteadOf "https://github.com/"; \
    fi && \
    conda env update --prefix /srv/conda/envs/notebook --file environment.yml && \
    if [ -n "$GH_PAT" ]; then \
        git config --global --unset-all url."https://github.com/".insteadOf || true; \
    fi

# === Layer 2: install disasters-product-algorithms at a pinned ref ===
# ALGORITHMS_REF is set per image variant by the build-and-push*.yaml
# workflows: prod -> algorithms main SHA, dev -> algorithms dev SHA.
# Each workflow resolves the branch HEAD to a concrete SHA before passing
# it in, so each unique algorithms commit gets its own cached layer
# (~30s rebuild vs full conda env update). Default of "main" matches the
# historical behavior for anyone running `docker build` locally without args.
ARG ALGORITHMS_REF=main
# --force-reinstall + --no-deps: we deliberately want this layer to ALWAYS
# re-install algorithms when its cache key (ALGORITHMS_REF) changes, even
# if the version string in pyproject.toml hasn't been bumped. Without
# --force-reinstall, pip sees "disasters-product-algorithms-X.Y.Z already
# satisfies the requirement" from a previous layer (in a multi-stage cache
# scenario) and skips. --no-deps avoids re-resolving the dep tree on every
# small algorithm change (transitive deps live in the conda env layer).
RUN echo "Installing disasters-product-algorithms@$ALGORITHMS_REF" && \
    if [ -n "$GH_PAT" ]; then \
        git config --global url."https://${GH_PAT}@github.com/".insteadOf "https://github.com/"; \
    fi && \
    /srv/conda/envs/notebook/bin/pip install --force-reinstall --no-deps \
        "git+https://github.com/Disasters-Learning-Portal/disasters-product-algorithms.git@$ALGORITHMS_REF" && \
    if [ -n "$GH_PAT" ]; then \
        git config --global --unset-all url."https://github.com/".insteadOf || true; \
    fi

COPY --chown=${NB_USER}:${NB_USER} scripts /srv/repo/scripts