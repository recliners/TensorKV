import { TensorKVAppliance, TensorKVContext } from "./appliance";
import { runBaselineSuite, ttftSim } from "./baselines";
import { CuckooTable } from "./core";
import { INLINE_IDS, decodeDescriptor, encodeRequest, getDescriptor } from "./descriptor";
import { PagedEngine } from "./engine";
import { ServingScheduler } from "./scheduler";
import { runServing } from "./serve";
import { SGLangEngine } from "./sglang";
import {
  DEFAULT_CREDIT_GBPS,
  EVAL_EVICTION,
  EVAL_TBT,
  EVAL_TTFT,
  FAST_PATH_HBM_HIT_NS,
  FAST_PATH_SRAM_HIT_NS,
  HANDLE_INSTALL_NS,
  HAZARD_RECIRC_NS,
  LINK_GBPS,
  ShareGPTWorkload,
  SLOW_PATH_CUCKOO_NS,
  SplitMix64,
  ZIPF_ALPHA,
  ZipfSampler,
  packKey,
  promptHash,
} from "./types";
import { VirtualOutputQueues, simulateAttentionIncast, simulateNoisyNeighbor } from "./transport";

export { promptHash };

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
    const head = new ZipfSampler(Math.max(1, live.length), ZIPF_ALPHA, new SplitMix64(100n)).empiricalHeadShare(
      2000,
      Math.max(1, Math.floor(live.length / 20)),
    );
    return {
      load,
      fillSlowInsertRate: fillSlow,
      churnSlowInsertRate: churnFast + churnSlow ? churnSlow / (churnFast + churnSlow) : 0,
      slowInsertRate: fillTotal + churnFast + churnSlow ? tkv.table.slowInserts / (tkv.table.fastInserts + tkv.table.slowInserts) : 0,
      hazardRate: tkv.scoreboard.hazardRate,
      victimBuffer: tkv.table.victimBuffer.size,
      kicks: tkv.table.kicks - kicks0,
      throughputKeep: extraNs ? idealNs / (idealNs + extraNs) : 1,
      extraNs,
      idealNs,
      serviceNs: idealNs + extraNs,
      zipfHeadShare: head,
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

export function prefixActivation(nPrefixTokens = 256) {
  const nPages = Math.max(256, Math.floor(nPrefixTokens / 16) * 4 + 64);
  const nBuckets = Math.max(64, Math.floor(nPages / 2));
  const engine = new PagedEngine(
    new TensorKVContext(new TensorKVAppliance({ nPages, nBuckets, storePayloads: false })),
  );
  const prefix = Array.from({ length: nPrefixTokens }, (_, i) => i);
  const first = engine.submit(1, [...prefix, 1000, 1001], prefix);
  const second = engine.submit(2, [...prefix, 2000, 2001], prefix);
  const third = engine.submit(3, Array.from({ length: 100 }, (_, i) => 300 + i), Array.from({ length: 60 }, (_, i) => 300 + i));
  const hitRef = ttftSim("tensorkv", nPrefixTokens);
  const missRef = ttftSim("recompute", nPrefixTokens);
  return {
    nPrefixTokens,
    firstMiss: !first.prefixHit,
    secondHit: second.prefixHit,
    firstPrefixHit: first.prefixHit,
    secondPrefixHit: second.prefixHit,
    thirdPrefixHit: third.prefixHit,
    handles: second.prefixBlocks,
    ref: 0,
    hbmAccessed: false,
    skippedTokens: engine.stats.skippedPrefillTokens,
    engineHits: engine.stats.prefixHits,
    engineMisses: engine.stats.prefixMisses,
    firstTtftMs: Number(first.ttftMs.toFixed(4)),
    secondTtftMs: Number(second.ttftMs.toFixed(4)),
    ttftGapMs: Number((first.ttftMs - second.ttftMs).toFixed(4)),
    firstTtftParts: {
      setup: Number(first.ttftSetupMs.toFixed(4)),
      fetch: Number(first.ttftFetchMs.toFixed(4)),
      compute: Number(first.ttftComputeMs.toFixed(4)),
    },
    secondTtftParts: {
      setup: Number(second.ttftSetupMs.toFixed(4)),
      fetch: Number(second.ttftFetchMs.toFixed(4)),
      compute: Number(second.ttftComputeMs.toFixed(4)),
    },
    evalSetupMs: 18 * (nPrefixTokens / 32768),
    evalRecomputeMs: 1200 * (nPrefixTokens / 32768),
    composedHitMs: hitRef.totalMs,
    composedMissMs: missRef.totalMs,
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
  const packetsEach = 64;
  for (let r = 0; r < packetsEach; r++) {
    for (let t = 0; t < nTenants; t++) {
      voq.enqueue({ readyAt: r, opcode: "GET", contextId: t, size: 4096, seq: seq++, tenant: "" });
    }
    voq.enqueue({ readyAt: r, opcode: "PUT", contextId: 100, size: 4096, seq: seq++, tenant: "put" });
  }
  const served = new Map<number, number>();
  let putsWhileGets = 0;
  const totalGets = nTenants * packetsEach;
  let getsDone = 0;
  while (voq.pending()) {
    const pkt = voq.dequeue();
    if (!pkt) break;
    served.set(pkt.contextId, (served.get(pkt.contextId) ?? 0) + pkt.size);
    if (pkt.opcode === "PUT" && getsDone < totalGets) putsWhileGets += 1;
    if (pkt.opcode === "GET") getsDone += 1;
  }
  const bytes = [0, 1, 2, 3].map((t) => served.get(t) ?? 0);
  const sum = bytes.reduce((a, b) => a + b, 0);
  const sq = bytes.reduce((a, b) => a + b * b, 0);
  const jain = sq ? (sum * sum) / (nTenants * sq) : 0;
  const mn = Math.min(...bytes);
  const mx = Math.max(...bytes);
  return {
    bytesPerTenant: bytes,
    jainFairness: Number(jain.toFixed(4)),
    preemptions: voq.preemptions,
    maxMinRatio: mn ? mx / mn : Infinity,
    putBytes: served.get(100) ?? 0,
    getsFinishBeforePut: putsWhileGets === 0,
  };
}

export function creditVsGemvSweep(gemvGbps = DEFAULT_CREDIT_GBPS, credits = [10, 20, 40, 80, 100]) {
  return credits.map((c) => {
    const blast = simulateAttentionIncast({ paced: false, gemvGbps });
    const paced = simulateAttentionIncast({ paced: true, creditGbps: c, gemvGbps });
    return {
      creditGbps: c,
      gemvGbps,
      linkGbps: LINK_GBPS,
      pacedDrops: paced.drops,
      blastDrops: blast.drops,
      gpuDrops: paced.gpuDrops,
      pacedArrivalGbps: paced.arrivalGbps,
      overflowBytes: paced.overflowBytes,
      gpuOverflowBytes: paced.gpuOverflowBytes,
      creditMatchesDrain: Math.abs(c - gemvGbps) < 1e-6,
      overCredit: c > gemvGbps + 1e-6,
    };
  });
}

export function fingerprintAndVictim(nBuckets = 64, fill = 0.97) {
  const table = new CuckooTable(nBuckets);
  const cap = table.capacity;
  const n = Math.floor(cap * fill);
  for (let i = 0; i < n; i++) table.insert(packKey(1, i), i);
  for (let i = 0; i < n; i++) {
    if (table.lookup(packKey(1, i)) !== i) throw new Error(`fingerprint lookup missed ${i}`);
  }
  let extraSlow = 0;
  for (let i = n; i < n + Math.floor(cap / 8); i++) {
    if (table.insert(packKey(2, i), i) === "slow") extraSlow += 1;
  }
  return {
    nBuckets,
    capacity: cap,
    fill,
    size: table.size,
    slowInserts: table.slowInserts,
    fastInserts: table.fastInserts,
    kicks: table.kicks,
    tagCollisions: table.tagCollisions,
    hbmKeyVerifies: table.hbmKeyVerifies,
    victimBuffer: table.victimBuffer.size,
    occupancyHist: table.occupancyHistogram(),
    extraSlow,
    sramSlotFields: ["fingerprint", "phys"] as const,
  };
}

export function sglangRadix() {
  const eng = new SGLangEngine();
  const prefix = Array.from({ length: 64 }, (_, i) => i);
  const leaf = eng.insertPrefix(prefix);
  const second = eng.activate([...prefix, 7, 8, 9]);
  return {
    leafBlocks: leaf.blockIds.length,
    secondPrefixHit: second.prefixHit,
    probeHits: eng.tkv.device.prefix.hits,
    skippedTokens: eng.paged.stats.skippedPrefillTokens,
    leaves: eng.leaves.size,
  };
}

export function mixedTrace(nContexts = 12, blocksPerCtx = 16, nOps = 240, seed = 11) {
  const rng = new SplitMix64(BigInt(seed));
  const tkv = new TensorKVAppliance({
    nBuckets: 64,
    nPages: nContexts * blocksPerCtx + 64,
    storePayloads: false,
    seed,
  });
  for (let ctx = 0; ctx < nContexts; ctx++) {
    for (let bid = 0; bid < blocksPerCtx; bid++) tkv.put(ctx, bid);
    tkv.publishPrefix(promptHash([...Array.from({ length: 8 }, (_, i) => i), ctx]), ctx, Array.from({ length: 8 }, (_, i) => i));
  }
  const sampler = new ZipfSampler(nContexts * blocksPerCtx, ZIPF_ALPHA, rng);
  const share = new ShareGPTWorkload({ nPrefixes: 6, nSessions: 16, prefixBlocks: 8, uniqueBlocks: 2, seed: seed + 3 });
  let gets = 0,
    puts = 0,
    probes = 0,
    evicts = 0,
    hits = 0,
    misses = 0;
  for (let i = 0; i < nOps; i++) {
    const u = rng.nextFloat();
    if (u < 0.55) {
      const flat = sampler.sampleIndex();
      const ctx = Math.floor(flat / blocksPerCtx);
      const bid = flat % blocksPerCtx;
      const g = tkv.get(ctx, [bid]);
      gets++;
      hits += g.hits.length;
      misses += g.misses.length;
    } else if (u < 0.7) {
      tkv.put(rng.randint(0, nContexts - 1), blocksPerCtx + rng.randint(0, 3));
      puts++;
    } else if (u < 0.88) {
      const sess = share.sessions[rng.randint(0, share.sessions.length - 1)];
      tkv.probe(share.prefixHash(sess.prefixId));
      probes++;
    } else {
      tkv.evict(rng.randint(0, nContexts - 1), "oldest", 1);
      evicts++;
    }
  }
  const st = tkv.stats();
  return { ops: nOps, gets, puts, probes, evicts, getHits: hits, getMisses: misses, hashLoad: st.hashLoad, pagesUsed: st.pagesUsed, gatheredBytes: st.gatheredBytes };
}

export function sessionTrace(nSessions = 12, nPrefixes = 4, prefixBlocks = 12, uniqueBlocks = 2, seed = 4) {
  const wl = new ShareGPTWorkload({ nPrefixes, nSessions, prefixBlocks, uniqueBlocks, seed });
  const tkv = new TensorKVAppliance({
    nBuckets: 64,
    nPages: wl.workingSetBlocks + 64,
    storePayloads: false,
    seed,
  });
  const prefixCtx = new Map<number, number>();
  let probes = 0,
    hits = 0,
    misses = 0,
    gets = 0,
    puts = 0,
    evicts = 0,
    overflowGets = 0,
    gathered = 0;
  for (const s of wl.sessions) {
    const ph = wl.prefixHash(s.prefixId);
    const pr = tkv.probe(ph);
    probes++;
    if (!pr.hit) {
      const ctx = 1 + s.prefixId;
      prefixCtx.set(s.prefixId, ctx);
      for (let bid = 0; bid < s.prefixBlocks; bid++) {
        tkv.put(ctx, bid);
        puts++;
      }
      tkv.publishPrefix(ph, ctx, Array.from({ length: s.prefixBlocks }, (_, i) => i));
      misses++;
    } else {
      hits++;
      if (!prefixCtx.has(s.prefixId)) prefixCtx.set(s.prefixId, pr.contextId ?? 1 + s.prefixId);
    }
    const uctx = 1000 + s.sessionId;
    for (let i = 0; i < s.uniqueBlocks; i++) {
      tkv.put(uctx, i);
      puts++;
    }
    const ids = Array.from({ length: s.prefixBlocks }, (_, i) => i);
    const { header, overflow } = encodeRequest(getDescriptor(prefixCtx.get(s.prefixId)!, ids));
    const walked = decodeDescriptor(header, overflow);
    if (walked.nBlocks > INLINE_IDS) overflowGets++;
    const g = tkv.get(prefixCtx.get(s.prefixId)!, walked.blockIds);
    gets++;
    gathered += g.gatheredBytes;
    tkv.get(uctx, Array.from({ length: s.uniqueBlocks }, (_, i) => i));
    gets++;
    tkv.evict(uctx, "all");
    evicts++;
  }
  return {
    sessions: nSessions,
    prefixBlocks,
    probes,
    prefixHits: hits,
    prefixMisses: misses,
    gets,
    puts,
    evicts,
    overflowGets,
    gatheredBytes: gathered,
    prefixHitRate: nSessions ? hits / nSessions : 0,
  };
}

export function schedulerBatch() {
  const eng = new PagedEngine(
    new TensorKVContext(new TensorKVAppliance({ nPages: 256, nBuckets: 64, storePayloads: false })),
  );
  const sch = new ServingScheduler(eng);
  const prefix = Array.from({ length: 16 }, (_, i) => i);
  const stats = sch.runBatch(
    [
      { reqId: 1, tokens: [...prefix, 1, 2], prefix },
      { reqId: 2, tokens: [...prefix, 3, 4], prefix },
      { reqId: 3, tokens: Array.from({ length: 20 }, (_, i) => i) },
    ],
    2,
  );
  return {
    submitted: stats.submitted,
    prefixHits: stats.prefixHits,
    decodeSteps: stats.decodeSteps,
    finished: stats.finished,
    meanTtftMs: stats.ttftMs.length ? stats.ttftMs.reduce((a, b) => a + b, 0) / stats.ttftMs.length : 0,
  };
}

export function runAllExperiments() {
  const iso = isolationExperiment();
  const ev = evictionSensitivity();
  const occupancy = occupancySweep();
  const scatterGather = scatterGatherRtts();
  const monotonic = monotonicRace();
  const prefix = prefixActivation();
  const drr = drrFairness();
  const fingerprint = fingerprintAndVictim();
  const sglang = sglangRadix();
  return {
    occupancy,
    scatterGather,
    monotonic,
    prefix,
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
    drr,
    sharegpt: sharegptEviction(0.6),
    sharegpt80: sharegptEviction(0.8),
    getLatency: getLatencyHistogram(),
    fingerprint,
    creditVsGemv: creditVsGemvSweep(),
    sglang,
    replay: mixedTrace(),
    sessionTrace: sessionTrace(),
    scheduler: schedulerBatch(),
    serving: runServing({ nSessions: 12, nPrefixes: 4, prefixBlocks: 10, uniqueBlocks: 2, maxBatch: 4, decodeTokens: 2, nPages: 160, seed: 9 }),
    selfCheck: selfCheck({ prefix, monotonic, scatterGather, eviction: ev, drr, sglang, fingerprint }),
    baselines: runBaselineSuite(),
  };
}

export function selfCheck(snap?: {
  prefix?: ReturnType<typeof prefixActivation>;
  monotonic?: ReturnType<typeof monotonicRace>;
  scatterGather?: ReturnType<typeof scatterGatherRtts>;
  eviction?: ReturnType<typeof evictionSensitivity>;
  drr?: ReturnType<typeof drrFairness>;
  sglang?: ReturnType<typeof sglangRadix>;
  fingerprint?: ReturnType<typeof fingerprintAndVictim>;
}) {
  const failures: string[] = [];
  const prefix = snap?.prefix ?? prefixActivation(256);
  if (prefix.firstPrefixHit) failures.push("first PagedEngine submit should miss the prefix");
  if (!prefix.secondPrefixHit) failures.push("second PagedEngine submit should hit the prefix");
  if (prefix.secondTtftMs >= prefix.firstTtftMs) failures.push("prefix hit TTFT should be below miss TTFT");
  const race = snap?.monotonic ?? monotonicRace();
  if (!race.monotonic) failures.push("GET∥EVICT must be monotonic");
  const sg = snap?.scatterGather ?? scatterGatherRtts(6);
  if (!sg.gatheredOk || sg.tensorkvRtts !== 1) failures.push("scatter-gather should be 1 RTT");
  const ev = snap?.eviction ?? evictionSensitivity();
  if (ev.lfru_60.prefixSurvival < ev.lru_60.prefixSurvival + 40) failures.push("LFRU should keep far more prefix than LRU at 60%");
  const drr = snap?.drr ?? drrFairness();
  if (drr.jainFairness < 0.98 || !drr.getsFinishBeforePut) failures.push("DRR should finish GETs first and stay fair");
  const sgl = snap?.sglang ?? sglangRadix();
  if (!sgl.secondPrefixHit) failures.push("SGLang longest-leaf activate should PROBE-hit");
  const fp = snap?.fingerprint ?? fingerprintAndVictim();
  if (fp.hbmKeyVerifies <= 0) failures.push("fingerprint match must verify the full HBM key");
  return { ok: failures.length === 0, failures };
}
