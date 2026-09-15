"""Multi-tenant serving scheduler on top of PagedEngine.

GET/PROBE stay high priority through the appliance VOQ. The scheduler only
decides *which* request to prefill or decode next, then issues vectorized
TKV_GET for the whole KV span. New arrivals PROBE then prefill; the live
set is decoded round-robin (continuous batching).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .engine import PagedEngine, Request


@dataclass
class Arrival:
    req_id: int
    tokens: list[int]
    prefix_tokens: list[int] | None = None
    max_new: int = 4
    arrive_tick: int = 0


@dataclass
class BatchStats:
    submitted: int = 0
    prefills: int = 0
    prefix_hits: int = 0
    decode_steps: int = 0
    finished: int = 0
    reclaims: int = 0
    ticks: int = 0
    peak_batch: int = 0
    vector_overflow: int = 0
    ttft_ms: list[float] = field(default_factory=list)
    tbt_ms: list[float] = field(default_factory=list)


class ServingScheduler:
    """Prefill-first continuous batching with round-robin decode."""

    def __init__(self, engine: PagedEngine | None = None, max_batch: int = 8) -> None:
        self.engine = engine or PagedEngine()
        self.max_batch = max(1, int(max_batch))
        self.live: list[int] = []
        self.cursor = 0
        self.waiting: deque[Arrival] = deque()
        self.remaining: dict[int, int] = {}
        self.stats = BatchStats()

    def enqueue(self, arrival: Arrival) -> None:
        self.waiting.append(arrival)

    def _ensure_pages(self, need: int) -> int:
        device = self.engine.tkv.device
        reclaimed = 0
        while device.allocator.free_pages < need:
            got = device.reclaim(max(4, need - device.allocator.free_pages), "lfru")
            if not got:
                break
            reclaimed += len(got)
            self.stats.reclaims += 1
        return reclaimed

    def admit(self, req_id: int, tokens: list[int], prefix_tokens: list[int] | None = None) -> Request:
        need = max(1, self.engine._n_blocks(len(tokens)))
        self._ensure_pages(need)
        if need > 8:
            self.stats.vector_overflow += 1
        req = self.engine.submit(req_id, tokens, prefix_tokens=prefix_tokens)
        self.live.append(req_id)
        self.stats.submitted += 1
        self.stats.prefills += 1
        if req.prefix_hit:
            self.stats.prefix_hits += 1
        self.stats.ttft_ms.append(req.ttft_ms)
        self.stats.peak_batch = max(self.stats.peak_batch, len(self.live))
        return req

    def decode_one(self, token: int = 1) -> float | None:
        if not self.live:
            return None
        req_id = self.live[self.cursor % len(self.live)]
        self.cursor += 1
        tbt = self.engine.decode(req_id, token)
        self.stats.decode_steps += 1
        self.stats.tbt_ms.append(tbt)
        return tbt

    def decode_all(self, steps: int, token: int = 1) -> list[float]:
        out: list[float] = []
        for i in range(steps):
            t = self.decode_one(token + i)
            if t is None:
                break
            out.append(t)
        return out

    def finish(self, req_id: int, keep_prefix: bool = True) -> None:
        if req_id in self.live:
            self.live.remove(req_id)
        self.remaining.pop(req_id, None)
        self.engine.finish(req_id, keep_prefix=keep_prefix)
        self.stats.finished += 1
        if self.live:
            self.cursor %= len(self.live)
        else:
            self.cursor = 0

    def admit_next(self) -> Request | None:
        if not self.waiting or len(self.live) >= self.max_batch:
            return None
        a = self.waiting.popleft()
        req = self.admit(a.req_id, a.tokens, a.prefix_tokens)
        self.remaining[a.req_id] = a.max_new
        return req

    def tick(self) -> str:
        """One scheduler quantum: prefill if the batch has a hole, else decode."""
        self.stats.ticks += 1
        if self.waiting and len(self.live) < self.max_batch:
            self.admit_next()
            return "prefill"
        if self.live:
            req_id = self.live[self.cursor % len(self.live)]
            self.decode_one()
            left = self.remaining.get(req_id)
            if left is None:
                return "decode"
            left -= 1
            if left <= 0:
                self.finish(req_id, keep_prefix=True)
            else:
                self.remaining[req_id] = left
            return "decode"
        return "idle"

    def run_arrivals(self, arrivals: list[Arrival], max_ticks: int = 80_000) -> BatchStats:
        pending = sorted(arrivals, key=lambda a: (a.arrive_tick, a.req_id))
        i = 0
        tick = 0
        while tick < max_ticks:
            while i < len(pending) and pending[i].arrive_tick <= tick:
                self.enqueue(pending[i])
                i += 1
            if not self.waiting and not self.live and i >= len(pending):
                break
            if self.tick() == "idle":
                tick += 1
                continue
            tick += 1
        return self.stats

    def run_batch(
        self,
        prompts: list[tuple[int, list[int], list[int] | None]],
        decode_steps: int = 4,
    ) -> BatchStats:
        for req_id, tokens, prefix in prompts:
            self.admit(req_id, tokens, prefix)
        self.decode_all(decode_steps * max(1, len(prompts)))
        for req_id, _, _ in prompts:
            self.finish(req_id, keep_prefix=True)
        return self.stats
