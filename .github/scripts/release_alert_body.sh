#!/usr/bin/env bash
#
# Render the body of the "new release is on main" alert issue.
#
#   Usage: release_alert_body.sh vX.Y.Z   # markdown to stdout
#
# Split out of .github/workflows/notify-release.yaml on purpose: this is the
# text every team member reads after a release, and it MUST be reviewable
# without cutting a release to see it. Render it locally with
#
#   bash .github/scripts/release_alert_body.sh v1.0.1
#
# Same convention as dps/blackmarble/naming.sh and dps/_validate.sh — the
# testable part lives in a sourceable/runnable script, not inline in YAML.
#
# Built with printf, not a heredoc: this text gets echoed from an indented
# `run:` block context, where an unquoted heredoc delimiter can't sit at
# column 0. See the same note at .github/workflows/sync-deploy-algorithm.yml.

set -euo pipefail

VERSION="${1:-}"

if [ -z "$VERSION" ]; then
  echo "usage: $(basename "$0") vX.Y.Z" >&2
  exit 1
fi

if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "error: version must match vX.Y.Z (got: $VERSION)" >&2
  exit 1
fi

REPO_URL="https://github.com/Disasters-Learning-Portal/disasters-product-algorithms"

# Who gets pinged. ONE place, so swapping the team for explicit @handles is a
# one-line change if team mentions ever stop notifying from a bot-authored issue.
MENTIONS="@Disasters-Learning-Portal/disasters"

