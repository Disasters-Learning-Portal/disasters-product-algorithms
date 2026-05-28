# Branch Protection & Repository Rulesets

This document specifies the **GitHub-native branch protection and ruleset
configuration** that replaces the deleted `enforce-dev-to-main.yml` and
`enforce-branch-protection.yml` workflows (Rec 2 of the automation audit).

Bash-in-CI was the wrong tool for two reasons:

1. It can't be bypassed in an emergency (e.g. a security hotfix that has to
   bypass the `dev → main` rule). GitHub rulesets support per-actor bypass.
2. It runs *after* the PR is opened — the user has already done the work of
   pushing a branch and opening a PR. Native rulesets reject at branch
   creation / push time where applicable.

**This is a one-time-setup document.** The repo owner must configure these
rules in the GitHub UI (or via `gh api` / Terraform). The settings live in
GitHub's data store, not in the repo.

---

## Rule 1: `main` accepts PRs only from `dev` (replaces `enforce-dev-to-main.yml`)

### What the workflow used to do

`enforce-dev-to-main.yml` failed any PR whose `head_ref != "dev"` and
`base_ref == "main"`. Hard-blocking, no bypass.

### Native replacement: branch protection on `main`

Configure under **Settings → Branches → Branch protection rules → Add rule**
(or **Settings → Rules → Rulesets → New ruleset**):

| Setting | Value |
|---|---|
| Branch name pattern | `main` |
| Require a pull request before merging | ✅ |
| Restrict who can push to matching branches | ✅ — restrict to repo admins for the bypass case |
| Require status checks to pass | ✅ — select `sensor-consistency` and `cli-smoke` |
| Restrict pushes that create matching branches | ✅ |
| Allow specified actors to bypass required pull requests | (optional) repo admins, for emergency hotfixes |

To enforce the **source branch must be `dev`** rule, the most direct native
expression is a **ruleset with a "Restrict updates" rule and a "Required
deployments / required workflows" condition**, but GitHub does not yet
expose a first-class "PR source branch must match X" toggle. Two practical
options:

#### Option A — Documented convention + minimal CI guardrail (recommended)

