from typing import TYPE_CHECKING, List

from .base import ChallengeHandler
from .cloudflare_v1 import CloudflareV1Handler
from .cloudflare_v2 import CloudflareV2Handler
from .cloudflare_v3 import CloudflareV3Handler
from .turnstile import TurnstileHandler

if TYPE_CHECKING:
    from ...config import CloudflareConfig

__all__ = [
    "ChallengeHandler",
    "CloudflareV1Handler",
    "CloudflareV2Handler",
    "CloudflareV3Handler",
    "TurnstileHandler",
]


def build_handlers(cfg: "CloudflareConfig") -> List[ChallengeHandler]:
    handlers: List[ChallengeHandler] = []
    if not cfg.disable_turnstile:
        handlers.append(TurnstileHandler())
    if not cfg.disable_v3:
        handlers.append(CloudflareV3Handler(debug=cfg.debug))
    if not cfg.disable_v2:
        handlers.append(CloudflareV2Handler(debug=cfg.debug))
    if not cfg.disable_v1:
        handlers.append(CloudflareV1Handler(debug=cfg.debug, double_down=cfg.double_down))
    return handlers
