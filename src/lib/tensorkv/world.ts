/** Discrete-event clock used by libtkv and overlapping GET/EVICT. */

type Scheduled = { atNs: number; seq: number; fn: () => void };

export class World {
  nowNs = 0;
  private seq = 0;
  private heap: Scheduled[] = [];

  advance(ns: number) {
    this.nowNs += Math.max(0, Math.trunc(ns));
  }

  after(delayNs: number, fn: () => void) {
    this.seq += 1;
    this.heap.push({ atNs: this.nowNs + Math.max(0, Math.trunc(delayNs)), seq: this.seq, fn });
    this.heap.sort((a, b) => a.atNs - b.atNs || a.seq - b.seq);
  }

  runUntil(tNs: number) {
    while (this.heap.length && this.heap[0].atNs <= tNs) {
      const ev = this.heap.shift()!;
      this.nowNs = Math.max(this.nowNs, ev.atNs);
      ev.fn();
    }
    if (tNs > this.nowNs) this.nowNs = tNs;
  }

  drain() {
    while (this.heap.length) {
      const ev = this.heap.shift()!;
      this.nowNs = Math.max(this.nowNs, ev.atNs);
      ev.fn();
    }
  }
}
