import { TensorKVAppliance, TensorKVContext } from "./appliance";
import { runBaselineSuite, ttftSim } from "./baselines";
import {
  EVAL_EVICTION,
  EVAL_TBT,
  EVAL_TTFT,
  FAST_PATH_HBM_HIT_NS,
  FAST_PATH_SRAM_HIT_NS,
  HANDLE_INSTALL_NS,
  HAZARD_RECIRC_NS,
  ShareGPTWorkload,
  SLOW_PATH_CUCKOO_NS,
  SplitMix64,
  ZIPF_ALPHA,
  ZipfSampler,
  mix64,
} from "./types";
import { VirtualOutputQueues, simulateAttentionIncast, simulateNoisyNeighbor } from "./transport";

export function promptHash(tokens: number[]): bigint {
  let h = 0x243f6a8885a308d3n;
  for (const t of tokens) h = mix64(h ^ BigInt(t >>> 0));
  return h;
}

export function hashPromptText(s: string): bigint {
  return promptHash(Array.from(s, (c) => c.charCodeAt(0)));
}

function putReclaim(tkv: TensorKVAppliance, ctx: number, bid: number, policy: "lru" | "lfru", prefixHash?: bigint) {
  let res = tkv.put(ctx, bid, undefined, prefixHash);
  let tries = 0;
  while (!res.ok && tries < 48) {
    tkv.reclaim(4, policy);
    res = tkv.put(ctx, bid, undefined, prefixHash);
    tries++;
  }
  return res;
}

