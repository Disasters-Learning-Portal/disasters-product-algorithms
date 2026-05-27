# Disasters JupyterHub Deployment

How this package reaches the VEDA JupyterHub image, and the cache-buster
pattern that makes algorithm updates actually land.

## Two-repo build flow

```
disasters-product-algorithms (this repo)
  push to <branch>
        │
        │ .github/workflows/trigger-docker-rebuild*.yml
        │ fires repository_dispatch with client_payload.sha
        ▼
pangeo-notebook-veda-image (Disasters-Learning-Portal)
  .github/workflows/build-and-push*.yaml runs
        │
        ├─ Step 1: resolve algorithms ref to a concrete SHA
        │    - dispatch trigger    → use client_payload.sha
        │    - direct image push   → gh api .../git/ref/heads/<branch>
        │      where <branch> = main (prod variant) or dev (dev variant)
        │
        └─ Step 2: jupyter-repo2docker (uses repo's own Dockerfile)
             │
             ▼
  Dockerfile (two RUN layers):
    ADD environment.yml environment.yml
    ARG GH_PAT
    RUN conda env update ...                          ← Layer 1
        (env.yml has NO disasters-product-algorithms line;
         this layer is cached on env.yml contents only)
    ARG ALGORITHMS_REF=main
    RUN pip install                                   ← Layer 2
        "git+https://...algorithms.git@$ALGORITHMS_REF"
        (cached per unique SHA — algorithm-only changes
         re-run JUST this small layer, ~30s)
        │
        ▼
  Image published to Docker Hub as
    disasters-jupyterhub-docker-image[-dev]:latest
```

Two branch-specific variants in the image repo, each with its own
default algorithms branch when triggered by a direct push (vs dispatch):

| Image variant | Workflow file | Default algorithms branch |
|---|---|---|
| Prod (`...image`) | `build-and-push.yaml` | `main` |
| Dev (`...image-dev`) | `build-and-push-dev.yaml` | `dev` |

When triggered by `repository_dispatch` (algorithm push), the SHA from
`client_payload.sha` overrides the branch fallback. So your dev-branch
push always lands in the dev image at that exact SHA; the prod image is
unaffected until you merge to main.

## How the two-layer install design fixes two old bugs

Before this design, the Dockerfile had a single `RUN conda env update`
layer that included the algorithms install (via env.yml's pip block, with
no `@ref`). Two consequences both bit us:

1. **Docker cached the layer indefinitely.** `env.yml` rarely changed, so
   Docker reused the cached layer across rebuilds. Algorithm pushes never
   landed. Symptom: `process_satellogic` etc. missing on fresh hub pods,
   workaround `pip install -e .` per session. Fixed by giving the
   algorithms install its own RUN layer cache-keyed on `ALGORITHMS_REF`.

2. **Both image variants installed the same code** (HEAD of algorithms
   `main` — whatever `pip install git+https://.../algorithms.git`
   resolves to without a ref). The "dev image" wasn't dev. Fixed by
   passing per-variant `ALGORITHMS_REF` (`main` for prod, `dev` for dev),
   resolved to a concrete SHA at workflow time.

Bonus from the split: algorithm-only changes now rebuild a tiny pip-install
layer (~30s) instead of the full conda env update (~2-3 min).

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
               pangeo-notebook-veda-image. Review + merge that PR;
               the next image build picks up the new conda dep on
               its own (the algorithms install layer is separate).
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
4. **`ALGORITHMS_REF` per-variant pinning is wired**: confirm
   `pangeo-notebook-veda-image/Dockerfile` has the two-layer design
   (separate `RUN pip install ...@$ALGORITHMS_REF`) and that each
   `build-and-push*.yaml` has a "Resolve algorithms ref" step + passes
   `--build-arg ALGORITHMS_REF=${{ steps.algo.outputs.sha }}`.

The `pip install -e .` workaround stays valid for hot-iterating local
edits inside a single hub session (see [README.md](../README.md) "Development
in JupyterHub" section), but should never be the answer to "the image is
broken on every fresh pod."
