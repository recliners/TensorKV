"""Minimal discrete-event clock used by libtkv and overlapping GET/EVICT."""

from __future__ import annotations

from dataclasses import dataclass, field
from heapq import heappop, heappush
from typing import Callable


@dataclass(order=True)
class Event:
    at_ns: int
    seq: int = field(compare=True)
    fn: Callable[[], None] = field(compare=False, repr=False)


class World:
    def __init__(self) -> None:
        self.now_ns = 0
        self._seq = 0
        self._heap: list[Event] = []

    def advance(self, ns: int) -> None:
        self.now_ns += max(0, int(ns))

    def after(self, delay_ns: int, fn: Callable[[], None]) -> None:
        self._seq += 1
        heappush(self._heap, Event(self.now_ns + max(0, int(delay_ns)), self._seq, fn))

    def run_until(self, t_ns: int) -> None:
        while self._heap and self._heap[0].at_ns <= t_ns:
            ev = heappop(self._heap)
            self.now_ns = max(self.now_ns, ev.at_ns)
            ev.fn()
        if t_ns > self.now_ns:
            self.now_ns = t_ns

    def drain(self) -> None:
        while self._heap:
            ev = heappop(self._heap)
            self.now_ns = max(self.now_ns, ev.at_ns)
            ev.fn()
