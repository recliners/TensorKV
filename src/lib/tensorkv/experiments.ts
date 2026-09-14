import { TensorKVAppliance, TensorKVContext } from "./appliance";
import { mix64, SplitMix64, ZIPF_ALPHA, PAPER_EVICTION, PAPER_TTFT, PAPER_TBT } from "./types";
import { simulateNoisyNeighbor } from "./transport";

export function promptHash(tokens: number[]): bigint {
  let h = 0x243f6a8885a308d3n;
  for (const t of tokens) h = mix64(h ^ BigInt(t >>> 0));
  return h;
}

function zipfSample(n: number, alpha: number, rng: SplitMix64): number {
  if (n <= 1) return 1;
  const u = Math.max(1e-12, rng.nextFloat());
  const rank = Math.floor(u ** (-1 / alpha));
  return Math.min(n, Math.max(1, (rank % n) + 1));
}

export function occupancySweep(loads = [0.5, 0.7, 0.8, 0.9, 0.95]) {
  const rng = new SplitMix64(1n);
  const nBuckets = 256;
  const cap = nBuckets * 4;
  return loads.map((load) => {
    const tkv = new TensorKVAppliance({ nBuckets, nPages: cap + 128, storePayloads: false });
    let target = Math.floor(cap * load);
    for (let i = 0; i < target; i++) tkv.put(1, i);
    const live = Array.from({ length: target }, (_, i) => i);
    for (let op = 0; op < 2500; op++) {
      const bid = live[(zipfSample(live.length, ZIPF_ALPHA, rng) - 1) % live.length];
      tkv.get(1, [bid]);
      if (rng.nextFloat() < 0.02 && live.length) {
        const vi = rng.randint(0, live.length - 1);
        const victim = live[vi];
        tkv.beginEvictKey(1, victim);
        tkv.get(1, [victim]);
        tkv.completeEvictKey(1, victim);
        live.splice(vi, 1);
        const nxt = target + rng.randint(0, 10000);
        tkv.put(1, nxt);
        live.push(nxt);
        target++;
      }
    }
    const inserts = tkv.table.fastInserts + tkv.table.slowInserts;
    return {
      load,
      slowInsertRate: inserts ? tkv.table.slowInserts / inserts : 0,
      hazardRate: tkv.scoreboard.hazardRate,
      victimBuffer: tkv.table.victimBuffer.size,
    };
  });
}

export function scatterGatherRtts(n = 8) {
  const tkv = new TensorKVAppliance({ nBuckets: 64, nPages: 256, blockSize: 64 });
  for (let i = 0; i < n; i++) tkv.put(7, i, new Uint8Array(64).fill(i));
  const got = tkv.get(7, Array.from({ length: n }, (_, i) => i));
  return {
    blocks: n,
    tensorkvRtts: 1,
    rdmaUncachedRtts: 1 + n,
    gatheredOk: got.ok && got.misses.length === 0,
    latencyNs: got.latencyNs,
  };
}

export function monotonicRace() {
  const tkv = new TensorKVAppliance({ nPages: 32, nBuckets: 16, blockSize: 64 });
  const marker = new TextEncoder().encode("BLOCK-X".padEnd(64, "\0"));
  tkv.put(3, 9, marker);
  tkv.beginEvictKey(3, 9);
  const mid = tkv.get(3, [9]);
  tkv.completeEvictKey(3, 9);
  const after = tkv.get(3, [9]);
  tkv.put(3, 10, new TextEncoder().encode("NEWDATA".padEnd(64, "\0")));
  const stale = new TextDecoder().decode(after.payload).includes("BLOCK-X");
  return {
    recirculated: mid.recirculations > 0 || mid.misses.includes(9),
    postEvictMiss: after.misses.includes(9),
    staleRead: stale,
    monotonic: !stale && after.misses.includes(9),
  };
}

export function prefixActivation() {
  const ctx = new TensorKVContext(new TensorKVAppliance({ nBuckets: 64, nPages: 256, blockSize: 64 }));
  const prefix = Array.from({ length: 48 }, (_, i) => i);
  const ph = promptHash(prefix);
  for (let i = 0; i < 3; i++) ctx.putAsync(1, i, new Uint8Array(8).fill(i));
  ctx.device.publishPrefix(ph, 1, [0, 1, 2]);
  const miss = ctx.probe(promptHash([9, 8, 7]));
  const hit = ctx.probe(ph);
  return {
    firstMiss: !miss.hit,
    secondHit: hit.hit,
    handles: hit.handles,
    ref: hit.refcount,
    hbmAccessed: hit.hbmAccessed,
    paperSetupMs: 18,
    paperRecomputeMs: 1218,
  };
}

export function evictionSensitivity() {
  const rng = new SplitMix64(11n);
  const nPrefix = 32;
  const nUnique = 4;
  const nReqs = 40;
  const working = nPrefix + nReqs * nUnique;
  const out: Record<string, { policy: string; frac: number; hitRate: number; paperHit: number; paperTtft: number }> = {};
  for (const frac of [0.6, 0.8]) {
    const cap = Math.max(nPrefix + 4, Math.floor(working * frac));
    for (const policy of ["lru", "lfru"] as const) {
      const tkv = new TensorKVAppliance({ nBuckets: 256, nPages: cap, storePayloads: false });
      for (let b = 0; b < nPrefix; b++) tkv.put(0, b, undefined, 0xabcn);
      tkv.publishPrefix(0xabcn, 0, Array.from({ length: nPrefix }, (_, i) => i));
      tkv.probe(0xabcn);
      let hits = 0;
      let acc = 0;
      for (let r = 0; r < nReqs; r++) {
        const c = r + 1;
        for (let u = 0; u < nUnique; u++) {
          let res = tkv.put(c, u);
          let tries = 0;
          while (!res.ok && tries < 8) {
            tkv.reclaim(1, policy);
            res = tkv.put(c, u);
            tries++;
          }
        }
        for (let a = 0; a < 20; a++) {
          acc++;
          const g = rng.nextFloat() < 0.7 ? tkv.get(0, [rng.randint(0, nPrefix - 1)]) : tkv.get(c, [rng.randint(0, nUnique - 1)]);
          if (g.ok) hits++;
        }
      }
      const hitRate = (100 * hits) / Math.max(1, acc);
      const paper = PAPER_EVICTION[policy];
      out[`${policy}_${Math.round(frac * 100)}`] = {
        policy,
        frac,
        hitRate: Number(hitRate.toFixed(2)),
        paperHit: frac < 0.7 ? paper.hit_60 : paper.hit_80,
        paperTtft: frac < 0.7 ? paper.ttft_60 : paper.ttft_80,
      };
    }
  }
  return out;
}

export function isolationExperiment() {
  return (["fifo", "qos", "pacing", "both"] as const).map((p) => simulateNoisyNeighbor(p));
}

export function paperTables() {
  return { ttft: PAPER_TTFT, tbt: PAPER_TBT, eviction: PAPER_EVICTION };
}

export function runAllExperiments() {
  return {
    occupancy: occupancySweep(),
    scatterGather: scatterGatherRtts(),
    monotonic: monotonicRace(),
    prefix: prefixActivation(),
    eviction: evictionSensitivity(),
    isolation: isolationExperiment().map((r) => ({
      policy: r.policy,
      p50: Number(r.p50.toFixed(2)),
      p99: Number(r.p99.toFixed(2)),
      drops: r.drops,
      interferenceP99: Number(r.interferenceP99.toFixed(2)),
      series: r.series.filter((_, i) => i % 4 === 0),
    })),
    paper: paperTables(),
  };
}
