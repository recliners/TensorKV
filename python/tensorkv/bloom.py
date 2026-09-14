"""Bloom filter used by TKV_PROBE before the prefix hash-table lookup.

False positives are allowed (fall through to the table). False negatives are
not: every inserted prefix hash must probe as maybe-present.
"""

from __future__ import annotations

from .constants import BLOOM_BITS, BLOOM_HASHES
from .hashutil import mix64


class BloomFilter:
    def __init__(self, n_bits: int = BLOOM_BITS, n_hashes: int = BLOOM_HASHES) -> None:
        if n_bits <= 0 or n_hashes <= 0:
            raise ValueError("bloom parameters must be positive")
        self.n_bits = n_bits
        self.n_hashes = n_hashes
        self._bits = bytearray((n_bits + 7) // 8)
        self.inserts = 0

    def _positions(self, key: int) -> list[int]:
        out = []
        for i in range(self.n_hashes):
            h = mix64(key + (i + 1) * 0xD1B54A32D192ED03)
            out.append(h % self.n_bits)
        return out

    def add(self, key: int) -> None:
        for pos in self._positions(key):
            self._bits[pos >> 3] |= 1 << (pos & 7)
        self.inserts += 1

    def maybe_contains(self, key: int) -> bool:
        for pos in self._positions(key):
            if (self._bits[pos >> 3] & (1 << (pos & 7))) == 0:
                return False
        return True
