import {
  CuckooTable,
  EvictionTracker,
  HierarchicalAllocator,
  PrefixIndex,
  Scoreboard,
} from "./core";
import {
  BLOCK_SIZE_BYTES,
  FAST_PATH_HBM_HIT_NS,
  FAST_PATH_SRAM_HIT_NS,
  HAZARD_RECIRC_NS,
  RMT_LOOKUP_NS,
  SLOW_PATH_CUCKOO_NS,
  packKey,
  unpackKey,
  type TraceEvent,
} from "./types";

export type PutResult = {
  ok: boolean;
  contextId: number;
  blockId: number;
  phys: number | null;
  path: "fast" | "slow";
  events: TraceEvent[];
  latencyNs: number;
  error?: string;
};

export type GetResult = {
  ok: boolean;
  payload: Uint8Array;
  hits: number[];
  misses: number[];
  events: TraceEvent[];
  latencyNs: number;
  recirculations: number;
  gatheredBytes: number;
};

export type ProbeResult = {
  hit: boolean;
  handles: number[];
  contextId: number | null;
  refcount: number;
  events: TraceEvent[];
  latencyNs: number;
  hbmAccessed: boolean;
};

export type ApplianceConfig = {
  nBuckets?: number;
  nPages?: number;
  blockSize?: number;
  storePayloads?: boolean;
};

export class TensorKVAppliance {
  cfg: Required<ApplianceConfig>;
  table: CuckooTable;
  allocator: HierarchicalAllocator;
  scoreboard = new Scoreboard();
  prefix = new PrefixIndex();
  eviction = new EvictionTracker();
  hbm: (Uint8Array | null)[];
  contexts = new Map<number, number[]>();
  inflightEvict = new Set<string>();
  shadowCommits = 0;
  putOps = 0;
  getOps = 0;
  probeOps = 0;
  evictOps = 0;
  gatheredBytes = 0;
  creditGbps = 40;

  constructor(cfg: ApplianceConfig = {}) {
    this.cfg = {
      nBuckets: cfg.nBuckets ?? 1024,
      nPages: cfg.nPages ?? 4096,
      blockSize: cfg.blockSize ?? BLOCK_SIZE_BYTES,
      storePayloads: cfg.storePayloads ?? true,
    };
    this.table = new CuckooTable(this.cfg.nBuckets);
    this.allocator = new HierarchicalAllocator(this.cfg.nPages);
    this.hbm = Array.from({ length: this.cfg.nPages }, () => null);
  }

  private pad(data: Uint8Array): Uint8Array {
    const out = new Uint8Array(this.cfg.blockSize);
    out.set(data.subarray(0, this.cfg.blockSize));
    return out;
  }

  put(contextId: number, seqId: number, data?: Uint8Array, prefixHash?: bigint): PutResult {
    const events: TraceEvent[] = [];
    this.putOps++;
    const key = packKey(contextId, seqId);
    events.push({ op: "PUT", path: "fast", stage: "parser", detail: `PUT(${contextId},${seqId})`, latency_ns: 0 });
    const existing = this.table.find(key);
    if (existing !== null) {
      this.hbm[existing] = this.pad(data ?? new Uint8Array([seqId & 0xff]));
      this.eviction.touch(key, prefixHash !== undefined, prefixHash ?? null);
      events.push({ op: "PUT", path: "fast", stage: "overwrite", detail: `HBM[${existing}]`, latency_ns: 80 });
      return { ok: true, contextId, blockId: seqId, phys: existing, path: "fast", events, latencyNs: FAST_PATH_SRAM_HIT_NS };
    }
    const page = this.allocator.alloc();
    if (page === null) {
      events.push({ op: "PUT", path: "slow", stage: "allocator", detail: "free list empty", latency_ns: 0 });
      return { ok: false, contextId, blockId: seqId, phys: null, path: "slow", events, latencyNs: 0, error: "remote allocation failed" };
    }
    events.push({
      op: "PUT",
      path: "fast",
      stage: "fifo_pop",
      detail: `page=${page} fifo=${this.allocator.fifo.length}`,
      latency_ns: 4,
    });
    this.hbm[page] = this.pad(data ?? new Uint8Array([seqId & 0xff]));
    events.push({ op: "PUT", path: "fast", stage: "dma_write", detail: `HBM[${page}] ${this.cfg.blockSize}B`, latency_ns: 80 });
    const path = this.table.insert(key, page);
    const latency = path === "fast" ? FAST_PATH_SRAM_HIT_NS : SLOW_PATH_CUCKOO_NS;
    events.push({
      op: "PUT",
      path,
      stage: "hash_insert",
      detail: `cuckoo=${path} load=${this.table.loadFactor.toFixed(3)}`,
      latency_ns: latency,
    });
    if (path === "slow") {
      this.shadowCommits++;
      events.push({ op: "PUT", path: "slow", stage: "crossbar", detail: "atomic shadow-row commit", latency_ns: 80 });
    }
    const chain = this.contexts.get(contextId) ?? [];
    chain.push(seqId);
    this.contexts.set(contextId, chain);
    this.eviction.add({
      key,
      contextId,
      blockId: seqId,
      phys: page,
      lastAccess: 0,
      frequency: 1,
      refcount: 1,
      isPrefix: prefixHash !== undefined,
      prefixHash: prefixHash ?? null,
    });
    return { ok: true, contextId, blockId: seqId, phys: page, path, events, latencyNs: events.reduce((s, e) => s + e.latency_ns, 0) };
  }

