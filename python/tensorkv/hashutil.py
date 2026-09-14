"""Fingerprint and bucket hashes used by the RMT match-action stages.

SRAM stores a 32-bit fingerprint plus a 32-bit physical pointer per slot
and verifies the full key in HBM on every fingerprint match.
"""

from __future__ import annotations

MASK64 = (1 << 64) - 1
MASK32 = (1 << 32) - 1


def mix64(x: int) -> int:
    """SplitMix64 finalizer (deterministic, portable)."""
    x &= MASK64
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9 & MASK64
    x = (x ^ (x >> 27)) * 0x94D049BB133111EB & MASK64
    return (x ^ (x >> 31)) & MASK64


def pack_key(context_id: int, block_id: int) -> int:
    return ((int(context_id) & MASK32) << 32) | (int(block_id) & MASK32)


def unpack_key(key: int) -> tuple[int, int]:
    return (key >> 32) & MASK32, key & MASK32


def fingerprint(key: int) -> int:
    """32-bit tag. Zero is reserved for an empty SRAM slot."""
    tag = (mix64(key) >> 32) & MASK32
    return tag if tag != 0 else 1


def bucket_pair(key: int, n_buckets: int) -> tuple[int, int]:
    """Two independent bucket indices for bucketized cuckoo hashing."""
    if n_buckets <= 0:
        raise ValueError("n_buckets must be positive")
    h1 = mix64(key) % n_buckets
    h2 = mix64(key ^ 0x9E3779B97F4A7C15) % n_buckets
    if h2 == h1:
        h2 = (h1 + 1) % n_buckets
    return int(h1), int(h2)


class SplitMix64:
    """Seeded generator for Zipfian workloads and cuckoo kick selection."""

    def __init__(self, seed: int = 0xC0FFEE) -> None:
        self.state = seed & MASK64

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        return mix64(self.state)

    def next_float(self) -> float:
        return (self.next_u64() >> 11) / float(1 << 53)

    def randint(self, lo: int, hi: int) -> int:
        if hi < lo:
            raise ValueError("hi < lo")
        span = hi - lo + 1
        return lo + (self.next_u64() % span)
