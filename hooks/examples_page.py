"""Generate the Examples page from `examples/*.py` at build time.

The examples are already written to be read in order, and each one opens with a
docstring that says what it shows. Turning that into a page means the site cannot drift
from the code and a new example needs no documentation edit — it appears because the
file exists.

The docstring is rendered as prose and then stripped from the listing below it, so the
same paragraphs are not shown twice.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from mkdocs.structure.files import File, Files

log = logging.getLogger("mkdocs.hooks.examples_page")

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
BLOB = "https://github.com/lncrawl/scraper/blob/main/examples"

PAGE_PATH = "examples.md"

INTRO = """# Examples

Runnable programs, ordered so that reading them in sequence explains the design. Each
one is self-contained and lives in
[`examples/`](https://github.com/lncrawl/scraper/tree/main/examples).

Three of them need an extra:

```bash
pip install "lncrawl-scraper[cdp]"       # 03
pip install "lncrawl-scraper[botauth]"   # 07
pip install "lncrawl-scraper[image]"     # 10, for get_image
```
"""


def _title(stem: str) -> str:
    """`01_quickstart` -> `01 · Quickstart`."""
    number, _, rest = stem.partition("_")
    words = rest.replace("_", " ")
    return f"{number} · {words[:1].upper()}{words[1:]}"


def _split_docstring(source: str) -> tuple[Optional[str], str]:
    """Return the module docstring and the source with that docstring removed."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None, source

    docstring = ast.get_docstring(tree)
    if not docstring or not tree.body:
        return None, source

    first = tree.body[0]
    end = getattr(first, "end_lineno", None)
    if end is None:
        return docstring, source

    remainder = source.splitlines()[end:]
    while remainder and not remainder[0].strip():
        remainder.pop(0)
    return docstring, "\n".join(remainder)


def _render() -> str:
    parts: List[str] = [INTRO]

    for path in sorted(EXAMPLES.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        docstring, body = _split_docstring(source)

        parts.append(f"## {_title(path.stem)}")
        if docstring:
            parts.append(docstring.strip())
        # Four backticks: the listing is inserted verbatim and may itself contain a fence.
        parts.append(f'````python title="examples/{path.name}"\n{body.rstrip()}\n````')
        parts.append(f"[View on GitHub]({BLOB}/{path.name})")

    return "\n\n".join(parts) + "\n"


def on_files(files: Files, config: Dict[str, Any], **kwargs: Any) -> Files:
    if not EXAMPLES.is_dir():
        log.warning("examples_page: %s is missing, /examples/ will 404", EXAMPLES)
        return files

    files.append(File.generated(config, PAGE_PATH, content=_render()))
    return files
