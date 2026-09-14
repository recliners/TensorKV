"""Zero-copy crossbar with atomic 512-bit shadow-row commit.

The control core cannot atomically overwrite a 512-bit SRAM bucket with 64-bit
stores. It fills an invisible shadow row, then COMMIT locks the bank for one
FPGA cycle (4 ns at 250 MHz) and swaps the row. Fast-path lookups that land
on a locked bank stall rather than observing a torn bucket.
"""

from __future__ import annotations

from .constants import ATOMIC_COMMIT_CYCLES, CROSSBAR_NOC_CYCLES, FPGA_CYCLE_NS


class AtomicCrossbar:
    def __init__(self) -> None:
        self.now_ns = 0
        self.lock_until_ns = 0
        self.commits = 0
        self.lookup_stalls = 0
        self.stall_ns = 0

    def advance(self, ns: int) -> None:
        self.now_ns += max(0, int(ns))

    def commit(self) -> int:
        """Fill the shadow row over the NoC, then hold a one-cycle bank lock.

        Time is left at the *start* of the lock so a concurrent lookup (the
        next software call before `release`) observes the stall. Returns the
        elapsed NoC + lock duration in ns.
        """
        noc = CROSSBAR_NOC_CYCLES * FPGA_CYCLE_NS
        lock = ATOMIC_COMMIT_CYCLES * FPGA_CYCLE_NS
        self.now_ns += noc
        self.lock_until_ns = max(self.lock_until_ns, self.now_ns + lock)
        self.commits += 1
        return noc + lock

    def release(self) -> None:
        """End the commit cycle (bank visible again)."""
        if self.now_ns < self.lock_until_ns:
            self.now_ns = self.lock_until_ns

    def lookup_gate(self) -> int:
        """Stall a fast-path lookup until the bank lock lifts. Returns stall ns."""
        if self.now_ns >= self.lock_until_ns:
            self.now_ns += FPGA_CYCLE_NS
            return 0
        wait = self.lock_until_ns - self.now_ns
        self.lookup_stalls += 1
        self.stall_ns += wait
        self.now_ns = self.lock_until_ns + FPGA_CYCLE_NS
        return wait
