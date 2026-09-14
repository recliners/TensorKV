import {
  ALLOC_FIFO_BATCH,
  ALLOC_FIFO_WATERMARK,
  BLOOM_BITS,
  BLOOM_HASHES,
  CUCKOO_MAX_KICKS,
  SLOTS_PER_BUCKET,
  SplitMix64,
  bucketPair,
  fingerprint,
  mix64,
  type BlockMeta,
  type Slot,
} from "./types";

export class BloomFilter {
  nBits: number;
  nHashes: number;
  bits: Uint8Array;
  inserts = 0;
  constructor(nBits = BLOOM_BITS, nHashes = BLOOM_HASHES) {
    this.nBits = nBits;
    this.nHashes = nHashes;
    this.bits = new Uint8Array(Math.ceil(nBits / 8));
  }
  private positions(key: bigint): number[] {
    const out: number[] = [];
    for (let i = 0; i < this.nHashes; i++) {
      const h = mix64(key + BigInt(i + 1) * 0xd1b54a32d192ed03n);
      out.push(Number(h % BigInt(this.nBits)));
    }
    return out;
  }
  add(key: bigint) {
    for (const pos of this.positions(key)) {
      this.bits[pos >> 3] |= 1 << (pos & 7);
    }
    this.inserts++;
  }
  maybeContains(key: bigint): boolean {
    for (const pos of this.positions(key)) {
      if ((this.bits[pos >> 3] & (1 << (pos & 7))) === 0) return false;
    }
    return true;
  }
}

export class CuckooTable {
  nBuckets: number;
  slotsPer = SLOTS_PER_BUCKET;
  maxKicks = CUCKOO_MAX_KICKS;
  rng: SplitMix64;
  buckets: Slot[][];
  victimBuffer = new Map<bigint, Slot>();
  hbmKeys = new Map<number, bigint>();
  size = 0;
  fastInserts = 0;
  slowInserts = 0;
  tagCollisions = 0;
  lookups = 0;
  hits = 0;
  hbmKeyVerifies = 0;
  kicks = 0;

  constructor(nBuckets: number, seed = 0xa5a5n) {
    this.nBuckets = nBuckets;
    this.rng = new SplitMix64(seed);
    this.buckets = Array.from({ length: nBuckets }, () =>
      Array.from({ length: this.slotsPer }, () => ({ fingerprint: 0, phys: 0 })),
    );
  }

  keyOf(phys: number): bigint | null {
    return this.hbmKeys.get(phys) ?? null;
  }

  private bind(phys: number, key: bigint) {
    this.hbmKeys.set(phys, key);
  }

  private unbind(phys: number) {
    this.hbmKeys.delete(phys);
  }

  private slotKey(slot: Slot): bigint {
    return this.hbmKeys.get(slot.phys) ?? 0n;
  }

  get capacity() {
    return this.nBuckets * this.slotsPer;
  }
  get loadFactor() {
    return this.capacity ? this.size / this.capacity : 0;
  }

  private emptyIndex(bucket: number): number | null {
    for (let i = 0; i < this.slotsPer; i++) {
      if (this.buckets[bucket][i].fingerprint === 0) return i;
    }
    return null;
  }

  find(key: bigint): number | null {
    const tag = fingerprint(key);
    for (const b of bucketPair(key, this.nBuckets)) {
      for (const slot of this.buckets[b]) {
        if (slot.fingerprint === tag && this.slotKey(slot) === key) return slot.phys;
      }
    }
    const vic = this.victimBuffer.get(key);
    return vic ? vic.phys : null;
  }

  lookup(key: bigint): number | null {
    this.lookups++;
    const tag = fingerprint(key);
    for (const b of bucketPair(key, this.nBuckets)) {
      for (const slot of this.buckets[b]) {
        if (slot.fingerprint !== tag) continue;
        this.hbmKeyVerifies++;
        if (this.slotKey(slot) !== key) {
          this.tagCollisions++;
          continue;
        }
        this.hits++;
        return slot.phys;
      }
    }
    const vic = this.victimBuffer.get(key);
    if (vic) {
      this.hbmKeyVerifies++;
      this.hits++;
      return vic.phys;
    }
    return null;
  }

