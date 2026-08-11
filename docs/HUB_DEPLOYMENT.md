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
        │   paths (allowlist): image/**, src/**, pyproject.toml, <workflow file>
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

Why `--no-deps`: the MAAP base image + Layer 1's conda env already
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

### Dockerfile gotcha: `ADD --chown=` for files NB_USER will later delete

The base image declares `USER ${NB_USER}` (`jovyan`) before our `RUN` steps,
so everything in `image/Dockerfile` runs as the non-root notebook user.
`ADD` and `COPY` default to creating files owned by `root` — fine for
files that just need to be read by NB_USER, but **fatal if NB_USER later
needs to delete them**. The first post-consolidation build failed at:

```
rm: cannot remove '/tmp/environment.yml': Operation not permitted
```

Fix: pass `--chown=${NB_USER}:${NB_USER}` to the ADD so NB_USER owns the
file. The current `image/Dockerfile` line 11 reflects this:

```dockerfile
ADD --chown=${NB_USER}:${NB_USER} image/environment.yml /tmp/environment.yml
```

Rule of thumb: if a RUN step downstream of an ADD/COPY removes or
mutates the same file, add `--chown=${NB_USER}:${NB_USER}` to the
ADD/COPY directive.

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

For a curated shortlist of **JupyterLab extensions that complement the MAAP DPS workflow**
(COG preview, notebook productivity, MAAP ecosystem tiles) — plus what's already in the Pangeo
base and what to skip — see [HUB_EXTENSIONS.md](HUB_EXTENSIONS.md).

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

Bumping the base image: edit `image/Dockerfile`'s top line —
`FROM mas.maap-project.org/root/maap-workspaces/2i2c/pangeo:<tag>` —
to the new tag (browse tags at the MAAP container registry:
https://repo.maap-project.org/root/maap-workspaces/container_registry/).
Push the change; Layer 1 invalidates cleanly, Layer 2 cache is preserved
(different cache key). The MAAP base is a NASA-VEDA / pangeo derivative, so
`NB_USER=jovyan` and the `/srv/conda/envs/notebook` prefix are unchanged — the
`--chown` gotcha below still applies. It also ships `maap-py` + the MAAP
JupyterLab extensions, so those are inherited (not pinned in `environment.yml`).

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

2. **Did the push touch a real image input?** Both workflows use a
   **`paths` allowlist** (switched 2026-07-21 from a `paths-ignore`
   denylist): a build fires ONLY when `image/**`, `src/**`,
   `pyproject.toml`, or the build workflow file itself changes. A push
   touching only `dps/`, `docs/`, `notebooks/`, `tests/`, `tools/`,
   `.github/` (other workflows), `bin/`, `dev-conda-deps.txt`, etc.
   fires no rebuild — all of those are `.dockerignore`d out of the
   build context, so they can't change image content anyway. The
   allowlist is exhaustive by construction (the old denylist leaked:
   `dps/` wasn't on it, so every `dps/` push triggered a redundant
   byte-identical rebuild). Caveat: a CLI added in the same commit as
   a `docs/`-only README change still ships because that commit also
   touches `src/`; but a `dps/`-only commit won't rebuild the hub
   image (correct — `dps/` isn't in the image).

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
  rebuild → check the `paths` allowlist: maybe the push touched nothing
  in `image/**`, `src/**`, or `pyproject.toml`, so the build shouldn't
  have fired at all (in which case it didn't), or Layer 2's COPY didn't
  actually pick up the file you expected (check `.dockerignore` for an
  accidental over-exclusion).
- "Successfully installed disasters-product-algorithms-..." line is
  **missing** from the build log → Layer 2 was cached entirely. Means
  the COPYed file tree post-`.dockerignore` was bit-identical to the
  prior build. This is correct behavior; the image content from the
  prior build is still valid.
- Layer 1 ran when you didn't expect it to → somebody touched
  `image/environment.yml`. Confirm via `git log -- image/environment.yml`.

## Customizing the JupyterLab UI (baked-in `overrides.json`)

The image ships an [`image/overrides.json`](../image/overrides.json) that
customizes JupyterLab's UI with **no extension and no node build**. The
Dockerfile COPYs it to:

```
/srv/conda/envs/notebook/share/jupyter/lab/settings/overrides.json
```

— the system-wide JupyterLab settings-override path
(`<sys-prefix>/share/jupyter/lab/settings/overrides.json`); the sys-prefix is
the same `/srv/conda/envs/notebook` conda env Layer 1 builds into.

Current use: a **Help → Disasters Resources** submenu — *Report a Bug* → repo
Issues, *Community Forum* → repo GitHub Discussions, *Email Support* → `mailto:`.
Added 2026-07-01 (commit `c6a4a97`), via the built-in `help:open` command.

Non-obvious rules when editing it:

- **Menu customizations MERGE with the defaults; they do NOT replace them.**
  `@jupyterlab/mainmenu-extension:plugin` special-cases this — for every *other*
  plugin, `overrides.json` *replaces* the default settings. So appending items
  to the Help menu (`id: jp-mainmenu-help`) keeps JupyterLab's built-in Help
  entries (About, reference links, …) intact. Don't try to re-declare the
  defaults.
- **External links MUST set `"newBrowserTab": true`.** Otherwise `help:open`
  loads the URL in an *in-lab iframe*, and any site sending
  `X-Frame-Options: DENY` or a restrictive `frame-ancestors` CSP (GitHub and
  most SaaS) renders a blank "refused to connect" pane. Every link in our
  submenu sets it.
- **Submenu item shape (JupyterLab 4 schema):** an item with
  `"type": "submenu"` carries a nested `submenu` object (`id`, `label`,
  `items`); leaf items are
  `{"command": "help:open", "args": {"text", "url", "newBrowserTab"}, "rank"}`.
- **`label` goes on the `submenu` object, NOT on the item.** The mainmenu
  schema sets `additionalProperties: false` on a menu *item*, so a stray
  `label` (or any other unknown key) on the submenu item fails settings
  validation (`data @ /menus/8/items/1 ... must NOT have additional
  properties`) and takes down the ENTIRE `mainmenu-extension` plugin —
  cascading into `help-extension:resources` + `mainmenu-extension:recents`,
  so *no* custom menu renders. The submenu's display name comes from the
  nested `submenu.label`. Discovered + fixed 2026-07-01 (commit `06bd552`).
- **No rebuild needed to iterate on the JSON** — mount it over the baked-in
  path in any existing image and launch Lab:
  ```bash
  docker run -p 8888:8888 \
    -v "$PWD/image/overrides.json":/srv/conda/envs/notebook/share/jupyter/lab/settings/overrides.json \
    <image> jupyter lab --ip 0.0.0.0
  ```
  Then check **Help → Disasters Resources**. `python -m json.tool
  image/overrides.json` catches syntax errors before you build.
- **It is its own COPY layer** (between Layer 1 and Layer 2 in the Dockerfile),
  and `image/**` **is** in the workflows' `paths` allowlist — so editing
  `overrides.json` *does* trigger a hub rebuild (unlike a docs-only change). It
  busts its own layer + Layer 2 (~30-60s); conda Layer 1 stays cached.

Ref: JupyterLab
[Interface Customization](https://jupyterlab.readthedocs.io/en/latest/user/interface_customization.html).

## Debugging: the whole Lab UI looks broken (arrows everywhere, clipped chrome)

Distinct failure class from the missing-CLI one above — this is **CSS**, not packaging.
Symptom: small chevron/arrow pairs over the entire UI (menu bar, *every* toolbar button,
breadcrumb, `Name | Modified` header, tab bar), menu bar clipped, while Launcher tiles
render at normal size. **Those arrows are scrollbar stepper buttons, not icons.**
Reported as [2i2c-org/infrastructure#8770](https://github.com/2i2c-org/infrastructure/issues/8770).

**Step 0 — are you even running the image you think you are?** A fix merged to `dev`
rebuilds only `…-dev:latest`; the hub's `klesinger/disasters-jupyterhub-docker-image:latest`
is built from **`main`**. Concluding "the fix didn't work" while the pod runs the old
prod image has already cost one debugging cycle (rule 40). Check on the pod:

```bash
jupyter labextension list            # is the thing you removed still there?
pip show <package-you-removed>
```

**Known cause — patched in the image since 2026-08-11.** `maap-dps-jupyter-extension` and
`maap_algorithms_jupyter_extension` both ship `.lm-Widget { overflow: scroll !important; }`
— leftover placeholder CSS from the JupyterLab extension cookiecutter. `.lm-Widget` is
Lumino's base class, on *every* widget; `scroll` (unlike `auto`) paints a scrollbar even
with nothing to scroll, and `!important` beats Lab's own per-widget `overflow`. Lab's
few-px-tall chrome then has no room for a track or thumb, so only the two stepper buttons
render. Nothing is rescaled, which is why the Launcher looks fine.

`image/Dockerfile` now runs
[`image/scripts/fix_lm_widget_overflow.py`](../image/scripts/fix_lm_widget_overflow.py)
to rewrite `scroll` -> `auto`, keeping the MAAP Launcher tiles working. It must NOT delete the declaration: the MAAP panels rely on it for their own scrolling (Lumino's base is `overflow: hidden`, and the Submit Jobs form is taller than its panel), and an earlier delete-it patch made that form impossible to scroll. `auto` shows a scrollbar only on real overflow, so their forms scroll and Lab's chrome stays quiet. On an unpatched
image, run the same script in place and hard-refresh, or fall back to
`jupyter labextension disable maap-dps-jupyter-extension maap_algorithms_jupyter_extension`
(costs the tiles; reversible with `enable`). See [HUB_EXTENSIONS.md](HUB_EXTENSIONS.md) and
`.clinerules.md` rule 39.

**The general class.** A federated extension's stylesheet is injected into `document.head`
**unscoped**, so any bare `*` / `html` / `body` rule — or a framework reset like Bootstrap
Reboot, Tailwind preflight, `normalize.css` — restyles the whole application.
`jupyterlab-bxplorer` was removed in PR #94 for exactly this (unscoped Bootstrap 5); that
was a real defect, though **not** the cause of #8770.

Why it is hard to catch: **Chrome-only** (Firefox and macOS overlay scrollbars have no
stepper arrows); **intermittent per user**, so one clean load is not proof — hard-reload
several times; and **invisible to CI**, since `cli-smoke` and `check_sensor_consistency.py`
never load a browser.

Triage tooling (both in `tools/`):

```bash
# On the pod — scans EVERY installed labextension, base-image ones included,
# for globally-scoped CSS. Exits non-zero on a hit, so it works as a check.
python tools/audit_labextension_css.py
python tools/audit_labextension_css.py --json > /tmp/css_audit.json

# In the Chrome console — paste tools/lab_ui_diagnostic.js. Downloads a JSON
# capture: browser zoom, computed <body> typography vs --jp-*, every stylesheet
# carrying a global rule, and per-element overflow state for each bit of chrome.
```

Run the audit after **any** hub-image extension change. Prefer extensions that scope their
styles (`@scope`, a build-time class prefix, MUI's `ScopedCssBaseline`). The unposted
upstream reports, plus a script that re-checks a future bxplorer release straight from the
PyPI wheel, are in [BXPLORER_BOOTSTRAP_ISSUE.md](BXPLORER_BOOTSTRAP_ISSUE.md).

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
