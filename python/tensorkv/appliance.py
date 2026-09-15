"""TensorKV memory appliance: dual-path semantic KV store.

Implements the four primitives from the paper:

  TKV_PUT(CtxID, SeqID, TensorData)   offloaded allocation + DMA write
  TKV_GET(CtxID, [BlockID...])        vectorized hash lookup + scatter-gather
  TKV_PROBE(PromptHash)               Bloom + prefix table, ref++, handles only
  TKV_EVICT(CtxID, Policy)            scoreboard-protected range reclaim

Fast path: parser, match-action cuckoo lookup, DMA gather, credit shaper.
Slow path: free-list refill, cuckoo re-insertion, eviction + atomic commit.
"""

from __future__ import annotations

from dataclasses import dataclass

from .allocator import HierarchicalAllocator
from .constants import (
    BLOCK_SIZE_BYTES,
    FAST_PATH_HBM_HIT_NS,
    FAST_PATH_SRAM_HIT_NS,
    HIGH_PRIORITY_OPCODES,
    SLOW_PATH_CUCKOO_NS,
)
from .crossbar import AtomicCrossbar
from .cuckoo import CuckooTable
from .eviction import BlockMeta, EvictionTracker
from .hashutil import SplitMix64, pack_key, unpack_key
from .hbm import BankedHBM
from .pipeline import dma_descriptor_ns, recirc_ns, rmt_lookup_ns
from .prefix import PrefixIndex, PrefixRecord
from .scoreboard import Scoreboard
from .transport import CreditShaper, Packet, VirtualOutputQueues
from .world import World


@dataclass
class TraceEvent:
    op: str
    path: str  # fast | slow
    stage: str
    detail: str
    latency_ns: int = 0


@dataclass
class PutResult:
    ok: bool
    context_id: int
    block_id: int
    phys: int | None
    path: str
    events: list[TraceEvent]
    latency_ns: int
    error: str | None = None


@dataclass
class GetResult:
    ok: bool
    payload: bytes
    hits: list[int]
    misses: list[int]
    events: list[TraceEvent]
    latency_ns: int
    recirculations: int
    gathered_bytes: int


@dataclass
class ProbeResult:
    hit: bool
    handles: list[int]
    context_id: int | None
    refcount: int
    events: list[TraceEvent]
    latency_ns: int
    hbm_accessed: bool = False


@dataclass
class EvictResult:
    evicted: list[int]
    events: list[TraceEvent]
    latency_ns: int


@dataclass
class ApplianceConfig:
    n_buckets: int = 1024
    n_pages: int = 4096
    block_size: int = BLOCK_SIZE_BYTES
    store_payloads: bool = True
    seed: int = 0xA5A5
    fast_slow_split: bool = True
    zero_copy_dma: bool = True