1. Document the `feature/* → dev → main` flow in
   `.github/PULL_REQUEST_TEMPLATE.md` (already done — see the "Target
   branch" reminder).
2. Leave the actual enforcement to **branch protection's required reviewers**
   + the PR template reminder. The previous bash check was easily bypassed
   anyway (anyone could re-target the PR after CI ran).
3. If a non-`dev` PR ever lands on `main`, the algorithms image will still
   only rebuild for whatever ref was pushed — so the cost of misuse is low.

#### Option B — Restore the check via a tiny GitHub Actions rule

If the team wants hard enforcement back, add this to `.github/workflows/lint.yml`
(or as a new dedicated workflow):

```yaml
  enforce-dev-to-main:
    if: github.event_name == 'pull_request' && github.base_ref == 'main'
    runs-on: ubuntu-latest
    steps:
      - name: PR to main must come from dev
        if: github.head_ref != 'dev'
        run: |
          echo "::error::PRs to main must come from dev branch (got: ${{ github.head_ref }})"
          exit 1
```

This is functionally the same as the deleted workflow, but inline with
`lint.yml` rather than a standalone file. **Not recommended** unless the
honor-system approach in Option A breaks down.

### Equivalent `gh api` / Terraform payload (for future automation)

```bash
gh api -X PUT repos/Disasters-Learning-Portal/disasters-product-algorithms/branches/main/protection \
  -f required_status_checks[strict]=true \
  -F 'required_status_checks[contexts][]=sensor-consistency' \
  -F 'required_status_checks[contexts][]=cli-smoke' \
  -f enforce_admins=false \
  -f required_pull_request_reviews[required_approving_review_count]=1 \
  -f restrictions=null
```

```hcl
# Terraform (github provider)
resource "github_branch_protection" "main" {
  repository_id  = "disasters-product-algorithms"
  pattern        = "main"

  required_status_checks {
    strict   = true
    contexts = ["sensor-consistency", "cli-smoke"]
  }

  required_pull_request_reviews {
    required_approving_review_count = 1
  }

  enforce_admins = false  # repo admins can bypass for hotfixes
}
```

---

## Rule 2: PR shape conventions on `dev` (replaces `enforce-branch-protection.yml`)

### What the workflow used to do

`enforce-branch-protection.yml` ran on PRs to `dev` and:

- **Blocked** PRs from `dev` itself (i.e. `head_ref == "dev"`).
- **Warned** (non-blocking) on PR title length < 10 chars.
- **Warned** on empty PR body.
- **Warned** if the head branch name didn't match
  `^(feature|fix|hotfix|refactor|docs|test|chore)/`.

The last three were all warnings, not failures.

### Native replacement: PR template + branch protection on `dev`

1. **`.github/PULL_REQUEST_TEMPLATE.md`** (shipped in this PR) gives every
   new PR a structured body: target-branch reminder, summary section, test
   plan, checklist of common gotchas (sensor consistency, conda dep sync,
   etc.). This replaces the warn-on-empty-body bash check with a much
   stronger signal: contributors *see* the expected shape before they
   write a description.
2. **Branch protection on `dev`** (Settings → Branches → Add rule):

| Setting | Value |
|---|---|
| Branch name pattern | `dev` |
| Require a pull request before merging | ✅ |
| Require status checks to pass | ✅ — `sensor-consistency`, `cli-smoke` |
| Require linear history | (optional) ✅ if you want to disallow merge commits |
| Restrict pushes that create matching branches | ✅ — prevents PR-from-dev-to-dev because there's nothing to PR |

3. The "block PR from `dev` to `dev`" rule is **automatically enforced by
   GitHub**: you cannot open a PR where source == target. The bash check
   was redundant.

### Optional: PR title convention enforcement

If the team later wants hard enforcement of conventional commit titles,
add **one** of:

#### Option A — `peter-evans/check-pr-title` action

```yaml
  check-pr-title:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: amannn/action-semantic-pull-request@v5
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        with:
          types: |
            feat
            fix
            chore
            docs
            refactor
            test
```

#### Option B — Branch name pattern ruleset (UI)

**Settings → Rules → Rulesets → New branch ruleset:**

- Target: `Branch name patterns` → matches `^(feature|fix|hotfix|refactor|docs|test|chore)/.*`
- Rule type: `Restrict branch creation` for non-matching names

This is hard-blocking at branch creation time (vs. the old workflow's
post-PR warning). More disruptive — only enable if the team agrees.

### Equivalent `gh api` payload for `dev`

```bash
gh api -X PUT repos/Disasters-Learning-Portal/disasters-product-algorithms/branches/dev/protection \
  -f required_status_checks[strict]=true \
  -F 'required_status_checks[contexts][]=sensor-consistency' \
  -F 'required_status_checks[contexts][]=cli-smoke' \
  -f enforce_admins=false \
  -f required_pull_request_reviews[required_approving_review_count]=0 \
  -f restrictions=null
```

---

## Verification after setup

After configuring the rulesets, smoke-test the configuration:

1. **Open a PR from a feature branch → `dev`.** Should succeed with the new
   PR template auto-populated.
2. **Open a PR from a non-`dev` branch → `main`.** If you chose Option A
   (honor system), it'll succeed; if Option B (CI check), it'll fail with
   a clear message.
3. **Try to force-push to `dev` directly.** Should be blocked by the
   "require PR before merging" rule.
4. **Confirm `sensor-consistency` and `cli-smoke` are listed as required
   checks** on both `dev` and `main` branch protection settings.

---

## Why this is better than the deleted workflows

| Concern | Old (deleted) | New (rulesets + PR template) |
|---|---|---|
| Lines of YAML/bash | ~117 | 0 (the PR template is markdown) |
| Emergency bypass | Impossible without editing the workflow | Per-actor bypass on rulesets |
| Source-of-truth | `.github/workflows/enforce-*.yml` | GitHub's branch protection config + this doc |
| Contributor visibility | Only after CI runs (post-PR-open) | PR template renders *while writing the PR* |
| Honest about what's enforced | Warnings looked like failures | Template is a checklist; protection rules are explicit |

The audit's verdict still stands: enforcing repo policy via bash in CI is
over-engineering when GitHub provides first-class primitives. This file
documents the migration path.
