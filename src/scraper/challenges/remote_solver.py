"""Remote challenge solver — a FlareSolverr / Byparr HTTP client.

Drives a browser running in a separate process/container through the FlareSolverr
``v1`` API (``cmd: request.get``) and returns the harvested ``cf_clearance``
cookie + User-Agent. Byparr exposes the same API, so the same client works for
both — point ``endpoint`` at whichever you run.

This keeps the scraper itself lightweight: no browser, Chrome, or Xvfb in its own
image. For best results run the service behind the same egress IP/proxy as the
scraper, since Cloudflare binds ``cf_clearance`` to IP + TLS + UA.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from ..exceptions import CloudflareSolveError
from .clearance import ClearanceResult, ClearanceSolver

logger = logging.getLogger(__name__)


class RemoteSolver(ClearanceSolver):
    """Solve Cloudflare challenges via a FlareSolverr/Byparr-compatible service."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 60.0,
        session: Optional[str] = None,
    ) -> None:
        """Args:
        endpoint: Base URL of the service, e.g. ``http://localhost:8191``.
        timeout: Max seconds the service may spend solving (sent as ``maxTimeout``).
        session: Optional FlareSolverr session id to reuse a warm browser session.
        """
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.session = session

    async def solve_async(
        self,
        url: str,
        *,
        proxy: str | None = None,
        user_agent: str | None = None,
    ) -> Optional[ClearanceResult]:
        payload: dict = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": int(self.timeout * 1000),
        }
        if self.session:
            payload["session"] = self.session
        if proxy:
            payload["proxy"] = {"url": proxy}

        try:
            # A small margin over maxTimeout so the HTTP call outlives the solve.
            resp = requests.post(f"{self.endpoint}/v1", json=payload, timeout=self.timeout + 15)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise CloudflareSolveError(
                f"RemoteSolver request to {self.endpoint} failed: {exc}"
            ) from exc

        if data.get("status") != "ok":
            raise CloudflareSolveError(
                f"RemoteSolver did not solve the challenge: {data.get('message') or data.get('status')}"
            )

        solution = data.get("solution") or {}
        cookies = {
            c["name"]: c["value"]
            for c in solution.get("cookies", [])
            if "name" in c and "value" in c
        }
        return ClearanceResult(cookies=cookies, user_agent=solution.get("userAgent", ""))
