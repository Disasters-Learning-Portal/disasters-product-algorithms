# Disasters Hub Image Update Guide

> ⚠️ **PARTIALLY OUTDATED — read [`docs/HUB_DEPLOYMENT.md`](docs/HUB_DEPLOYMENT.md) first.**
>
> This guide is the original setup walkthrough for the two-repo CI/CD flow.
> The high-level architecture (algorithms repo dispatches → image repo
> rebuilds → Docker Hub) is still correct, but three things have changed:
>
> 1. **`environment.yml` does not live in this repo.** It lives in
>    `pangeo-notebook-veda-image`. `jupyter-repo2docker` only reads the
>    env file from the repo it is invoked on (the image repo), so any
>    env file here is ignored. Step 5 of this guide is wrong on that
>    point.
>
> 2. **Conda dependencies are now managed via `hub-conda-deps.txt`
>    (at the root of this repo) with auto-PR.** When you push a change
>    to `hub-conda-deps.txt`, `.github/workflows/sync-conda-deps.yml`
>    opens a PR in `pangeo-notebook-veda-image` updating a managed
>    block in its `environment.yml`. Review + merge that PR; no manual
>    editing of the image repo is needed. **Pip-installable deps are
>    preferred** — put them in `pyproject.toml`'s `[project] dependencies`
>    and skip the image repo entirely.
>
> 3. **`ARG ALGORITHMS_SHA` cache-buster** in
>    `pangeo-notebook-veda-image/Dockerfile` is what makes algorithm
>    pushes actually land in the next image. Without it, the
>    `RUN conda env update` layer is cached on `environment.yml` contents
>    and the unpinned `git+https://...algorithms.git` install never
>    re-runs. The three `build-and-push*.yaml` workflows pass
>    `--build-arg ALGORITHMS_SHA=${{ github.event.client_payload.sha || github.sha }}`
>    to wire it up.
>
> See [docs/HUB_DEPLOYMENT.md](docs/HUB_DEPLOYMENT.md) for the corrected
> deployment story, the decision flow (pip vs conda), auto-sync mechanics,
> and the debug checklist when CLIs go missing on the hub.

