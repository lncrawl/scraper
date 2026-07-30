"""Cancellation, as something a caller can hand in per request.

A scraper is shared — that is the point of it, since per-origin state is what the
behavioural layer reads — but cancellation is per unit of work. A consumer running
several jobs through one scraper needs to stop one of them, and the only lever used
to be the scraper's own attribute, so cancelling a job either stopped every other
job on that origin or forced a scraper per thread.

So anything with ``is_set()`` counts as a signal, and several combine. The scraper's
own signal is always one of them: ``abort()`` must keep stopping everything.
"""

from typing import Optional, Protocol, Tuple


class AbortSignal(Protocol):
    """Anything that can report having been tripped. ``threading.Event`` is one."""

    def is_set(self) -> bool: ...


class AnySignal:
    """Set as soon as any of its members is.

    Deliberately not a ``threading.Event`` that a watcher thread mirrors into: the
    combination is read, never waited on, and a thread per in-flight request to
    maintain a copy of a boolean is a lot of machinery for ``or``.
    """

    __slots__ = ("_signals",)

    def __init__(self, *signals: Optional[AbortSignal]) -> None:
        self._signals: Tuple[AbortSignal, ...] = tuple(s for s in signals if s is not None)

    def is_set(self) -> bool:
        return any(signal.is_set() for signal in self._signals)


def combine(*signals: Optional[AbortSignal]) -> AbortSignal:
    """One signal reading as set when any of *signals* is.

    Returns the single member unchanged when there is only one, so the common case
    of no per-request signal costs nothing.
    """
    present = [signal for signal in signals if signal is not None]
    if len(present) == 1:
        return present[0]
    return AnySignal(*present)
