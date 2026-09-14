"""Hierarchical HBM page allocator.

Slow path (control core) maintains the free list and pushes batches of 64
addresses into a hardware prefetch FIFO. Fast-path PUT pops one address in a
single pipeline cycle (paper § Slow Path, Figure: Hierarchical Allocation).
"""

from __future__ import annotations

from collections import deque

from .constants import ALLOC_FIFO_BATCH, ALLOC_FIFO_WATERMARK


class HierarchicalAllocator:
    def __init__(
        self,
        n_pages: int,
        batch: int = ALLOC_FIFO_BATCH,
        watermark: int = ALLOC_FIFO_WATERMARK,
    ) -> None:
        if n_pages <= 0:
            raise ValueError("n_pages must be positive")
        self.n_pages = n_pages
        self.batch = batch
        self.watermark = watermark
        self.free_list: deque[int] = deque(range(n_pages))
        self.fifo: deque[int] = deque()
        self.slow_refills = 0
        self.fast_pops = 0
        self.failed_allocs = 0
        self._refill()

    def _refill(self) -> None:
        """Control-core refill: push up to `batch` free HBM addresses."""
        pushed = 0
        while self.free_list and pushed < self.batch:
            self.fifo.append(self.free_list.popleft())
            pushed += 1
        if pushed:
            self.slow_refills += 1

    def maybe_refill(self) -> None:
        if len(self.fifo) < self.watermark:
            self._refill()

    def alloc(self) -> int | None:
        """Fast-path pop. Returns None if the pool is exhausted."""
        if not self.fifo:
            self._refill()
        if not self.fifo:
            self.failed_allocs += 1
            return None
        page = self.fifo.popleft()
        self.fast_pops += 1
        self.maybe_refill()
        return page

    def free(self, page: int) -> None:
        if page < 0 or page >= self.n_pages:
            raise ValueError(f"page {page} out of range")
        self.free_list.append(page)
        self.maybe_refill()

    @property
    def free_pages(self) -> int:
        return len(self.free_list) + len(self.fifo)

    @property
    def used_pages(self) -> int:
        return self.n_pages - self.free_pages
