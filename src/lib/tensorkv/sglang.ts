/** SGLang RadixAttention leaf mapped onto TKV_PROBE / TKV_PUT. */

import { PagedEngine, type EngineRequest } from "./engine";
import { TensorKVContext } from "./appliance";
import { promptHash } from "./types";

export type RadixLeaf = {
  promptHash: bigint;
  contextId: number;
  blockIds: number[];
  tokens: number[];
};

export class SGLangEngine {
  tkv: TensorKVContext;
  paged: PagedEngine;
  leaves = new Map<string, RadixLeaf>();

  constructor(tkv?: TensorKVContext) {
    this.tkv = tkv ?? new TensorKVContext();
    this.paged = new PagedEngine(this.tkv);
  }

  insertPrefix(tokens: number[]): RadixLeaf {
    const ph = promptHash(tokens);
    const existing = this.tkv.probe(ph);
    if (existing.hit && existing.contextId !== null) {
      const leaf = { promptHash: ph, contextId: existing.contextId, blockIds: [...existing.handles], tokens: [...tokens] };
      this.leaves.set(ph.toString(), leaf);
      return leaf;
    }
    const req = this.paged.submit(this.leaves.size + 1, tokens, tokens);
    const leaf: RadixLeaf = {
      promptHash: ph,
      contextId: req.contextId,
      blockIds: Array.from({ length: this.paged.nBlocks(tokens.length) }, (_, i) => i),
      tokens: [...tokens],
    };
    this.leaves.set(ph.toString(), leaf);
    return leaf;
  }

  longestLeaf(tokens: number[]): RadixLeaf | null {
    let best: RadixLeaf | null = null;
    for (const leaf of this.leaves.values()) {
      const n = leaf.tokens.length;
      if (!n) continue;
      const prefix = tokens.slice(0, n);
      if (prefix.length === n && prefix.every((t, i) => t === leaf.tokens[i])) {
        if (!best || n > best.tokens.length) best = leaf;
      }
    }
    return best;
  }

  activate(tokens: number[]): EngineRequest {
    const leaf = this.longestLeaf(tokens);
    return this.paged.submit(this.paged.requests.size + 1, tokens, leaf?.tokens);
  }

  decodeRemote(reqId: number, newToken = 1) {
    return this.paged.decode(reqId, newToken);
  }

  finish(reqId: number, keepPrefix = true) {
    this.paged.finish(reqId, keepPrefix);
  }
}
