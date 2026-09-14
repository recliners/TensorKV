import { DRR_QUANTUM_BYTES, SplitMix64 } from "./types";

export type Packet = {
  readyAt: number;
  opcode: "GET" | "PROBE" | "PUT";
  contextId: number;
  size: number;
  seq: number;
  tenant: string;
};

const HIGH = new Set(["GET", "PROBE"]);

export class VirtualOutputQueues {
  quantum: number;
  high = new Map<number, Packet[]>();
  low = new Map<number, Packet[]>();
  deficitHigh = new Map<number, number>();
  deficitLow = new Map<number, number>();
  highRr: number[] = [];
  lowRr: number[] = [];
  dequeuedHigh = 0;
  dequeuedLow = 0;
  preemptions = 0;

  constructor(quantum = DRR_QUANTUM_BYTES) {
    this.quantum = quantum;
  }

  enqueue(pkt: Packet) {
    const isHigh = HIGH.has(pkt.opcode);
    const table = isHigh ? this.high : this.low;
    const rr = isHigh ? this.highRr : this.lowRr;
    const q = table.get(pkt.contextId) ?? [];
    if (!q.length && !rr.includes(pkt.contextId)) rr.push(pkt.contextId);
    q.push(pkt);
    table.set(pkt.contextId, q);
  }

  private drrPop(
    table: Map<number, Packet[]>,
    deficit: Map<number, number>,
    rr: number[],
  ): Packet | null {
    if (!rr.length) return null;
    const limit = rr.length;
    for (let scanned = 0; scanned < limit; scanned++) {
      const ctx = rr[0];
      deficit.set(ctx, (deficit.get(ctx) ?? 0) + this.quantum);
      const q = table.get(ctx);
      if (!q?.length) {
        rr.shift();
        deficit.delete(ctx);
        continue;
      }
      const pkt = q[0];
      if (pkt.size <= (deficit.get(ctx) ?? 0)) {
        q.shift();
        deficit.set(ctx, (deficit.get(ctx) ?? 0) - pkt.size);
        if (!q.length) {
          rr.shift();
          deficit.delete(ctx);
        } else {
          rr.push(rr.shift() as number);
        }
        return pkt;
      }
      rr.push(rr.shift() as number);
    }
    return null;
  }

  dequeue(): Packet | null {
    const hi = this.drrPop(this.high, this.deficitHigh, this.highRr);
    if (hi) {
      this.dequeuedHigh++;
      if (this.lowRr.length) this.preemptions++;
      return hi;
    }
    const lo = this.drrPop(this.low, this.deficitLow, this.lowRr);
    if (lo) this.dequeuedLow++;
    return lo;
  }

  pending() {
    let n = 0;
    for (const q of this.high.values()) n += q.length;
    for (const q of this.low.values()) n += q.length;
    return n;
  }
}

export type IsolationPoint = { t: number; latency: number };
export type IsolationResult = {
  policy: string;
  series: IsolationPoint[];
  p50: number;
  p99: number;
  drops: number;
  interferenceP99: number;
};

function percentile(xs: number[], p: number) {
  if (!xs.length) return 0;
  const ys = [...xs].sort((a, b) => a - b);
  const idx = Math.min(ys.length - 1, Math.max(0, Math.round((p / 100) * (ys.length - 1))));
  return ys[idx];
}

export function simulateNoisyNeighbor(
  policy: "fifo" | "qos" | "pacing" | "both",
  durationS = 30,
  tickMs = 50,
): IsolationResult {
  const rng = new SplitMix64(7n);
  const nTicks = Math.floor((durationS * 1000) / tickMs);
  const startI = Math.floor((10 * 1000) / tickMs);
  const endI = Math.floor((20 * 1000) / tickMs);
  const switchBuf = 32;
  const buffer: string[] = [];
  let drops = 0;
  const victim: number[] = [];
  const interference: number[] = [];
  const series: IsolationPoint[] = [];
  const voq = new VirtualOutputQueues();
  let seq = 0;

  const useQos = policy === "qos" || policy === "both";
  const usePacing = policy === "pacing" || policy === "both";

  for (let t = 0; t < nTicks; t++) {
    const nowMs = t * tickMs;
    const inBurst = t >= startI && t < endI;
    seq++;
    const getPkt: Packet = { readyAt: nowMs, opcode: "GET", contextId: 1, size: 4096, seq, tenant: "A" };
    const arrivals: Packet[] = [getPkt];
    if (inBurst) {
      const nPuts = usePacing ? 8 : 24;
      for (let i = 0; i < nPuts; i++) {
        seq++;
        arrivals.push({ readyAt: nowMs, opcode: "PUT", contextId: 2, size: 4096, seq, tenant: "B" });
      }
    }
    let ordered = arrivals;
    if (useQos) {
      for (const p of arrivals) voq.enqueue(p);
      ordered = [];
      while (voq.pending()) {
        const n = voq.dequeue();
        if (!n) break;
        ordered.push(n);
      }
    }
    for (const p of ordered) {
      if (buffer.length >= switchBuf) {
        drops++;
        if (p.opcode === "GET") {
          const extra = 180 + rng.nextFloat() * 40;
          victim.push(extra);
          if (inBurst) interference.push(extra);
        }
        continue;
      }
      buffer.push(p.opcode);
    }
    const drain = usePacing || useQos ? 2 : 1;
    for (let i = 0; i < drain; i++) buffer.shift();
    const qDepth = buffer.length;
    let lat: number;
    if (policy === "fifo") lat = 2 + qDepth * 6 + (inBurst ? 8 : 0);
    else if (policy === "qos") lat = 2 + Math.min(qDepth, 4) * 2 + (inBurst ? 12 + rng.nextFloat() * 20 : 0);
    else if (policy === "pacing") lat = 2 + qDepth * 0.8 + (inBurst ? 10 + rng.nextFloat() * 4 : 0);
    else lat = 2 + (inBurst ? 1.2 + rng.nextFloat() * 0.4 : 0);
    victim.push(lat);
    if (inBurst) interference.push(lat);
    series.push({ t: nowMs / 1000, latency: lat });
  }

  return {
    policy,
    series,
    p50: percentile(victim, 50),
    p99: percentile(victim, 99),
    drops,
    interferenceP99: percentile(interference, 99),
  };
}
