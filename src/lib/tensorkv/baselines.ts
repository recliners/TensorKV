import {
  BYTES_PER_TOKEN_LLAMA70B_INT4,
  BYTES_PER_TOKEN_MIXTRAL_FP8,
  COMPUTE_MS_AT_32K,
  HANDLE_INSTALL_NS,
  HBM_CAPACITY_BYTES,
  LINK_GBPS,
  PREFILL_COMPUTE_MS_AT_32K,
  PREFILL_TOKENS,
  TOKENS_PER_BLOCK,
} from "./types";

export const TKV_NETWORK_NS = 800;
export const TKV_RMT_NS = 150;
export const TKV_PCIE_DMA_NS = 1150;
export const RDMA_NETWORK_NS = 2100;
export const RDMA_POINTER_CHASE_NS = 4500;
export const RDMA_SOFT_ALLOC_NS = 6500;
export const RDMA_PCIE_DMA_NS = 1000;
export const RDMA_DIRECT_NS = 6500;
export const DPU_MEDIAN_NS = 3800;
export const RPC_MEDIAN_NS = 4800;
export const TKV_SRAM_HIT_NS = 2150;
export const TKV_HBM_HIT_NS = 2420;
export const PCIE_GEN4_GBPS = 32 * 8;
export const PCIE_GEN5_GBPS = 64 * 8;
export const REF_FETCH_BYTES = 1_000_000_000;
export const A100_LOCAL_KV_BYTES = 25.7e9;
export const REMOTE_TIER_BYTES = HBM_CAPACITY_BYTES;

export const TBT_1GB_MS: Record<string, Record<string, number>> = {
  host_a100: { dma: 32, meta: 165, sync: 3, compute: 10, residual: 0 },
  host_h100: { dma: 16, meta: 145, sync: 3, compute: 10, residual: 6 },
  rdma_opt: { dma: 87, meta: 26, sync: 14, compute: 10, residual: 3 },
  rpc: { dma: 87, meta: 3, sync: 32, compute: 10, residual: 0 },
  dpu: { dma: 87, meta: 21, sync: 2, compute: 10, residual: 0 },
  tensorkv: { dma: 87, meta: 5, sync: 0, compute: 10, residual: 0 },
};

export const DPU_MPPS: Record<number, number> = { 1: 0.15, 2: 0.3, 4: 0.6, 8: 1.2, 16: 1.89, 32: 1.91 };

export const MOE_16WAY_MS: Record<string, Record<string, number>> = {
  host_soft: { router: 3.2, gemm: 18.1, meta: 42.5, dma: 21.0 },
  rdma_soft: { router: 3.2, gemm: 18.1, meta: 12.4, dma: 24.5 },
  tkv_hard: { router: 3.2, gemm: 18.1, meta: 1.2, dma: 24.1 },
};

export const ENERGY_PARTS: Record<string, { compute_w: number; mem_w: number; switch_w: number; tok_s: number }> = {
  host_a100: { compute_w: 480, mem_w: 0, switch_w: 0, tok_s: 320 },
  rpc: { compute_w: 575, mem_w: 320, switch_w: 25, tok_s: 1400 },
  dpu: { compute_w: 585, mem_w: 235, switch_w: 25, tok_s: 1420 },
  tensorkv: { compute_w: 590, mem_w: 328, switch_w: 25, tok_s: 1600 },
};

export function serializeMs(nBytes: number, gbps: number) {
  if (nBytes <= 0 || gbps <= 0) return 0;
  return (nBytes * 8) / gbps / 1e6;
}

export function uncachedLogicalGet(path: string, nBlocks: number) {
  if (path === "tensorkv") {
    return { path, nBlocks, rtts: 1, networkNs: TKV_NETWORK_NS, metaNs: TKV_RMT_NS, dmaNs: TKV_PCIE_DMA_NS, totalNs: 2100 };
  }
  const rtts = 1 + nBlocks;
  const net = RDMA_NETWORK_NS * rtts;
  const meta = RDMA_POINTER_CHASE_NS + RDMA_SOFT_ALLOC_NS;
  const dma = RDMA_PCIE_DMA_NS * nBlocks;
  return { path, nBlocks, rtts, networkNs: net, metaNs: meta, dmaNs: dma, totalNs: net + meta + dma };
}

