from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator


class EventBroadcaster:
    """Tiny in-process pub/sub for live event streaming to the web UI."""

    def __init__(self, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._queue_size = queue_size

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow subscriber — drop the oldest, push the new.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        try:
            while True:
                event = await q.get()
                yield event
        finally:
            self._subscribers.discard(q)


broadcaster = EventBroadcaster()
