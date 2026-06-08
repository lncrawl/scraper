# Examples

Runnable usage examples for `lncrawl-scraper`. Each file is standalone:

```bash
uv run python examples/01_basic_html.py
# or, once installed:
python examples/01_basic_html.py
```

| File                                                       | Shows                                                                      |
| ---------------------------------------------------------- | -------------------------------------------------------------------------- |
| [01_basic_html.py](01_basic_html.py)                       | Fetch a page and extract data with `get_soup` / `PageSoup`                 |
| [02_pagesoup_parsing.py](02_pagesoup_parsing.py)           | PageSoup tour: CSS select, attrs, navigation, XPath, raw tag access        |
| [03_json_api.py](03_json_api.py)                           | `get_json` / `post_json` and raw `Response` access                         |
| [04_files_and_images.py](04_files_and_images.py)           | `get_file` (streamed, atomic) and `get_image` (Pillow)                     |
| [05_forms_cookies_headers.py](05_forms_cookies_headers.py) | `submit_form`, `set_header`, `set_cookie`, `post_soup`, `reset`            |
| [06_configuration.py](06_configuration.py)                 | `ScraperConfig`, `default_config()`, stealth, throttling, browser identity |
| [07_impersonation.py](07_impersonation.py)                 | Real browser TLS/HTTP-2 fingerprint via `impersonate` (curl_cffi)          |
| [08_browser_clearance.py](08_browser_clearance.py)         | Reuse a `cf_clearance` solved by a real browser                            |
| [09_proxies.py](09_proxies.py)                             | Round-robin proxy rotation with direct fallback                            |
| [10_concurrency_and_abort.py](10_concurrency_and_abort.py) | Threaded fetches and cooperative cancellation via `close()`                |
| [11_error_handling.py](11_error_handling.py)               | HTTP, Cloudflare, and abort error handling                                 |
| [12_browser_auto_solve.py](12_browser_auto_solve.py)       | Auto-solve challenges with `BrowserSolver` (nodriver)                     |
| [13_remote_auto_solve.py](13_remote_auto_solve.py)         | Auto-solve challenges with `RemoteSolver` (FlareSolverr/Byparr)           |
| [14_tor_proxy.py](14_tor_proxy.py)                         | Tor proxy with `rotate_proxy()` for a fresh exit circuit (NEWNYM)         |

## Notes

- Example 04 needs the optional `image` extra:

  ```bash
  pip install "lncrawl-scraper[image]"   # get_image
  ```

- Examples 12 and 13 need the optional `browser` extra or a running FlareSolverr
  container respectively — they illustrate the API shape.
- Impersonation (example 07) works out of the box — `curl_cffi` is a core
  dependency and is enabled by default.
- Several examples hit `httpbin.org` / `example.com` for live demonstration.
- Example 14 requires a running Tor daemon (`socks5h://127.0.0.1:9150`) with the
  control port open (`9151`) and a matching password in `torrc`.
