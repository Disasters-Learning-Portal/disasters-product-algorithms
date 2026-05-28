# Disasters JupyterHub Deployment

How this package reaches the VEDA JupyterHub image. As of 2026-05-28 the
image build lives in this repo — `image/` is a checked-in subtree of the
former `pangeo-notebook-veda-image` fork and the build workflows fire
directly on pushes to `dev` / `main`.

## Single-repo build flow

```
disasters-product-algorithms (this repo)
  push to <branch>
        │
        │ .github/workflows/build-and-push{,-dev}.yaml
        │   on: push: branches: [<branch>]
        │   paths-ignore: docs/**, notebooks/**, tests/**, tools/**, **.md
        ▼
  docker build -f image/Dockerfile .   (build context = repo root)
        │
        ├─ Layer 1: ADD image/environment.yml + conda env update
        │            (cached on environment.yml content; ~2-3 min cold)
        │
        └─ Layer 2: COPY . /srv/repo/algorithms + pip install --no-deps
                     (cached on COPYed file content — .dockerignore at the
                      repo root strips notebooks/, docs/, tests/, .git/,
                      tools/, .github/ etc.; ~30s cold)
        │
        ▼
  docker push klesinger/disasters-jupyterhub-docker-image[-dev]:{<sha-12>,latest}
```

Per-branch wiring:

| Branch | Workflow | Docker Hub tag |
|---|---|---|
| `main` | `.github/workflows/build-and-push.yaml` | `klesinger/disasters-jupyterhub-docker-image:{<sha-12>,latest}` |
| `dev` | `.github/workflows/build-and-push-dev.yaml` | `klesinger/disasters-jupyterhub-docker-image-dev:{<sha-12>,latest}` |

Both workflows expose `workflow_dispatch` for manual re-runs from the
Actions UI. `--cache-from <DOCKER_USERNAME>/...:latest` pulls the previous
image's layers as a remote registry cache — survives the move from the
old image repo because Docker Hub doesn't care which CI built the layers.

## Two cache layers

The Dockerfile is intentionally split. Algorithm-only edits (the common
case) invalidate only the small Layer 2; conda env changes invalidate
Layer 1.

- **Layer 1** — `ADD image/environment.yml /tmp/environment.yml` then
  `conda env update`. Cache key is the SHA256 of `environment.yml`'s
  contents. Cold runtime ~2-3 min. Re-runs only when somebody edits
  `image/environment.yml`.
- **Layer 2** — `COPY --chown=... . /srv/repo/algorithms` then
  `pip install --no-deps /srv/repo/algorithms`. Cache key is the SHA256
  of the COPYed file tree (post-`.dockerignore` filtering). Cold runtime
  ~30s. Re-runs on any algorithms code change.

Why `--no-deps`: the Pangeo base image + Layer 1's conda env already
provide everything `[project.dependencies]` resolves to. Letting pip
walk the dep graph would either be a no-op (if conda already satisfies
the requirement) or, worse, install a pip variant that shadows the
conda binary build. Pip-installable deps that aren't in the Pangeo
base + `image/environment.yml` should be added explicitly to the conda
env, not relied on to come in via `pip install`.

Pre-consolidation this used `ARG ALGORITHMS_REF` + `pip install
git+https://...@$ALGORITHMS_REF` keyed on a SHA the workflow resolved
via `gh api .../heads/<branch>`. That mechanism is gone; the algorithms
SHA is implicit in the build context.

## Source of truth files

| File | Lives in | Role |
|---|---|---|
| `pyproject.toml` (`[project] dependencies`) | this repo | **Pip-installable deps.** Installed by Layer 2's `pip install --no-deps` from the local checkout. Prefer this path for any new dep with a manylinux wheel. |
| `dev-conda-deps.txt` | this repo | **Local-dev + CI smoke spec.** Read by `cli-smoke` in `lint.yml` and by contributors setting up a laptop env. Does NOT flow into the hub image. |
| `image/environment.yml` | this repo (subtree) | **Hub-image conda env.** The file Layer 1 actually reads. Add hub-image conda-only deps here. |

