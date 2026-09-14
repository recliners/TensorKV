import {
  DEFAULT_CREDIT_GBPS,
  DRR_QUANTUM_BYTES,
  FAST_PATH_HBM_HIT_NS,
  HIGH_PRIORITY_OPCODES,
  LINK_GBPS,
} from "./types";

export type Packet = {
  readyAt: number;
  opcode: string;
  contextId: number;
  size: number;
  seq: number;
  tenant: string;
};

export class CreditShaper {
  rateGbps: number;
  nextFree = 0;
  packetsShaped = 0;

  constructor(defaultGbps = LINK_GBPS) {
    this.rateGbps = defaultGbps;
  }

  setCredit(gbps: number) {
    this.rateGbps = Math.max(0.1, gbps);
  }

  transmit(sizeBytes: number, now: number, paced = true): [number, number] {
    const bits = sizeBytes * 8;
    const durationUs = bits / (this.rateGbps * 1e3);
    const start = paced ? Math.max(now, this.nextFree) : now;
    const end = start + durationUs;
    this.nextFree = paced ? end : now;
    this.packetsShaped++;
    return [start, end];
  }
}

export class VirtualOutputQueues {
  quantum: number;
  high = new Map<number, Packet[]>();
  low = new Map<number, Packet[]>();
  deficitHigh = new Map<number, number>();
  deficitLow = new Map<number, number>();
  highRr: number[] = [];
  lowRr: number[] = [];
  enqueued = 0;
  dequeuedHigh = 0;
  dequeuedLow = 0;
  preemptions = 0;

  constructor(quantum = DRR_QUANTUM_BYTES) {
    this.quantum = quantum;
  }

  enqueue(pkt: Packet) {
    const isHigh = HIGH_PRIORITY_OPCODES.has(pkt.opcode);
    const table = isHigh ? this.high : this.low;
    const rr = isHigh ? this.highRr : this.lowRr;
    const q = table.get(pkt.contextId) ?? [];
    if (!q.length && !rr.includes(pkt.contextId)) rr.push(pkt.contextId);
    q.push(pkt);
    table.set(pkt.contextId, q);
    this.enqueued++;
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

const PKT_BYTES = 4096;
const RTO_MS = 200;

function packetsPerTick(gbps: number, tickUs: number, pktBytes = PKT_BYTES) {
  const bytesPerUs = (gbps * 1e9) / 8 / 1e6;
  return (bytesPerUs * tickUs) / pktBytes;
}

function percentile(xs: number[], p: number) {
  if (!xs.length) return 0;
  const ys = [...xs].sort((a, b) => a - b);
  const idx = Math.min(ys.length - 1, Math.max(0, Math.round((p / 100) * (ys.length - 1))));
  return ys[idx];
}

export function simulateNoisyNeighbor(
  policy: "fifo" | "qos" | "pacing" | "both",
  durationS = 30,
  tickUs = 50,
  interferenceStartS = 10,
  interferenceEndS = 20,
  switchBufferPackets = 32,
  creditGbps = DEFAULT_CREDIT_GBPS,
  getPeriodUs = 100,
  linkGbps = LINK_GBPS,
): IsolationResult {
  const useQos = policy === "qos" || policy === "both";
  const usePacing = policy === "pacing" || policy === "both";
  const cap = packetsPerTick(linkGbps, tickUs);
  const paced = packetsPerTick(creditGbps, tickUs);
  const nTicks = Math.floor((durationS * 1e6) / tickUs);
  const startI = Math.floor((interferenceStartS * 1e6) / tickUs);
  const endI = Math.floor((interferenceEndS * 1e6) / tickUs);
  const getEvery = Math.max(1, Math.round(getPeriodUs / tickUs));

  let switchQ = 0;
  let drops = 0;
  const victim: number[] = [];
  const interference: number[] = [];
  const series: IsolationPoint[] = [];
  const serial = (PKT_BYTES * 8) / (linkGbps * 1e6);
  const sampleEvery = Math.max(1, Math.floor(nTicks / 400));

  for (let t = 0; t < nTicks; t++) {
    const inBurst = t >= startI && t < endI;
    const putRate = inBurst ? (usePacing ? paced : cap) : 0;
    const getRate = t % getEvery === 0 ? 1 : 0;
    const offered = putRate + getRate;
    const slack = cap + (switchBufferPackets - switchQ);
    const overflow = Math.max(0, offered - slack);
    const admitted = offered - overflow;
    switchQ = Math.min(switchBufferPackets, Math.max(0, switchQ + admitted - cap));
    drops += overflow;
    const getDrop = overflow > 0 ? Math.min(getRate, overflow) : 0;

    if (getRate > 0) {
      let lat: number;
      if (getDrop >= getRate) lat = RTO_MS;
      else {
        const holMs = useQos ? 0 : putRate * (FAST_PATH_HBM_HIT_NS / 1e6);
        lat = serial + holMs;
      }
      victim.push(lat);
      if (inBurst) interference.push(lat);
    }
    if (t % sampleEvery === 0) {
      series.push({ t: (t * tickUs) / 1e6, latency: victim.length ? victim[victim.length - 1] : serial });
    }
  }

  return {
    policy,
    series,
    p50: percentile(victim, 50),
    p99: percentile(victim, 99),
    drops: Math.trunc(drops),
    interferenceP99: percentile(interference.length ? interference : victim, 99),
  };
}

export type IncastResult = {
  nSources: number;
  totalBytes: number;
  paced: boolean;
  arrivalGbps: number;
  destGbps: number;
  drops: number;
  overflowBytes: number;
};

export function simulateAttentionIncast(opts?: { paced?: boolean; creditGbps?: number }): IncastResult {
  const nSources = 16;
  const totalBytes = 128 * 1024;
  const destGbps = LINK_GBPS;
  const paced = opts?.paced ?? true;
  const creditGbps = opts?.creditGbps ?? DEFAULT_CREDIT_GBPS;
  const arrivalGbps = paced ? Math.min(creditGbps, destGbps) : nSources * destGbps;
  const perSrc = totalBytes / nSources;
  const burstS = (perSrc * 8) / destGbps / 1e9;
  const arrived = (arrivalGbps * 1e9) / 8 * burstS;
  const drained = (destGbps * 1e9) / 8 * burstS;
  const bufferBytes = 8 * 4096;
  const overflow = Math.max(0, arrived - drained - bufferBytes);
  return {
    nSources,
    totalBytes,
    paced,
    arrivalGbps,
    destGbps,
    drops: overflow > 0 ? Math.trunc(overflow / 4096) : 0,
    overflowBytes: overflow,
  };
}
