# Disasters JupyterHub Deployment

How this package reaches the VEDA JupyterHub image, and the cache-buster
pattern that makes algorithm updates actually land.

## Two-repo build flow

```
disasters-product-algorithms (this repo)
  push to main
        │
        │ .github/workflows/trigger-docker-rebuild.yml
        │ fires repository_dispatch with client_payload.sha
        ▼
pangeo-notebook-veda-image (Disasters-Learning-Portal)
  .github/workflows/build-and-push.yaml runs
        │
        │ jupyter-repo2docker → uses repo's own Dockerfile
        │ (NOT auto-detected env; the Dockerfile takes precedence)
        ▼
  Dockerfile:
    ADD environment.yml environment.yml
    ARG GH_PAT
    ARG ALGORITHMS_SHA=unknown                  ← cache-buster
    RUN echo "Building with algorithms SHA: $ALGORITHMS_SHA" && \
        conda env update --prefix /srv/conda/envs/notebook --file environment.yml ...
        │
        │ environment.yml contains:
        │   pip:
        │     - git+https://github.com/Disasters-Learning-Portal/disasters-product-algorithms.git
        ▼
  Image published to Docker Hub as
    disasters-jupyterhub-docker-image:latest  (and :<sha-12>)
```

There are three branch-specific variants in the image repo:
`build-and-push.yaml` (main), `build-and-push-dev.yaml` (dev), and
`build-and-push-testmerge.yaml` (testMerge). Each is triggered by a matching
workflow here (`trigger-docker-rebuild*.yml`) and produces a separate Docker
Hub repo (`...image`, `...image-dev`, `...image-testmerge`).

## Why the cache-buster is required

The Dockerfile's `RUN conda env update` layer is keyed on the contents of
`environment.yml`. The pip line in that file is unpinned
(`git+https://.../algorithms.git` — no `@ref`). Without the cache-buster:

1. Push to algorithms `main` fires `repository_dispatch`.
2. Image-repo workflow runs `jupyter-repo2docker --cache-from=...:latest`.
3. Docker sees `environment.yml` unchanged → **reuses the cached `RUN` layer**.
4. No new clone of this repo happens. New algorithm code never lands.
5. Image is retagged and pushed under a new SHA — same content.

The symptom: a fresh hub pod is missing `process_satellogic`,
`process_landsat89`, etc. — because the cached install layer predates when
the entry points were added. The usual "fix" (`pip install -e .` in a
notebook terminal) writes the scripts into the conda env, but they're gone
on the next pod spawn because the image layer is restored from cache.

The cache-buster `ARG ALGORITHMS_SHA` changes per algorithms commit (passed
via `--build-arg ALGORITHMS_SHA=${{ github.event.client_payload.sha || github.sha }}`),
which changes the `RUN` layer's cache key, forcing the conda+pip step to
actually re-execute.

## Source of truth files

| File | Lives in | Role |
|---|---|---|
| `pyproject.toml` (comment block above `[project.optional-dependencies]`) | this repo | **DEV-LOCAL spec.** What to `conda install` for local development. Documentation only — not consumed by the image build. |
| `pyproject.toml` (`[project] dependencies`) | this repo | **Pip-installable deps** that flow into the image transitively via the existing `pip: - git+https://...algorithms.git` line. **Prefer this path** for any new dep that ships a manylinux wheel — zero touch of the image repo required. |
| `hub-conda-deps.txt` | this repo | **Conda-only deps** that the image must install ON TOP OF the Pangeo base. Empty by default. Source of truth for the managed block in image-repo `environment.yml`. Auto-syncs (see below). |
| `pangeo-notebook-veda-image/environment.yml` | image repo | The file `repo2docker` actually reads. Contains the Pangeo extras, the `pip: - git+https://...algorithms.git` install line, **and** a managed block populated by the auto-sync workflow. |

**Do not create `environment.yml` in this repo.** repo2docker is invoked on
the image repo's checkout, not this one, so any env file here is ignored.

## Adding dependencies — decision flow

```
Need a new dep?
    │
    ├── pip-installable (manylinux wheel exists)?
    │      │
    │      └── YES → add to `[project] dependencies` in pyproject.toml.
    │                Push to main. Done. (Cache-buster + dispatch handle it.)
    │
    └── conda-only (binary system lib, GDAL plugin, etc.)?
           │
           └── add a line to hub-conda-deps.txt. Push to main.
               The sync-conda-deps workflow opens an auto-PR in
               pangeo-notebook-veda-image. Review + merge that PR.
               Next image build picks it up via ALGORITHMS_SHA.
```

You only need to **interact with** the image repo when adding a conda-only
dep — and even then, it's a one-click merge of an auto-opened PR, not
manual editing.

## How the auto-sync works

`.github/workflows/sync-conda-deps.yml` (in this repo):

1. Triggers on push to `main` that touches `hub-conda-deps.txt`.
2. Checks out this repo + `pangeo-notebook-veda-image` (using
   `secrets.PANGEO_REBUILD_TOKEN`, same PAT used by the dispatch trigger).
3. Reads `hub-conda-deps.txt`, then replaces the contents of the
   `# === BEGIN/END disasters-product-algorithms managed conda deps ===`
   block in the image-repo `environment.yml`.
4. Opens a PR in the image repo (`peter-evans/create-pull-request`)
   for human review and merge.

After that PR merges, the existing dispatch + cache-buster handles the
rest: any subsequent push to algorithms `main` (or the merge itself, since
the image-repo workflow runs on push) triggers a real rebuild that picks
up both the new conda dep and the latest algorithms code.

**One-time setup already done:** `pangeo-notebook-veda-image/environment.yml`
has the BEGIN/END managed-block markers inserted between the workaround
deps (`pystac`) and the `- pip` line. If you ever need to move them or
re-add them, the markers are literally:

```
  # === BEGIN disasters-product-algorithms managed conda deps (do not edit by hand) ===
  # === END disasters-product-algorithms managed conda deps ===
```

## Debugging: `process_*` CLI missing in a fresh hub pod

Don't reach for `pip install -e .` first — that masks the real problem.
Order of checks:

1. **Image-repo `environment.yml` line 26-ish**: confirm the
   `git+https://...algorithms.git` line is present.
2. **Most recent build log on Docker Hub or GH Actions**: look for the
   `pip install` step and any error from the algorithms-repo install. A
   build that completes in well under 5 minutes is suspicious — that's
   probably full-cache reuse, meaning the install didn't actually run.
3. **The image's installed version**:
   ```
   docker pull <user>/disasters-jupyterhub-docker-image:latest
   docker run --rm <user>/disasters-jupyterhub-docker-image:latest \
     bash -lc 'which process_satellogic && \
               pip show disasters-product-algorithms | grep -E "Version|Location"'
   ```
4. **`ALGORITHMS_SHA` cache-buster is wired**: confirm
   `pangeo-notebook-veda-image/Dockerfile` has `ARG ALGORITHMS_SHA` and the
   three `build-and-push*.yaml` files pass
   `--build-arg ALGORITHMS_SHA=${{ github.event.client_payload.sha || github.sha }}`.

The `pip install -e .` workaround stays valid for hot-iterating local
edits inside a single hub session (see [README.md](../README.md) "Development
in JupyterHub" section), but should never be the answer to "the image is
broken on every fresh pod."