There is no longer a separate `hub-conda-deps.txt` (deleted in the
consolidation — its auto-sync target was the old image repo's
`environment.yml`, which is now `image/environment.yml` directly).

## Adding dependencies — decision flow

```
Need a new dep?
    │
    ├── pip-installable (manylinux wheel exists)?
    │      └── YES → add to [project.dependencies] in pyproject.toml.
    │                Pushed to dev / main flows into the next image build
    │                via Layer 2's pip install. Done.
    │
    └── conda-only (binary system lib, GDAL plugin, etc.)?
           │
           ├── Local-dev only?
           │      └── add a line to dev-conda-deps.txt.
           │
           └── Hub image too?
                  └── add a line to image/environment.yml's dependencies:
                      section. (And also to dev-conda-deps.txt if you
                      want laptop parity.)
```

Three files, three audiences, no cross-repo PR ceremony.

## Pulling upstream image changes

The `image/` subtree was added via
`git subtree add --prefix=image https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image.git main` (non-squash, so the
fork's full history is preserved in this repo's `git log`). To pull
future upstream changes:

```bash
git subtree pull --prefix=image \
    https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image.git \
    main --squash
```

The Disasters-Learning-Portal fork is archived but the remote URL
remains valid for read-only fetches. NASA-IMPACT's `pangeo-notebook-veda-image`
upstream-of-upstream is still active; pulls from there go through the
archived fork unless you rewire the remote.

Bumping the Pangeo base image: edit
`image/Dockerfile`'s top line — `FROM pangeo/pangeo-notebook:<tag>` —
to the new tag (see https://github.com/pangeo-data/pangeo-docker-images
for release cadence). Push the change; Layer 1 invalidates cleanly,
Layer 2 cache is preserved (different cache key).

## Debugging: `process_*` CLI missing in a fresh hub pod

The cross-repo "wrong branch on wrong image variant" footgun is mostly
gone — one repo, one push triggers one build per branch — but a few
failure modes remain.

Order of checks:

1. **Did the build workflow actually run?** Recent runs for the branch
   the hub image variant tracks:
   ```bash
   gh run list --branch main --workflow=build-and-push.yaml --limit 3
   gh run list --branch dev  --workflow=build-and-push-dev.yaml --limit 3
   ```
   Look for a green run whose SHA matches the algorithms commit you
   expect to be in the pod.

2. **Was the push doc-only?** `paths-ignore` on both workflows excludes
   `docs/**`, `notebooks/**`, `tests/**`, `tools/**`, `**.md`,
   `.clinerules.md`, `.pre-commit-config.yaml`. A push touching ONLY
   those paths fires no rebuild. Intentional — saves ~2-4 min per
   doc-only push — but it does mean a CLI added in the same commit
   as a README change won't ship until a subsequent code-touching
   push lands.

