# Examples

Run them with `uv run python examples/<file>`. They are ordered so that reading them
in sequence explains the design; each one is self-contained.

| Example | What it shows |
| --- | --- |
| [01_quickstart.py](01_quickstart.py) | The shortest useful program. Nothing configured. |
| [02_the_model.py](02_the_model.py) | The 19 layers, what each reads, and the bound — read this first. |
| [03_challenged_site.py](03_challenged_site.py) | A JavaScript challenge: solve once with a browser, reuse after. |
| [04_addresses.py](04_addresses.py) | Exit kinds, sticky leases, tor-pool, and why rotation needs somewhere better. |
| [05_behaviour.py](05_behaviour.py) | Pacing drawn from a distribution, warm-up, and sharing site state. |
| [06_decoy_content.py](06_decoy_content.py) | The layer that returns no error: safe link extraction and topic drift. |
| [07_web_bot_auth.py](07_web_bot_auth.py) | Signed requests (RFC 9421) and the key directory to publish. |
| [08_archive_and_managed.py](08_archive_and_managed.py) | The cheapest rung and the most expensive one. |
| [09_diagnostics.py](09_diagnostics.py) | `explain()`, the exception taxonomy, and offline diagnosis. |
| [10_files_and_soup.py](10_files_and_soup.py) | Soup, JSON, forms, files, images. |

Some examples need an extra:

```bash
pip install "lncrawl-scraper[browser]"   # 03
pip install "lncrawl-scraper[botauth]"   # 07
pip install "lncrawl-scraper[image]"     # 10, for get_image
```