  get(contextId: number, blockIds: number[], creditGbps?: number): GetResult {
    const events: TraceEvent[] = [];
    this.getOps++;
    if (creditGbps !== undefined) {
      this.creditGbps = creditGbps;
      events.push({ op: "GET", path: "fast", stage: "credit", detail: `shaper=${creditGbps} Gbps`, latency_ns: 0 });
    }
    events.push({
      op: "GET",
      path: "fast",
      stage: "parser",
      detail: `GET(${contextId}, [${blockIds.slice(0, 8).join(",")}])`,
      latency_ns: 0,
    });
    const chunks: Uint8Array[] = [];
    const hits: number[] = [];
    const misses: number[] = [];
    let recirc = 0;
    let latency = RMT_LOOKUP_NS;
    for (const bid of blockIds) {
      const key = packKey(contextId, bid);
      if (this.scoreboard.isHazard(key) || this.inflightEvict.has(key.toString())) {
        recirc++;
        events.push({ op: "GET", path: "fast", stage: "scoreboard", detail: `hazard block ${bid} recirculate`, latency_ns: HAZARD_RECIRC_NS });
        latency += HAZARD_RECIRC_NS;
      }
      const phys = this.table.lookup(key);
      const payload = phys !== null ? this.hbm[phys] : null;
      if (phys === null || !payload) {
        misses.push(bid);
        events.push({ op: "GET", path: "fast", stage: "lookup", detail: `MISS block ${bid}`, latency_ns: 0 });
        continue;
      }
      chunks.push(payload);
      hits.push(bid);
      this.eviction.touch(key);
      latency += 20;
    }
    events.push({
      op: "GET",
      path: "fast",
      stage: "dma_gather",
      detail: `scatter-gather ${hits.length} blocks → contiguous stream`,
      latency_ns: 200,
    });
    const totalLen = chunks.reduce((s, c) => s + c.length, 0);
    const payload = new Uint8Array(totalLen);
    let off = 0;
    for (const c of chunks) {
      payload.set(c, off);
      off += c.length;
    }
    this.gatheredBytes += payload.length;
    latency += hits.length ? FAST_PATH_HBM_HIT_NS : FAST_PATH_SRAM_HIT_NS;
    events.push({
      op: "GET",
      path: "fast",
      stage: "egress",
      detail: `hits=${hits.length} misses=${misses.length} recirc=${recirc}`,
      latency_ns: latency,
    });
    return {
      ok: misses.length === 0 && hits.length > 0,
      payload,
      hits,
      misses,
      events,
      latencyNs: latency,
      recirculations: recirc,
      gatheredBytes: payload.length,
    };
  }

