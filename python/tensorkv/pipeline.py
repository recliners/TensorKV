"""RMT fast-path stage timing (paper § Fast Path, Figure latency_breakdown).

Vector block IDs are spatially unrolled across match-action stages. The
measured RMT+scoreboard component of an uncached logical GET is 150 ns.
"""

from __future__ import annotations

from .constants import FPGA_CYCLE_NS, RMT_LOOKUP_NS

PARSER_CYCLES = 2
HASH_CYCLES = 1
SRAM_CYCLES = 2
MATCH_CYCLES = 1
SCOREBOARD_CYCLES = 1
# Paper: recirc adds <80 ns to P99; 20 cycles at 250 MHz = 80 ns.
RECIRC_CYCLES = 20
DMA_DESC_CYCLES = 4


def rmt_lookup_ns(n_block_ids: int) -> int:
    """Unrolled vector lookup. Base 150 ns covers the measured pipe; extra IDs add stages."""
    extra = max(0, n_block_ids - 1)
    per = (HASH_CYCLES + SRAM_CYCLES + MATCH_CYCLES) * FPGA_CYCLE_NS
    return RMT_LOOKUP_NS + extra * per


def recirc_ns() -> int:
    return RECIRC_CYCLES * FPGA_CYCLE_NS


def dma_descriptor_ns(n_hits: int) -> int:
    return DMA_DESC_CYCLES * FPGA_CYCLE_NS + max(0, n_hits) * FPGA_CYCLE_NS
