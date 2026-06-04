import unicodedata
from urllib.parse import urlparse


def extract_base(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def extract_host(url: str) -> str:
    parsed = urlparse(url)

    host = parsed.hostname
    port = str(parsed.port or "")
    if not host:
        host = parsed.path.split("/")[0]
        if ":" in host:
            host, port = host.split(":")
    if not host:
        return ""

    normalized = unicodedata.normalize("NFKD", host).casefold()
    host = normalized.encode("idna").decode("ascii")
    if host.startswith("www."):
        host = host[4:]
    if port:
        host += f":{port}"
    return host


def validate_url(url: str, allowed_schemes=["http", "https"]) -> bool:
    parsed = urlparse(url)
    return all([parsed.scheme, parsed.netloc, parsed.scheme in allowed_schemes])
