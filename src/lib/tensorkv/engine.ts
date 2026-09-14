/** Mini PagedAttention serving engine on top of TensorKV. */

import { TensorKVAppliance, TensorKVContext, type GetResult } from "./appliance";
import { attentionComputeMs, serializeMs, ttftBreakdown } from "./timing";
import {
  BYTES_PER_TOKEN_LLAMA70B_INT4,
  DEFAULT_CREDIT_GBPS,
  LINK_GBPS,
  TOKENS_PER_BLOCK,
  promptHash,
} from "./types";

export type EngineRequest = {
  reqId: number;
  tokens: number[];
  prefixLen: number;
  contextId: number;
  prefixOwner: number | null;
  prefixBlocks: number[];
  prefixHit: boolean;
  prefixHash: bigint | null;
  generated: number;
  ttftMs: number;
  ttftSetupMs: number;
  ttftFetchMs: number;
  ttftComputeMs: number;
  tbtMs: number[];
};

export type EngineStats = {
  prefills: number;
  prefixHits: number;
  prefixMisses: number;
  decodeSteps: number;
  tokensGenerated: number;
  bytesPut: number;
  bytesGet: number;
  skippedPrefillTokens: number;
};

function chunks(items: number[], n: number): number[][] {
  if (!items.length) return [];
  const out: number[][] = [];
  for (let i = 0; i < items.length; i += n) out.push(items.slice(i, i + n));
  return out;
}

export class PagedEngine {
  tkv: TensorKVContext;
  tpb: number;
  bytesPerToken: number;
  stats: EngineStats = {
    prefills: 0,
    prefixHits: 0,
    prefixMisses: 0,
    decodeSteps: 0,
    tokensGenerated: 0,
    bytesPut: 0,
    bytesGet: 0,
    skippedPrefillTokens: 0,
  };
  requests = new Map<number, EngineRequest>();
  private nextCtx = 1;

  constructor(
    ctx?: TensorKVContext,
    tokensPerBlock = TOKENS_PER_BLOCK,
    bytesPerToken = BYTES_PER_TOKEN_LLAMA70B_INT4,
  ) {
    this.tkv = ctx ?? new TensorKVContext(new TensorKVAppliance({ storePayloads: false }));
    this.tpb = tokensPerBlock;
    this.bytesPerToken = bytesPerToken;
  }

  logicalBytes(nTokens: number) {
    return Math.max(0, nTokens) * this.bytesPerToken;
  }

  nBlocks(nTokens: number) {
    return Math.ceil(nTokens / this.tpb);
  }

  submit(reqId: number, tokens: number[], prefixTokens?: number[]): EngineRequest {
    const contextId = this.nextCtx++;
    const req: EngineRequest = {
      reqId,
      tokens: [...tokens],
      prefixLen: 0,
      contextId,
      prefixOwner: null,
      prefixBlocks: [],
      prefixHit: false,
      prefixHash: null,
      generated: 0,
      ttftMs: 0,
      ttftSetupMs: 0,
      ttftFetchMs: 0,
      ttftComputeMs: 0,
      tbtMs: [],
    };

    let probeNs = 0;
    let localTokens = tokens.length;
    const prefix = prefixTokens ?? [];
    if (prefix.length) {
      const ph = promptHash(prefix);
      req.prefixHash = ph;
      const probe = this.tkv.probe(ph);
      probeNs = probe.latencyNs;
      req.prefixLen = prefix.length;
      if (probe.hit && probe.contextId !== null) {
        req.prefixHit = true;
        req.prefixOwner = probe.contextId;
        req.prefixBlocks = [...probe.handles];
        this.stats.prefixHits += 1;
        this.stats.skippedPrefillTokens += prefix.length;
        const suffix = tokens.slice(prefix.length);
        req.tokens = [...prefix, ...suffix];
        localTokens = suffix.length;
        for (const [i, chunk] of chunks(suffix, this.tpb).entries()) {
          this.tkv.putAsync(req.contextId, i, Uint8Array.from(chunk.map((b) => b & 0xff)));
          this.stats.bytesPut += this.tkv.device.cfg.blockSize;
        }
      } else {
        this.stats.prefixMisses += 1;
        localTokens = tokens.length;
        this.materialize(req, tokens, ph);
      }
    } else {
      this.materialize(req, tokens, null);
      localTokens = tokens.length;
    }

    this.stats.prefills += 1;
    const fetch = this.gather(req);
    const logical = this.logicalBytes(req.tokens.length);
    this.stats.bytesGet += logical;
    const parts = ttftBreakdown({
      prefixHit: req.prefixHit,
      prefixTokens: req.prefixLen,
      localTokens,
      gatheredBytes: logical,
      getLatencyNs: fetch.latencyNs,
      probeLatencyNs: probeNs,
      creditGbps: LINK_GBPS,
    });
    req.ttftMs = parts.totalMs;
    req.ttftSetupMs = parts.setupMs;
    req.ttftFetchMs = parts.fetchMs;
    req.ttftComputeMs = parts.computeMs;
    this.requests.set(reqId, req);
    return req;
  }

