"""Serialized background execution for a single instrument session."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


class SerialWorker:
    """Run operations one at a time on a background thread."""

    def __init__(self, *, thread_name: str = "oscill-visa") -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name)
        self._closed = False

    def submit(self, operation: Callable[[], Any]) -> Future[Any]:
        if self._closed:
            raise RuntimeError("worker is closed")
        return self._executor.submit(operation)

    def close(self, finalizer: Callable[[], Any] | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        if finalizer is not None:
            self._executor.submit(finalizer)
        self._executor.shutdown(wait=False)