  probe(promptHash: bigint): ProbeResult {
    const events: TraceEvent[] = [];
    this.probeOps++;
    events.push({ op: "PROBE", path: "fast", stage: "parser", detail: `PROBE(${Number(promptHash & 0xffffffffn)})`, latency_ns: 0 });
    events.push({ op: "PROBE", path: "fast", stage: "bloom", detail: "check prefix bloom filter", latency_ns: 20 });
    const rec = this.prefix.probe(promptHash);
    if (!rec) {
      events.push({ op: "PROBE", path: "fast", stage: "index", detail: "MISS (no HBM access)", latency_ns: FAST_PATH_SRAM_HIT_NS });
      return { hit: false, handles: [], contextId: null, refcount: 0, events, latencyNs: FAST_PATH_SRAM_HIT_NS, hbmAccessed: false };
    }
    for (const bid of rec.blockIds) {
      const key = packKey(rec.contextId, bid);
      this.eviction.setRefcount(key, rec.refcount);
      this.eviction.touch(key, true, promptHash);
    }
    events.push({
      op: "PROBE",
      path: "fast",
      stage: "index",
      detail: `HIT handles=${rec.blockIds.length} ref=${rec.refcount} (no HBM)`,
      latency_ns: FAST_PATH_SRAM_HIT_NS,
    });
    return {
      hit: true,
      handles: [...rec.blockIds],
      contextId: rec.contextId,
      refcount: rec.refcount,
      events,
      latencyNs: FAST_PATH_SRAM_HIT_NS,
      hbmAccessed: false,
    };
  }

  publishPrefix(promptHash: bigint, contextId: number, blockIds: number[]) {
    const rec = this.prefix.register(promptHash, contextId, blockIds);
    for (const bid of blockIds) {
      const key = packKey(contextId, bid);
      this.eviction.setRefcount(key, rec.refcount);
      this.eviction.touch(key, true, promptHash);
    }
    return rec;
  }

  evict(contextId: number, policy: "lru" | "lfru" | "all" | "oldest" = "lru", k?: number) {
    this.evictOps++;
    const events: TraceEvent[] = [{ op: "EVICT", path: "slow", stage: "control_core", detail: `EVICT(${contextId},${policy},${k ?? ""})`, latency_ns: 0 }];
    let targets: number[] = [];
    if (policy === "all") targets = [...(this.contexts.get(contextId) ?? [])];
    else if (policy === "oldest") {
      const blocks = [...(this.contexts.get(contextId) ?? [])];
      const n = k ?? Math.max(1, Math.floor(blocks.length / 4));
      targets = blocks.slice(0, n);
    } else {
      const n = k ?? 1;
      const metas = this.eviction.selectVictims(n, policy);
      const scoped = metas.filter((m) => m.contextId === contextId);
      targets = (scoped.length ? scoped : metas).slice(0, n).map((m) => m.blockId);
      const chain = this.contexts.get(contextId) ?? [];
      if (k && chain.length) targets = chain.slice(0, n);
    }
    const evicted: number[] = [];
    for (const bid of targets) {
      this.evictOne(packKey(contextId, bid), events);
      evicted.push(bid);
    }
    events.push({ op: "EVICT", path: "slow", stage: "done", detail: `reclaimed ${evicted.length} blocks`, latency_ns: 400 * Math.max(1, evicted.length) });
    return { evicted, events, latencyNs: 400 * Math.max(1, evicted.length) };
  }

  evictKey(contextId: number, blockId: number) {
    this.evictOne(packKey(contextId, blockId), []);
  }

  beginEvictKey(contextId: number, blockId: number) {
    const key = packKey(contextId, blockId);
    this.scoreboard.setHazard(key);
    this.inflightEvict.add(key.toString());
  }

  completeEvictKey(contextId: number, blockId: number) {
    const key = packKey(contextId, blockId);
    this.evictOne(key, [], true);
    this.inflightEvict.delete(key.toString());
  }

  reclaim(n: number, policy: "lru" | "lfru" = "lfru") {
    const metas = this.eviction.selectVictims(n, policy);
    const evicted: number[] = [];
    for (const meta of metas) {
      this.evictOne(meta.key, []);
      evicted.push(meta.blockId);
    }
    return evicted;
  }

