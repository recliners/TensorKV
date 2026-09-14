"""Per-block scoreboard that enforces monotonic reads.

Before recycling a page, the slow path sets a hazard bit. An observing GET
is recirculated until the mapping is invalidated, then returns a clean miss.
The GPU never reads a reallocated page (paper § Consistency Model).
"""

from __future__ import annotations


class Scoreboard:
    def __init__(self) -> None:
        self._hazards: set[int] = set()
        self.set_count = 0
        self.clear_count = 0
        self.recirculations = 0
        self.checks = 0
        self.hazard_hits = 0

    def set_hazard(self, key: int) -> None:
        self._hazards.add(key)
        self.set_count += 1

    def clear_hazard(self, key: int) -> None:
        self._hazards.discard(key)
        self.clear_count += 1

    def is_hazard(self, key: int) -> bool:
        self.checks += 1
        if key in self._hazards:
            self.hazard_hits += 1
            self.recirculations += 1
            return True
        return False

    @property
    def hazard_rate(self) -> float:
        return self.hazard_hits / self.checks if self.checks else 0.0

    def active(self) -> int:
        return len(self._hazards)
