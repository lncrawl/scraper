import unicodedata
from urllib.parse import urlparse


def _format_and_parse(url: str):
    if not (url.startswith("//") or "://" in url):
        url = f"//{url}"
    return urlparse(url)


def extract_base(url: str) -> str:
    parsed = _format_and_parse(url)
    scheme = parsed.scheme or "http"
    return f"{scheme}://{parsed.netloc}/"


def extract_host(url: str) -> str:
    parsed = _format_and_parse(url)
    host = parsed.hostname
    try:
        port = str(parsed.port or "")
    except ValueError:
        port = ""
    if not host:
        return ""

    # Normalize and IDNA encode
    host = unicodedata.normalize("NFKD", host).casefold()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass  # Failsafe in case of garbage data that breaks IDNA encoding

    if host.startswith("www."):
        host = host[4:]
    if port:
        host += f":{port}"
    return host


def validate_url(url: str, allowed_schemes=["http", "https"]) -> bool:
    parsed = _format_and_parse(url)
    return all([parsed.scheme, parsed.netloc, parsed.scheme in allowed_schemes])