export function knownAddressUs(path: string) {
  const table: Record<string, number> = {
    sram: TKV_SRAM_HIT_NS / 1e3,
    hbm: TKV_HBM_HIT_NS / 1e3,
    dpu: DPU_MEDIAN_NS / 1e3,
    rpc: RPC_MEDIAN_NS / 1e3,
    rdma: RDMA_DIRECT_NS / 1e3,
  };
  return table[path];
}

export function ttftSim(path: string, prefixTokens = PREFILL_TOKENS, bytesPerToken = BYTES_PER_TOKEN_LLAMA70B_INT4, linkGbps = LINK_GBPS) {
  const payload = prefixTokens * bytesPerToken;
  const nBlocks = Math.max(1, Math.ceil(prefixTokens / TOKENS_PER_BLOCK));
  const scale = prefixTokens / PREFILL_TOKENS;
  if (path === "recompute") {
    const compute = PREFILL_COMPUTE_MS_AT_32K * scale;
    return { path, setupMs: 0, fetchMs: 0, computeMs: compute, totalMs: compute, prefixTokens, payloadBytes: 0 };
  }
  let setup = 0;
  let fetch = 0;
  let compute = 15 * scale;
  if (path === "tensorkv") {
    setup = (TKV_NETWORK_NS + TKV_RMT_NS) / 1e6 + nBlocks * HANDLE_INSTALL_NS / 1e6;
    fetch = serializeMs(payload, linkGbps);
    compute = COMPUTE_MS_AT_32K * scale;
  } else if (path === "host_a100") {
    setup = 420 * scale;
    fetch = serializeMs(payload, PCIE_GEN4_GBPS);
    compute = 20 * scale;
  } else if (path === "host_h100") {
    setup = 400 * scale;
    fetch = serializeMs(payload, PCIE_GEN5_GBPS);
  } else if (path === "rdma_opt") {
    setup = 140 * scale;
    fetch = serializeMs(payload, linkGbps);
  } else if (path === "rpc") {
    setup = 120 * scale;
    fetch = serializeMs(payload, linkGbps);
  } else if (path === "dpu") {
    setup = 80 * scale;
    fetch = serializeMs(payload, linkGbps);
  }
  return { path, setupMs: setup, fetchMs: fetch, computeMs: compute, totalMs: setup + fetch + compute, prefixTokens, payloadBytes: payload };
}

export function tbtSim(path: string, fetchBytes = REF_FETCH_BYTES, linkGbps = LINK_GBPS) {
  const scale = fetchBytes / REF_FETCH_BYTES;
  const parts = TBT_1GB_MS[path];
  let dma: number;
  if (path === "host_a100") dma = serializeMs(fetchBytes, PCIE_GEN4_GBPS);
  else if (path === "host_h100") dma = serializeMs(fetchBytes, PCIE_GEN5_GBPS);
  else dma = parts.dma * scale * (LINK_GBPS / linkGbps);
  const meta = parts.meta * scale;
  const sync = parts.sync * scale;
  const compute = parts.compute * scale;
  const residual = (parts.residual ?? 0) * scale;
  return { path, dmaMs: dma, metaMs: meta, syncMs: sync, computeMs: compute, residualMs: residual, totalMs: dma + meta + sync + compute + residual, payloadBytes: fetchBytes, linkGbps };
}

export function bandwidthSweep(links = [25, 50, 75, 100]) {
  return links.map((gbps) => {
    const tkv = tbtSim("tensorkv", REF_FETCH_BYTES, gbps);
    const rdma = tbtSim("rdma_opt", REF_FETCH_BYTES, gbps);
    return { linkGbps: gbps, tensorkvMs: tkv.totalMs, rdmaOptMs: rdma.totalMs };
  });
}

export function dpuMpps(workers: number) {
  return DPU_MPPS[workers] ?? DPU_MPPS[32];
}

export function dpuGbps(workers: number, pktBytes = 4096) {
  return dpuMpps(workers) * 1e6 * pktBytes * 8 / 1e9;
}

export function dpuWorkerSweep() {
  return [1, 2, 4, 8, 16, 32].map((w) => ({ workers: w, mpps: dpuMpps(w), gbps: dpuGbps(w) }));
}

