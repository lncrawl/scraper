"""Detect Cloudflare challenges and, when a solver is configured, pass them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

from requests import Response

from ...challenges import CloudflareChallengeKind
from ...exceptions import CloudflareSolveError
from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine


class ChallengeMiddleware(Middleware):
    """Classify each response; raise on a challenge, or auto-solve and retry.

    With no ``config.cloudflare.solver`` configured this raises a clear exception
    on a detected challenge. With a solver, it drives the solver to obtain a
    ``cf_clearance`` cookie, applies it, and re-issues the original request.
    Attempts are bounded by ``config.cloudflare.max_solve_attempts``.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        e = self._engine
        response = nxt(ctx)

        detector = e.cf_detector
        if detector is None:
            return response

        kind = detector.classify(response)
        if kind is CloudflareChallengeKind.NONE:
            e.chain.solve_attempts = 0
            return response

        e.state.mark_cf_active()

        solver = e.cf_solver
        if solver is None:
            detector.raise_for(kind, response)  # NoReturn

        cfg = e.config.cloudflare
        chain = e.chain
        if chain.solve_attempts >= cfg.max_solve_attempts:
            chain.solve_attempts = 0
            raise CloudflareSolveError(
                f"{kind.name} challenge persisted after {cfg.max_solve_attempts} solve "
                "attempt(s): a cf_clearance was obtained but the site re-challenged. "
                "Cloudflare binds the clearance to the solver's IP + TLS fingerprint + "
                "User-Agent — run the solver from the scraper's egress IP/proxy, or scrape "
                "the page directly in the browser."
            )
        chain.solve_attempts += 1

        result = solver.solve(
            ctx.url,
            proxy=self._current_proxy(),
            user_agent=e.headers.get("User-Agent"),
        )
        if not result or "cf_clearance" not in result.cookies:
            chain.solve_attempts = 0
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
        return e.request(ctx.method, ctx.url, *ctx.args, **ctx.kwargs)

    def _current_proxy(self) -> Optional[str]:
        proxy = self._engine.proxy_manager.get_proxy()
        if not proxy:
            return None
        return proxy.get("https") or proxy.get("http")
