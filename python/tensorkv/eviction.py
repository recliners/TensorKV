"""Cache replacement: standard LRU vs prefix-aware LFRU.

Prefix-aware LFRU uses TKV_PROBE reference counts: blocks with refcount > 1
(shared system prompts) are preserved. Remaining victims are chosen by
least-frequency then least-recency (paper § Eviction Policy Sensitivity).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BlockMeta:
    key: int
    context_id: int
    block_id: int
    phys: int
    last_access: int = 0
    frequency: int = 0
    refcount: int = 1
    is_prefix: bool = False
    prefix_hash: int | None = None


@dataclass
class EvictionTracker:
    clock: int = 0
    blocks: dict[int, BlockMeta] = field(default_factory=dict)

    def touch(self, key: int, *, prefix: bool = False, prefix_hash: int | None = None) -> None:
        self.clock += 1
        meta = self.blocks.get(key)
        if meta is None:
            return
        meta.last_access = self.clock
        meta.frequency += 1
        if prefix:
            meta.is_prefix = True
            meta.prefix_hash = prefix_hash

    def add(self, meta: BlockMeta) -> None:
        self.clock += 1
        meta.last_access = self.clock
        meta.frequency = max(1, meta.frequency)
        self.blocks[meta.key] = meta

    def remove(self, key: int) -> BlockMeta | None:
        return self.blocks.pop(key, None)

    def set_refcount(self, key: int, refcount: int) -> None:
        if key in self.blocks:
            self.blocks[key].refcount = refcount

    def bump_refcount(self, keys: list[int], delta: int = 1) -> None:
        for key in keys:
            if key in self.blocks:
                self.blocks[key].refcount += delta

    def select_victims(self, n: int, policy: str) -> list[BlockMeta]:
        if n <= 0:
            return []
        items = list(self.blocks.values())
        if not items:
            return []
        if policy == "lru":
            items.sort(key=lambda m: m.last_access)
        elif policy in ("lfru", "prefix-lfru"):
            # Protect shared prefixes (refcount > 1); LFRU among the rest.
            unprotected = [m for m in items if m.refcount <= 1]
            pool = unprotected if unprotected else items
            pool.sort(key=lambda m: (m.frequency, m.last_access))
            items = pool
        else:
            raise ValueError(f"unknown eviction policy {policy}")
        return items[: min(n, len(items))]
