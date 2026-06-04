"""Downloading files and images.

`get_file` streams to disk atomically and can be aborted mid-download.
`get_image` returns a Pillow Image (requires the `image` extra:
    pip install "lncrawl-scraper[image]"
).

Run:
    uv run python examples/04_files_and_images.py
"""

import tempfile
from pathlib import Path

from scraper import Scraper


def main() -> None:
    s = Scraper()
    out_dir = Path(tempfile.mkdtemp(prefix="scraper_example_"))

    # --- Stream a file to disk (atomic write) -----------------------------
    target = out_dir / "robots.txt"
    s.get_file("https://www.google.com/robots.txt", output_file=target)
    print(f"downloaded {target} ({target.stat().st_size} bytes)")

    # --- Download an image (needs the `image` extra) ----------------------
    try:
        img = s.get_image("https://httpbin.org/image/png")
        print(f"image: {img.format} {img.size}")
    except ImportError:
        print("install the 'image' extra to use get_image: pip install 'lncrawl-scraper[image]'")

    # get_image also accepts data: URIs (decoded in-memory, no network call).
    # img = s.get_image("data:image/png;base64,iVBORw0KGgo...")


if __name__ == "__main__":
    main()
