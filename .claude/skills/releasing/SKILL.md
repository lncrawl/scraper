---
name: releasing
description: How to cut a release of lncrawl-scraper — the Bump Version workflow (incl. pre-releases), the tag → release → PyPI publish chain, and CHANGELOG handling. Use when publishing a new version.
---

# Releasing lncrawl-scraper

Releases are automated through three GitHub Actions workflows. A single manual
"Bump Version" run cascades all the way to a published GitHub Release (with
artifacts + changelog) and a PyPI upload.

## The pipeline

```
Bump Version (manual workflow_dispatch)
  → uv version --bump … → commit pyproject.toml + uv.lock → push tag vX.Y.Z
      → release.yml (on tag v*)
          → CI → uv build → twine check → extract CHANGELOG section
          → create GitHub Release (artifacts + notes; pre-release auto-flagged)
              → publish.yml (on release: published)
                  → uv build → twine check → PyPI via OIDC trusted publishing
```

- **bump.yml** — `workflow_dispatch` with two inputs: `bump`
  (`patch`/`minor`/`major`/`stable`/`none`) and `pre` (`none`/`alpha`/`beta`/`rc`).
  Composes `uv version --bump …`, commits, and pushes the `vX.Y.Z` tag.
- **release.yml** — fires on the `v*` tag; builds, verifies the tag matches the
  package version, extracts the matching `## [x.y.z]` CHANGELOG section as notes,
  and creates the GitHub Release. PEP 440 pre-release/dev versions
  (`aN`/`bN`/`rcN`/`.devN`) are marked as GitHub pre-releases.
- **publish.yml** — fires when the release is *published*; uploads to PyPI via
  trusted publishing (no token). PyPI auto-classifies pre-release versions, so
  plain `pip install` won't pick them up unless users pass `--pre`.

## To cut a release

1. **Update `CHANGELOG.md` first** — add a `## [x.y.z] - YYYY-MM-DD` section with
   the changes (release.yml extracts it verbatim as the release notes; without an
   entry it falls back to a generic line). Commit it to `main`.
2. Run the **Bump Version** workflow from the Actions tab, choosing the segment:

   | Goal | `bump` | `pre` | Result |
   |------|--------|-------|--------|
   | Normal release | `minor` | `none` | `0.1.0` → `0.2.0` |
   | New RC of next version | `minor` | `rc` | `0.1.0` → `0.2.0rc1` |
   | Next RC iteration | `none` | `rc` | `0.2.0rc1` → `0.2.0rc2` |
   | Beta of next patch | `patch` | `beta` | `0.1.0` → `0.1.1b1` |
   | Promote RC → final | `stable` | `none` | `0.2.0rc1` → `0.2.0` |

3. That's it — the tag triggers the rest. Watch the **Release** and **Publish**
   workflow runs.

## Notes & gotchas

- `bump=none, pre=rc` (iterate a pre-release) only works when **already on a
  pre-release**; from a stable version uv requires a release segment too, and the
  step fails with a clear message.
- A manual `git tag vX.Y.Z && git push --tags` also works — the version-match
  guard in release.yml rejects a tag that disagrees with `pyproject.toml`.
- First PyPI publish requires registering `lncrawl/scraper` as a trusted
  publisher for the `lncrawl-scraper` project on PyPI (the `pypi` GitHub
  environment, no API token).
- Local manual release commands, if ever needed: `uv run poe build` then
  `uv run poe publish`.