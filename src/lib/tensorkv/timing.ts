/** Named-part timing: serialization, handle install, prefill vs decode compute. */

import {
  COMPUTE_MS_AT_32K,
  DEFAULT_CREDIT_GBPS,
  HANDLE_INSTALL_NS,
  PREFILL_COMPUTE_MS_AT_32K,
  PREFILL_TOKENS,
  TOKENS_PER_BLOCK,
} from "./types";

export const TKV_NETWORK_NS = 800;
export const TKV_RMT_NS = 150;
export const TKV_PCIE_DMA_NS = 1150;

export function serializeNs(nBytes: number, gbps: number) {
  if (nBytes <= 0 || gbps <= 0) return 0;
  return Math.trunc((nBytes * 8) / gbps);
}

export function serializeMs(nBytes: number, gbps: number) {
  return serializeNs(nBytes, gbps) / 1e6;
}

export function attentionComputeMs(nTokens: number) {
  return (COMPUTE_MS_AT_32K * Math.max(0, nTokens)) / PREFILL_TOKENS;
}

export function prefillComputeMs(nTokens: number) {
  return (PREFILL_COMPUTE_MS_AT_32K * Math.max(0, nTokens)) / PREFILL_TOKENS;
}

export function handleInstallMs(nTokens: number) {
  const nBlocks = Math.max(1, Math.ceil(Math.max(0, nTokens) / TOKENS_PER_BLOCK));
  return (nBlocks * HANDLE_INSTALL_NS) / 1e6;
}

export type TTFTParts = {
  setupMs: number;
  fetchMs: number;
  computeMs: number;
  totalMs: number;
};

export function ttftBreakdown(args: {
  prefixHit: boolean;
  prefixTokens: number;
  localTokens: number;
  gatheredBytes: number;
  getLatencyNs: number;
  probeLatencyNs: number;
  creditGbps?: number;
}): TTFTParts {
  const credit = args.creditGbps ?? DEFAULT_CREDIT_GBPS;
  const contextTokens = args.prefixHit
    ? args.prefixTokens + Math.max(0, args.localTokens)
    : Math.max(args.prefixTokens, args.localTokens);
  let setupMs = args.probeLatencyNs / 1e6;
  let computeMs: number;
  if (args.prefixHit) {
    setupMs += handleInstallMs(args.prefixTokens);
    computeMs = attentionComputeMs(Math.max(contextTokens, args.prefixTokens));
  } else {
    computeMs = prefillComputeMs(Math.max(args.localTokens, args.prefixTokens, 1));
  }
  const fetchMs = serializeMs(args.gatheredBytes, credit) + args.getLatencyNs / 1e6;
  return { setupMs, fetchMs, computeMs, totalMs: setupMs + fetchMs + computeMs };
}