  insert(key: bigint, phys: number): "fast" | "slow" {
    if (this.find(key) !== null) {
      this.update(key, phys);
      return "fast";
    }
    const tag = fingerprint(key);
    const [h1, h2] = bucketPair(key, this.nBuckets);
    for (const b of [h1, h2]) {
      const empty = this.emptyIndex(b);
      if (empty !== null) {
        this.buckets[b][empty] = { fingerprint: tag, phys };
        this.bind(phys, key);
        this.size++;
        this.fastInserts++;
        return "fast";
      }
    }
    let curKey = key;
    let curPhys = phys;
    let curTag = tag;
    let curBucket = h1;
    for (let k = 0; k < this.maxKicks; k++) {
      this.kicks++;
      const slotI = this.rng.randint(0, this.slotsPer - 1);
      const victim = this.buckets[curBucket][slotI];
      const victimKey = this.slotKey(victim);
      this.buckets[curBucket][slotI] = { fingerprint: curTag, phys: curPhys };
      this.bind(curPhys, curKey);
      curKey = victimKey;
      curPhys = victim.phys;
      curTag = victim.fingerprint;
      const [vh1, vh2] = bucketPair(curKey, this.nBuckets);
      curBucket = curBucket === vh1 ? vh2 : vh1;
      const empty = this.emptyIndex(curBucket);
      if (empty !== null) {
        this.buckets[curBucket][empty] = { fingerprint: curTag, phys: curPhys };
        this.bind(curPhys, curKey);
        this.size++;
        this.slowInserts++;
        return "slow";
      }
    }
    this.victimBuffer.set(curKey, { fingerprint: curTag, phys: curPhys });
    this.bind(curPhys, curKey);
    this.size++;
    this.slowInserts++;
    return "slow";
  }

  update(key: bigint, phys: number) {
    const tag = fingerprint(key);
    for (const b of bucketPair(key, this.nBuckets)) {
      for (const slot of this.buckets[b]) {
        if (slot.fingerprint === tag && this.slotKey(slot) === key) {
          const old = slot.phys;
          slot.phys = phys;
          if (old !== phys) this.unbind(old);
          this.bind(phys, key);
          return;
        }
      }
    }
    const vic = this.victimBuffer.get(key);
    if (vic) {
      const old = vic.phys;
      vic.phys = phys;
      if (old !== phys) this.unbind(old);
      this.bind(phys, key);
    }
  }

  delete(key: bigint): number | null {
    const tag = fingerprint(key);
    for (const b of bucketPair(key, this.nBuckets)) {
      for (const slot of this.buckets[b]) {
        if (slot.fingerprint === tag && this.slotKey(slot) === key) {
          const phys = slot.phys;
          slot.fingerprint = 0;
          slot.phys = 0;
          this.unbind(phys);
          this.size--;
          return phys;
        }
      }
    }
    const vic = this.victimBuffer.get(key);
    if (vic) {
      this.victimBuffer.delete(key);
      this.size--;
      this.unbind(vic.phys);
      return vic.phys;
    }
    return null;
  }

  occupancyHistogram(): number[] {
    const hist = Array.from({ length: this.slotsPer + 1 }, () => 0);
    for (const bucket of this.buckets) {
      const used = bucket.filter((s) => s.fingerprint !== 0).length;
      hist[used]++;
    }
    return hist;
  }
}

export class HierarchicalAllocator {
  nPages: number;
  batch: number;
  watermark: number;
  freeList: number[];
  fifo: number[] = [];
  slowRefills = 0;
  fastPops = 0;
  failedAllocs = 0;

  constructor(nPages: number, batch = ALLOC_FIFO_BATCH, watermark = ALLOC_FIFO_WATERMARK) {
    this.nPages = nPages;
    this.batch = batch;
    this.watermark = watermark;
    this.freeList = Array.from({ length: nPages }, (_, i) => i);
    this.refill();
  }

  private refill() {
    let pushed = 0;
    while (this.freeList.length && pushed < this.batch) {
      this.fifo.push(this.freeList.shift() as number);
      pushed++;
    }
    if (pushed) this.slowRefills++;
  }

  maybeRefill() {
    if (this.fifo.length < this.watermark) this.refill();
  }

  alloc(): number | null {
    if (!this.fifo.length) this.refill();
    if (!this.fifo.length) {
      this.failedAllocs++;
      return null;
    }
    const page = this.fifo.shift() as number;
    this.fastPops++;
    this.maybeRefill();
    return page;
  }

