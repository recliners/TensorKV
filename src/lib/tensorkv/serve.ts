import { TensorKVAppliance, TensorKVContext } from "./appliance";
import { PagedEngine } from "./engine";
import { Arrival, ServingScheduler } from "./scheduler";
import { ShareGPTWorkload, SplitMix64, TOKENS_PER_BLOCK } from "./types";

function percentile(xs: number[], p: number) {
  if (!xs.length) return 0;
  const ys = [...xs].sort((a, b) => a - b);
  const i = Math.min(ys.length - 1, Math.max(0, Math.round((p / 100) * (ys.length - 1))));
  return ys[i];
}

function summary(xs: number[]) {
  if (!xs.length) return { n: 0, mean: 0, p50: 0, p90: 0, p99: 0, max: 0 };
  return {
    n: xs.length,
    mean: xs.reduce((a, b) => a + b, 0) / xs.length,
    p50: percentile(xs, 50),
    p90: percentile(xs, 90),
    p99: percentile(xs, 99),
    max: Math.max(...xs),
  };
}

export function sharegptArrivals(opts: {
  nSessions?: number;
  nPrefixes?: number;
  prefixBlocks?: number;
  uniqueBlocks?: number;
  decodeTokens?: number;
  seed?: number;
  tpb?: number;
} = {}) {
  const nSessions = opts.nSessions ?? 16;
  const nPrefixes = opts.nPrefixes ?? 4;
  const prefixBlocks = opts.prefixBlocks ?? 10;
  const uniqueBlocks = opts.uniqueBlocks ?? 2;
  const decodeTokens = opts.decodeTokens ?? 3;
  const seed = opts.seed ?? 5;
  const tpb = opts.tpb ?? TOKENS_PER_BLOCK;
  const wl = new ShareGPTWorkload({
    nPrefixes,
    nSessions,
    prefixBlocks,
    uniqueBlocks,
    seed,
  });
  const rng = new SplitMix64(BigInt(seed + 17));
  const arrivals: Arrival[] = [];
  let t = 0;
  for (const s of wl.sessions) {
    const prefix = Array.from({ length: s.prefixBlocks * tpb }, (_, i) => s.prefixId * 256 + i);
    const suffix = Array.from({ length: s.uniqueBlocks * tpb }, (_, i) => 1_000_000 + s.sessionId * 256 + i);
    t += rng.randint(0, 2);
    arrivals.push({
      reqId: s.sessionId + 1,
      tokens: prefix.concat(suffix),
      prefixTokens: prefix,
      maxNew: Math.max(2, decodeTokens),
      arriveTick: t,
    });
  }
  return { wl, arrivals };
}

export function runServing(opts: {
  nSessions?: number;
  nPrefixes?: number;
  prefixBlocks?: number;
  uniqueBlocks?: number;
  maxBatch?: number;
  decodeTokens?: number;
  nPages?: number;
  nBuckets?: number;
  seed?: number;
} = {}) {
  const nSessions = opts.nSessions ?? 16;
  const nPrefixes = opts.nPrefixes ?? 4;
  const prefixBlocks = opts.prefixBlocks ?? 10;
  const uniqueBlocks = opts.uniqueBlocks ?? 2;
  const maxBatch = opts.maxBatch ?? 6;
  const decodeTokens = opts.decodeTokens ?? 3;
  const seed = opts.seed ?? 5;
  const nPages = Math.max(opts.nPages ?? 192, 32);
  const nBuckets = opts.nBuckets ?? 64;
  const { wl, arrivals } = sharegptArrivals({
    nSessions,
    nPrefixes,
    prefixBlocks,
    uniqueBlocks,
    decodeTokens,
    seed,
  });
  const tkv = new TensorKVAppliance({
    nBuckets,
    nPages,
    storePayloads: false,
    seed,
  });
  const eng = new PagedEngine(new TensorKVContext(tkv));
  const sch = new ServingScheduler(eng, maxBatch);
  const stats = sch.runArrivals(arrivals);
  const st = tkv.stats();
  return {
    nSessions,
    nPrefixes,
    prefixBlocks,
    uniqueBlocks,
    workingSetBlocks: wl.workingSetBlocks,
    nPages,
    maxBatch,
    submitted: stats.submitted,
    finished: stats.finished,
    prefixHits: stats.prefixHits,
    prefixHitRate: stats.submitted ? stats.prefixHits / stats.submitted : 0,
    decodeSteps: stats.decodeSteps,
    reclaims: stats.reclaims,
    ticks: stats.ticks,
    peakBatch: stats.peakBatch,
    vectorOverflow: stats.vectorOverflow,
    ttft: summary(stats.ttftMs),
    tbt: summary(stats.tbtMs),
    overflowBytes: eng.tkv.overflowBytes,
    descriptorsPosted: eng.tkv.descriptorsPosted,
    device: {
      hashLoad: st.hashLoad,
      pagesUsed: st.pagesUsed,
      getOps: st.gets,
      putOps: st.puts,
      probeOps: st.probes,
      evictOps: st.evicts,
      gatheredBytes: st.gatheredBytes,
    },
    complete: stats.finished === nSessions,
    ttftSeries: stats.ttftMs.map((ms, i) => ({ req: i + 1, ttft: Number(ms.toFixed(3)) })),
    getLatencyNs: summary(tkv.getLatencies),
  };
}
