# Examples

Runnable usage examples for `lncrawl-scraper`. Each file is standalone:

```bash
uv run python examples/01_basic_html.py
# or, once installed:
python examples/01_basic_html.py
```

| File | Shows |
| ---- | ----- |
| [01_basic_html.py](01_basic_html.py) | Fetch a page and extract data with `get_soup` / `PageSoup` |
| [02_pagesoup_parsing.py](02_pagesoup_parsing.py) | PageSoup tour: CSS select, attrs, navigation, XPath, raw tag access |
| [03_json_api.py](03_json_api.py) | `get_json` / `post_json` and raw `Response` access |
| [04_files_and_images.py](04_files_and_images.py) | `get_file` (streamed, atomic) and `get_image` (Pillow) |
| [05_forms_cookies_headers.py](05_forms_cookies_headers.py) | `submit_form`, `set_header`, `set_cookie`, `post_soup`, `reset` |
| [06_configuration.py](06_configuration.py) | `ScraperConfig`, `default_config()`, stealth, throttling, browser identity |
| [07_impersonation.py](07_impersonation.py) | Real browser TLS/HTTP-2 fingerprint via `impersonate` (curl_cffi) |
| [08_browser_clearance.py](08_browser_clearance.py) | Reuse a `cf_clearance` solved by a real browser |
| [09_proxies_and_tor.py](09_proxies_and_tor.py) | Proxy rotation and Tor identity refresh |
| [10_concurrency_and_abort.py](10_concurrency_and_abort.py) | Threaded fetches and cooperative `abort()` |
| [11_error_handling.py](11_error_handling.py) | HTTP, Cloudflare, and abort error handling |

## Notes

- Examples 04 and 07 need optional extras:
  ```bash
  pip install "lncrawl-scraper[image]"        # get_image
  pip install "lncrawl-scraper[impersonate]"  # impersonate=
  ```
- Several examples hit `httpbin.org` / `example.com` for live demonstration.
- Examples 08 and 09 use placeholder credentials/hosts — they illustrate the
  API shape and won't perform real network calls as written.
