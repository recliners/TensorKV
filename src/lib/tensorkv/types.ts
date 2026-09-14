/** TensorKV datapath and eval-table constants. */
export const TOKENS_PER_BLOCK = 16;
export const BLOCK_SIZE_BYTES = 4096;
export const ALLOC_FIFO_BATCH = 64;
export const ALLOC_FIFO_WATERMARK = 16;
export const SLOTS_PER_BUCKET = 4;
export const CUCKOO_MAX_KICKS = 32;
export const BLOOM_BITS = 1 << 16;
export const BLOOM_HASHES = 3;
export const ZIPF_ALPHA = 1.2;
export const FAST_PATH_SRAM_HIT_NS = 2150;
export const FAST_PATH_HBM_HIT_NS = 2420;
export const RMT_LOOKUP_NS = 150;
export const HAZARD_RECIRC_NS = 80;
export const SLOW_PATH_CUCKOO_NS = 12400;
export const FPGA_CYCLE_NS = 4;
export const CROSSBAR_NOC_CYCLES = 20;
export const ATOMIC_COMMIT_CYCLES = 1;
export const DRR_QUANTUM_BYTES = 16384;
export const LINK_GBPS = 100;
export const DEFAULT_CREDIT_GBPS = 40;
export const HIGH_PRIORITY_OPCODES = new Set(["GET", "PROBE"]);
export const BYTES_PER_TOKEN_LLAMA70B_INT4 = 81920;
export const BYTES_PER_TOKEN_MIXTRAL_FP8 = 65536;
export const PREFILL_TOKENS = 32768;
export const COMPUTE_MS_AT_32K = 15;
export const PREFILL_COMPUTE_MS_AT_32K = 1200;
export const HANDLE_INSTALL_NS = 8789;
export const HBM_CAPACITY_BYTES = 8 * 1024 * 1024 * 1024;
export const DESCRIPTOR_BYTES = 64;

export const EVAL_TTFT = {
  recompute: { setup: 0, fetch: 0, compute: 1200, total: 1200 },
  host_a100: { setup: 420, fetch: 80, compute: 20, total: 520 },
  host_h100: { setup: 400, fetch: 40, compute: 15, total: 455 },
  rdma: { setup: 140, fetch: 250, compute: 15, total: 405 },
  rpc: { setup: 120, fetch: 240, compute: 15, total: 375 },
  dpu: { setup: 80, fetch: 235, compute: 15, total: 330 },
  tensorkv: { setup: 18, fetch: 230, compute: 15, total: 263 },
} as const;

export const EVAL_TBT: Record<string, Record<string, number>> = {
  "0.25": { host_a: 52, host_h: 45, rdma: 38, rpc: 36, dpu: 35, tkv: 33 },
  "0.5": { host_a: 110, host_h: 95, rdma: 75, rpc: 70, dpu: 65, tkv: 58 },
  "1.0": { host_a: 210, host_h: 180, rdma: 140, rpc: 132, dpu: 120, tkv: 102 },
  "1.5": { host_a: 290, host_h: 250, rdma: 210, rpc: 195, dpu: 175, tkv: 145 },
};

export const EVAL_EVICTION = {
  lru: { hit_60: 30, hit_80: 65, ttft_60: 810, ttft_80: 245 },
  lfru: { hit_60: 78, hit_80: 92, ttft_60: 135, ttft_80: 42 },
} as const;

export const MASK64 = 0xffffffffffffffffn;
export const MASK32 = 0xffffffffn;

export function mix64(x: bigint): bigint {
  let v = x & MASK64;
  v = ((v ^ (v >> 30n)) * 0xbf58476d1ce4e5b9n) & MASK64;
  v = ((v ^ (v >> 27n)) * 0x94d049bb133111ebn) & MASK64;
  return (v ^ (v >> 31n)) & MASK64;
}

export function packKey(ctx: number, block: number): bigint {
  return ((BigInt(ctx) & MASK32) << 32n) | (BigInt(block) & MASK32);
}

export function unpackKey(key: bigint): [number, number] {
  return [Number((key >> 32n) & MASK32), Number(key & MASK32)];
}

export function fingerprint(key: bigint): number {
  const tag = Number((mix64(key) >> 32n) & MASK32);
  return tag === 0 ? 1 : tag;
}

export function bucketPair(key: bigint, nBuckets: number): [number, number] {
  const h1 = Number(mix64(key) % BigInt(nBuckets));
  let h2 = Number(mix64(key ^ 0x9e3779b97f4a7c15n) % BigInt(nBuckets));
  if (h2 === h1) h2 = (h1 + 1) % nBuckets;
  return [h1, h2];
}

export class SplitMix64 {
  state: bigint;
  constructor(seed = 0xc0ffeen) {
    this.state = seed & MASK64;
  }
  nextU64(): bigint {
    this.state = (this.state + 0x9e3779b97f4a7c15n) & MASK64;
    return mix64(this.state);
  }
  nextFloat(): number {
    return Number(this.nextU64() >> 11n) / Number(1n << 53n);
  }
  randint(lo: number, hi: number): number {
    const span = hi - lo + 1;
    return lo + Number(this.nextU64() % BigInt(span));
  }
}

export class ZipfSampler {
  n: number;
  alpha: number;
  rng: SplitMix64;
  cdf: number[];
  constructor(n: number, alpha = ZIPF_ALPHA, rng?: SplitMix64) {
    this.n = n;
    this.alpha = alpha;
    this.rng = rng ?? new SplitMix64(0x5eedn);
    const weights = Array.from({ length: n }, (_, i) => (i + 1) ** -alpha);
    const total = weights.reduce((a, b) => a + b, 0);
    let acc = 0;
    this.cdf = weights.map((w) => {
      acc += w / total;
      return acc;
    });
    this.cdf[this.cdf.length - 1] = 1;
  }
  sample(): number {
    const u = this.rng.nextFloat();
    let lo = 0;
    let hi = this.n - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.cdf[mid] < u) lo = mid + 1;
      else hi = mid;
    }
    return lo + 1;
  }
  sampleIndex(): number {
    return this.sample() - 1;
  }
}

export type TraceEvent = {
  op: string;
  path: "fast" | "slow";
  stage: string;
  detail: string;
  latency_ns: number;
};

export type Slot = { fingerprint: number; phys: number };
export type BlockMeta = {
  key: bigint;
  contextId: number;
  blockId: number;
  phys: number;
  lastAccess: number;
  frequency: number;
  refcount: number;
  isPrefix: boolean;
  prefixHash: bigint | null;
};
