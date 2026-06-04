import base64
import logging
from io import BytesIO
from pathlib import Path
from typing import Any, MutableMapping, Optional, Tuple, Union

from requests import Response
from requests.structures import CaseInsensitiveDict

from ._engine import ScraperEngine
from ._engine.config import ScraperConfig
from ._engine.exceptions import AbortedException
from ._utils.file_tools import atomic_write
from ._utils.url_tools import extract_base
from .config import default_config
from .soup import PageSoup

logger = logging.getLogger(__name__)


class Scraper(ScraperEngine):
    def __init__(
        self,
        origin: Optional[str] = None,
        parser: Optional[str] = None,
        config: Optional[ScraperConfig] = None,
        **kwargs,
    ) -> None:
        super().__init__(config=config or default_config())
        self.origin = origin or ""
        self.parser = parser or "lxml"
        self.last_soup_url = self.origin

    def reset(self) -> None:
        """Reset the scraper to its initial state."""
        self.cookies.clear()
        if self._impersonate is not None:
            self._impersonate.cookies.clear()
        self.headers.clear()
        self.last_soup_url = ""

    def set_header(self, key: str, value: Union[str, bytes]) -> None:
        """Set a default header for subsequent requests."""
        if isinstance(value, bytes):
            value = value.decode()
        self.headers[key] = str(value)

    def set_cookie(self, name: str, value: Union[str, bytes]) -> None:
        """Set a session cookie (also propagated to the impersonation jar)."""
        if isinstance(value, bytes):
            value = value.decode()
        self.put_cookie(name, str(value))

    def request(self, method, url, *args, **kwargs):
        origin_url = extract_base(self.last_soup_url or self.origin or url)

        headers = CaseInsensitiveDict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Origin", origin_url.strip("/"))
        headers.setdefault("Referer", origin_url)
        kwargs["headers"] = headers

        kwargs.setdefault("allow_redirects", True)

        response = super().request(method, url, *args, **kwargs)
        response.raise_for_status()
        response.encoding = "utf8"
        return response

    def ping(self, url: str, timeout=5, **kwargs):
        return self.request("head", url, **kwargs, timeout=timeout)

    def get(self, url, **kwargs) -> Response:  # type: ignore[override]
        kwargs.setdefault("timeout", (15, 301))
        return super().get(url, **kwargs)

    def post(self, url, **kwargs) -> Response:  # type: ignore[override]
        kwargs.setdefault("timeout", (15, 301))
        return super().post(url, **kwargs)

    def submit_form(
        self,
        url: str,
        data: Optional[Union[MutableMapping, str, bytes]] = None,
        json: Optional[Union[MutableMapping, str, bytes]] = None,
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

    def get_file(
        self,
        url: str,
        output_file: Union[str, Path],
        headers: MutableMapping = {},
        **kwargs,
    ) -> None:
        """Download content of the url to a file.

        Checks the abort signal between chunks so downloads can be cancelled
        via :meth:`abort`.
        """
        if isinstance(output_file, str):
            output_file = Path(output_file)

        kwargs["headers"] = headers
        kwargs.setdefault("stream", True)
        response = self.get(url, **kwargs)
        with atomic_write(output_file) as tmp:
            for chunk in response.iter_content(chunk_size=10240):
                if self.signal.is_set():
                    response.close()
                    raise AbortedException("Download aborted.")
                tmp.write(chunk)

    def get_image(
        self,
        url: str,
        headers: MutableMapping = {},
        timeout: Tuple[float, float] = (3, 30),
        **kwargs,
    ):
        """Download image from url and return a PIL Image object."""
        from PIL import Image, UnidentifiedImageError

        if url.startswith("data:"):
            content = base64.b64decode(url.split("base64,")[-1])
            return Image.open(BytesIO(content))

        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Origin", None)
        headers.setdefault("Referer", None)
        kwargs["timeout"] = timeout
        kwargs["headers"] = headers

        try:
            response = self.get(url, **kwargs)
            return Image.open(BytesIO(response.content))
        except UnidentifiedImageError:
            headers.setdefault(
                "Accept",
                "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.9",
            )
            response = self.get(url, **kwargs)
            return Image.open(BytesIO(response.content))

    def get_json(self, url: str, headers: MutableMapping = {}, **kwargs) -> Any:
        """Fetch content and return it as a JSON object."""
        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Accept", "application/json,text/plain,*/*")
        kwargs["headers"] = headers
        return self.get(url, **kwargs).json()

    def post_json(
        self,
        url: str,
        data: Optional[Union[MutableMapping, str, bytes]] = None,
        headers: MutableMapping = {},
        **kwargs,
    ) -> Any:
        """Make a POST request and return the content as a JSON object."""
        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json,text/plain,*/*")
        response = self.post(url, data=data, headers=headers, **kwargs)
        return response.json()

    def make_soup(
        self,
        data: Union[Response, bytes, str, Any],
        encoding: Optional[str] = None,
    ) -> PageSoup:
        return PageSoup.create(data, encoding, self.parser)

    def get_soup(
        self,
        url: str,
        headers: MutableMapping = {},
        encoding: Optional[str] = None,
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
        data: Optional[Union[MutableMapping, str, bytes]] = None,
        headers: MutableMapping = {},
        encoding: Optional[str] = None,
        **kwargs,
    ) -> PageSoup:
        """Make a POST request and return a PageSoup instance."""
        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9")
        kwargs["headers"] = headers
        response = self.post(url, data=data, **kwargs)
        self.last_soup_url = url
        return self.make_soup(response, encoding)