  private materialize(req: EngineRequest, tokens: number[], prefixHashVal: bigint | null) {
    for (const [i, chunk] of chunks(tokens, this.tpb).entries()) {
      this.tkv.putAsync(
        req.contextId,
        i,
        Uint8Array.from(chunk.map((b) => b & 0xff)),
        prefixHashVal ?? undefined,
      );
      this.stats.bytesPut += this.tkv.device.cfg.blockSize;
    }
    if (prefixHashVal !== null) {
      const n = this.nBlocks(req.prefixLen ? req.prefixLen : tokens.length);
      const nPrefixBlocks = req.prefixLen ? this.nBlocks(req.prefixLen) : n;
      this.tkv.device.publishPrefix(prefixHashVal, req.contextId, Array.from({ length: nPrefixBlocks }, (_, i) => i));
    }
  }

  private gather(req: EngineRequest): GetResult {
    const parts: GetResult[] = [];
    if (req.prefixHit && req.prefixOwner !== null && req.prefixBlocks.length) {
      parts.push(this.tkv.getAsync(req.prefixOwner, req.prefixBlocks, 40));
      const suffixTokens = req.tokens.slice(req.prefixLen);
      const nSuffix = suffixTokens.length ? this.nBlocks(suffixTokens.length) : 0;
      if (nSuffix) parts.push(this.tkv.getAsync(req.contextId, Array.from({ length: nSuffix }, (_, i) => i), 40));
    } else {
      const nBlocks = this.nBlocks(req.tokens.length);
      parts.push(this.tkv.getAsync(req.contextId, Array.from({ length: nBlocks }, (_, i) => i), 40));
    }
    const payloadLen = parts.reduce((s, p) => s + p.payload.length, 0);
    const payload = new Uint8Array(payloadLen);
    let off = 0;
    for (const p of parts) {
      payload.set(p.payload, off);
      off += p.payload.length;
    }
    return {
      ok: parts.length ? parts.every((p) => p.ok) : false,
      payload,
      hits: parts.flatMap((p) => p.hits),
      misses: parts.flatMap((p) => p.misses),
      events: parts.flatMap((p) => p.events),
      latencyNs: parts.reduce((s, p) => s + p.latencyNs, 0),
      recirculations: parts.reduce((s, p) => s + p.recirculations, 0),
      gatheredBytes: parts.reduce((s, p) => s + p.gatheredBytes, 0),
    };
  }

  decode(reqId: number, newToken = 1) {
    const req = this.requests.get(reqId);
    if (!req) throw new Error(`unknown request ${reqId}`);
    this.gather(req);
    const logical = this.logicalBytes(req.tokens.length);
    this.stats.bytesGet += logical;
    const tbt = serializeMs(logical, DEFAULT_CREDIT_GBPS) + attentionComputeMs(req.tokens.length) * 0.2;
    req.tokens.push(newToken);
    req.generated += 1;
    const suffixLen = req.tokens.length - req.prefixLen;
    if (suffixLen > 0 && suffixLen % this.tpb === 1) {
      const bid = this.nBlocks(suffixLen) - 1;
      this.tkv.putAsync(req.contextId, bid, Uint8Array.of(newToken & 0xff));
      this.stats.bytesPut += this.tkv.device.cfg.blockSize;
    }
    req.tbtMs.push(tbt);
    this.stats.decodeSteps += 1;
    this.stats.tokensGenerated += 1;
    return tbt;
  }

  finish(reqId: number, keepPrefix = true) {
    const req = this.requests.get(reqId);
    if (!req) return;
    this.requests.delete(reqId);
    if (req.prefixHit && req.prefixHash !== null) this.tkv.device.releasePrefix(req.prefixHash);
    if (keepPrefix && req.prefixHit) {
      this.tkv.evict(req.contextId, "all");
    } else if (keepPrefix && req.prefixLen) {
      const start = this.nBlocks(req.prefixLen);
      const chain = [...(this.tkv.device.contexts.get(req.contextId) ?? [])];
      for (const bid of chain) {
        if (bid >= start) this.tkv.device.evictKey(req.contextId, bid);
      }
    } else {
      this.tkv.evict(req.contextId, "all");
    }
  }
}
