from contextlib import AbstractContextManager, contextmanager
from threading import Event, Semaphore
from typing import Optional

from ..exceptions import AbortedException


class EventLock(AbstractContextManager):
    def __init__(self, concurrency=1) -> None:
        self._signal = Event()
        self._sema = Semaphore(concurrency)

    def abort(self) -> None:
        self._signal.set()

    def acquire(self, signal: Event) -> None:
        while not signal.is_set():
            if self._sema.acquire(timeout=0.1):
                return
        raise AbortedException()

    def release(self) -> None:
        self._sema.release()

    def __enter__(self):
        self.acquire(self._signal)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    @contextmanager
    def using(self, signal: Optional[Event] = None):
        try:
            self.acquire(signal or self._signal)
            yield self
        finally:
            self.release()
