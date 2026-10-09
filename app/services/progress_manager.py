from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import AsyncIterator

from app.models import EventMessage


class ProgressManager:
    def __init__(self, interval: float = 0.25) -> None:
        self.interval = interval
        self._queues: set[asyncio.Queue[EventMessage]] = set()
        self._last_sent: dict[str, float] = defaultdict(float)

    async def subscribe(self) -> AsyncIterator[EventMessage]:
        queue: asyncio.Queue[EventMessage] = asyncio.Queue(maxsize=256)
        self._queues.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._queues.discard(queue)

    async def emit(self, message: EventMessage, *, force: bool = False) -> None:
        now = time.monotonic()
        key = f"{message.event_type}:{message.task_id or ''}"
        if not force and now - self._last_sent[key] < self.interval:
            return
        self._last_sent[key] = now
        for queue in list(self._queues):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Drop older progress rather than blocking transfer workers.
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except asyncio.QueueEmpty:
                    pass

    def clear_task(self, task_id: str) -> None:
        for key in list(self._last_sent):
            if key.endswith(f":{task_id}"):
                del self._last_sent[key]
