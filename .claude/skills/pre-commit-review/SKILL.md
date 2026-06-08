---
name: pre-commit-review
description: Checklist to run before every commit in lncrawl-scraper — lint, type-check, tests, and a manual diff review. Run this automatically before proposing or creating any git commit.
---

# Pre-commit review

Run every check below before creating a commit. Stop and fix any failure before
proceeding. Do not skip steps for "small" changes — a one-line edit can still
break the type checker or a test.

## 1. Lint

```bash
uv run poe lint
```

This runs `ruff check`, `ruff format --check`, and `pyright` in one shot.
Fix all errors before continuing. For format issues, `uv run poe lint-fix`
applies them automatically.

## 2. Tests

```bash
uv run poe test
```

All tests must pass. If a change is expected to break a test, update the test
first as part of the same commit.

## 3. Diff review

Read the staged diff and verify:

- No debug/print statements left in.
- No commented-out code.
- No TODO/FIXME introduced without an accompanying explanation.
- If a public name was added, removed, or renamed — `__all__` in
  `src/scraper/__init__.py` is updated and the README reflects it.
- If `engine/` or `utils/` internals changed in a way that affects public
  behaviour — `CHANGELOG.md` has a note (or the change is pre-release).
- If `pyproject.toml` deps changed — `uv.lock` is staged too.

## 4. Only then commit

Stage only the files relevant to this logical change. Do not bundle unrelated
modifications in one commit.

After the commit, **stop** — do not push. See the [[commit-messages]] skill for
message format.
