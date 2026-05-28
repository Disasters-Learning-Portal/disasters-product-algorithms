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

0. **Did CI pass on the algorithms commit that pinned the image?**
   `.github/workflows/lint.yml` runs `sensor-consistency` + `cli-smoke`
   on every push to `dev` and `main`. If those jobs failed and the image
   built anyway (e.g. someone bypassed branch protection), the pod will
   show `ModuleNotFoundError: No module named '<sensor>'` even though
   the console-script shim exists in `/srv/conda/envs/notebook/bin/`.
   Fix in algorithms (`tools/check_sensor_consistency.py` shows the
   exact pyproject.toml edit needed), push, wait for the rebuild. This
   check was added after the capella rollout exhibited exactly this
   failure mode — see [AUTOMATION.md §Post-mortems](AUTOMATION.md#post-mortems).

1. **Which image variant did the hub spawn, and which algorithms branch
   has the CLI?** Prod image installs from `main` HEAD; dev image from
   `dev` HEAD. If your CLI lives on `dev` and you're on the prod image,
   you need to merge `dev` → `main` (and wait for the rebuild) before
   it shows up in prod.
2. **Most recent build log on GitHub Actions for the image repo**:
   - Confirm the "Resolve algorithms ref to a concrete SHA" step echoed
     the SHA you expect (e.g. for the dev image, a SHA on the algorithms
     `dev` branch).
   - Confirm the Dockerfile Layer 2 RUN ran (`Installing
     disasters-product-algorithms@<sha>`) and reported
     `Successfully installed disasters-product-algorithms-<version>`.
3. **The image's actually-installed version**:
   ```
   docker pull <DOCKER_USERNAME>/disasters-jupyterhub-docker-image:latest
   docker run --rm <DOCKER_USERNAME>/disasters-jupyterhub-docker-image:latest \
     bash -lc 'which process_satellogic && \
               pip show disasters-product-algorithms | grep -E "Version|Location"'
   ```
4. **`ALGORITHMS_REF` per-variant pinning is wired**: confirm
   `pangeo-notebook-veda-image/Dockerfile` has the two-layer design
   (separate `RUN pip install ...@$ALGORITHMS_REF`), `environment.yml`
   does NOT have a `disasters-product-algorithms` line, and each
   `build-and-push*.yaml` has a "Resolve algorithms ref" step + passes
   `--build-arg ALGORITHMS_REF=${{ steps.algo.outputs.sha }}`.

The `pip install -e .` workaround stays valid for hot-iterating local
edits inside a single hub session (see [README.md](../README.md) "Development
in JupyterHub" section), but should never be the answer to "the image is
broken on every fresh pod."

## Build duration expectations & diagnostic signals

Empirical durations from the 27 May 2026 deployment of this two-layer
design (cached `--cache-from=...:latest`, runs on `ubuntu-latest`):

| Scenario | Conda env update layer | Algorithms install layer | Total wall-clock |
|---|---|---|---|
| Cache-cold (first build, or `env.yml` changed) | ~2-3 min | ~5s (with `--no-deps`) | ~3-4 min |
| Algorithm-only change (`env.yml` unchanged, new `ALGORITHMS_REF`) | cached, <1s | ~5s | ~1-1.5 min |
| No-op (re-trigger with same inputs) | cached, <1s | cached, <1s | ~30-60s |

Wall-clock includes GitHub-runner setup, `pip install jupyter-repo2docker`,
`docker login`, `cache-from` manifest import, and the final `docker push`
to Docker Hub — typically ~1-1.5 min of overhead independent of the build
itself.

**Red flags in build logs:**
- A build that finishes in **under 90 seconds** when you'd expect a real
  rebuild → cache reused something that shouldn't have been. Check that
  `ALGORITHMS_REF` differs from the previous successful build, and that
  the Dockerfile actually references `$ALGORITHMS_REF` in a `RUN`.
- "Resolve algorithms ref" step prints the wrong SHA (e.g., prod build
  resolved a dev-branch SHA) → workflow's fallback branch is wrong, or
  the dispatch payload SHA is bleeding across variants. Check
  `github.event.client_payload.sha || gh api .../heads/<branch>`.
- "Installing disasters-product-algorithms@..." line is **missing** from
  the build log → Layer 2 was cached entirely. Means
  `ALGORITHMS_REF` is identical to a previous build for this image variant.
  This is correct behavior; the image content from that previous build
  is still valid.

## Known limitations / cleanup follow-ups

1. **Image-repo `build-and-push*.yaml` workflows use `on: push:` with no
   branch filter.** Any push to any branch (including transient PR branches
   from auto-sync) fires every workflow. Today the auto-PR branch
   `sync-conda-deps-from-algorithms` triggered 3 spurious builds and even
   tagged a Docker Hub image off that branch. Fix: add
   `on: push: branches: [main]` to all three workflows.
2. **Sync-conda-deps script wipes any comment lines inside the BEGIN/END
   managed block** in image-repo `environment.yml`. The script treats `#`
   lines in `hub-conda-deps.txt` as comments-to-skip, so an empty deps
   list writes a literally-empty block. Mitigation: explanatory comments
   live OUTSIDE the markers; this is documented inline in `environment.yml`
   so future contributors don't drop comments back in.
3. **Stale `disasters-jupyterhub-docker-image-testmerge` Docker Hub repo.**
   Workflow that built it was deleted in this session; the existing image
   in Docker Hub will no longer receive updates. Delete manually via the
   Docker Hub UI if no longer needed.
4. **Stale `merge-aws-conversion` branch in this repo.** No longer tracked
   by any trigger workflow. Delete with
   `git push origin --delete merge-aws-conversion` if no longer needed
   (destructive — confirm first).

## Design history (short)

The fix landed in two iterations on 27 May 2026 because the first round
exposed a deeper bug:

1. **First attempt: cache-buster `ARG ALGORITHMS_SHA`** on the single
   `RUN conda env update` layer. Theory was that the Docker layer was
   cached forever and pip never re-fetched. The buster did force the
   layer to re-run — confirmed in build logs — but it still installed
   algorithms `main` HEAD because the pip line in `environment.yml` had
   no `@ref`. Every image variant (prod / dev / testmerge) silently
   pulled the same `main` code regardless of which trigger fired.
2. **Second attempt (current): two-layer Dockerfile + per-variant
   `ALGORITHMS_REF`.** Algorithms install moves out of `environment.yml`
   into its own `RUN` layer, pinned to a SHA the workflow resolves from
   the dispatch payload or `gh api .../heads/<branch>`. Each variant
   gets its own concrete branch fallback. Solves both the caching bug
   and the wrong-branch bug, plus makes algorithm-only rebuilds ~10x
   faster.

Don't go back to the single-layer design. The `ALGORITHMS_SHA`
cache-buster will appear to work but won't fix the actual bug, because
the bug isn't really about caching — it's about WHAT pip resolves.
