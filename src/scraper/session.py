"""The :class:`Scraper` session — the library's main entry point.

A thin composition facade over the Cloudflare-bypass :class:`~scraper.engine.core.Engine`:
it delegates the HTTP surface to the engine and adds ergonomic helpers for HTML,
JSON, file, and image retrieval, automatic Origin/Referer injection, and sensible
default timeouts.
"""

from __future__ import annotations

import base64
import logging
import threading
from io import BytesIO
from pathlib import Path
from typing import Any, MutableMapping, Optional

import httpx
from curl_cffi.requests.session import HttpMethod

from .config import ScraperConfig, default_config
from .engine import Engine, ProxyManager, create_engine
from .exceptions import AbortedException
from .soup import PageSoup
from .utils import RequestHeaders, atomic_write, extract_base, validate_url

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
        **kwargs: Any,
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
    def cookies(self) -> httpx.Cookies:
        return self.engine.cookies

    @property
    def proxy_manager(self) -> ProxyManager:
        return self.engine.proxy_manager

    def put_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        """Set a cookie on the session (and the transport's jar)."""
        self.engine.put_cookie(name, value, domain=domain, path=path)

    def apply_browser_clearance(self, domain: str, **kwargs: Any) -> None:
        """Reuse a Cloudflare clearance solved by a real browser (see Engine)."""
        self.engine.apply_browser_clearance(domain, **kwargs)

    def rotate_proxy(self) -> None:
        """Rotate to the next proxy and reset the transport connection pool.

        For a :class:`~scraper.config.TorProxyUrl` with a ``control_port``,
        sends ``SIGNAL NEWNYM`` to obtain a new Tor exit circuit. For any
        other proxy type (or when NEWNYM fails), advances the round-robin
        index to the next configured proxy. In both cases the transport
        session is recreated so the next request uses fresh TCP connections.
        """
        self.engine.rotate_proxy()

    def abort_on(self, signal: threading.Event) -> None:
        """Abort the scraper once *signal* is set (see :meth:`Engine.abort_on`)."""
        self.engine.abort_on(signal)

    def close(self) -> None:
        """Abort all in-progress requests and release transport resources."""
        self.engine.abort()
        self.engine.close()

    def __enter__(self) -> "Scraper":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

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

    def request(
        self,
        method: HttpMethod,
        url: str,
        *args: Any,
        cancel_token: Optional[Any] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue a request with auto Origin/Referer, then raise on HTTP errors."""
        kwargs.setdefault("allow_redirects", True)

        headers = RequestHeaders(kwargs.pop("headers", {}) or {})
        last_url = self.last_soup_url or self.origin
        if last_url:
            origin = extract_base(last_url)
            headers.setdefault("Origin", origin.strip("/"))
            headers.setdefault("Referer", origin)
        kwargs["headers"] = {k: v for k, v in headers.items() if v is not None}

        response = self.engine.request(method, url, *args, cancel_token=cancel_token, **kwargs)
        response.raise_for_status()
        return response

    def ping(self, url: str, timeout: float = 5, **kwargs: Any) -> httpx.Response:
        """Send a HEAD request — a lightweight reachability check."""
        return self.request("HEAD", url, timeout=timeout, **kwargs)

    def options(self, url: str, **kwargs: Any) -> httpx.Response:
        """OPTIONS to ``url`` with a default ``(connect, read)`` timeout."""
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return self.request("OPTIONS", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        """HEAD to ``url`` with a default ``(connect, read)`` timeout."""
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return self.request("HEAD", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET ``url`` with a default ``(connect, read)`` timeout."""
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return self.request("GET", url, **kwargs)

    def post(self, url: str, data: Any = None, json: Any = None, **kwargs: Any) -> httpx.Response:
        """Raw POST to ``url`` with a default ``(connect, read)`` timeout."""
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return self.request("POST", url, data=data, json=json, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        """Raw PUT to ``url``"""
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        """Raw PATCH to ``url``"""
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        """Raw DELETE to ``url``"""
        return self.request("DELETE", url, **kwargs)

    def get_json(self, url: str, headers: Optional[MutableMapping] = None, **kwargs: Any) -> Any:
        """Fetch content and return it as a JSON object."""
        merged = RequestHeaders(headers or {})
        merged.setdefault("Accept", "application/json,text/plain,*/*")
        kwargs["headers"] = dict(merged)
        return self.get(url, **kwargs).json()

    def post_json(
        self, url: str, data: Any = None, headers: Optional[MutableMapping] = None, **kwargs: Any
    ) -> Any:
        """Make a POST request and return the content as a JSON object."""
        merged = RequestHeaders(headers or {})
        merged.setdefault("Content-Type", "application/json")
        merged.setdefault("Accept", "application/json,text/plain,*/*")
        response = self.post(url, data=data, headers=dict(merged), **kwargs)
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
        headers: Optional[MutableMapping] = None,
        encoding: str | None = None,
        **kwargs: Any,
    ) -> PageSoup:
        """Fetch content and return a PageSoup instance."""
        merged = RequestHeaders(headers or {})
        merged.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9")
        kwargs["headers"] = dict(merged)
        response = self.get(url, **kwargs)
        self.last_soup_url = url
        return self.make_soup(response, encoding)

    def post_soup(
        self,
        url: str,
        data: Any = None,
        headers: Optional[MutableMapping] = None,
        encoding: str | None = None,
        **kwargs: Any,
    ) -> PageSoup:
        """Make a POST request and return a PageSoup instance."""
        merged = RequestHeaders(headers or {})
        merged.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9")
        kwargs["headers"] = dict(merged)
        response = self.post(url, data=data, **kwargs)
        self.last_soup_url = url
        return self.make_soup(response, encoding)

    def submit_form(
        self,
        url: str,
        data: Any = None,
        json: Any = None,
        headers: Optional[MutableMapping] = None,
        multipart: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        if multipart:
            content_type = "multipart/form-data"
        else:
            content_type = "application/x-www-form-urlencoded; charset=UTF-8"

        merged = RequestHeaders(headers or {})
        merged["Content-Type"] = content_type
        kwargs["headers"] = dict(merged)

        return self.post(url, data=data, json=json, **kwargs)

    # -- File / image -------------------------------------------------------------

    def get_file(
        self,
        url: str,
        output_file: str | Path,
        headers: Optional[MutableMapping] = None,
        stream: bool = True,
        cancel_token: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        """Download content of the url to a file.

        Checks the abort flag between chunks so downloads can be cancelled via
        :meth:`abort` or a ``cancel_token``.
        """
        if isinstance(output_file, str):
            output_file = Path(output_file)

        response = self.get(
            url,
            headers=headers,
            stream=stream,
            cancel_token=cancel_token,
            **kwargs,
        )
        with atomic_write(output_file) as tmp:
            for chunk in response.iter_bytes(chunk_size=2048):
                if self.engine._aborted:
                    raise AbortedException("Download aborted.")
                tmp.write(chunk)

    def get_image(
        self,
        url: str,
        headers: Optional[MutableMapping] = None,
        timeout: tuple[float, float] = _DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> Any:
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
            raise NotImplementedError("SVG Images are not supported")

        # reject all non-http urls
        if not validate_url(url):
            raise ValueError(f"Invalid URL: '{url}'")

        # build headers
        merged = RequestHeaders(headers or {})
        merged.setdefault("Origin", None)
        merged.setdefault("Referer", None)
        merged.setdefault(
            "Accept",
            "image/webp,image/png,image/jpeg,image/gif,image/tiff,image/bmp,image/*,*/*;q=0.8",
        )

        try:
            response = self.get(url, headers=dict(merged), timeout=timeout, **kwargs)
            return Image.open(BytesIO(response.content))
        except UnidentifiedImageError:
            merged["Accept"] = None
            response = self.get(url, headers=dict(merged), timeout=timeout, **kwargs)
            return Image.open(BytesIO(response.content))
