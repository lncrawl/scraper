from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from ..config import ProxyConfig, ProxyUrl, TorPoolProxyUrl, TorProxyUrl

logger = logging.getLogger(__name__)

# Calls to the pool's API are best-effort bookkeeping, never the critical path,
# so they get a short timeout and are never allowed to raise.
POOL_API_TIMEOUT = 5.0

# What each failure reason means to tor-pool, which weighs a report by its kind:
# a captcha says the exit IP is burnt and retires it in a couple of reports,
# while a 429 says the exit works and is merely busy and barely counts.
#
# Sent explicitly rather than left to the pool to read out of the free text. Its
# aliases exist for callers written before kinds did, so relying on them means a
# vocabulary drift on either side silently downgrades every report to an
# unremarkable "other" — the same soft failure mode as sending no credential.
# A reason not listed here is still reported; the pool counts it as "other".
FAILURE_KINDS = {
    # What the engine reports on its own.
    "transport": "transport",
    "http_403": "blocked",
    "challenge": "captcha",
    "rate_limited": "rate_limited",
    # The pool's own vocabulary, for callers that pass it straight through.
    "blocked": "blocked",
    "captcha": "captcha",
    "other": "other",
}


class ProxyManager:
    """Round-robin proxy selector for SOCKS/HTTP proxies.

    Tor proxies (:class:`~scraper.config.TorProxyUrl`) get a new exit circuit
    (SIGNAL NEWNYM) on rotate/failure; plain proxies advance round-robin.
    Exhausted proxies are temporarily disabled and re-enabled after
    ``disable_cooldown`` seconds.
    """

    def __init__(self, config: ProxyConfig | None = None) -> None:
        self.config = config or ProxyConfig()

        self._index = 0
        self._lock = threading.Lock()
        self._available: list[ProxyUrl | TorProxyUrl] = []
        self._tor_rotated_at: dict[TorProxyUrl, float] = {}
        self._disabled_at: dict[ProxyUrl | TorProxyUrl, float] = {}
        # ProxyUrl is frozen, so a generated session key cannot live on the
        # entry itself.
        self._session_keys: dict[TorPoolProxyUrl, str] = {}

        for p in self.config.proxy_urls:
            if isinstance(p, str):
                p = ProxyUrl(url=p)
            if "://" not in p.url:
                logger.warning(f"Ignoring proxy with no scheme: {p.url}")
                continue
            self._available.append(p)
        logger.debug(f"{len(self._available)} proxy URL(s) configured.")

    @property
    def has_proxy(self) -> bool:
        self._restore_disabled()
        return bool(self._available)

    def get_proxy(self) -> dict | None:
        """Return a proxies dict for requests, or ``None`` when no proxy is active."""
        if not self.has_proxy:
            return None
        with self._lock:
            current = self._available[self._index]
        url = self._proxy_url(current)
        return {
            "http": url,
            "https": url,
        }

    def _proxy_url(self, entry: ProxyUrl | TorProxyUrl) -> str:
        """Return the URL to dial, with a pool session key applied."""
        if not isinstance(entry, TorPoolProxyUrl):
            return entry.url
        return _with_credentials(entry.url, self._session_key(entry), entry.token)

    def _session_key(self, entry: TorPoolProxyUrl) -> str:
        """Return this entry's session key, generating one on first use."""
        if entry.session:
            return entry.session
        with self._lock:
            return self._session_key_locked(entry)

    def _session_key_locked(self, entry: TorPoolProxyUrl) -> str:
        """``_session_key`` for callers already holding the lock.

        ``threading.Lock`` is not reentrant, so ``rotate()`` — which holds the
        lock for the whole selection — must not go through the locking variant.
        """
        if entry.session:
            return entry.session
        key = self._session_keys.get(entry)
        if key is None:
            key = f"s-{uuid.uuid4().hex[:12]}"
            self._session_keys[entry] = key
        return key

    def disable_current(self):
        with self._lock:
            current = self._available[self._index]
            now = time.monotonic()
            self._disabled_at[current] = now
            self._available.pop(self._index)
            if self._index >= len(self._available):
                self._index = 0
        logger.debug(f"Disabled: {current.url!r}")

    def rotate(self) -> None:
        """Advance to the next proxy identity.

        Pass ``disable=True``to temporarily disable the proxy.

        For a :class:`~scraper.config.TorProxyUrl` with a ``control_port``, sends
        SIGNAL NEWNYM to obtain a new exit circuit; falls back to advancing the
        round-robin index if NEWNYM fails or isn't applicable.
        """
        if not self._available:
            return
        with self._lock:
            current = self._available[self._index]
            # For a pool, rotation is a reassignment to another already-built
            # instance, so there is no cooldown to respect and the URL is
            # unchanged — only the instance behind it moves.
            if isinstance(current, TorPoolProxyUrl) and self._rotate_pool_session(current):
                return
            # for tor-proxies, keep using current proxy on rotation success
            if isinstance(current, TorProxyUrl) and self._try_rotate_tor_circuit(current):
                return
            # move to next proxy
            self._index = (self._index + 1) % len(self._available)

    def report_failure(self, reason: str) -> None:
        """Tell a tor-pool that the current proxy just failed.

        This is the only signal that catches a soft block: the pool relays bytes
        and cannot see a 403, a 429 or a captcha inside an HTTPS tunnel, so
        without this a burnt exit keeps taking traffic until it happens to fail
        at the transport level. Best-effort — a pool that is down must never
        break the scrape.

        ``reason`` is free text the pool keeps for its audit log. A reason it
        recognises also carries a ``kind``, which is what the pool weighs the
        report by — see :data:`FAILURE_KINDS`.
        """
        if not self._available:
            return
        with self._lock:
            current = self._available[self._index]
        if not isinstance(current, TorPoolProxyUrl) or not current.report_failures:
            return

        payload = {"reason": reason}
        kind = FAILURE_KINDS.get(reason.strip().lower())
        if kind:
            payload["kind"] = kind

        key = self._session_key(current)
        self._pool_request(
            current,
            f"/api/sessions/{urllib.parse.quote(key, safe='')}/failure",
            payload,
        )

    def _rotate_pool_session(self, entry: TorPoolProxyUrl) -> bool:
        """Ask the pool to move this session to a different instance."""
        # Called with the lock held by rotate().
        key = self._session_key_locked(entry)
        body = self._pool_request(entry, f"/api/sessions/{urllib.parse.quote(key, safe='')}/rotate")
        if body is None:
            return False
        logger.debug("tor-pool moved session %s to instance %s", key, body.get("instance"))
        return True

    def _pool_request(
        self, entry: TorPoolProxyUrl, path: str, payload: dict | None = None
    ) -> dict | None:
        """POST to the pool's API, returning the decoded body or None on failure."""
        url = entry.api_url.rstrip("/") + path
        data = json.dumps(payload).encode() if payload is not None else b""
        headers = {"content-type": "application/json"}
        if entry.token:
            headers["authorization"] = f"Bearer {entry.token}"
        request = urllib.request.Request(  # noqa: S310 - operator-configured URL
            url,
            data=data,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=POOL_API_TIMEOUT) as resp:  # noqa: S310
                raw = resp.read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            # Called out because it degrades silently otherwise: without a valid
            # token the pool stops hearing about captchas and soft blocks, burnt
            # exits keep taking traffic, and neither side looks broken.
            if exc.code in (401, 403):
                logger.error(
                    "tor-pool rejected the credential for %s (%s). Set TorPoolProxyUrl.token "
                    "to a proxy-scoped token; failure reporting and rotation are not working.",
                    path,
                    exc.code,
                )
            elif exc.code == 404:
                # Routine, not a problem: acting on a report the pool takes the
                # instance out of rotation and unpins its sessions, so the next
                # report about that session has nothing to attach to. The next
                # request re-pins to a healthy instance. Logged quietly because a
                # burnt exit otherwise produces a warning per report.
                logger.debug("tor-pool has no session for %s: %s", path, exc)
            else:
                logger.warning("tor-pool request to %s failed: %s", path, exc)
            return None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            logger.warning("tor-pool request to %s failed: %s", path, exc)
            return None

    def _restore_disabled(self) -> None:
        if not self._disabled_at:
            return
        with self._lock:
            now = time.monotonic()
            to_restore = [
                url
                for url, add_time in self._disabled_at.items()
                if now - add_time >= self.config.disable_cooldown
            ]
            for url in to_restore:
                del self._disabled_at[url]
                self._available.append(url)
                logger.debug(f"Restored: {url!r}")

    def _try_rotate_tor_circuit(self, entry: TorProxyUrl) -> bool:
        """Send SIGNAL NEWNYM to the Tor control port. Returns True on success."""
        if not entry.control_port:
            return False  # no control port

        now = time.monotonic()
        last_rotate = self._tor_rotated_at.get(entry, float("-inf"))
        if (now - last_rotate) <= self.config.tor_rotation_cooldown:
            return False  # still under cooldown
        self._tor_rotated_at[entry] = time.monotonic()

        try:
            _rotate_tor_circuit(
                host=entry.control_host,
                port=entry.control_port,
                password=entry.control_password,
            )
            return True  # rotation successfull
        except Exception:
            logger.warning("Tor circuit rotation failed", exc_info=True)
            return False


