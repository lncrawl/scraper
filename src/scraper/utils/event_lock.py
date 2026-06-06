from __future__ import annotations

from threading import Event, Semaphore

from ..exceptions import AbortedException


class EventLock:
    def __init__(self, concurrency=1, interval=0.1) -> None:
        self._signal = Event()
        self._interval = interval
        self._sema = Semaphore(concurrency)

    @property
    def aborted(self) -> bool:
        return self._signal.is_set()

    def abort(self):
        self._signal.set()

    def reset(self):
        self._signal = Event()

    def acquire(self):
        while not self._signal.is_set():
            if self._sema.acquire(timeout=self._interval):
                return True
        return False

    def release(self):
        self._sema.release()

    def __enter__(self):
        if not self.acquire():
            raise AbortedException()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False
