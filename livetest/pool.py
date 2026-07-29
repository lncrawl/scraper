"""Credentials for the local tor-pool, shared by the harness scripts.

tor-pool enforces authentication on both the REST API and the proxy listeners, so a
run needs two things: an operator login to read instance state, and a `proxy`-scoped
token to be the SOCKS5 password.

The token is **minted here rather than read from the environment**. `export
TORPOOL_TOKEN=…` does not survive the shell that ran it, and the failure that follows
is quiet: the pool rejects the SOCKS5 handshake, which never becomes an HTTP response,
and every request through the pool fails for a reason that has nothing to do with the
site being measured. That produced four scenarios confidently reporting a layer-1
reputation block that did not exist, and a reputation probe in which every host
appeared to refuse Tor. An explicit `TORPOOL_TOKEN` still wins, for a real deployment.
"""

from __future__ import annotations

import os
from typing import Dict

import requests

API = os.environ.get("TORPOOL_API", "http://127.0.0.1:8080")
USER = os.environ.get("TORPOOL_USER", "admin")
PASSWORD = os.environ.get("TORPOOL_PASSWORD", "admin")
SOCKS = os.environ.get("TORPOOL_SOCKS", "socks5h://127.0.0.1:9250")

_CACHE: Dict[str, str] = {}


def jwt() -> str:
    """An operator JWT, for the REST API."""
    reply = requests.post(
        f"{API}/api/auth/login", json={"user": USER, "password": PASSWORD}, timeout=10
    )
    if reply.status_code != 200:
        raise RuntimeError(
            f"tor-pool rejected the operator login for {USER!r} (HTTP {reply.status_code}); "
            "set TORPOOL_USER and TORPOOL_PASSWORD"
        )
    return str(reply.json()["token"])


def get(path: str) -> object:
    return requests.get(
        f"{API}{path}", headers={"Authorization": f"Bearer {jwt()}"}, timeout=10
    ).json()


def token() -> str:
    """A `proxy`-scoped token: the password half of the proxy credential."""
    if "token" not in _CACHE:
        supplied = os.environ.get("TORPOOL_TOKEN", "")
        if supplied:
            _CACHE["token"] = supplied
        else:
            reply = requests.post(
                f"{API}/api/tokens",
                headers={"Authorization": f"Bearer {jwt()}"},
                json={"name": "livetest", "scope": "proxy"},
                timeout=10,
            )
            reply.raise_for_status()
            _CACHE["token"] = str(reply.json()["secret"])
    return _CACHE["token"]


def ready() -> str:
    """`""` when the pool is usable, otherwise the reason it is not.

    Used as a scenario precondition. Reporting "not run, and here is why" is the
    whole point: a scenario that runs without its infrastructure still emits steps
    and a verdict, and those read as findings about the library.
    """
    try:
        health = requests.get(f"{API}/health", timeout=5)
        if health.status_code != 200:
            return f"tor-pool at {API} answered HTTP {health.status_code}"
    except requests.RequestException as exc:
        return f"no tor-pool at {API} ({type(exc).__name__}) - see livetest/README.md"
    try:
        token()
    except Exception as exc:  # noqa: BLE001 - the reason is the payload
        return f"tor-pool credential unavailable: {exc}"
    return ""
