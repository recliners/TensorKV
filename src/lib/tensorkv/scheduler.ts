import { PagedEngine, type EngineRequest } from "./engine";

export type Arrival = {
  reqId: number;
  tokens: number[];
  prefixTokens?: number[];
  maxNew: number;
  arriveTick: number;
};

export type BatchStats = {
  submitted: number;
  prefills: number;
  prefixHits: number;
  decodeSteps: number;
  finished: number;
  reclaims: number;
  ticks: number;
  peakBatch: number;
  vectorOverflow: number;
  ttftMs: number[];
  tbtMs: number[];
};

export class ServingScheduler {
  engine: PagedEngine;
  maxBatch: number;
  live: number[] = [];
  cursor = 0;
  waiting: Arrival[] = [];
  remaining = new Map<number, number>();
  stats: BatchStats = {
    submitted: 0,
    prefills: 0,
    prefixHits: 0,
    decodeSteps: 0,
    finished: 0,
    reclaims: 0,
    ticks: 0,
    peakBatch: 0,
    vectorOverflow: 0,
    ttftMs: [],
    tbtMs: [],
  };

  constructor(engine?: PagedEngine, maxBatch = 8) {
    this.engine = engine ?? new PagedEngine();
    this.maxBatch = Math.max(1, maxBatch);
  }

  enqueue(arrival: Arrival) {
    this.waiting.push(arrival);
  }

  private ensurePages(need: number) {
    const device = this.engine.tkv.device;
    while (device.allocator.freePages < need) {
      const got = device.reclaim(Math.max(4, need - device.allocator.freePages), "lfru");
      if (!got.length) break;
      this.stats.reclaims += 1;
    }
  }

  admit(reqId: number, tokens: number[], prefixTokens?: number[]): EngineRequest {
    const need = Math.max(1, this.engine.nBlocks(tokens.length));
    this.ensurePages(need);
    if (need > 8) this.stats.vectorOverflow += 1;
    const req = this.engine.submit(reqId, tokens, prefixTokens);
    this.live.push(reqId);
    this.stats.submitted++;
    this.stats.prefills++;
    if (req.prefixHit) this.stats.prefixHits++;
    this.stats.ttftMs.push(req.ttftMs);
    this.stats.peakBatch = Math.max(this.stats.peakBatch, this.live.length);
    return req;
  }

  decodeOne(token = 1): number | null {
    if (!this.live.length) return null;
    const reqId = this.live[this.cursor % this.live.length];
    this.cursor++;
    const tbt = this.engine.decode(reqId, token);
    this.stats.decodeSteps++;
    this.stats.tbtMs.push(tbt);
    return tbt;
  }

  finish(reqId: number, keepPrefix = true) {
    this.live = this.live.filter((id) => id !== reqId);
    this.remaining.delete(reqId);
    this.engine.finish(reqId, keepPrefix);
    this.stats.finished++;
    this.cursor = this.live.length ? this.cursor % this.live.length : 0;
  }

  admitNext(): EngineRequest | null {
    if (!this.waiting.length || this.live.length >= this.maxBatch) return null;
    const a = this.waiting.shift()!;
    const req = this.admit(a.reqId, a.tokens, a.prefixTokens);
    this.remaining.set(a.reqId, a.maxNew);
    return req;
  }

  tick(): "prefill" | "decode" | "idle" {
    this.stats.ticks++;
    if (this.waiting.length && this.live.length < this.maxBatch) {
      this.admitNext();
      return "prefill";
    }
    if (this.live.length) {
      const reqId = this.live[this.cursor % this.live.length];
      this.decodeOne();
      const left = this.remaining.get(reqId);
      if (left === undefined) return "decode";
      if (left - 1 <= 0) this.finish(reqId, true);
      else this.remaining.set(reqId, left - 1);
      return "decode";
    }
    return "idle";
  }

  runArrivals(arrivals: Arrival[], maxTicks = 80_000): BatchStats {
    const pending = [...arrivals].sort((a, b) => a.arriveTick - b.arriveTick || a.reqId - b.reqId);
    let i = 0;
    let tick = 0;
    while (tick < maxTicks) {
      while (i < pending.length && pending[i].arriveTick <= tick) {
        this.enqueue(pending[i]);
        i++;
      }
      if (!this.waiting.length && !this.live.length && i >= pending.length) break;
      if (this.tick() === "idle") {
        tick++;
        continue;
      }
      tick++;
    }
    return this.stats;
  }

  runBatch(prompts: { reqId: number; tokens: number[]; prefix?: number[] }[], decodeSteps = 4): BatchStats {
    for (const p of prompts) this.admit(p.reqId, p.tokens, p.prefix);
    const n = decodeSteps * Math.max(1, prompts.length);
    for (let i = 0; i < n; i++) this.decodeOne(1 + i);
    for (const p of prompts) this.finish(p.reqId, true);
    return this.stats;
  }
}