A comprehensive guide for updating and maintaining the Disasters Hub Docker image through automated CI/CD pipelines.

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Step-by-Step Instructions](#step-by-step-instructions)
5. [Workflow Configuration Details](#workflow-configuration-details)
6. [Technical Rationale](#technical-rationale)
7. [Secrets and Authentication](#secrets-and-authentication)
8. [Troubleshooting](#troubleshooting)

---

## Overview

This guide describes the process for updating the Disasters Hub JupyterHub Docker image. The system uses a two-repository architecture with automated triggers to rebuild Docker images when algorithm code changes.

### Key Components

| Component | Purpose |
|-----------|---------|
| `disasters-product-algorithms` | Contains all Landsat and Sentinel-2 processing functions |
| `pangeo-notebook-veda-image` | Builds and publishes the Docker image to Docker Hub |
| Docker Hub | Hosts the final container image (`disasters-jupyterhub-docker-image`) |

---

## Architecture

```
┌─────────────────────────────────┐
│  disasters-product-algorithms   │
│  (Algorithm Source Code)        │
│                                 │
│  • Landsat functions            │
│  • Sentinel-2 functions         │
│  • environment.yml              │
└──────────────┬──────────────────┘
               │
               │ Push to main branch
               │ triggers repository_dispatch
               ▼
┌─────────────────────────────────┐
│  pangeo-notebook-veda-image     │
│  (Docker Build Repository)      │
│                                 │
│  • build-and-push.yaml workflow │
│  • jupyter-repo2docker          │
└──────────────┬──────────────────┘
               │
               │ Builds and pushes image
               ▼
┌─────────────────────────────────┐
│  Docker Hub                     │
│  disasters-jupyterhub-docker-   │
│  image:latest                   │
└─────────────────────────────────┘
```

### Why This Architecture?

**Separation of Concerns**: By separating the algorithm code from the Docker build configuration, teams can work on algorithms without needing to understand Docker internals. The build process is abstracted away.

**Automated Rebuilds**: Using GitHub Actions with `repository_dispatch` events enables automatic image rebuilds whenever algorithm code changes, ensuring the deployed image always reflects the latest code.

**Traceability**: Each Docker image is tagged with the Git commit SHA, allowing you to trace any deployed image back to the exact code version.

---

## Prerequisites

Before beginning, ensure you have:

- [ ] Git installed and configured locally
- [ ] Access to the Disasters-Learning-Portal GitHub organization
- [ ] Permissions to create repositories and manage secrets
- [ ] A Docker Hub account
- [ ] GitHub Personal Access Token (Classic) with `repo` scope

---

## Step-by-Step Instructions

### Step 1: Set Up the Algorithm Repository

Create or identify the directory containing all Landsat and Sentinel-2 processing functions.

```bash
# Current repository name
disasters-product-algorithms/
├── landsat/
│   └── [landsat processing functions]
├── sentinel2/
│   └── [sentinel-2 processing functions]
├── environment.yml
└── .github/
    └── workflows/
        └── trigger-rebuild.yaml
```

> **Why a dedicated algorithms repository?**  
> Centralizing all satellite processing functions in one repository ensures consistent versioning, easier dependency management, and simplified testing. It also allows data scientists to work independently of DevOps concerns.

---

### Step 2: Fork the Pangeo Notebook Image Repository

Fork `pangeo-notebook-veda-image` into the Disasters-Learning-Portal organization.

```bash
# Navigate to GitHub and fork:
# https://github.com/[original-org]/pangeo-notebook-veda-image
# Fork to: Disasters-Learning-Portal/pangeo-notebook-veda-image
```

> **Why fork pangeo-notebook-veda-image?**  
> The Pangeo project provides well-maintained, geoscience-focused Jupyter notebook images. Forking allows us to customize the image while benefiting from upstream improvements. The pangeo base image includes optimized configurations for large-scale geospatial data processing.

---

### Step 3: Create a New Branch

In the `disasters-product-algorithms` repository, create a new branch from main:

```bash
git checkout -b combinedENV main
```

> **Why branch from main?**  
> Creating a feature branch allows you to test changes in isolation before merging to main. The naming convention `combinedENV` suggests this branch combines multiple environment configurations—use descriptive names that indicate the branch's purpose.

---

### Step 4: Add the Trigger Workflow

Create `.github/workflows/trigger-rebuild.yaml` in the `disasters-product-algorithms` repository:

```yaml
name: Trigger Docker Image Rebuild

on:
  push:
    branches:
      - main
    paths-ignore:
      - '**.md'
      - 'docs/**'
      - 'notebooks/**'
      - '.github/DOCKER_REBUILD_SETUP.md'

jobs:
  trigger-rebuild:
    name: Notify pangeo-notebook-veda-image
    runs-on: ubuntu-latest

    steps:
      - name: Check if PANGEO_REBUILD_TOKEN is set
        run: |
          if [ -z "${{ secrets.PANGEO_REBUILD_TOKEN }}" ]; then
            echo "::error::PANGEO_REBUILD_TOKEN secret is not set!"
            echo "Please follow the setup instructions in .github/DOCKER_REBUILD_SETUP.md"
            exit 1
          fi
          echo "✓ Token is configured"

      - name: Trigger pangeo-notebook-veda-image rebuild (main branch)
        run: |
          echo "Sending repository_dispatch event to pangeo-notebook-veda-image (main branch)..."

          response=$(curl -w "\n%{http_code}" -X POST \
            -H "Accept: application/vnd.github.v3+json" \
            -H "Authorization: token ${{ secrets.PANGEO_REBUILD_TOKEN }}" \
            https://api.github.com/repos/Disasters-Learning-Portal/pangeo-notebook-veda-image/dispatches \
            -d '{"event_type":"algorithm-updated","client_payload":{"sha":"${{ github.sha }}","ref":"${{ github.ref }}","repository":"${{ github.repository }}"}}')

          http_code=$(echo "$response" | tail -n1)

          if [ "$http_code" = "204" ]; then
            echo "✓ Successfully triggered rebuild on main branch!"
          else
            echo "::error::Failed to trigger main branch build (HTTP $http_code)"
            exit 1
          fi

      - name: Note about branch-specific builds
        run: |
          echo "::notice::repository_dispatch triggers the default branch (main)"
          echo "::notice::To trigger <new branch>, you have two options:"
          echo "::notice::1. Merge main → <new branch> to sync the changes"
          echo "::notice::2. Manually trigger: https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image/actions"

      - name: Workflow summary
        run: |
          echo "### ✅ Docker Image Rebuild Triggered" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "Successfully triggered rebuild of pangeo-notebook-veda-image Docker image on **main branch**." >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "**Triggered Branch:** main (default branch)" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "**Details:**" >> $GITHUB_STEP_SUMMARY
          echo "- Source Commit: \`${{ github.sha }}\`" >> $GITHUB_STEP_SUMMARY
          echo "- Source Ref: \`${{ github.ref }}\`" >> $GITHUB_STEP_SUMMARY
          echo "- Source Repository: \`${{ github.repository }}\`" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "**Monitor Build Progress:**" >> $GITHUB_STEP_SUMMARY
          echo "- [View Actions](https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image/actions)" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "---" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "### 📝 Note: Triggering Other Branches" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "GitHub's \`repository_dispatch\` only triggers workflows on the **default branch** (main)." >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "**To rebuild other branches, choose one option:**" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "1. **Merge main → target branch**" >> $GITHUB_STEP_SUMMARY
          echo "   \`\`\`bash" >> $GITHUB_STEP_SUMMARY
          echo "   cd pangeo-notebook-veda-image" >> $GITHUB_STEP_SUMMARY
          echo "   git checkout <target-branch>" >> $GITHUB_STEP_SUMMARY
          echo "   git merge main" >> $GITHUB_STEP_SUMMARY
          echo "   git push" >> $GITHUB_STEP_SUMMARY
          echo "   \`\`\`" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "2. **Manual trigger:** Push an empty commit to target branch" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "3. **Change default branch** (if target should always be built)" >> $GITHUB_STEP_SUMMARY
```

#### Workflow Configuration Explained

| Configuration | Purpose |
|---------------|---------|
| `paths-ignore` | Prevents unnecessary rebuilds when only documentation changes |
| `repository_dispatch` | Cross-repository event triggering mechanism |
| `client_payload` | Passes metadata about the triggering commit for traceability |
| `GITHUB_STEP_SUMMARY` | Creates readable summaries in the Actions UI |

> **Why use `repository_dispatch`?**  
> GitHub's `repository_dispatch` event is the recommended way to trigger workflows across repositories. Unlike webhooks, it's native to GitHub Actions, requires no external infrastructure, and provides built-in authentication through personal access tokens.

> **Why `paths-ignore`?**  
> Docker image builds are resource-intensive and time-consuming. By ignoring documentation-only changes, we save CI/CD minutes and avoid unnecessary image churn when no functional code has changed.

---

### Step 5: Update the Environment File

Add new packages to `environment.yml` in `disasters-product-algorithms`:

```yaml
name: disasters-env
channels:
  - conda-forge
  - defaults
dependencies:
  # Existing dependencies
  - python=3.10
  - numpy
  - pandas
  - xarray
  - rasterio
  - geopandas
  
  # New additions for Landsat/Sentinel processing
  - rioxarray
  - stackstac
  - pystac-client
  - planetary-computer
  
  # pip dependencies
  - pip:
    # Install the algorithms package directly from GitHub
    - git+https://github.com/Disasters-Learning-Portal/disasters-product-algorithms.git@main
```

> **Why include the repository as a pip install?**  
> Installing the algorithms repository as a package makes all functions importable in Jupyter notebooks (e.g., `from disasters_product_algorithms import landsat_functions`). Using `git+https://` ensures the latest code is always installed during image builds.

> **Why conda-forge channel?**  
> The conda-forge channel provides the most up-to-date and comprehensive collection of geospatial packages. Many scientific Python packages are available on conda-forge before PyPI, and conda handles complex binary dependencies (like GDAL) more reliably than pip.

---

### Step 6: Configure the Algorithm Repository Token

1. Generate a new GitHub Classic Personal Access Token:
   - Go to GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
   - Click "Generate new token (classic)"
   - Select the `repo` scope (full control of private repositories)
   - Set an appropriate expiration date
   - Copy the generated token

2. Add the token as a repository secret:
   - Navigate to `disasters-product-algorithms` → Settings → Secrets and variables → Actions
   - Click "New repository secret"
   - Name: `PANGEO_REBUILD_TOKEN`
   - Value: [paste the token]

> **Why a Classic token instead of Fine-grained?**  
> Classic tokens with `repo` scope are required for `repository_dispatch` events. Fine-grained tokens currently have limitations with cross-repository dispatch events. The `repo` scope grants the minimum permissions needed to trigger workflows in another repository.

---

### Step 7: Remove Problematic Directories

⚠️ **Important**: Remove the `image-tests` directory before building.

```bash
rm -rf image-tests/
```

> **Why remove `image-tests`?**  
> The `image-tests` directory contains test configurations that may conflict with the `jupyter-repo2docker` build process. These tests are designed to run against the final image, not during the build phase. Removing them prevents build failures caused by missing dependencies or circular references.

---

### Step 8: Set Up Docker Hub

1. **Create a Docker Hub account** at [hub.docker.com](https://hub.docker.com)

2. **Create a new repository**:
   - Click "Create Repository"
   - Name: `disasters-jupyterhub-docker-image`
   - Visibility: Choose based on your requirements (public/private)
   - Click "Create"

> **Why Docker Hub?**  
> Docker Hub is the default container registry and integrates seamlessly with most container orchestration platforms. It offers free public repositories and straightforward access control. For JupyterHub deployments, images on Docker Hub can be pulled without additional authentication configuration.

---

### Step 9: Add the Build Workflow

Create `.github/workflows/build-and-push.yaml` in `pangeo-notebook-veda-image`:

```yaml
name: Build Notebook Container

on:
  push:
  repository_dispatch:
    types: [algorithm-updated]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:

    - name: checkout files in repo
      uses: actions/checkout@main

    - name: Build and push Docker image with jupyter-repo2docker
      run: |
        pip install jupyter-repo2docker
        docker login -u ${{ secrets.DOCKER_USERNAME }} -p ${{ secrets.DOCKER_PASSWORD }}

        # Build with GH_PAT as build arg
        jupyter-repo2docker \
          --no-run \
          --user-name=jovyan \
          --user-id=1000 \
          --image-name=${{ secrets.DOCKER_USERNAME }}/disasters-jupyterhub-docker-image:${GITHUB_SHA::12} \
          --cache-from=${{ secrets.DOCKER_USERNAME }}/disasters-jupyterhub-docker-image:latest \
          --build-arg GH_PAT=${{ secrets.GH_PAT }} \
          .

        # Tag and push
        docker tag ${{ secrets.DOCKER_USERNAME }}/disasters-jupyterhub-docker-image:${GITHUB_SHA::12} ${{ secrets.DOCKER_USERNAME }}/disasters-jupyterhub-docker-image:latest
        docker push ${{ secrets.DOCKER_USERNAME }}/disasters-jupyterhub-docker-image:${GITHUB_SHA::12}
        docker push ${{ secrets.DOCKER_USERNAME }}/disasters-jupyterhub-docker-image:latest
```

#### Build Configuration Explained

| Parameter | Purpose |
|-----------|---------|
| `--no-run` | Only builds the image; doesn't start a container |
| `--user-name=jovyan` | Standard JupyterHub username for compatibility |
| `--user-id=1000` | Standard UID matching most JupyterHub deployments |
| `--cache-from` | Speeds up builds by reusing layers from previous images |
| `--build-arg GH_PAT` | Passes GitHub token for private repository access |
| `${GITHUB_SHA::12}` | Uses first 12 characters of commit SHA as tag |

> **Why `jupyter-repo2docker`?**  
> `repo2docker` is the standard tool used by JupyterHub and Binder to create reproducible computational environments. It automatically detects configuration files (`environment.yml`, `requirements.txt`, etc.) and creates optimized Docker images. This ensures consistency with how JupyterHub expects images to be structured.

> **Why tag with both SHA and `latest`?**  
> The SHA tag provides immutable versioning—you can always reference a specific build. The `latest` tag provides convenience for deployments that should automatically use the newest version. Having both gives flexibility for different deployment strategies.

> **Why `--user-id=1000`?**  
> User ID 1000 is the standard first non-root user ID on Linux systems. JupyterHub expects this UID for proper file permissions. Using a different UID can cause permission issues when mounting volumes or persisting user data.

---

### Step 10: Generate Docker Hub Access Token

1. Log into Docker Hub
2. Go to Account Settings → Security → Access Tokens
3. Click "New Access Token"
4. Description: `github-actions-disasters-hub`
5. Access permissions: Read, Write, Delete
6. Click "Generate"
7. Copy the token immediately (it won't be shown again)

> **Why use an access token instead of password?**  
> Access tokens can be scoped and revoked individually without changing your account password. They're the recommended authentication method for CI/CD systems and provide better security audit trails.

---

### Step 11: Configure GitHub Secrets

Add the following secrets to `pangeo-notebook-veda-image`:

Navigate to: Settings → Secrets and variables → Actions → New repository secret

| Secret Name | Value | Purpose |
|-------------|-------|---------|
| `DOCKER_USERNAME` | Your Docker Hub username | Authentication for docker login |
| `DOCKER_PASSWORD` | Docker Hub access token from Step 10 | Authentication for docker login |
| `GH_PAT` | GitHub Classic token with `repo` scope | Access to private `disasters-product-algorithms` repo |

> **Why is `GH_PAT` needed?**  
> If `disasters-product-algorithms` is a private repository, the Docker build process needs authentication to clone it when processing the `environment.yml` pip dependencies. The `GH_PAT` is passed as a build argument and used during the `pip install git+https://...` step.

---

### Step 12: Push and Verify

Push your branch to GitHub remote:

```bash
git add .
git commit -m "Add automated Docker image rebuild workflow"
git push -u origin combinedENV
```

Then merge to main (via PR or direct push if permitted):

```bash
git checkout main
git merge combinedENV
git push origin main
```

---

### Step 13: Monitor the Build

1. Navigate to: `https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image/actions`
2. Look for the workflow run triggered by `algorithm-updated` event
3. Monitor the build progress
4. Verify the image appears in Docker Hub after successful completion

---

## Technical Rationale

### Why Two Repositories?

```
┌────────────────────────────────────────────────────────────────┐
│                    Single Repository Approach                   │
│  ❌ Algorithm developers need Docker knowledge                  │
│  ❌ Every code change requires understanding build process      │
│  ❌ Harder to maintain separate concerns                        │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│                    Two Repository Approach                      │
│  ✅ Clear separation of concerns                                │
│  ✅ Algorithm developers focus on algorithms                    │
│  ✅ DevOps manages build infrastructure                         │
│  ✅ Independent versioning and release cycles                   │
│  ✅ Easier testing and validation                               │
└────────────────────────────────────────────────────────────────┘
```

### Why `repository_dispatch` Over Alternatives?

| Alternative | Drawbacks |
|-------------|-----------|
| Git submodules | Complex to manage, requires manual updates |
| Webhooks | Requires external infrastructure, security concerns |
| Scheduled builds | Wasteful if no changes, delays when changes occur |
| Manual triggers | Human error, delays, doesn't scale |
| **repository_dispatch** | ✅ Native GitHub, secure, immediate, traceable |

### Why Pangeo Base Image?

The Pangeo project provides Jupyter notebook images specifically designed for:

- Large-scale geospatial data processing
- Cloud-native workflows (S3, GCS, Azure Blob)
- Dask distributed computing
- Optimized I/O for formats like Zarr, NetCDF, GeoTIFF

Building on Pangeo rather than starting from scratch provides a battle-tested foundation with the scientific Python stack pre-configured.

---

## Secrets and Authentication

### Summary of Required Secrets

#### In `disasters-product-algorithms`:

| Secret | Type | Scope | Purpose |
|--------|------|-------|---------|
| `PANGEO_REBUILD_TOKEN` | GitHub Classic PAT | `repo` | Trigger builds in pangeo-notebook-veda-image |

#### In `pangeo-notebook-veda-image`:

| Secret | Type | Scope | Purpose |
|--------|------|-------|---------|
| `DOCKER_USERNAME` | Docker Hub username | N/A | Docker Hub authentication |
| `DOCKER_PASSWORD` | Docker Hub access token | Read/Write/Delete | Docker Hub authentication |
| `GH_PAT` | GitHub Classic PAT | `repo` | Clone private repos during build |

### Token Rotation Schedule

Recommended rotation frequency:

- **GitHub PATs**: Every 90 days or per organization policy
- **Docker Hub tokens**: Every 180 days or per organization policy

Set calendar reminders to rotate tokens before expiration to avoid build failures.

---

## Troubleshooting

### Build Fails: Token Not Found

```
error: PANGEO_REBUILD_TOKEN secret is not set!
```

**Solution**: Verify the secret is added to the correct repository with the exact name `PANGEO_REBUILD_TOKEN`.

---

### Build Fails: Permission Denied

```
fatal: could not read Username for 'https://github.com': terminal prompts disabled
```

**Solution**: The `GH_PAT` token is missing, expired, or doesn't have `repo` scope. Generate a new token and update the secret.

---

### Build Fails: Image Tests Directory

```
Error: Unable to find installation candidate for image-tests
```

**Solution**: Remove the `image-tests` directory from the repository before building:

```bash
rm -rf image-tests/
git add -A
git commit -m "Remove image-tests directory"
git push
```

---

### repository_dispatch Not Triggering

```
HTTP 404 or HTTP 403 when sending dispatch
```

**Possible causes**:

1. Token doesn't have `repo` scope
2. Token owner doesn't have write access to target repository
3. Repository name or organization is misspelled in the workflow

**Solution**: Verify token permissions and repository access.

---

### Image Not Updating in JupyterHub

After a successful build, the new image may not appear immediately in JupyterHub.

**Solutions**:

1. JupyterHub may cache image references—restart the hub
2. Verify the hub configuration points to `:latest` tag
3. Check that the image was pushed to the correct Docker Hub repository

---

## Appendix: Complete Workflow Diagram

```
Developer pushes to
disasters-product-algorithms (main)
            │
            ▼
    ┌───────────────────┐
    │ GitHub detects    │
    │ push event        │
    └─────────┬─────────┘
              │
              ▼
    ┌───────────────────┐
    │ Trigger workflow  │
    │ runs              │
    │ (trigger-rebuild) │
    └─────────┬─────────┘
              │
              │ Sends repository_dispatch
              │ event via GitHub API
              ▼
    ┌───────────────────┐
    │ pangeo-notebook-  │
    │ veda-image        │
    │ receives dispatch │
    └─────────┬─────────┘
              │
              ▼
    ┌───────────────────┐
    │ build-and-push    │
    │ workflow runs     │
    │                   │
    │ 1. Checkout code  │
    │ 2. repo2docker    │
    │ 3. docker push    │
    └─────────┬─────────┘
              │
              ▼
    ┌───────────────────┐
    │ Docker Hub        │
    │ disasters-        │
    │ jupyterhub-       │
    │ docker-image      │
    │                   │
    │ Tags:             │
    │ - :latest         │
    │ - :<sha>          │
    └───────────────────┘
              │
              ▼
    ┌───────────────────┐
    │ JupyterHub pulls  │
    │ updated image on  │
    │ next user spawn   │
    └───────────────────┘
```

---

## Document Information

| Field | Value |
|-------|-------|
| Version | 1.0 |
| Last Updated | January 2026 |
| Maintainer | Disasters Learning Portal Team |

---

## Quick Reference Commands

```bash
# Clone the algorithms repository
git clone https://github.com/Disasters-Learning-Portal/disasters-product-algorithms.git

# Create a new feature branch
git checkout -b feature/my-changes main

# Check GitHub Actions status
gh run list --repo Disasters-Learning-Portal/pangeo-notebook-veda-image

# Manually trigger a rebuild (requires gh CLI)
gh api repos/Disasters-Learning-Portal/pangeo-notebook-veda-image/dispatches \
  -f event_type=algorithm-updated

# Pull the latest image locally for testing
docker pull <username>/disasters-jupyterhub-docker-image:latest
```
