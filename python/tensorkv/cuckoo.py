"""Bucketized cuckoo hash table (fast-path SRAM metadata).

Each bucket holds four 64-bit slots: {32-bit fingerprint, 32-bit phys ptr}.
Common-case insert uses a free slot in either hash bucket (RMT pipeline).
When both buckets are full, the slow path performs Cuckoo displacement from
a victim buffer (paper § Slow Path / Appendix microarchitecture).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import CUCKOO_MAX_KICKS, SLOTS_PER_BUCKET
from .hashutil import SplitMix64, bucket_pair, fingerprint


EMPTY_FP = 0


@dataclass
class Slot:
    fingerprint: int = EMPTY_FP
    phys: int = 0
    full_key: int = 0  # HBM-resident full key; consulted on tag match


@dataclass
class CuckooTable:
    n_buckets: int
    slots_per_bucket: int = SLOTS_PER_BUCKET
    max_kicks: int = CUCKOO_MAX_KICKS
    rng: SplitMix64 = field(default_factory=lambda: SplitMix64(0xA5A5))

    def __post_init__(self) -> None:
        self.buckets: list[list[Slot]] = [
            [Slot() for _ in range(self.slots_per_bucket)] for _ in range(self.n_buckets)
        ]
        # Slow-path victim buffer: entries that exceeded the kick budget.
        self.victim_buffer: dict[int, Slot] = {}
        self.size = 0
        self.fast_inserts = 0
        self.slow_inserts = 0
        self.failed_inserts = 0
        self.tag_collisions = 0  # fingerprint match, full-key mismatch
        self.lookups = 0
        self.hits = 0

    @property
    def capacity(self) -> int:
        return self.n_buckets * self.slots_per_bucket

    @property
    def load_factor(self) -> float:
        return self.size / self.capacity if self.capacity else 0.0

    def _find_empty(self, bucket: int) -> int | None:
        for i, slot in enumerate(self.buckets[bucket]):
            if slot.fingerprint == EMPTY_FP:
                return i
        return None

    def find(self, key: int) -> int | None:
        """Silent lookup (no counters). Checks SRAM then victim buffer."""
        tag = fingerprint(key)
        for b in bucket_pair(key, self.n_buckets):
            for slot in self.buckets[b]:
                if slot.fingerprint != tag:
                    continue
                if slot.full_key != key:
                    continue
                return slot.phys
        vic = self.victim_buffer.get(key)
        return vic.phys if vic is not None else None

    def lookup(self, key: int) -> int | None:
        """Return physical page index, or None. Fast-path match-action."""
        self.lookups += 1
        tag = fingerprint(key)
        for b in bucket_pair(key, self.n_buckets):
            for slot in self.buckets[b]:
                if slot.fingerprint != tag:
                    continue
                if slot.full_key != key:
                    self.tag_collisions += 1
                    continue
                self.hits += 1
                return slot.phys
        vic = self.victim_buffer.get(key)
        if vic is not None:
            self.hits += 1
            return vic.phys
        return None

    def insert(self, key: int, phys: int) -> str:
        """Insert mapping. Returns 'fast', 'slow', or 'fail'."""
        if self.find(key) is not None:
            self._update(key, phys)
            return "fast"

        tag = fingerprint(key)
        h1, h2 = bucket_pair(key, self.n_buckets)
        for b in (h1, h2):
            empty = self._find_empty(b)
            if empty is not None:
                self.buckets[b][empty] = Slot(tag, phys, key)
                self.size += 1
                self.fast_inserts += 1
                return "fast"

        # Slow path: Cuckoo displacement.
        cur_key, cur_phys, cur_tag = key, phys, tag
        cur_bucket = h1
        for _ in range(self.max_kicks):
            slot_i = self.rng.randint(0, self.slots_per_bucket - 1)
            victim = self.buckets[cur_bucket][slot_i]
            self.buckets[cur_bucket][slot_i] = Slot(cur_tag, cur_phys, cur_key)
            cur_key, cur_phys, cur_tag = victim.full_key, victim.phys, victim.fingerprint
            h1v, h2v = bucket_pair(cur_key, self.n_buckets)
            cur_bucket = h2v if cur_bucket == h1v else h1v
            empty = self._find_empty(cur_bucket)
            if empty is not None:
                self.buckets[cur_bucket][empty] = Slot(cur_tag, cur_phys, cur_key)
                self.size += 1
                self.slow_inserts += 1
                return "slow"

        # Kick budget exhausted: park the last displaced entry in the victim buffer.
        self.victim_buffer[cur_key] = Slot(cur_tag, cur_phys, cur_key)
        self.size += 1
        self.slow_inserts += 1
        return "slow"

    def _update(self, key: int, phys: int) -> None:
        tag = fingerprint(key)
        for b in bucket_pair(key, self.n_buckets):
            for slot in self.buckets[b]:
                if slot.fingerprint == tag and slot.full_key == key:
                    slot.phys = phys
                    return
        if key in self.victim_buffer:
            self.victim_buffer[key].phys = phys

    def delete(self, key: int) -> int | None:
        """Invalidate mapping; return the freed physical pointer if present."""
        tag = fingerprint(key)
        for b in bucket_pair(key, self.n_buckets):
            for slot in self.buckets[b]:
                if slot.fingerprint == tag and slot.full_key == key:
                    phys = slot.phys
                    slot.fingerprint = EMPTY_FP
                    slot.phys = 0
                    slot.full_key = 0
                    self.size -= 1
                    return phys
        vic = self.victim_buffer.pop(key, None)
        if vic is not None:
            self.size -= 1
            return vic.phys
        return None

    def occupancy_histogram(self) -> list[int]:
        hist = [0] * (self.slots_per_bucket + 1)
        for bucket in self.buckets:
            used = sum(1 for s in bucket if s.fingerprint != EMPTY_FP)
            hist[used] += 1
        return hist
