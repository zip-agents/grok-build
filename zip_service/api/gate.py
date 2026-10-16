"""In-flight zip requests and graceful shutdown.

Health checks do not take a slot. A request over the limit is rejected at
once; the gate is a counter, not a semaphore that callers wait on.
"""

from __future__ import annotations

import asyncio


class Gate:
    def __init__(self, max_concurrent: int) -> None:
        self._max = max_concurrent
        self._lock = asyncio.Lock()
        self._inflight = 0
        self._shutting_down = False
        self._drained = asyncio.Event()
        self._drained.set()
        self._tasks: set[asyncio.Task[None]] = set()
        # Set when grace has expired and remaining work must stop.
        self.force_code: str | None = None

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    def begin_shutdown(self) -> None:
        """Reject new zip work and fail readiness. In-flight requests continue."""

        self._shutting_down = True

    def force_stop(self) -> None:
        """Cancel in-flight work. Used when shutdown is signalled twice."""

        self._shutting_down = True
        self.force_code = "shutting-down"
        for task in list(self._tasks):
            task.cancel()

    def track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def try_acquire(self) -> str | None:
        """Reserve a slot. Returns an error code, or None when the slot is held."""

        async with self._lock:
            if self._shutting_down:
                return "shutting-down"
            if self._inflight >= self._max:
                return "overloaded"
            self._inflight += 1
            if self._inflight == 1:
                self._drained.clear()
            return None

    async def release(self) -> None:
        async with self._lock:
            if self._inflight > 0:
                self._inflight -= 1
            if self._inflight == 0:
                self._drained.set()

    async def wait_for_drain(self, grace_seconds: float) -> None:
        """Wait for in-flight work, then cancel whatever is still running."""

        if self._inflight == 0:
            return
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=grace_seconds)
        except TimeoutError:
            self.force_code = "shutting-down"
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=2)