printf '%s\n' \
"$MENTIONS" \
"" \
"**Version \`$VERSION\` is now on \`main\`.** Release notes: $REPO_URL/releases/tag/$VERSION" \
"" \
"Please update your setup on the Disasters hub. It takes about two minutes." \
"" \
"---" \
"" \
"## First: which copy are you actually running?" \
"" \
"Two different things go stale, and **\`git pull\` only fixes one of them.**" \
"" \
"| What's stale | How it actually updates |" \
"|---|---|" \
"| The \`process_*\` CLIs + \`shared_utils\` **baked into the hub image** (most people) | **Restart your server** — no git needed |" \
"| Your own clone at \`~/disasters-product-algorithms\` (only if you develop the code) | \`git pull\` — steps below |" \
"" \
"Not sure which you are? Run this in a hub terminal:" \
"" \
'```bash' \
"pip show disasters-product-algorithms | grep -E 'Version|Location'" \
'```' \
"" \
"- \`Location: /srv/conda/envs/notebook/...\` → you're on the **image** copy. Skip to" \
"  [Restart your server](#restart-your-server). A \`git pull\` will not change what runs." \
"- \`Location: /home/jovyan/disasters-product-algorithms/src\` → you have an **editable install**" \
"  of your own clone. Follow the git steps below." \
"" \
"---" \
"" \
"## ⚠️ Before you touch git — JupyterHub gotchas" \
"" \
"These are the things that actually go wrong. Do them **first**:" \
"" \
"1. **Save and CLOSE every open notebook tab.** \`Cmd/Ctrl+S\`, then close the tab." \
"   An open notebook rewrites its own \`.ipynb\` whenever it autosaves and drops files into" \
"   \`.ipynb_checkpoints/\` — so it is an uncommitted change waiting to happen. This is the #1" \
"   cause of:" \
"   \`\`\`" \
"   error: Your local changes to the following files would be overwritten by merge" \
"   \`\`\`" \
"2. **Shut down running kernels.** Left sidebar → **Running Terminals and Kernels** (the ⏹ icon)" \
"   → **Shut Down All**. A live kernel holds the already-imported modules in memory, so after a" \
"   pull you keep running the **old** code — or worse, get a half-old/half-new mix on the next" \
"   import. Restarting the kernel is not optional." \
"3. **Don't pull mid-run.** If a \`process_*\` job or a DPS submission is in flight, let it finish." \
"4. **Back up anything precious** before you start:" \
"   \`\`\`bash" \
"   cp ~/my_working_notebook.ipynb ~/my_working_notebook.BACKUP.ipynb" \
"   \`\`\`" \
"" \
"---" \
"" \
"## Step 1 — get into the correct repo" \
"" \
'```bash' \
"cd ~/disasters-product-algorithms" \
"" \
"# Confirm you are in the right place before running anything else:" \
"pwd            # → /home/jovyan/disasters-product-algorithms" \
"git remote -v  # → .../Disasters-Learning-Portal/disasters-product-algorithms" \
'```' \
"" \
"**If \`cd\` fails with \`No such file or directory\`:** you don't have a clone. You only need one" \
"if you're editing the code — otherwise just [restart your server](#restart-your-server). To" \
"create one:" \
"" \
'```bash' \
"cd ~" \
"git clone $REPO_URL.git" \
"cd disasters-product-algorithms" \
'```' \
"" \
"⚠️ **\`~/disasters-docs\` is a DIFFERENT repo.** That's where the operator notebooks come from" \
"(pulled in via the nbgitpuller link). Running these commands there will not update the" \
"algorithms package." \
"" \
"## Step 2 — check where you are and whether anything is dirty" \
"" \
'```bash' \
"git branch --show-current   # which branch am I on?" \
"git status                  # anything uncommitted?" \
'```' \
"" \
"If \`git status\` says *\"nothing to commit, working tree clean\"*, skip Step 3." \
"" \
"## Step 3 — stash your uncommitted work" \
"" \
'```bash' \
"git stash push -u -m 'before $VERSION update'" \
"git stash list              # confirm it was saved" \
'```' \
"" \
"\`-u\` also stashes **untracked** files (new notebooks, scratch scripts) — without it they're" \
"left behind and can still block the pull. You'll get this back in the last step." \
"" \
"## Step 4 — switch to main and pull" \
"" \
'```bash' \
"git switch main             # older git: git checkout main" \
"git pull origin main" \
'```' \
"" \
"Verify you actually landed on the new version:" \
"" \
'```bash' \
"git log --oneline -1        # → Release $VERSION" \
"git describe --tags         # → $VERSION" \
'```' \
"" \
"## Step 5 — reinstall if you're on an editable install" \
"" \
'```bash' \
"pip show disasters-product-algorithms | grep -E 'Version|Location'" \
'```' \
"" \
"- **Location under \`~/disasters-product-algorithms/src\`** → the new code is already live." \
"  Re-run \`pip install -e .\` only if this release added or renamed a CLI entry point." \
"- **Location under \`/srv/conda/envs/notebook/...\`** → your pull changed nothing that runs." \
"  Either restart your server (below), or take over with your clone:" \
"  \`\`\`bash" \
"  pip uninstall -y disasters-product-algorithms && pip install -e ." \
"  \`\`\`" \
"" \
"## Step 6 — verify" \
"" \
'```bash' \
"python -c 'from shared_utils import __version__; print(__version__)'" \
"which process_landsat89" \
'```' \
"" \
"## Step 7 — get your stashed work back" \
"" \
'```bash' \
"git stash pop" \
'```' \
"" \
"If that conflicts on a \`.ipynb\`, the conflict markers make the JSON unopenable in Jupyter." \
"Pick a side rather than hand-editing:" \
"" \
'```bash' \
"git checkout --theirs path/to/notebook.ipynb   # keep YOUR stashed version" \
"# git checkout --ours  path/to/notebook.ipynb  # keep the NEW version from main" \
"git add path/to/notebook.ipynb" \
'```' \
"" \
"---" \
"" \
"## Restart your server" \
"" \
"This is what updates the pre-installed \`process_*\` CLIs for everyone who doesn't keep a clone." \
"" \
"**Wait for the image build to finish first.** Pushing to \`main\` kicks off" \
"\`build-and-push.yaml\`, which takes **~1–4 minutes**. Restarting before it goes green just" \
"gets you the old image again. Check it here:" \
"" \
"$REPO_URL/actions/workflows/build-and-push.yaml" \
"" \
"Then, in JupyterLab: **File → Hub Control Panel → Stop My Server**, wait for it to fully stop," \
"then **Start My Server**. A fresh pod always pulls \`:latest\`." \
"" \
"> **Note:** the image only rebuilds when \`image/**\`, \`src/**\`, or \`pyproject.toml\` changed." \
"> If this release was docs-only or \`dps/\`-only, no build runs and there is nothing to restart" \
"> for — that's expected, not a failure." \
"" \
"---" \
"" \
"Questions or something broken after updating? Post in" \
"[Discussions]($REPO_URL/discussions) (also under **Help → Disasters Resources** in JupyterLab)" \
"or open a [bug report]($REPO_URL/issues/new)." \
"" \
"<sub>Automated by \`.github/workflows/notify-release.yaml\`. Close this issue once you've updated —" \
"it is closed automatically when the next release goes out.</sub>"
