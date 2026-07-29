"""Credentials for the local tor-pool, shared by the harness scripts.

The pool the README starts runs with `AUTH_DISABLED=true`: every port is published to
`127.0.0.1`, so minting a token to talk to a container on this machine is friction that
buys nothing. Nothing here assumes it, though — `/api/auth/status` says which kind of
pool is running, and the authenticated path is kept for a closed one, which is both a
real deployment and what S29 needs.

The proxy password is a placeholder rather than empty when checking is off. It has to be
non-empty: urllib3's `SOCKSProxyManager` only splits userinfo that contains a colon, so
`socks5h://session@host:9250` sends no username at all — and the username *is* the
session key, so every scenario would quietly share one exit and the stickiness ones
would fail for a reason nothing in the run reports.

For a closed pool the token is **minted here rather than read from the environment**.
`export TORPOOL_TOKEN=…` does not survive the shell that ran it, and the failure that
follows is quiet: the pool rejects the SOCKS5 handshake, which never becomes an HTTP
response, and every request through the pool fails for a reason that has nothing to do
with the site being measured. That produced four scenarios confidently reporting a
layer-1 reputation block that did not exist, and a reputation probe in which every host
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

# Stands in for the password an open pool reads and drops. Non-empty on purpose — see
# the module docstring.
OPEN_TOKEN = "tp_auth_disabled"

_CACHE: Dict[str, str] = {}
_AUTH: Dict[str, bool] = {}


def auth_required() -> bool:
    """Whether the pool checks credentials at all. `AUTH_DISABLED` makes it false."""
    if "required" not in _AUTH:
        try:
            reply = requests.get(f"{API}/api/auth/status", timeout=10)
        except requests.RequestException:
            # Silence is not evidence of an open pool. Assume checking is on, so the
            # problem surfaces as a refused credential rather than as a handshake that
            # mysteriously carries no session key.
            return True
        # Pools before 0.3.0 have no such route and always enforced.
        if reply.status_code == 404:
            _AUTH["required"] = True
        else:
            _AUTH["required"] = bool(reply.json().get("required", True))
    return _AUTH["required"]


def jwt() -> str:
    """An operator JWT for the REST API, or `""` when the pool checks nothing."""
    if not auth_required():
        return ""
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
    credential = jwt()
    headers = {"Authorization": f"Bearer {credential}"} if credential else {}
    return requests.get(f"{API}{path}", headers=headers, timeout=10).json()


def token() -> str:
    """The password half of the proxy credential.

    A `proxy`-scoped token against a closed pool, `OPEN_TOKEN` against an open one.
    """
    if "token" not in _CACHE:
        supplied = os.environ.get("TORPOOL_TOKEN", "")
        if supplied:
            _CACHE["token"] = supplied
        elif not auth_required():
            _CACHE["token"] = OPEN_TOKEN
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


def enforcing() -> str:
    """`""` when the pool checks credentials, otherwise the reason it does not.

    The precondition for a scenario whose subject *is* a refused credential. Under
    `AUTH_DISABLED` there is nothing left to refuse, so the request succeeds and the
    scenario would report the opposite of what it measures.
    """
    blocked = ready()
    if blocked:
        return blocked
    if not auth_required():
        return (
            f"tor-pool at {API} runs with AUTH_DISABLED, so any credential is accepted "
            "- restart it with AUTH_DISABLED=false to run this"
        )
    return ""
