import { FPGA_CYCLE_NS } from "./types";

export const HBM_BANKS = 32;
export const HBM_READ_CYCLES = 20;
export const HBM_WRITE_CYCLES = 20;

export type HBMPage = { fullKey: bigint; payload: Uint8Array | null; valid: boolean };

export class BankedHBM {
  nPages: number;
  nBanks: number;
  pages: HBMPage[];
  bankBusyUntil: number[];
  reads = 0;
  writes = 0;
  metaReads = 0;
  bankConflicts = 0;

  constructor(nPages: number, nBanks = HBM_BANKS) {
    this.nPages = nPages;
    this.nBanks = Math.max(1, nBanks);
    this.pages = Array.from({ length: nPages }, () => ({ fullKey: 0n, payload: null, valid: false }));
    this.bankBusyUntil = Array(this.nBanks).fill(0);
  }

  bankOf(page: number) {
    return page % this.nBanks;
  }

  private acquire(page: number, nowNs: number, cycles: number) {
    const bank = this.bankOf(page);
    let stall = 0;
    if (nowNs < this.bankBusyUntil[bank]) {
      stall = this.bankBusyUntil[bank] - nowNs;
      this.bankConflicts++;
    }
    const service = cycles * FPGA_CYCLE_NS;
    this.bankBusyUntil[bank] = nowNs + stall + service;
    return stall + service;
  }

  write(page: number, key: bigint, payload: Uint8Array | null, nowNs = 0) {
    this.pages[page] = { fullKey: key, payload, valid: true };
    this.writes++;
    return this.acquire(page, nowNs, HBM_WRITE_CYCLES);
  }

  readKey(page: number, nowNs = 0): [bigint | null, number] {
    const rec = this.pages[page];
    this.metaReads++;
    const cost = this.acquire(page, nowNs, HBM_READ_CYCLES);
    return rec.valid ? [rec.fullKey, cost] : [null, cost];
  }

  readPayload(page: number, nowNs = 0): [Uint8Array | null, number] {
    const rec = this.pages[page];
    this.reads++;
    const cost = this.acquire(page, nowNs, HBM_READ_CYCLES);
    return rec.valid ? [rec.payload, cost] : [null, cost];
  }

  clear(page: number) {
    this.pages[page] = { fullKey: 0n, payload: null, valid: false };
  }

  keyOf(page: number): bigint | null {
    const rec = this.pages[page];
    return rec.valid ? rec.fullKey : null;
  }
}
