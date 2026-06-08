"""Concurrent downloads and cooperative cancellation.

A single Scraper is thread-safe (requests are serialized/throttled by the
engine). `close()` aborts all pending and in-progress requests, raising
AbortedException in any thread that is waiting on or streaming a response.

Run:
    uv run python examples/10_concurrency_and_abort.py
"""

import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scraper import AbortedException, Scraper, ScraperConfig


def fetch_many() -> None:
    # Allow a few requests in flight at once.
    s = Scraper(config=ScraperConfig(max_concurrent_requests=4))

    urls = [f"https://httpbin.io/anything/{i}" for i in range(6)]

    def worker(url: str) -> int:
        return s.get(url).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, urls))
    print("statuses:", results)


def abort_demo() -> None:
    s = Scraper()

    # Abort from another thread shortly after a streaming download starts.
    def stopper() -> None:
        time.sleep(0.5)
        print("aborting...")
        s.close()  # aborts all in-progress requests and releases resources

    threading.Thread(target=stopper, daemon=True).start()

    out = Path(tempfile.mkdtemp(prefix="scraper_example_")) / "slow.bin"
    try:
        # A deliberately slow stream; get_file checks the abort signal between
        # chunks, so the download is interrupted mid-flight.
        s.get_file("https://httpbin.io/drip?duration=10&numbytes=200", output_file=out)
        print("download finished (not expected here)")
    except AbortedException:
        print("download was aborted cleanly")
    except Exception as exc:  # network/other errors
        print("download ended with:", type(exc).__name__)


def main() -> None:
    fetch_many()
    abort_demo()


if __name__ == "__main__":
    main()
