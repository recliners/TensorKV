import { ATOMIC_COMMIT_CYCLES, CROSSBAR_NOC_CYCLES, FPGA_CYCLE_NS } from "./types";

/** 512-bit shadow-row commit with a one-cycle bank lock. */
export class AtomicCrossbar {
  nowNs = 0;
  lockUntilNs = 0;
  commits = 0;
  lookupStalls = 0;
  stallNs = 0;

  advance(ns: number) {
    this.nowNs += Math.max(0, Math.trunc(ns));
  }

  commit(): number {
    const noc = CROSSBAR_NOC_CYCLES * FPGA_CYCLE_NS;
    const lock = ATOMIC_COMMIT_CYCLES * FPGA_CYCLE_NS;
    this.nowNs += noc;
    this.lockUntilNs = Math.max(this.lockUntilNs, this.nowNs + lock);
    this.commits++;
    return noc + lock;
  }

  release() {
    if (this.nowNs < this.lockUntilNs) this.nowNs = this.lockUntilNs;
  }

  lookupGate(): number {
    if (this.nowNs >= this.lockUntilNs) {
      this.nowNs += FPGA_CYCLE_NS;
      return 0;
    }
    const wait = this.lockUntilNs - this.nowNs;
    this.lookupStalls++;
    this.stallNs += wait;
    this.nowNs = this.lockUntilNs + FPGA_CYCLE_NS;
    return wait;
  }
}
