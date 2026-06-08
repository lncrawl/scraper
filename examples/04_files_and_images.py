"""Downloading files and images.

`get_file` streams to disk atomically and can be aborted mid-download.
`get_image` returns a Pillow Image (requires the `image` extra:
    pip install "lncrawl-scraper[image]"
).

Run:
    uv run python examples/04_files_and_images.py
"""

import shutil
import tempfile
from pathlib import Path

from scraper import Scraper


def main(out_dir: Path) -> None:
    s = Scraper()

    # --- Stream a file to disk (atomic write) -----------------------------
    target = out_dir / "robots.txt"
    s.get_file("https://www.google.com/robots.txt", output_file=target)
    print(f"downloaded {target} ({target.stat().st_size} bytes)")

    # --- Download an image (needs the `image` extra) ----------------------
    try:
        img = s.get_image("https://httpbin.io/image")
        print(f"image: {img.format} {img.size}")
        # Save a copy so you can inspect it.
        out_path = out_dir / "httpbin.png"
        img.save(out_path)
        print(f"saved to {out_path}")
    except ImportError:
        print("install the 'image' extra to use get_image: pip install 'lncrawl-scraper[image]'")

    input("Press enter to delete temp directory...")


if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="scraper_example_")
    try:
        main(Path(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