3. **Did `lint.yml` (`sensor-consistency` + `cli-smoke`) fail on the
   same commit?** The build workflow doesn't gate on lint, so a broken
   pyproject can in principle still publish an image. Confirm:
   ```bash
   gh run list --branch <branch> --workflow=lint.yml --limit 3
   ```
   If lint failed and the build went through anyway, the pod will show
   `ModuleNotFoundError: No module named '<sensor>'` even though the
   console-script shim exists in `/srv/conda/envs/notebook/bin/`. Fix
   the lint failure (`tools/check_sensor_consistency.py` shows the
   exact pyproject edit needed), push, wait for the rebuild. See
   [AUTOMATION.md §Post-mortems](AUTOMATION.md#post-mortems) for the
   original capella rollout that motivated this check.

4. **The image's actually-installed version**:
   ```bash
   docker pull <DOCKER_USERNAME>/disasters-jupyterhub-docker-image:latest
   docker run --rm <DOCKER_USERNAME>/disasters-jupyterhub-docker-image:latest \
     bash -lc 'which process_satellogic && \
               pip show disasters-product-algorithms | grep -E "Version|Location"'
   ```

5. **Which image variant did the hub spawn?** Prod pods use
   `...image:latest`; dev pods use `...image-dev:latest`. If your CLI
   lives on `dev` and you spawned a prod pod, you'll need to merge
   `dev` → `main` and wait for the `build-and-push.yaml` run.

The `pip install -e .` workaround stays valid for hot-iterating local
edits inside a single hub session (see README.md "Development in
JupyterHub"), but should never be the answer to "the image is broken
on every fresh pod."

## Build duration expectations

Empirical durations for the consolidated single-repo build (cached
`--cache-from=...:latest`, GitHub `ubuntu-latest` runner):

| Scenario | Layer 1 (conda) | Layer 2 (algorithms) | Total wall-clock |
|---|---|---|---|
| Cache-cold (first build, or `image/environment.yml` changed) | ~2-3 min | ~30s | ~3-4 min |
| Algorithm-only change (env.yml unchanged) | cached, <1s | ~30s | ~1-1.5 min |
| No-op (re-trigger with same inputs) | cached, <1s | cached, <1s | ~30-60s |

Wall-clock includes runner setup, `docker login`, `cache-from` manifest
import, and `docker push` — typically ~1-1.5 min of overhead independent
of the build itself.

**Red flags in build logs:**

- A build that finishes in under 60 seconds when you'd expect a real
  rebuild → check `paths-ignore`: maybe only doc files changed and the
  build shouldn't have fired at all (in which case it didn't), or
  Layer 2's COPY didn't actually pick up the file you expected (check
  `.dockerignore` for an accidental over-exclusion).
- "Successfully installed disasters-product-algorithms-..." line is
  **missing** from the build log → Layer 2 was cached entirely. Means
  the COPYed file tree post-`.dockerignore` was bit-identical to the
  prior build. This is correct behavior; the image content from the
  prior build is still valid.
- Layer 1 ran when you didn't expect it to → somebody touched
  `image/environment.yml`. Confirm via `git log -- image/environment.yml`.

## Design history (short)

The hub-image build mechanism has gone through three iterations:

1. **Single-layer `conda env update` (pre-2026-05-27).** `environment.yml`
   contained a `pip: - git+https://.../algorithms.git` line with no `@ref`.
   Docker cached the layer indefinitely, so algorithm pushes never
   landed in fresh pods. Workaround was `pip install -e .` per session.
   Bonus bug: every image variant (prod / dev / testmerge) installed
   the same `main` HEAD because the pip line had no ref.

2. **Two-layer + per-variant `ALGORITHMS_REF` (2026-05-27 to 2026-05-28).**
   Split the algorithms install into its own RUN layer pinned to a SHA
   the workflow resolved via `gh api .../heads/<branch>` (or from
   `repository_dispatch` payload). Algorithm-only rebuilds dropped to
   ~30s. The dev image actually installed dev code. This shipped as
   commit `a9cf2ea` and required a cross-repo `repository_dispatch`
   from this repo to `pangeo-notebook-veda-image` plus a separate
   `sync-conda-deps.yml` workflow to mirror `hub-conda-deps.txt` into
   the image repo's `environment.yml` via auto-PR.

3. **Single-repo consolidation (2026-05-28, current).** An architecture
   audit flagged that the two-repo split was paying ongoing complexity
   tax — two PAT secrets (`PANGEO_REBUILD_TOKEN`,
   `PANGEO_REBUILD_TOKEN_DEV`), a cross-repo dispatch contract, a
   sync-conda-deps auto-PR flow, three dep files, and per-variant
   `ALGORITHMS_REF` resolution — for what was effectively one team and
   one release surface. The `pangeo-notebook-veda-image` fork was
   imported into this repo as a `git subtree add --prefix=image`
   (non-squash, full history preserved), the Dockerfile was rewritten
   to `COPY . /srv/repo/algorithms` against the repo-root build context
   (no more `ARG ALGORITHMS_REF` / `ARG GH_PAT`), and new in-repo
   `build-and-push{,-dev}.yaml` workflows replaced the cross-repo
   dispatch. `hub-conda-deps.txt`, `sync-conda-deps.yml`, and both
   `trigger-docker-rebuild*.yml` workflows were deleted. Net effect:
   one repo, one push triggers one build, three dep files collapsed
   to the two with real semantic differences (pip wheels vs hub-image
   conda env), and the "wrong branch on wrong variant" debug surface
   shrank to "did the workflow run, and did lint pass."
