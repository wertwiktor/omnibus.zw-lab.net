"""Per-session resource slots: display number, pulse sink, VNC/noVNC ports.

Replaces the old fixed display :99 / port 5900/6080 singletons so several
recording (or auth) sessions can run concurrently without colliding.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from omnibus.config import settings


@dataclass(frozen=True)
class ResourceSlot:
    index: int

    @property
    def display_number(self) -> int:
        return settings.display_number_base + self.index

    @property
    def pulse_sink(self) -> str:
        return f"omnibus_sink_{self.index}"

    @property
    def vnc_port(self) -> int:
        return settings.vnc_port_base + self.index

    @property
    def novnc_port(self) -> int:
        return settings.novnc_port_base + self.index


class SlotPool:
    """Thread-safe fixed pool of resource slots."""

    def __init__(self, size: int) -> None:
        self._lock = threading.Lock()
        self._free = list(range(size))
        self._size = size

    def acquire(self) -> ResourceSlot:
        with self._lock:
            if not self._free:
                raise RuntimeError(
                    f"All {self._size} recording slots are busy — "
                    "wait for a session to finish or stop one."
                )
            return ResourceSlot(self._free.pop(0))

    def release(self, slot: ResourceSlot) -> None:
        with self._lock:
            if slot.index not in self._free:
                self._free.append(slot.index)
                self._free.sort()

    @property
    def free_count(self) -> int:
        with self._lock:
            return len(self._free)


pool = SlotPool(settings.max_concurrent_sessions)
