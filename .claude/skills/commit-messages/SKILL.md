---
name: commit-messages
description: Commit message conventions for lncrawl-scraper — no type prefix, imperative subject, body bullets for non-trivial changes, and no Co-Authored-By trailer. Use whenever writing a commit message or committing in this repo.
---

# Commit messages

Match the existing history (`git log`):

- **No type prefix.** Do NOT use Conventional Commits (`feat:`, `fix:`,
  `docs:`, …) — subjects are plain capitalized text.
- **Imperative mood**, capitalized first word, no trailing period, subject
  ≤ ~60 chars (e.g. `Add coverage reporting to CI`, `Restructure into src layout`).
- **Body only for non-trivial changes**: a blank line, then a short rationale
  paragraph and/or `-` bullets covering *what* changed and *why* (wrap at ~72
  chars). Small changes are subject-only.
- **Do NOT append a `Co-Authored-By` trailer** — this overrides the default
  Claude Code behaviour; the maintainer's commits never carry it.
- Keep commits focused: one logical change per commit. When the working tree
  mixes unrelated changes, propose splitting them rather than one mixed commit.

## Examples

```
Restructure into src layout

- Adopt src/ layout to stop the working tree from shadowing the installed
  package during tests
- Rename scraper.py -> session.py (removes the package/module name shadow)
```

```
Group Scraper helper methods by purpose
```