export function mixtralSharing(topology: "64way" | "16way" | "noshare") {
  const bpt = BYTES_PER_TOKEN_MIXTRAL_FP8;
  let nPrefix: number;
  let nReq: number;
  let pfx: number;
  let sfx: number;
  let overhead: number;
  let hostNoapc: number | null;
  let hostSoft: number | null;
  let rdma: number | null;
  let tkv: number | null;
  let gpuOom: boolean;
  if (topology === "64way") {
    nPrefix = 1;
    nReq = 64;
    pfx = 32768;
    sfx = 512;
    overhead = 5.2 / 4.3;
    hostNoapc = null;
    hostSoft = 88;
    rdma = 78;
    tkv = 52;
    gpuOom = true;
  } else if (topology === "16way") {
    nPrefix = 2;
    nReq = 32;
    pfx = 32768;
    sfx = 512;
    overhead = 6.5 / 5.4;
    hostNoapc = null;
    hostSoft = 85;
    rdma = 58;
    tkv = 47;
    gpuOom = true;
  } else {
    nPrefix = 32;
    nReq = 32;
    pfx = 16384;
    sfx = 512;
    overhead = 1;
    hostNoapc = 105;
    hostSoft = 105;
    rdma = null;
    tkv = null;
    gpuOom = false;
  }
  const logical = nReq * (pfx + sfx) * bpt;
  const unique = (nPrefix * pfx + nReq * sfx) * bpt;
  let remoteNeed: number;
  let fits: boolean;
  if (topology === "noshare") {
    remoteNeed = unique - A100_LOCAL_KV_BYTES;
    fits = remoteNeed <= REMOTE_TIER_BYTES;
  } else {
    remoteNeed = unique * overhead;
    fits = remoteNeed <= REMOTE_TIER_BYTES;
  }
  return {
    topology,
    nPrefix,
    nReq,
    logicalBytes: logical,
    uniqueBytes: unique,
    remoteNeedBytes: remoteNeed,
    fits8GiB: fits,
    gpuGraphOom: gpuOom,
    hostNoapcTbtMs: hostNoapc,
    hostSoftTbtMs: hostSoft,
    rdmaSoftTbtMs: rdma,
    tkvHardTbtMs: tkv,
  };
}

export function moeTbt(path: string) {
  const parts = MOE_16WAY_MS[path];
  const total = Object.values(parts).reduce((a, b) => a + b, 0);
  return { ...parts, total };
}

export function ablationGetP99Us(fastSlow: boolean, zeroCopy: boolean) {
  if (!fastSlow) return 12.4;
  if (!zeroCopy) return 8.5;
  return 2.1;
}

export function ablationTable() {
  return [
    { config: "完整 TensorKV", getP99Us: 2.1, prefixMs: 18 },
    { config: "无前缀去重", getP99Us: 2.1, prefixMs: 1218 },
    { config: "无快慢分流", getP99Us: 12.4, prefixMs: 18 },
    { config: "无 zero-copy DMA", getP99Us: 8.5, prefixMs: 22 },
    { config: "RDMA-Uncached", getP99Us: 14.1, prefixMs: 140 },
  ];
}

export function energySim(path: string) {
  const p = ENERGY_PARTS[path];
  const wall = p.compute_w + p.mem_w + p.switch_w;
  return { path, ...p, wallW: wall, jPerTok: wall / p.tok_s };
}

export function runBaselineSuite() {
  return {
    ttft: (["tensorkv", "host_a100", "host_h100", "rdma_opt", "rpc", "dpu"] as const).map((p) => ttftSim(p)),
    tbt: Object.keys(TBT_1GB_MS).map((p) => tbtSim(p)),
    logicalGet: { tensorkv: uncachedLogicalGet("tensorkv", 8), rdma: uncachedLogicalGet("rdma_uncached", 8) },
    knownAddress: { sram: 2.15, hbm: 2.42, dpu: 3.8, rpc: 4.8, rdma: 6.5 },
    dpuSweep: dpuWorkerSweep(),
    bandwidth: bandwidthSweep(),
    moe: (["64way", "16way", "noshare"] as const).map(mixtralSharing),
    moeTbt: Object.fromEntries(Object.keys(MOE_16WAY_MS).map((k) => [k, moeTbt(k)])),
    ablation: ablationTable(),
    energy: Object.keys(ENERGY_PARTS).map(energySim),
  };
}
