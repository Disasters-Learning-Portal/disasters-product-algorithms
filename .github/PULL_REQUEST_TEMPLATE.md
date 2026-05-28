<!--
Thanks for contributing to disasters-product-algorithms!

Branch flow: feature/* → dev → main
- PRs to `dev` should come from a feature branch (suggested prefixes:
  feature/, fix/, hotfix/, refactor/, docs/, test/, chore/).
- PRs to `main` should come from `dev`. See .github/RULESETS.md.

Useful docs:
- docs/ADDING_A_NEW_SENSOR.md  - end-to-end guide for shipping a new sensor
- docs/AUTOMATION.md            - CI / lint / dependency surface
- docs/HUB_DEPLOYMENT.md        - hub image rebuild flow + debug checklist
-->

## Summary

<!-- 1-3 sentences. What changed and why. -->

## Target branch

- [ ] This PR targets `dev` (feature/fix/refactor/etc. work) **OR**
- [ ] This PR targets `main` and the source branch is `dev` (promotion)

## Test plan

<!-- How did you verify the change? Bulleted list of commands/notebooks run. -->

-
-

## Checklist

<!-- Tick what applies; leave the rest. -->

- [ ] Ran `python tools/check_sensor_consistency.py` locally (required if you
      touched `<sensor>/`, `pyproject.toml [project.scripts]`, or
      `[tool.setuptools.packages.find].include`).
- [ ] CI is green (`sensor-consistency` + `cli-smoke` jobs in `lint.yml`).
- [ ] If a new sensor: scaffolded with `python tools/new_sensor.py <name>`
      OR followed the manual checklist in `docs/ADDING_A_NEW_SENSOR.md`.
- [ ] If a new pip dependency: added to `pyproject.toml [project.dependencies]`.
- [ ] If a new conda dependency:
  - [ ] Added to `dev-conda-deps.txt` (local dev + CI smoke env).
  - [ ] If NOT already in the Pangeo base image: also added to
        `hub-conda-deps.txt` (the `sync-conda-deps` workflow will open a
        PR in `pangeo-notebook-veda-image` after this merges to `main`).
- [ ] If touching the COG output path or `convert_to_cog` defaults:
      confirmed downstream `build_stac` compatibility (see `CLAUDE.md`
      "Critical Constraints" — Web Mercator default is load-bearing).
- [ ] Updated relevant docs in `docs/` if user-facing behavior changed.

## Related issues / context

<!-- Link to issues, prior PRs, or audit recs this addresses. -->

---

<sub>By submitting this PR you agree the change will be exercised in the
`dev` hub image variant before promoting to `main` (prod image). See
`docs/AUTOMATION.md` for the post-merge image-rebuild flow.</sub>