class TensorKVAppliance:
    def __init__(self, cfg: ApplianceConfig | None = None) -> None:
        self.cfg = cfg or ApplianceConfig()
        self.table = CuckooTable(n_buckets=self.cfg.n_buckets, rng=SplitMix64(self.cfg.seed))
        self.allocator = HierarchicalAllocator(self.cfg.n_pages)
        self.scoreboard = Scoreboard()
        self.prefix = PrefixIndex()
        self.eviction = EvictionTracker()
        self.shaper = CreditShaper()
        self.voq = VirtualOutputQueues()
        self.crossbar = AtomicCrossbar()
        self.world = World()
        self.store = BankedHBM(self.cfg.n_pages)
        self.contexts: dict[int, list[int]] = {}
        self.shadow_commits = 0
        self.bank_locks = 0
        self.put_ops = 0
        self.get_ops = 0
        self.probe_ops = 0
        self.evict_ops = 0
        self.gathered_bytes = 0
        self.inflight_evict: set[int] = set()
        self.credit_gbps = 40.0
        self.get_latencies: list[int] = []

    def _payload_bytes(self, context_id: int, seq_id: int, data: bytes | None) -> bytes:
        if not self.cfg.store_payloads:
            return pack_key(context_id, seq_id).to_bytes(8, "little")
        payload = data if data is not None else bytes([seq_id & 0xFF]) * min(16, self.cfg.block_size)
        if len(payload) > self.cfg.block_size:
            payload = payload[: self.cfg.block_size]
        elif len(payload) < self.cfg.block_size:
            payload = payload + bytes(self.cfg.block_size - len(payload))
        return payload

    # ------------------------------------------------------------------ PUT
    def put(
        self,
        context_id: int,
        seq_id: int,
        data: bytes | None = None,
        prefix_hash: int | None = None,
    ) -> PutResult:
        events: list[TraceEvent] = []
        self.put_ops += 1
        key = pack_key(context_id, seq_id)
        events.append(TraceEvent("PUT", "fast", "parser", f"PUT({context_id},{seq_id})"))

        existing = self.table.find(key)
        if existing is not None:
            payload = self._payload_bytes(context_id, seq_id, data)
            self.store.write(existing, key, payload, self.crossbar.now_ns)
            self.eviction.touch(key, prefix=prefix_hash is not None, prefix_hash=prefix_hash)
            events.append(TraceEvent("PUT", "fast", "overwrite", f"HBM[{existing}] in-place", 80))
            return PutResult(True, context_id, seq_id, existing, "fast", events, FAST_PATH_SRAM_HIT_NS)

        page = self.allocator.alloc()
        if page is None:
            events.append(TraceEvent("PUT", "slow", "allocator", "free list empty", 0))
            return PutResult(False, context_id, seq_id, None, "slow", events, 0, "remote allocation failed")

        events.append(TraceEvent("PUT", "fast", "fifo_pop", f"page={page} fifo={len(self.allocator.fifo)}", 4))

        payload = self._payload_bytes(context_id, seq_id, data)
        hbm_ns = self.store.write(page, key, payload, self.crossbar.now_ns)
        events.append(TraceEvent("PUT", "fast", "dma_write", f"HBM[{page}] {self.cfg.block_size}B", hbm_ns))

        stall = self.crossbar.lookup_gate()
        path = self.table.insert(key, page)
        if not self.cfg.fast_slow_split:
            path = "slow"
        latency = FAST_PATH_SRAM_HIT_NS if path == "fast" else SLOW_PATH_CUCKOO_NS
        events.append(
            TraceEvent(
                "PUT",
                path,
                "hash_insert",
                f"cuckoo={path} load={self.table.load_factor:.3f}",
                latency + stall,
            )
        )
        if path == "slow":
            commit_ns = self.crossbar.commit()
            self.crossbar.release()
            self.shadow_commits += 1
            self.bank_locks += 1
            events.append(TraceEvent("PUT", "slow", "crossbar", "atomic shadow-row commit", commit_ns))

        self.contexts.setdefault(context_id, []).append(seq_id)
        self.eviction.add(
            BlockMeta(
                key=key,
                context_id=context_id,
                block_id=seq_id,
                phys=page,
                refcount=1,
                is_prefix=prefix_hash is not None,
                prefix_hash=prefix_hash,
            )
        )
        self._schedule("PUT", context_id, self.cfg.block_size)
        total = sum(e.latency_ns for e in events)
        return PutResult(True, context_id, seq_id, page, path, events, total)

    def reclaim(self, n: int, policy: str = "lfru") -> list[int]:
        """Host-triggered capacity reclaim using LRU or prefix-aware LFRU."""
        metas = self.eviction.select_victims(n, "lfru" if policy == "lfru" else "lru")
        evicted = []
        for meta in metas:
            self._evict_one(meta.key, [])
            evicted.append(meta.block_id)
        return evicted

    # ------------------------------------------------------------------ GET
    def get(
        self,
        context_id: int,
        block_ids: list[int],
        credit_gbps: float | None = None,
    ) -> GetResult:
        events: list[TraceEvent] = []
        self.get_ops += 1
        if credit_gbps is not None:
            self.shaper.set_credit(credit_gbps)
            self.credit_gbps = credit_gbps
            events.append(TraceEvent("GET", "fast", "credit", f"shaper={credit_gbps} Gbps"))

        events.append(
            TraceEvent("GET", "fast", "parser", f"GET({context_id}, {block_ids[:8]}{'...' if len(block_ids)>8 else ''})")
        )
        chunks: list[bytes] = []
        hits: list[int] = []
        misses: list[int] = []
        recirc = 0
        latency = rmt_lookup_ns(max(1, len(block_ids)))

        for bid in block_ids:
            key = pack_key(context_id, bid)
            stall = self.crossbar.lookup_gate()
            latency += stall
            if self.scoreboard.is_hazard(key) or key in self.inflight_evict:
                recirc += 1
                events.append(
                    TraceEvent("GET", "fast", "scoreboard", f"hazard block {bid} recirculate", recirc_ns())
                )
                latency += recirc_ns()
                misses.append(bid)
                events.append(TraceEvent("GET", "fast", "lookup", f"MISS block {bid} (hazard, no HBM)"))
                continue
            phys = self.table.lookup(key)
            if phys is None:
                misses.append(bid)
                events.append(TraceEvent("GET", "fast", "lookup", f"MISS block {bid}"))
                continue
            payload, hbm_ns = self.store.read_payload(phys, self.crossbar.now_ns)
            latency += hbm_ns
            if payload is None:
                misses.append(bid)
                continue
            chunks.append(payload)
            hits.append(bid)
            self.eviction.touch(key)

        dma_ns = dma_descriptor_ns(len(hits))
        if not self.cfg.zero_copy_dma:
            dma_ns += 6_400
            events.append(TraceEvent("GET", "slow", "host_copy", "fallback copy into registered buffer", 6_400))
        events.append(
            TraceEvent("GET", "fast", "dma_gather", f"scatter-gather {len(hits)} blocks -> contiguous stream", dma_ns)
        )
        latency += dma_ns
        if not self.cfg.fast_slow_split:
            extra = SLOW_PATH_CUCKOO_NS - FAST_PATH_SRAM_HIT_NS
            latency += extra
            events.append(TraceEvent("GET", "slow", "control_core", "no RMT split; every GET on control core", extra))
        shape_ns = self._schedule("GET", context_id, max(1, len(hits)) * self.cfg.block_size)
        latency += shape_ns

        payload = b"".join(chunks)
        self.gathered_bytes += len(payload)
        latency += FAST_PATH_HBM_HIT_NS if hits else FAST_PATH_SRAM_HIT_NS
        events.append(TraceEvent("GET", "fast", "egress", f"hits={len(hits)} misses={len(misses)} recirc={recirc}", latency))
        self.get_latencies.append(latency)
        if len(self.get_latencies) > 4096:
            del self.get_latencies[:2048]
        return GetResult(
            ok=len(misses) == 0 and len(hits) > 0,
            payload=payload,
            hits=hits,
            misses=misses,
            events=events,
            latency_ns=latency,
            recirculations=recirc,
            gathered_bytes=len(payload),
        )

    def finish_get(
        self,
        context_id: int,
        block_ids: list[int],
        credit_gbps: float | None = None,
        max_recirc: int = 64,
    ) -> GetResult:
        """Retry GET after scoreboard recirculation until the map is stable."""
        total_recirc = 0
        last: GetResult | None = None
        for _ in range(max_recirc):
            last = self.get(context_id, block_ids, credit_gbps)
            total_recirc += last.recirculations
            if last.recirculations == 0:
                last.recirculations = total_recirc
                return last
            self.world.run_until(self.world.now_ns + recirc_ns())
            self.crossbar.advance(recirc_ns())
        assert last is not None
        last.recirculations = total_recirc
        return last

    # ------------------------------------------------------------------ PROBE
    def probe(self, prompt_hash: int) -> ProbeResult:
        events: list[TraceEvent] = []
        self.probe_ops += 1
        events.append(TraceEvent("PROBE", "fast", "parser", f"PROBE({prompt_hash & 0xFFFFFFFF})"))
        events.append(TraceEvent("PROBE", "fast", "bloom", "check prefix bloom filter", 20))
        stall = self.crossbar.lookup_gate()
        rec = self.prefix.probe(prompt_hash)
        if rec is None:
            events.append(TraceEvent("PROBE", "fast", "index", "MISS (no HBM access)", FAST_PATH_SRAM_HIT_NS + stall))
            self._schedule("PROBE", 0, 64)
            return ProbeResult(False, [], None, 0, events, FAST_PATH_SRAM_HIT_NS + stall, False)

        for bid in rec.block_ids:
            key = pack_key(rec.context_id, bid)
            self.eviction.set_refcount(key, rec.refcount)
            self.eviction.touch(key, prefix=True, prefix_hash=prompt_hash)
        events.append(
            TraceEvent(
                "PROBE",
                "fast",
                "index",
                f"HIT handles={len(rec.block_ids)} ref={rec.refcount} (no HBM)",
                FAST_PATH_SRAM_HIT_NS,
            )
        )
        self._schedule("PROBE", rec.context_id, 64)
        return ProbeResult(True, list(rec.block_ids), rec.context_id, rec.refcount, events, FAST_PATH_SRAM_HIT_NS + stall, False)

    def publish_prefix(self, prompt_hash: int, context_id: int, block_ids: list[int]) -> PrefixRecord:
        rec = self.prefix.register(prompt_hash, context_id, block_ids, self.cfg.block_size)
        for bid in block_ids:
            key = pack_key(context_id, bid)
            self.eviction.set_refcount(key, rec.refcount)
            self.eviction.touch(key, prefix=True, prefix_hash=prompt_hash)
        return rec

    # ------------------------------------------------------------------ EVICT
    def evict(self, context_id: int, policy: str = "lru", k: int | None = None) -> EvictResult:
        """Safe range eviction. policy: lru | lfru | all | oldest."""
        events: list[TraceEvent] = []
        self.evict_ops += 1
        events.append(TraceEvent("EVICT", "slow", "control_core", f"EVICT({context_id},{policy},{k})"))

        targets: list[int]
        if policy == "all":
            targets = list(self.contexts.get(context_id, []))
        elif policy == "oldest":
            blocks = list(self.contexts.get(context_id, []))
            n = k if k is not None else max(1, len(blocks) // 4)
            targets = blocks[:n]
        else:
            n = k if k is not None else 1
            metas = self.eviction.select_victims(n, "lfru" if policy == "lfru" else "lru")
            if context_id >= 0:
                metas = [m for m in metas if m.context_id == context_id] or metas
            targets = [m.block_id for m in metas[:n]]
            # If context-scoped LRU of this context's own chain:
            if policy in ("lru", "lfru") and context_id in self.contexts and k:
                chain = self.contexts[context_id]
                # Prefer tracker order intersected with this context.
                ctx_targets = [b for b in targets if b in chain]
                if len(ctx_targets) < n:
                    ctx_targets = chain[:n]
                targets = ctx_targets

        evicted: list[int] = []
        for bid in list(targets):
            key = pack_key(context_id, bid) if context_id >= 0 else None
            if key is None:
                continue
            # If lru/lfru selected a foreign context, use that key's ctx.
            meta = self.eviction.blocks.get(key)
            if meta is None and policy in ("lru", "lfru"):
                # search by block id in tracker
                for m in self.eviction.blocks.values():
                    if m.block_id == bid:
                        key = m.key
                        context_id = m.context_id
                        meta = m
                        break
            if key is None:
                continue
            self._evict_one(key, events)
            evicted.append(unpack_key(key)[1])

        total = 400 * max(1, len(evicted))
        events.append(TraceEvent("EVICT", "slow", "done", f"reclaimed {len(evicted)} blocks", total))
        return EvictResult(evicted, events, total)

    def evict_key(self, context_id: int, block_id: int) -> None:
        self._evict_one(pack_key(context_id, block_id), [])

    def begin_evict_key(self, context_id: int, block_id: int) -> None:
        """Split eviction so tests can observe the GET/EVICT race."""
        key = pack_key(context_id, block_id)
        self.scoreboard.set_hazard(key)
        self.inflight_evict.add(key)
        self.crossbar.commit()

    def complete_evict_key(self, context_id: int, block_id: int) -> None:
        key = pack_key(context_id, block_id)
        self._evict_one(key, [], hazard_already_set=True)
        self.inflight_evict.discard(key)

    def schedule_evict(self, context_id: int, block_id: int, delay_ns: int = 400) -> None:
        """Begin a hazarded eviction and complete it on the discrete-event clock."""
        self.begin_evict_key(context_id, block_id)

        def _done() -> None:
            self.complete_evict_key(context_id, block_id)

        self.world.after(delay_ns, _done)

    def release_prefix(self, prompt_hash: int) -> int:
        rec = self.prefix.table.get(prompt_hash)
        ref = self.prefix.release(prompt_hash)
        if rec is not None:
            for bid in rec.block_ids:
                self.eviction.set_refcount(pack_key(rec.context_id, bid), ref)
        return ref

    def _evict_one(self, key: int, events: list[TraceEvent], hazard_already_set: bool = False) -> None:
        ctx, bid = unpack_key(key)
        if not hazard_already_set:
            self.scoreboard.set_hazard(key)
        events.append(TraceEvent("EVICT", "slow", "scoreboard", f"lock({ctx},{bid})", 20))
        stall = self.crossbar.lookup_gate()
        phys = self.table.delete(key)
        commit_ns = self.crossbar.commit()
        self.crossbar.release()
        events.append(TraceEvent("EVICT", "slow", "crossbar", "clear map + atomic commit", commit_ns + stall))
        self.shadow_commits += 1
        self.bank_locks += 1
        if phys is not None:
            self.store.clear(phys)
            self.allocator.free(phys)
            events.append(TraceEvent("EVICT", "slow", "free_page", f"HBM[{phys}] -> free list", 20))
        meta = self.eviction.remove(key)
        if meta and meta.prefix_hash is not None:
            self.prefix.forget_block(meta.prefix_hash, meta.block_id)
        chain = self.contexts.get(ctx)
        if chain and bid in chain:
            chain.remove(bid)
            if not chain:
                self.contexts.pop(ctx, None)
        self.scoreboard.clear_hazard(key)
        self._schedule("EVICT", ctx, 64)

    def _schedule(self, opcode: str, context_id: int, size: int) -> int:
        """Enqueue on the matching VOQ, dequeue with SP+DRR, apply credit shaping.

        Returns serialization delay in ns for the dequeued packet.
        """
        self.voq.enqueue(
            Packet(
                ready_at=float(self.crossbar.now_ns),
                opcode=opcode,
                context_id=context_id,
                size=max(1, size),
            )
        )
        pkt = self.voq.dequeue()
        if pkt is None:
            return 0
        now_us = self.crossbar.now_ns / 1000.0
        paced = pkt.opcode in HIGH_PRIORITY_OPCODES
        _start, end = self.shaper.transmit(pkt.size, now_us, paced=paced)
        ns = max(0, int((end - now_us) * 1000.0))
        self.crossbar.advance(ns)
        return ns

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict:
        return {
            "pages_used": self.allocator.used_pages,
            "pages_free": self.allocator.free_pages,
            "fifo_depth": len(self.allocator.fifo),
            "fifo_refills": self.allocator.slow_refills,
            "hash_load": round(self.table.load_factor, 4),
            "hash_size": self.table.size,
            "hash_capacity": self.table.capacity,
            "fast_inserts": self.table.fast_inserts,
            "slow_inserts": self.table.slow_inserts,
            "slow_insert_rate": round(
                self.table.slow_inserts / max(1, self.table.fast_inserts + self.table.slow_inserts), 6
            ),
            "tag_collisions": self.table.tag_collisions,
            "victim_buffer": len(self.table.victim_buffer),
            "hazard_rate": round(self.scoreboard.hazard_rate, 6),
            "recirculations": self.scoreboard.recirculations,
            "shadow_commits": self.shadow_commits,
            "crossbar_commits": self.crossbar.commits,
            "lookup_stalls": self.crossbar.lookup_stalls,
            "hbm_key_verifies": self.table.hbm_key_verifies,
            "cuckoo_kicks": self.table.kicks,
            "bank_locks": self.bank_locks,
            "puts": self.put_ops,
            "gets": self.get_ops,
            "probes": self.probe_ops,
            "evicts": self.evict_ops,
            "probe_hits": self.prefix.hits,
            "probe_misses": self.prefix.misses,
            "bloom_false_positives": self.prefix.bloom_false_positives,
            "gathered_bytes": self.gathered_bytes,
            "contexts": len(self.contexts),
            "voq_high": self.voq.dequeued_high,
            "voq_low": self.voq.dequeued_low,
            "voq_preempt": self.voq.preemptions,
            "hbm_bank_conflicts": self.store.bank_conflicts,
            "hbm_meta_reads": self.store.meta_reads,
        }

    def snapshot_buckets(self, limit: int = 32) -> list[dict]:
        rows = []
        for i, bucket in enumerate(self.table.buckets[:limit]):
            rows.append(
                {
                    "bucket": i,
                    "slots": [
                        {
                            "fp": s.fingerprint,
                            "phys": s.phys,
                            "key": self.table.key_of(s.phys) or 0,
                            "ctx": unpack_key(self.table.key_of(s.phys) or 0)[0] if s.fingerprint else None,
                            "block": unpack_key(self.table.key_of(s.phys) or 0)[1] if s.fingerprint else None,
                        }
                        for s in bucket
                    ],
                }
            )
        return rows
