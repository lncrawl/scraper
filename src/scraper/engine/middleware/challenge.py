"""Detect Cloudflare challenges and, when a solver is configured, pass them."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

import httpx

from ...challenges import CloudflareChallengeKind
from ...exceptions import CloudflareSolveError
from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..core import Engine
    from ..state import RequestState


class ChallengeMiddleware(Middleware):
    """Classify each response; raise on a challenge, or auto-solve and retry.

    With no solvers configured this raises a clear exception on a detected
    challenge. With solvers, it drives each in order until one obtains a
    ``cf_clearance`` cookie, applies it, and re-issues the original request.
    Attempts are bounded by ``config.cloudflare.max_solve_attempts``.
    """

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    async def handle(self, ctx: "RequestState", nxt: NextHandler) -> httpx.Response:
        e = self._engine
        response = await nxt(ctx)

        detector = e.cf_detector
        if detector is None:
            return response

        kind = detector.classify(response)
        if kind is CloudflareChallengeKind.NONE:
            return response

        e.state.mark_cf_active()

        solvers = e.config.cloudflare.effective_solvers()
        if not solvers:
            detector.raise_for(kind, response)  # NoReturn

        cfg = e.config.cloudflare
        if ctx.solve_attempts >= cfg.max_solve_attempts:
            raise CloudflareSolveError(
                f"{kind.name} challenge persisted after {cfg.max_solve_attempts} solve "
                "attempt(s): a cf_clearance was obtained but the site re-challenged. "
                "Cloudflare binds the clearance to the solver's IP + TLS fingerprint + "
                "User-Agent - run the solver from the scraper's egress IP/proxy, or scrape "
                "the page directly in the browser."
            )

        current_proxy = self._current_proxy()
        result = None
        for solver in solvers:
            result = await solver.solve(
                ctx.url,
                proxy=current_proxy,
                user_agent=e.headers.get("User-Agent"),
            )
            if result and result.cf_clearance:
                break

        if not result or not result.cf_clearance:
            raise CloudflareSolveError(
                f"Solver did not obtain a cf_clearance cookie for the {kind.name} challenge "
                "(it may require an interactive Turnstile click the solver cannot perform "
                "headlessly). Try BrowserSolver(headless=False) and solve it manually, or use "
                "a solver service (FlareSolverr/Byparr) via RemoteSolver."
            )

        host = urlparse(ctx.url).hostname or ctx.url
        e.apply_browser_clearance(
            host, cookies=result.cookies, user_agent=result.user_agent or None
        )
        retry_ctx = replace(ctx, depth=ctx.depth + 1, solve_attempts=ctx.solve_attempts + 1)
        return await e._run_pipeline(retry_ctx)

    def _current_proxy(self) -> Optional[str]:
        return self._engine.proxy_manager.get_proxy()