def _with_credentials(url: str, username: str, token: str) -> str:
    """Return ``url`` with ``username`` as the user and ``token`` as the password.

    The pool reads the username as a session key, which is how a caller stays
    pinned to one exit IP, and the password as the credential that authorises the
    connection at all. tor-pool 0.1.x ignored the password; 0.2 and later refuse
    the handshake without a valid one.

    Any credentials already in the URL win — an explicitly configured username is
    the operator naming their own session, and they own the password too.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or not username:
        return url

    host = parsed.hostname or ""
    if ":" in host:  # IPv6 literals must stay bracketed
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"

    userinfo = urllib.parse.quote(username, safe="")
    if token:
        userinfo = f"{userinfo}:{urllib.parse.quote(token, safe='')}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"{userinfo}@{host}", parsed.path, parsed.query, parsed.fragment)
    )


def _rotate_tor_circuit(host: str, port: int, password: str) -> None:
    """Authenticate to the Tor control port and request a new exit circuit."""
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(f'AUTHENTICATE "{password}"\r\n'.encode())
        resp = s.recv(128).decode().strip()
        if resp != "250 OK":
            raise RuntimeError(resp)

        s.sendall(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(128).decode().strip()
        if resp != "250 OK":
            raise RuntimeError(resp)

        for _ in range(10):
            s.sendall(b"GETINFO status/bootstrap-phase\r\n")
            resp = s.recv(256).decode().strip()
            if "250" in resp and "PROGRESS=100" in resp:
                return
            time.sleep(0.5)
