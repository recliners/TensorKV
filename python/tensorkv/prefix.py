"""Prefix index backing TKV_PROBE.

PROBE is metadata-only: Bloom filter + hash table, increment reference count,
return logical block handles. No HBM payload access on a hit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bloom import BloomFilter


@dataclass
class PrefixRecord:
    prompt_hash: int
    context_id: int
    block_ids: list[int]
    refcount: int = 1
    bytes_per_block: int = 4096


@dataclass
class PrefixIndex:
    bloom: BloomFilter = field(default_factory=BloomFilter)
    table: dict[int, PrefixRecord] = field(default_factory=dict)
    probes: int = 0
    hits: int = 0
    misses: int = 0
    bloom_negatives: int = 0
    bloom_false_positives: int = 0

    def register(self, prompt_hash: int, context_id: int, block_ids: list[int], bytes_per_block: int = 4096) -> PrefixRecord:
        rec = PrefixRecord(
            prompt_hash=prompt_hash,
            context_id=context_id,
            block_ids=list(block_ids),
            refcount=1,
            bytes_per_block=bytes_per_block,
        )
        self.table[prompt_hash] = rec
        self.bloom.add(prompt_hash)
        return rec

    def probe(self, prompt_hash: int) -> PrefixRecord | None:
        """Hardware-accelerated prefix matching. Hit => ref++ and handles."""
        self.probes += 1
        if not self.bloom.maybe_contains(prompt_hash):
            self.bloom_negatives += 1
            self.misses += 1
            return None
        rec = self.table.get(prompt_hash)
        if rec is None:
            self.bloom_false_positives += 1
            self.misses += 1
            return None
        rec.refcount += 1
        self.hits += 1
        return rec

    def release(self, prompt_hash: int) -> int:
        rec = self.table.get(prompt_hash)
        if rec is None:
            return 0
        rec.refcount = max(0, rec.refcount - 1)
        return rec.refcount

    def drop(self, prompt_hash: int) -> None:
        self.table.pop(prompt_hash, None)

    def forget_block(self, prompt_hash: int, block_id: int) -> None:
        """Drop one cached prefix block; keep the record until the span is empty."""
        rec = self.table.get(prompt_hash)
        if rec is None:
            return
        rec.block_ids = [b for b in rec.block_ids if b != block_id]
        if not rec.block_ids:
            self.drop(prompt_hash)
