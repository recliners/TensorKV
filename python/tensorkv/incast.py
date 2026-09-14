"""Synchronized attention incast: 16 sources × 128 KB into one 100GbE GPU.

Paper § The Incast Challenge: one decode step requests 128 KB from 16 storage
shards at the same microsecond. Instantaneous fan-in exceeds the destination
link; shallow ToR buffers overflow unless GET credits shape each source.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import DEFAULT_CREDIT_GBPS, LINK_GBPS
from .timing import serialize_ms

ATTENTION_SOURCES = 16
ATTENTION_BYTES = 128 * 1024
# Per-port slice of a shallow ToR buffer. 128 KB arriving in <1 µs overflows
# this; a 32-packet (~128 KB) slice would absorb the whole attention step.
SHALLOW_BUFFER_PACKETS = 8
PKT_BYTES = 4096
# GPU RX queue sitting behind GEMV drain. Credit above the drain fills this.
GPU_RX_BUFFER_PACKETS = 8


@dataclass
class IncastResult:
    n_sources: int
    total_bytes: int
    per_source_bytes: int
    paced: bool
    credit_gbps: float
    dest_gbps: float
    arrival_gbps: float
    buffer_bytes: int
    overflow_bytes: float
    drops: int
    gpu_overflow_bytes: float
    gpu_drops: int
    dest_busy_us: float
    gemv_drain_us: float


def simulate_attention_incast(
    n_sources: int = ATTENTION_SOURCES,
    total_bytes: int = ATTENTION_BYTES,
    dest_gbps: float = LINK_GBPS,
    credit_gbps: float | None = DEFAULT_CREDIT_GBPS,
    switch_buffer_packets: int = SHALLOW_BUFFER_PACKETS,
    gemv_gbps: float | None = None,
) -> IncastResult:
    """Blast or credit-shape `n_sources` responses toward one destination port.

    Unpaced: every shard transmits at `dest_gbps` at once, so the ToR sees
    ``n_sources * dest_gbps``. Paced: the GET credit is split across sources
    so aggregate arrival equals the GEMV drain (default 40 Gbps).
    """
    per_src = total_bytes / max(1, n_sources)
    paced = credit_gbps is not None
    drain = gemv_gbps if gemv_gbps is not None else (credit_gbps or dest_gbps)
    if paced:
        arrival_gbps = min(float(credit_gbps), dest_gbps)
        # Each source is shaped to arrival/n so they do not line-rate collide.
        src_gbps = arrival_gbps / n_sources
        _ = src_gbps
    else:
        arrival_gbps = n_sources * dest_gbps

    buffer_bytes = switch_buffer_packets * PKT_BYTES
    # Fluid burst: bytes that arrive before the dest serializer can drain them.
    burst_s = serialize_ms(int(per_src), dest_gbps) / 1e3  # ms → s
    arrived = arrival_gbps * 1e9 / 8.0 * burst_s
    drained = dest_gbps * 1e9 / 8.0 * burst_s
    overflow = max(0.0, arrived - drained - buffer_bytes)
    drops = int(overflow / PKT_BYTES) if overflow > 0 else 0
    # Bytes that reach the GPU still have to match GEMV drain. Credit > GEMV
    # does not overflow the ToR (arrival ≤ 100 GbE) but piles up in the RX queue.
    transfer_s = (total_bytes * 8) / (max(arrival_gbps, 1e-9) * 1e9)
    gpu_drained = drain * 1e9 / 8.0 * transfer_s
    gpu_buffer = GPU_RX_BUFFER_PACKETS * PKT_BYTES
    gpu_overflow = max(0.0, total_bytes - gpu_drained - gpu_buffer)
    gpu_drops = int(gpu_overflow / PKT_BYTES) if gpu_overflow > 0 else 0
    dest_busy_us = serialize_ms(total_bytes, dest_gbps) * 1000.0
    gemv_drain_us = serialize_ms(total_bytes, drain) * 1000.0
    return IncastResult(
        n_sources=n_sources,
        total_bytes=total_bytes,
        per_source_bytes=int(per_src),
        paced=paced,
        credit_gbps=float(credit_gbps or 0.0),
        dest_gbps=dest_gbps,
        arrival_gbps=arrival_gbps,
        buffer_bytes=buffer_bytes,
        overflow_bytes=overflow,
        drops=drops,
        gpu_overflow_bytes=gpu_overflow,
        gpu_drops=gpu_drops,
        dest_busy_us=dest_busy_us,
        gemv_drain_us=gemv_drain_us,
    )