  private evictOne(key: bigint, events: TraceEvent[], hazardAlready = false) {
    const [ctx, bid] = unpackKey(key);
    if (!hazardAlready) this.scoreboard.setHazard(key);
    events.push({ op: "EVICT", path: "slow", stage: "scoreboard", detail: `lock(${ctx},${bid})`, latency_ns: 20 });
    const phys = this.table.delete(key);
    events.push({ op: "EVICT", path: "slow", stage: "crossbar", detail: "clear map + atomic commit", latency_ns: 80 });
    this.shadowCommits++;
    if (phys !== null) {
      this.hbm[phys] = null;
      this.allocator.free(phys);
      events.push({ op: "EVICT", path: "slow", stage: "free_page", detail: `HBM[${phys}] → free list`, latency_ns: 20 });
    }
    const meta = this.eviction.remove(key);
    if (meta?.prefixHash !== null && meta?.prefixHash !== undefined) {
      const leftover = this.prefix.release(meta.prefixHash);
      if (leftover <= 0) this.prefix.drop(meta.prefixHash);
    }
    const chain = this.contexts.get(ctx);
    if (chain) {
      const next = chain.filter((b) => b !== bid);
      if (next.length) this.contexts.set(ctx, next);
      else this.contexts.delete(ctx);
    }
    this.scoreboard.clearHazard(key);
  }

  stats() {
    const inserts = this.table.fastInserts + this.table.slowInserts;
    return {
      pagesUsed: this.allocator.usedPages,
      pagesFree: this.allocator.freePages,
      fifoDepth: this.allocator.fifo.length,
      fifoRefills: this.allocator.slowRefills,
      hashLoad: Number(this.table.loadFactor.toFixed(4)),
      hashSize: this.table.size,
      hashCapacity: this.table.capacity,
      fastInserts: this.table.fastInserts,
      slowInserts: this.table.slowInserts,
      slowInsertRate: inserts ? Number((this.table.slowInserts / inserts).toFixed(6)) : 0,
      tagCollisions: this.table.tagCollisions,
      victimBuffer: this.table.victimBuffer.size,
      hazardRate: Number(this.scoreboard.hazardRate.toFixed(6)),
      recirculations: this.scoreboard.recirculations,
      shadowCommits: this.shadowCommits,
      puts: this.putOps,
      gets: this.getOps,
      probes: this.probeOps,
      evicts: this.evictOps,
      probeHits: this.prefix.hits,
      probeMisses: this.prefix.misses,
      gatheredBytes: this.gatheredBytes,
      contexts: this.contexts.size,
    };
  }

  snapshotBuckets(limit = 24) {
    return this.table.buckets.slice(0, limit).map((bucket, i) => ({
      bucket: i,
      slots: bucket.map((s) => {
        const [ctx, block] = s.fingerprint ? unpackKey(s.fullKey) : [null, null];
        return { fp: s.fingerprint, phys: s.phys, ctx, block };
      }),
    }));
  }
}

export class TensorKVContext {
  device: TensorKVAppliance;
  cq: { op: string; ok: boolean; detail: string }[] = [];
  constructor(device?: TensorKVAppliance) {
    this.device = device ?? new TensorKVAppliance();
  }
  putAsync(ctx: number, seq: number, data?: Uint8Array, prefixHash?: bigint) {
    const r = this.device.put(ctx, seq, data, prefixHash);
    this.cq.push({ op: "PUT", ok: r.ok, detail: `block ${seq} page=${r.phys}` });
    return r;
  }
  getAsync(ctx: number, ids: number[], credit = 40) {
    const r = this.device.get(ctx, ids, credit);
    this.cq.push({ op: "GET", ok: r.ok, detail: `hits=${r.hits.length} misses=${r.misses.length}` });
    return r;
  }
  probe(hash: bigint) {
    const r = this.device.probe(hash);
    this.cq.push({ op: "PROBE", ok: r.hit, detail: r.hit ? `handles=${r.handles.length}` : "miss" });
    return r;
  }
  evict(ctx: number, policy: "lru" | "lfru" | "all" | "oldest" = "lru", k?: number) {
    const r = this.device.evict(ctx, policy, k);
    this.cq.push({ op: "EVICT", ok: true, detail: `n=${r.evicted.length}` });
    return r;
  }
  poll() {
    return this.cq.shift() ?? null;
  }
}
