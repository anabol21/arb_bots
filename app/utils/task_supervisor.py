"""Mutable asyncio task set.

One-shot ``asyncio.gather(*tasks)`` snapshots the wait set. Hot-added
listener tasks would then run orphaned and would not be cancelled on
SIGTERM. This supervisor includes tasks added after wait() started.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Optional


class TaskSupervisor:
    """Dynamic set of tasks; wait() sees late adds; cancel_all() covers them."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._wakeup = asyncio.Event()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        return len(self._tasks)

    def snapshot(self) -> list[asyncio.Task[Any]]:
        return list(self._tasks)

    def add(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: Optional[str] = None,
    ) -> asyncio.Task[Any]:
        if self._closed:
            coro.close()
            raise RuntimeError(
                f"refusing to add task {name!r}: supervisor is closed"
            )
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        self._wakeup.set()
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        self._wakeup.set()

    def cancel_all(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        self._wakeup.set()

    async def wait(self) -> None:
        """Block until closed and drained, or a non-cancelled task fails."""
        while True:
            live = {task for task in self._tasks if not task.done()}
            if self._closed and not live:
                return
            if not live:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            wakeup_task = asyncio.create_task(
                self._wakeup.wait(), name="supervisor-wakeup"
            )
            try:
                done, _pending = await asyncio.wait(
                    live | {wakeup_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not wakeup_task.done():
                    wakeup_task.cancel()
                    try:
                        await wakeup_task
                    except asyncio.CancelledError:
                        pass
            self._wakeup.clear()
            for task in done:
                if task is wakeup_task:
                    continue
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    self.cancel_all()
                    raise exc

    async def drain(self) -> None:
        """Cancel remaining tasks and wait (return_exceptions)."""
        self.cancel_all()
        pending = [task for task in list(self._tasks) if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
