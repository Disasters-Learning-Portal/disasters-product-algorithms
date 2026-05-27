FROM pangeo/pangeo-notebook:2025.08.14

LABEL org.opencontainers.image.source="https://github.com/nasa-impact/pangeo-notebook-veda-image"

USER ${NB_USER}

ADD environment.yml environment.yml

# Accept GitHub PAT as build argument and configure git temporarily
ARG GH_PAT
# Cache-buster: when the algorithms repo dispatches a rebuild, this changes
# per-SHA so the RUN layer below is re-executed and pip actually re-fetches
# disasters-product-algorithms instead of reusing a cached layer.
ARG ALGORITHMS_SHA=unknown
RUN echo "Building with algorithms SHA: $ALGORITHMS_SHA" && \
    if [ -n "$GH_PAT" ]; then \
        git config --global url."https://${GH_PAT}@github.com/".insteadOf "https://github.com/"; \
    fi && \
    conda env update --prefix /srv/conda/envs/notebook --file environment.yml && \
    if [ -n "$GH_PAT" ]; then \
        git config --global --unset-all url."https://github.com/".insteadOf || true; \
    fi

COPY --chown=${NB_USER}:${NB_USER} scripts /srv/repo/scripts