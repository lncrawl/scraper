"""One WebSocket JSON-RPC channel, for both browser protocols.

CDP and WebDriver BiDi are the same shape on the wire: an object with an ``id`` goes
out, an object carrying that ``id`` comes back, and events carrying none are
interleaved. That is the whole of the transport, and it is why a Firefox backend costs
a vocabulary rather than a rewrite.

They differ in exactly one place, which :func:`describe_error` absorbs. Everything else
here is protocol-agnostic on purpose — put a CDP or BiDi assumption in this module and
the next backend pays for it.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from .browser import SolveError
from .exceptions import MissingDependency

logger = logging.getLogger(__name__)

MAX_FRAME = 64 * 1024 * 1024
"""Cap on one protocol message.

The library default is 1 MiB, which a page's own HTML clears without difficulty — and
the failure is a closed connection mid-solve rather than anything naming a size."""


class ProtocolError(SolveError):
    """The browser answered a command with an error, or stopped answering."""


def connect() -> Any:
    try:
        from websockets.sync.client import connect as _connect
    except ImportError as exc:
        raise MissingDependency("cdp", "driving a browser over a debugging protocol") from exc
    return _connect


def describe_error(reply: Dict[str, Any]) -> str:
    """What went wrong, in whichever way this protocol says it.

    The one place the two vocabularies are not the same shape. CDP puts an object under
    ``error`` with the text in ``message``; BiDi puts a code *string* there and the
    detail in a sibling ``message``. Reading either shape as the other raises an
    ``AttributeError`` from inside the transport, which buries the actual failure.
    """
    error = reply.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    detail = reply.get("message")
    return f"{error}: {detail}" if detail else str(error)


class WsClient:
    """Send a command, get the matching reply.

    Synchronous, and single-caller by construction: a solver holds its lock for the
    whole of a solve, so there is never a second command in flight and correlation
    needs no more than reading until the id matches. Events arriving in between are
    dropped rather than queued, because nothing here subscribes to any.
    """

    def __init__(self, url: str, *, open_timeout: float = 10.0) -> None:
        self._sock = connect()(url, open_timeout=open_timeout, max_size=MAX_FRAME)
        self._next_id = 0

    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: str = "",
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Issue *method* and return its ``result``.

        *session* is CDP's flattened session id, sent as ``sessionId``. BiDi carries
        its session on the connection instead and leaves this empty.
        """
        self._next_id += 1
        request_id = self._next_id
        # `params` always travels, even empty. CDP tolerates it missing and BiDi does
        # not — it answers `Expected "params" to be an object, got undefined`, which
        # reads like a bug in the command rather than in the envelope.
        message: Dict[str, Any] = {"id": request_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._sock.send(json.dumps(message))

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(f"{method} did not answer within {timeout:.0f}s")
            try:
                raw = self._sock.recv(timeout=remaining)
            except TimeoutError as exc:
                raise ProtocolError(f"{method} did not answer within {timeout:.0f}s") from exc
            except Exception as exc:  # noqa: BLE001 - a dropped socket is a solve failure
                raise ProtocolError(f"the browser stopped answering during {method}") from exc
            try:
                reply = json.loads(raw)
            except ValueError:
                continue
            if reply.get("id") != request_id:
                continue  # an event, or a reply to something already abandoned
            if "error" in reply or reply.get("type") == "error":
                raise ProtocolError(f"{method}: {describe_error(reply)}")
            return reply.get("result") or {}

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is not a failure
            logger.debug("the protocol socket did not close cleanly", exc_info=True)