  free(page: number) {
    this.freeList.push(page);
    this.maybeRefill();
  }

  get freePages() {
    return this.freeList.length + this.fifo.length;
  }
  get usedPages() {
    return this.nPages - this.freePages;
  }
}

export class Scoreboard {
  hazards = new Set<string>();
  checks = 0;
  hazardHits = 0;
  recirculations = 0;
  setHazard(key: bigint) {
    this.hazards.add(key.toString());
  }
  clearHazard(key: bigint) {
    this.hazards.delete(key.toString());
  }
  isHazard(key: bigint): boolean {
    this.checks++;
    if (this.hazards.has(key.toString())) {
      this.hazardHits++;
      this.recirculations++;
      return true;
    }
    return false;
  }
  get hazardRate() {
    return this.checks ? this.hazardHits / this.checks : 0;
  }
}

export class PrefixIndex {
  bloom = new BloomFilter();
  table = new Map<string, { promptHash: bigint; contextId: number; blockIds: number[]; refcount: number }>();
  probes = 0;
  hits = 0;
  misses = 0;
  bloomNegatives = 0;
  bloomFalsePositives = 0;

  register(promptHash: bigint, contextId: number, blockIds: number[]) {
    const rec = { promptHash, contextId, blockIds: [...blockIds], refcount: 1 };
    this.table.set(promptHash.toString(), rec);
    this.bloom.add(promptHash);
    return rec;
  }

  probe(promptHash: bigint) {
    this.probes++;
    if (!this.bloom.maybeContains(promptHash)) {
      this.bloomNegatives++;
      this.misses++;
      return null;
    }
    const rec = this.table.get(promptHash.toString());
    if (!rec) {
      this.bloomFalsePositives++;
      this.misses++;
      return null;
    }
    rec.refcount++;
    this.hits++;
    return rec;
  }

  release(promptHash: bigint) {
    const rec = this.table.get(promptHash.toString());
    if (!rec) return 0;
    rec.refcount = Math.max(0, rec.refcount - 1);
    return rec.refcount;
  }

  drop(promptHash: bigint) {
    this.table.delete(promptHash.toString());
  }

  forgetBlock(promptHash: bigint, blockId: number) {
    const rec = this.table.get(promptHash.toString());
    if (!rec) return;
    rec.blockIds = rec.blockIds.filter((b) => b !== blockId);
    if (!rec.blockIds.length) this.drop(promptHash);
  }
}

export class EvictionTracker {
  clock = 0;
  blocks = new Map<string, BlockMeta>();

  add(meta: BlockMeta) {
    this.clock++;
    meta.lastAccess = this.clock;
    meta.frequency = Math.max(1, meta.frequency);
    this.blocks.set(meta.key.toString(), meta);
  }

  touch(key: bigint, prefix = false, prefixHash: bigint | null = null) {
    this.clock++;
    const meta = this.blocks.get(key.toString());
    if (!meta) return;
    meta.lastAccess = this.clock;
    meta.frequency++;
    if (prefix) {
      meta.isPrefix = true;
      meta.prefixHash = prefixHash;
    }
  }

  remove(key: bigint) {
    const meta = this.blocks.get(key.toString());
    this.blocks.delete(key.toString());
    return meta ?? null;
  }

  setRefcount(key: bigint, ref: number) {
    const meta = this.blocks.get(key.toString());
    if (meta) meta.refcount = ref;
  }

  bumpRefcount(keys: bigint[], delta = 1) {
    for (const key of keys) {
      const meta = this.blocks.get(key.toString());
      if (meta) meta.refcount += delta;
    }
  }

  selectVictims(n: number, policy: "lru" | "lfru"): BlockMeta[] {
    const items = [...this.blocks.values()];
    if (!items.length || n <= 0) return [];
    if (policy === "lru") {
      items.sort((a, b) => a.lastAccess - b.lastAccess);
      return items.slice(0, n);
    }
    const unprotected = items.filter((m) => m.refcount <= 1);
    const pool = unprotected.length ? unprotected : items;
    pool.sort((a, b) => a.frequency - b.frequency || a.lastAccess - b.lastAccess);
    return pool.slice(0, n);
  }
}
