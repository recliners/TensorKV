"""Banked HBM: payloads and full keys live here, not in SRAM slots.

Each SRAM slot stores {32b fingerprint, 32b phys}. Full keys reside in HBM
and are checked on every fingerprint match, so a tag collision cannot alias
another key.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import FPGA_CYCLE_NS

HBM_BANKS = 32
# U280-class: a 4 KB read is a handful of cycles at HBM; paper DMA remainder
# of a 4 KB GET is ~80 ns after the pipeline. One bank conflict waits 1 cycle.
HBM_READ_CYCLES = 20
HBM_WRITE_CYCLES = 20


@dataclass
class HBMPage:
    full_key: int = 0
    payload: bytes | None = None
    valid: bool = False


class BankedHBM:
    def __init__(self, n_pages: int, n_banks: int = HBM_BANKS) -> None:
        if n_pages <= 0:
            raise ValueError("n_pages must be positive")
        self.n_pages = n_pages
        self.n_banks = max(1, n_banks)
        self.pages: list[HBMPage] = [HBMPage() for _ in range(n_pages)]
        self.bank_busy_until: list[int] = [0] * self.n_banks
        self.reads = 0
        self.writes = 0
        self.meta_reads = 0
        self.bank_conflicts = 0

    def bank_of(self, page: int) -> int:
        return page % self.n_banks

    def _acquire(self, page: int, now_ns: int, cycles: int) -> int:
        bank = self.bank_of(page)
        stall = 0
        if now_ns < self.bank_busy_until[bank]:
            stall = self.bank_busy_until[bank] - now_ns
            self.bank_conflicts += 1
        service = cycles * FPGA_CYCLE_NS
        self.bank_busy_until[bank] = now_ns + stall + service
        return stall + service

    def write(self, page: int, key: int, payload: bytes | None, now_ns: int = 0) -> int:
        rec = self.pages[page]
        rec.full_key = key
        rec.payload = payload
        rec.valid = True
        self.writes += 1
        return self._acquire(page, now_ns, HBM_WRITE_CYCLES)

    def read_key(self, page: int, now_ns: int = 0) -> tuple[int | None, int]:
        rec = self.pages[page]
        self.meta_reads += 1
        cost = self._acquire(page, now_ns, HBM_READ_CYCLES)
        if not rec.valid:
            return None, cost
        return rec.full_key, cost

    def read_payload(self, page: int, now_ns: int = 0) -> tuple[bytes | None, int]:
        rec = self.pages[page]
        self.reads += 1
        cost = self._acquire(page, now_ns, HBM_READ_CYCLES)
        if not rec.valid:
            return None, cost
        return rec.payload, cost

    def clear(self, page: int) -> None:
        self.pages[page] = HBMPage()

    def key_of(self, page: int) -> int | None:
        rec = self.pages[page]
        return rec.full_key if rec.valid else None
