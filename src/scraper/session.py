"""The :class:`Scraper` session — the library's main entry point.

A thin composition facade over the Cloudflare-bypass :class:`~scraper.engine.core.Engine`:
it delegates the HTTP surface to the engine and adds ergonomic helpers for HTML,
JSON, file, and image retrieval, automatic Origin/Referer injection, and sensible
default timeouts.
"""

from __future__ import annotations

import base64
import logging
from io import BytesIO
from pathlib import Path
from typing import Any, MutableMapping

from requests import Response
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from .config import ScraperConfig, default_config
from .engine import Engine, ProxyManager, create_engine
from .exceptions import AbortedException
from .soup import PageSoup
from .utils import atomic_write, extract_base, validate_url

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = (15, 300)


class Scraper:
    """HTTP scraper with Cloudflare bypass and HTML/JSON/file helpers.

    Composes an :class:`~scraper.engine.core.Engine` (the request pipeline +
    transport) and exposes a curated, ergonomic surface: :meth:`get`, :meth:`post`,
    :meth:`get_soup`, :meth:`get_json`, :meth:`get_file`, :meth:`get_image`, and
    friends. The underlying engine is available as :attr:`engine` for advanced use.

    Args:
        origin: Base site URL; used to set default Origin/Referer headers.
        parser: BeautifulSoup parser feature (default ``"lxml"``).
        config: A :class:`~scraper.ScraperConfig`. Defaults to
            :func:`~scraper.default_config` (a fresh instance per scraper).
    """

    def __init__(
        self,
        origin: str | None = None,
        parser: str | None = None,
        config: ScraperConfig | None = None,
        **kwargs,
    ) -> None:
        self.engine: Engine = create_engine(config or default_config())
        self.origin = origin or ""
        self.parser = parser or "lxml"
        self.last_soup_url = self.origin

    # -- Delegated state ----------------------------------------------------------

    @property
    def config(self) -> ScraperConfig:
        return self.engine.config

    @property
    def headers(self) -> MutableMapping:
        return self.engine.headers

    @property
    def cookies(self) -> RequestsCookieJar:
        return self.engine.cookies

    @property
    def proxy_manager(self) -> ProxyManager:
        return self.engine.proxy_manager

    @property
    def signal(self):
        """The cross-thread abort signal (a :class:`threading.Event`)."""
        return self.engine.signal

    @signal.setter
    def signal(self, value) -> None:
        self.engine.signal = value

    def abort(self) -> None:
        """Signal all pending and in-progress requests (incl. downloads) to stop."""
        self.engine.abort()

    def put_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        """Set a cookie on the session (and the transport's jar)."""
        self.engine.put_cookie(name, value, domain=domain, path=path)

    def apply_browser_clearance(self, domain: str, **kwargs) -> None:
        """Reuse a Cloudflare clearance solved by a real browser (see Engine)."""
        self.engine.apply_browser_clearance(domain, **kwargs)

    def close(self) -> None:
        self.engine.close()

    def reset(self) -> None:
        """Reset the scraper to its initial state."""
        self.engine.reset()
        self.last_soup_url = ""

    def set_header(self, key: str, value: str | bytes) -> None:
        """Set a default header for subsequent requests."""
        if isinstance(value, bytes):
            value = value.decode()
        self.headers[key] = str(value)

    def set_cookie(self, name: str, value: str | bytes) -> None:
        """Set a session cookie (also propagated to the transport jar)."""
        if isinstance(value, bytes):
            value = value.decode()
        self.put_cookie(name, str(value))

    # -- HTTP surface -------------------------------------------------------------

    def request(self, method: str, url: str, *args, **kwargs) -> Response:
        """Issue a request with auto Origin/Referer, then raise on HTTP errors."""
        kwargs.setdefault("allow_redirects", True)

        headers = CaseInsensitiveDict(kwargs.pop("headers", {}) or {})
        last_url = self.last_soup_url or self.origin
        if last_url:
            origin = extract_base(last_url)
            headers.setdefault("Origin", origin.strip("/"))
            headers.setdefault("Referer", origin)
        kwargs["headers"] = headers

        response = self.engine.request(method, url, *args, **kwargs)
        response.raise_for_status()
        response.encoding = "utf8"
        return response

    def ping(self, url: str, timeout: float = 5, **kwargs) -> Response:
        """Send a HEAD request - a lightweight reachability check."""
        return self.request("HEAD", url, timeout=timeout, **kwargs)

    def options(self, url: str, **kwargs) -> Response:
        """OPTIONS to ``url`` with a default ``(connect, read)`` timeout."""
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return self.request("OPTIONS", url, **kwargs)

    def head(self, url: str, **kwargs) -> Response:
        """HEAD to ``url`` with a default ``(connect, read)`` timeout."""
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return self.request("HEAD", url, **kwargs)

    def get(self, url: str, **kwargs) -> Response:
        """GET ``url`` with a default ``(connect, read)`` timeout."""
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return self.request("GET", url, **kwargs)

    def post(self, url: str, data: Any = None, json: Any = None, **kwargs) -> Response:
        """Raw POST to ``url`` with a default ``(connect, read)`` timeout."""
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return self.request("POST", url, data=data, json=json, **kwargs)

    def put(self, url: str, **kwargs) -> Response:
        """Raw PUT to ``url``"""
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs) -> Response:
        """Raw PATCH to ``url``"""
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs) -> Response:
        """Raw DELETE to ``url``"""
        return self.request("DELETE", url, **kwargs)

    def get_json(self, url: str, headers: MutableMapping = {}, **kwargs) -> Any:
        """Fetch content and return it as a JSON object."""
        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Accept", "application/json,text/plain,*/*")
        kwargs["headers"] = headers
        return self.get(url, **kwargs).json()

    def post_json(self, url: str, data: Any = None, headers: MutableMapping = {}, **kwargs) -> Any:
        """Make a POST request and return the content as a JSON object."""
        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json,text/plain,*/*")
        response = self.post(url, data=data, headers=headers, **kwargs)
        return response.json()

    # -- Soup helpers -------------------------------------------------------------

    def make_soup(
        self,
        data: Any,
        encoding: str | None = None,
    ) -> PageSoup:
        return PageSoup.create(data, encoding, self.parser)

    def get_soup(
        self,
        url: str,
        headers: MutableMapping = {},
        encoding: str | None = None,
        **kwargs,
    ) -> PageSoup:
        """Fetch content and return a PageSoup instance."""
        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9")
        kwargs["headers"] = headers
        response = self.get(url, **kwargs)
        self.last_soup_url = url
        return self.make_soup(response, encoding)

    def post_soup(
        self,
        url: str,
        data: Any = None,
        headers: MutableMapping = {},
        encoding: str | None = None,
        **kwargs,
    ) -> PageSoup:
        """Make a POST request and return a PageSoup instance."""
        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9")
        kwargs["headers"] = headers
        response = self.post(url, data=data, **kwargs)
        self.last_soup_url = url
        return self.make_soup(response, encoding)

    def submit_form(
        self,
        url: str,
        data: Any = None,
        json: Any = None,
        headers: MutableMapping = {},
        multipart: bool = False,
        **kwargs,
    ) -> Response:
        if multipart:
            content_type = "multipart/form-data"
        else:
            content_type = "application/x-www-form-urlencoded; charset=UTF-8"

        headers = CaseInsensitiveDict(headers or {})
        headers["Content-Type"] = content_type
        kwargs["headers"] = headers

        return self.post(url, data=data, json=json, **kwargs)

    # -- File / image -------------------------------------------------------------

    def get_file(
        self,
        url: str,
        output_file: str | Path,
        headers: MutableMapping = {},
        stream: bool = True,
        **kwargs,
    ) -> None:
        """Download content of the url to a file.

        Checks the abort signal between chunks so downloads can be cancelled via
        :meth:`abort`.
        """
        if isinstance(output_file, str):
            output_file = Path(output_file)

        response = self.get(
            url,
            headers=headers,
            stream=stream,
            **kwargs,
        )
        with atomic_write(output_file) as tmp:
            for chunk in response.iter_content(chunk_size=2048):
                if self.signal.aborted:
                    response.close()
                    raise AbortedException("Download aborted.")
                tmp.write(chunk)

    def get_image(
        self,
        url: str,
        headers: MutableMapping = {},
        timeout: tuple[float, float] = (3, 30),
        **kwargs,
    ):
        """Download image from url and return a `PIL` Image object.

        **Important**: Using this function requires the `image` extra dependency.
        ```
        pip install 'lncrawl-scraper[image]'
        ```

        It installs `Pillow` and `CairoSVG` dependencies.
        The function will throw an `ImportError` without these dependencies.
        """
        from PIL import Image, UnidentifiedImageError

        # base64 data url
        if url.startswith("data:") and ";base64," in url:
            content = base64.b64decode(url.split(",", 1)[-1])
            return Image.open(BytesIO(content))

        # svg data url
        if url.startswith("data:image/svg"):
            import cairosvg

            content = url.split(",", 1)[-1].encode()
            png_bytes = cairosvg.svg2png(bytestring=content)
            if not png_bytes:
                raise RuntimeError("Failed to parse convert SVG to PNG")
            return Image.open(BytesIO(png_bytes))

        # reject all non-http urls
        if not validate_url(url):
            raise ValueError(f"Invalid URL: '{url}'")

        # build headers
        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Origin", None)
        headers.setdefault("Referer", None)
        headers.setdefault(
            "Accept",
            "image/webp,image/png,image/jpeg,image/gif,image/tiff,image/bmp,image/*,*/*;q=0.8",
        )

        # try fetching with explicit Accept headers first,
        # in case of failure try again without the accept headers
        try:
            response = self.get(url, headers=headers, timeout=timeout, **kwargs)
            return Image.open(BytesIO(response.content))
        except UnidentifiedImageError:
            headers["Accept"] = None
            response = self.get(url, headers=headers, timeout=timeout, **kwargs)
            return Image.open(BytesIO(response.content))
