"""Copy the non-markdown artifacts into the built site.

The white paper and the live report are not documentation sources — the PDF is a build
product and `report.html` is rewritten in place by `poe live-report` — so neither lives
under `docs/`. MkDocs only copies files it finds in `docs_dir`, hence this hook.

Missing artifacts warn rather than raise, which under `mkdocs build --strict` still
fails the build: a README that links a file the site does not serve is a broken site.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

log = logging.getLogger("mkdocs.hooks.site_assets")

ROOT = Path(__file__).resolve().parent.parent

# (source file, destination relative to site_dir)
ASSETS: List[Tuple[Path, str]] = [
    (ROOT / "livetest" / "report.html", "live-report/index.html"),
    (ROOT / "whitepaper" / "Cloudflare_Bypass.pdf", "whitepaper/Cloudflare_Bypass.pdf"),
]


def on_post_build(config: Dict[str, Any], **kwargs: Any) -> None:
    site_dir = Path(config["site_dir"])
    for source, relative in ASSETS:
        if not source.is_file():
            log.warning("site_assets: missing %s, /%s will 404", source.name, relative)
            continue
        target = site_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        log.info("site_assets: %s -> /%s", source.name, relative)