export function occupancySweep(loads = [0.5, 0.7, 0.8, 0.9, 0.95]) {
  // Browser table is smaller than the Python report (1024 buckets / 6000 ops)
  // so the page stays interactive; the trend is the same.
  const rng = new SplitMix64(1n);
  const nBuckets = 512;
  const nOps = 3000;
  const cap = nBuckets * 4;
  return loads.map((load) => {
    const tkv = new TensorKVAppliance({ nBuckets, nPages: cap + 256, storePayloads: false, seed: 1 });
    const target = Math.floor(cap * load);
    for (let i = 0; i < target; i++) tkv.put(1, i);
    const fillTotal = tkv.table.fastInserts + tkv.table.slowInserts;
    const fillSlow = fillTotal ? tkv.table.slowInserts / fillTotal : 0;
    const fast0 = tkv.table.fastInserts;
    const slow0 = tkv.table.slowInserts;
    const recirc0 = tkv.scoreboard.recirculations;
    const kicks0 = tkv.table.kicks;
    const live = Array.from({ length: target }, (_, i) => i);
    const sampler = new ZipfSampler(Math.max(1, live.length), ZIPF_ALPHA, rng);
    let nxt = target;
    for (let op = 0; op < nOps; op++) {
      const bid = live[sampler.sampleIndex() % live.length];
      tkv.get(1, [bid]);
      const u = rng.nextFloat();
      if (u < 0.12 && live.length) {
        const vi = rng.randint(0, live.length - 1);
        const victim = live.splice(vi, 1)[0];
        tkv.evictKey(1, victim);
        tkv.put(1, nxt);
        live.push(nxt);
        nxt++;
      } else if (u < 0.16 && live.length) {
        const vi = rng.randint(0, live.length - 1);
        const victim = live[vi];
        tkv.beginEvictKey(1, victim);
        tkv.get(1, [victim]);
        tkv.completeEvictKey(1, victim);
        live.splice(vi, 1);
        tkv.put(1, nxt);
        live.push(nxt);
        nxt++;
      }
    }
    const churnFast = tkv.table.fastInserts - fast0;
    const churnSlow = tkv.table.slowInserts - slow0;
    const extraNs =
      (tkv.scoreboard.recirculations - recirc0) * HAZARD_RECIRC_NS + churnSlow * (SLOW_PATH_CUCKOO_NS - FAST_PATH_SRAM_HIT_NS);
    const idealNs = nOps * FAST_PATH_HBM_HIT_NS;
    return {
      load,
      fillSlowInsertRate: fillSlow,
      churnSlowInsertRate: churnFast + churnSlow ? churnSlow / (churnFast + churnSlow) : 0,
      slowInsertRate: fillTotal + churnFast + churnSlow ? tkv.table.slowInserts / (tkv.table.fastInserts + tkv.table.slowInserts) : 0,
      hazardRate: tkv.scoreboard.hazardRate,
      victimBuffer: tkv.table.victimBuffer.size,
      kicks: tkv.table.kicks - kicks0,
      throughputKeep: extraNs ? idealNs / (idealNs + extraNs) : 1,
      bucketHist: tkv.table.occupancyHistogram(),
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
  const midText = new TextDecoder().decode(mid.payload);
  tkv.completeEvictKey(3, 9);
  const after = tkv.get(3, [9]);
  tkv.put(3, 10, new TextEncoder().encode("NEWDATA".padEnd(64, "\0")));
  const stale = new TextDecoder().decode(after.payload).includes("BLOCK-X") || midText.includes("BLOCK-X");
  return {
    recirculated: mid.recirculations > 0,
    missDuring: mid.misses.includes(9),
    noPayloadDuring: !midText.includes("BLOCK-X"),
    postEvictMiss: after.misses.includes(9),
    staleRead: stale,
    monotonic: !stale && after.misses.includes(9) && mid.misses.includes(9) && mid.recirculations > 0,
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
  const composedHit = ttftSim("tensorkv", 32768);
  const composedMiss = ttftSim("recompute", 32768);
  return {
    firstMiss: !miss.hit,
    secondHit: hit.hit,
    handles: hit.handles,
    ref: hit.refcount,
    hbmAccessed: hit.hbmAccessed,
    evalSetupMs: 18,
    evalRecomputeMs: 1200,
    composedHitMs: composedHit.totalMs,
    composedMissMs: composedMiss.totalMs,
    ttftGapMs: composedMiss.totalMs - composedHit.totalMs,
    handleInstallNs: HANDLE_INSTALL_NS,
  };
}

export function evictionSensitivity() {
  const rng = new SplitMix64(11n);
  const nPrefix = 80;
  const nUnique = 3;
  const nReqs = 20;
  const working = nPrefix + nReqs * nUnique;
  const prefixHash = 0xabc00n;
  const out: Record<
    string,
    {
      policy: string;
      frac: number;
      hitRate: number;
      prefixSurvival: number;
      weightedTtft: number;
      evalHit: number;
      evalTtft: number;
    }
  > = {};
  for (const frac of [0.6, 0.8]) {
    const cap = Math.max(nPrefix + 4, Math.floor(working * frac));
    for (const policy of ["lru", "lfru"] as const) {
      const tkv = new TensorKVAppliance({
        nBuckets: Math.max(64, Math.floor((cap + 3) / 2)),
        nPages: cap,
        storePayloads: false,
        seed: 11,
      });
      for (let b = 0; b < nPrefix; b++) tkv.put(0, b, undefined, prefixHash);
      tkv.publishPrefix(prefixHash, 0, Array.from({ length: nPrefix }, (_, i) => i));
      for (let i = 0; i < 8; i++) tkv.probe(prefixHash);
      const uniqueKeys: [number, number][] = [];
      for (let r = 0; r < nReqs; r++) {
        const c = r + 1;
        for (let u = 0; u < nUnique; u++) {
          const res = putReclaim(tkv, c, u, policy);
          if (res.ok) uniqueKeys.push([c, u]);
        }
      }
      const pref = tkv.get(0, Array.from({ length: nPrefix }, (_, i) => i));
      const survival = (100 * (nPrefix - pref.misses.length)) / nPrefix;
      let hits = 0;
      let acc = 0;
      for (let a = 0; a < 800; a++) {
        acc++;
        if (rng.nextFloat() < 0.7) {
          const g = tkv.get(0, [rng.randint(0, nPrefix - 1)]);
          if (g.ok) hits++;
        } else if (uniqueKeys.length) {
          const [c, u] = uniqueKeys[rng.randint(0, uniqueKeys.length - 1)];
          if (tkv.get(c, [u]).ok) hits++;
        }
      }
      const hitRate = (100 * hits) / Math.max(1, acc);
      const evalRow = EVAL_EVICTION[policy];
      const evalHit = frac < 0.7 ? evalRow.hit_60 : evalRow.hit_80;
      const evalTtft = frac < 0.7 ? evalRow.ttft_60 : evalRow.ttft_80;
      const weightedTtft = (survival / 100) * EVAL_TTFT.tensorkv.total + (1 - survival / 100) * EVAL_TTFT.recompute.total;
      out[`${policy}_${Math.round(frac * 100)}`] = {
        policy,
        frac,
        hitRate: Number(hitRate.toFixed(2)),
        prefixSurvival: Number(survival.toFixed(2)),
        weightedTtft: Number(weightedTtft.toFixed(1)),
        evalHit,
        evalTtft,
      };
    }
  }
  return out;
}

export function isolationExperiment() {
  return (["fifo", "qos", "pacing", "both"] as const).map((p) => simulateNoisyNeighbor(p));
}

export function sharegptEviction(capacityFrac = 0.6, seed = 23) {
  const wl = new ShareGPTWorkload({ seed });
  const cap = Math.max(wl.prefixBlocks + wl.uniqueBlocks, Math.floor(wl.workingSetBlocks * capacityFrac));
  const out: Record<
    "lru" | "lfru",
    { policy: string; prefixSurvival: number; hitRate: number; capacityBlocks: number; workingSet: number }
  > = {
    lru: { policy: "lru", prefixSurvival: 0, hitRate: 0, capacityBlocks: cap, workingSet: wl.workingSetBlocks },
    lfru: { policy: "lfru", prefixSurvival: 0, hitRate: 0, capacityBlocks: cap, workingSet: wl.workingSetBlocks },
  };
  for (const policy of ["lru", "lfru"] as const) {
    const tkv = new TensorKVAppliance({
      nBuckets: Math.max(128, cap),
      nPages: cap,
      storePayloads: false,
      seed,
    });
    for (let pid = 0; pid < wl.nPrefixes; pid++) {
      const ctx = 1000 + pid;
      const ph = wl.prefixHash(pid);
      for (let b = 0; b < wl.prefixBlocks; b++) putReclaim(tkv, ctx, b, policy, ph);
      tkv.publishPrefix(ph, ctx, Array.from({ length: wl.prefixBlocks }, (_, i) => i));
      tkv.probe(ph);
      tkv.probe(ph);
    }
    for (const sess of wl.sessions) tkv.probe(wl.prefixHash(sess.prefixId));
    for (const sess of wl.sessions) {
      for (let u = 0; u < sess.uniqueBlocks; u++) putReclaim(tkv, sess.contextId, u, policy);
    }
    const rng = new SplitMix64(BigInt(seed + 1));
    let prefixLive = 0;
    for (let pid = 0; pid < wl.nPrefixes; pid++) {
      const g = tkv.get(1000 + pid, Array.from({ length: wl.prefixBlocks }, (_, i) => i));
      prefixLive += wl.prefixBlocks - g.misses.length;
    }
    const survival = (100 * prefixLive) / Math.max(1, wl.nPrefixes * wl.prefixBlocks);
    let mixedHits = 0;
    let mixedN = 0;
    for (let i = 0; i < 600; i++) {
      mixedN++;
      const sess = wl.sessions[rng.randint(0, wl.sessions.length - 1)];
      const g =
        rng.nextFloat() < 0.65
          ? tkv.get(1000 + sess.prefixId, [rng.randint(0, wl.prefixBlocks - 1)])
          : tkv.get(sess.contextId, [rng.randint(0, sess.uniqueBlocks - 1)]);
      if (g.ok) mixedHits++;
    }
    out[policy] = {
      policy,
      prefixSurvival: Number(survival.toFixed(2)),
      hitRate: Number(((100 * mixedHits) / Math.max(1, mixedN)).toFixed(2)),
      capacityBlocks: cap,
      workingSet: wl.workingSetBlocks,
    };
  }
  return {
    ...out,
    lfruMinusLruSurvival: Number((out.lfru.prefixSurvival - out.lru.prefixSurvival).toFixed(2)),
  };
}

function summaryNs(xs: number[]) {
  if (!xs.length) return { n: 0, mean: 0, p50: 0, p90: 0, p99: 0, max: 0 };
  const ys = [...xs].sort((a, b) => a - b);
  const at = (p: number) => ys[Math.min(ys.length - 1, Math.max(0, Math.round((p / 100) * (ys.length - 1))))];
  return {
    n: xs.length,
    mean: xs.reduce((a, b) => a + b, 0) / xs.length,
    p50: at(50),
    p90: at(90),
    p99: at(99),
    max: ys[ys.length - 1],
  };
}

export function getLatencyHistogram(nKeys = 256, nOps = 800, seed = 3) {
  const rng = new SplitMix64(BigInt(seed));
  const tkv = new TensorKVAppliance({ nBuckets: 256, nPages: nKeys + 64, storePayloads: false, seed });
  const tkvSlow = new TensorKVAppliance({
    nBuckets: 256,
    nPages: nKeys + 64,
    storePayloads: false,
    fastSlowSplit: false,
    seed: seed + 1,
  });
  for (let i = 0; i < nKeys; i++) {
    tkv.put(1, i);
    tkvSlow.put(1, i);
  }
  const sampler = new ZipfSampler(nKeys, ZIPF_ALPHA, rng);
  const samples: number[] = [];
  const slowSamples: number[] = [];
  const lo = 1000;
  const hi = 20000;
  const nBins = 24;
  const counts = Array(nBins).fill(0);
  const width = (hi - lo) / nBins;
  const kinds = { hit: 0, gather: 0, miss: 0, hazard: 0 };
  for (let i = 0; i < nOps; i++) {
    const u = rng.nextFloat();
    let g;
    if (u < 0.05) {
      const vic = rng.randint(0, nKeys - 1);
      tkv.beginEvictKey(1, vic);
      g = tkv.get(1, [vic]);
      tkv.completeEvictKey(1, vic);
      tkv.put(1, vic);
      kinds.hazard++;
    } else if (u < 0.12) {
      g = tkv.get(1, [nKeys + rng.randint(0, 50)]);
      kinds.miss++;
    } else if (u < 0.28) {
      g = tkv.get(1, Array.from({ length: 8 }, () => sampler.sampleIndex()));
      kinds.gather++;
    } else {
      g = tkv.get(1, [sampler.sampleIndex()]);
      kinds.hit++;
    }
    samples.push(g.latencyNs);
    if (g.latencyNs < lo) {
      /* underflow */
    } else if (g.latencyNs >= hi) {
      /* overflow */
    } else {
      counts[Math.min(nBins - 1, Math.floor((g.latencyNs - lo) / width))]++;
    }
    slowSamples.push(tkvSlow.get(1, [sampler.sampleIndex()]).latencyNs);
  }
  const fast = summaryNs(samples);
  const slow = summaryNs(slowSamples);
  return {
    summaryNs: fast,
    slowPathSummaryNs: slow,
    histogram: counts.map((count, i) => ({ lo: lo + i * width, hi: lo + (i + 1) * width, count })),
    kinds,
    nOps,
  };
}

export function evalTables() {
  return { ttft: EVAL_TTFT, tbt: EVAL_TBT, eviction: EVAL_EVICTION };
}

export function drrFairness() {
  const voq = new VirtualOutputQueues(16384);
  let seq = 0;
  const nTenants = 4;
  const packetsEach = 32;
  for (let r = 0; r < packetsEach; r++) {
    for (let t = 0; t < nTenants; t++) {
      voq.enqueue({ readyAt: r, opcode: "GET", contextId: t, size: 4096, seq: seq++, tenant: "" });
    }
    voq.enqueue({ readyAt: r, opcode: "PUT", contextId: 100, size: 4096, seq: seq++, tenant: "put" });
  }
  const served = new Map<number, number>();
  while (voq.pending()) {
    const pkt = voq.dequeue();
    if (!pkt) break;
    served.set(pkt.contextId, (served.get(pkt.contextId) ?? 0) + pkt.size);
  }
  const bytes = [0, 1, 2, 3].map((t) => served.get(t) ?? 0);
  const sum = bytes.reduce((a, b) => a + b, 0);
  const sq = bytes.reduce((a, b) => a + b * b, 0);
  const jain = sq ? (sum * sum) / (nTenants * sq) : 0;
  return { bytesPerTenant: bytes, jainFairness: Number(jain.toFixed(4)), preemptions: voq.preemptions };
}

export function runAllExperiments() {
  const iso = isolationExperiment();
  const ev = evictionSensitivity();
  return {
    occupancy: occupancySweep(),
    scatterGather: scatterGatherRtts(),
    monotonic: monotonicRace(),
    prefix: prefixActivation(),
    eviction: ev,
    isolation: iso.map((r) => ({
      policy: r.policy,
      p50: Number(r.p50.toFixed(2)),
      p99: Number(r.p99.toFixed(2)),
      drops: r.drops,
      getDrops: r.getDrops,
      putDrops: r.putDrops,
      interferenceP99: Number(r.interferenceP99.toFixed(2)),
      interferenceP50: Number(r.interferenceP50.toFixed(2)),
      quietP99: Number(r.quietP99.toFixed(2)),
      series: r.series.filter((_, i) => i % 4 === 0),
    })),
    evalTables: evalTables(),
    incast: {
      blast: simulateAttentionIncast({ paced: false }),
      paced: simulateAttentionIncast({ paced: true }),
    },
    drr: drrFairness(),
    sharegpt: sharegptEviction(0.6),
    sharegpt80: sharegptEviction(0.8),
    getLatency: getLatencyHistogram(),
    baselines: runBaselineSuite(),
  };
}
