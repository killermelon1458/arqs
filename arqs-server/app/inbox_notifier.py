from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field


@dataclass
class _NodeSlot:
    version: int = 0
    waiters: list[asyncio.Future[int]] = field(default_factory=list)


class InboxNotifier:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._slots: dict[str, _NodeSlot] = {}

    def _bind_running_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        thread_id = threading.get_ident()
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = loop
                self._loop_thread_id = thread_id
            elif self._loop is not loop:
                raise RuntimeError("InboxNotifier cannot span multiple event loops")
        return loop

    def snapshot(self, node_id: str) -> int:
        self._bind_running_loop()
        with self._lock:
            slot = self._slots.setdefault(node_id, _NodeSlot())
            return slot.version

    async def wait_for_change(self, node_id: str, *, after_version: int, timeout: float) -> int:
        loop = self._bind_running_loop()
        with self._lock:
            slot = self._slots.setdefault(node_id, _NodeSlot())
            if slot.version != after_version:
                return slot.version
            future: asyncio.Future[int] = loop.create_future()
            slot.waiters.append(future)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            with self._lock:
                slot = self._slots.get(node_id)
                if slot is not None:
                    try:
                        slot.waiters.remove(future)
                    except ValueError:
                        pass
            return after_version
        except asyncio.CancelledError:
            with self._lock:
                slot = self._slots.get(node_id)
                if slot is not None:
                    try:
                        slot.waiters.remove(future)
                    except ValueError:
                        pass
            raise

    def notify(self, node_id: str) -> None:
        with self._lock:
            loop = self._loop
            loop_thread_id = self._loop_thread_id
        if loop is None or loop.is_closed():
            return

        if threading.get_ident() == loop_thread_id:
            self._notify_in_loop(node_id)
            return

        loop.call_soon_threadsafe(self._notify_in_loop, node_id)

    def _notify_in_loop(self, node_id: str) -> None:
        with self._lock:
            slot = self._slots.setdefault(node_id, _NodeSlot())
            slot.version += 1
            version = slot.version
            waiters = slot.waiters
            slot.waiters = []

        for future in waiters:
            if not future.done():
                future.set_result(version)
