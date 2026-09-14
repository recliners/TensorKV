"""First-principles timing for the software replica.

FPGA wall-clock numbers from the paper stay in ``constants.PAPER_*``.
These helpers compose serialization delay, SRAM/HBM pipeline time, and
attention compute scaled from the paper's 32K-token 15 ms kernel.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    DEFAULT_CREDIT_GBPS,
    LINK_GBPS,
    PAPER_COMPUTE_MS_AT_32K,
    PAPER_PREFILL_TOKENS,
)


def serialize_ns(n_bytes: int, gbps: float) -> int:
    """Wire serialization time for `n_bytes` at `gbps` gigabits/sec."""
    if n_bytes <= 0 or gbps <= 0:
        return 0
    return int(n_bytes * 8 / gbps)


def serialize_ms(n_bytes: int, gbps: float) -> float:
    return serialize_ns(n_bytes, gbps) / 1e6


def attention_compute_ms(n_tokens: int) -> float:
    """Scale the paper's 15 ms / 32K-token kernel to `n_tokens`."""
    return PAPER_COMPUTE_MS_AT_32K * max(0, n_tokens) / PAPER_PREFILL_TOKENS


@dataclass
class TTFTParts:
    setup_ms: float
    fetch_ms: float
    compute_ms: float
    total_ms: float


def ttft_breakdown(
    *,
    prefix_hit: bool,
    prefix_tokens: int,
    local_tokens: int,
    gathered_bytes: int,
    get_latency_ns: int,
    probe_latency_ns: int,
    credit_gbps: float = DEFAULT_CREDIT_GBPS,
    link_gbps: float = LINK_GBPS,
) -> TTFTParts:
    """Compose TTFT from control RTT + credit-shaped gather + local compute.

    Prefix hit: PROBE returns handles only (no HBM); compute covers the suffix.
    Prefix miss: local prefill of the whole prompt, then gather.
    """
    descriptor = 64
    setup_ms = serialize_ms(descriptor, link_gbps) + probe_latency_ns / 1e6
    if prefix_hit:
        setup_ms += serialize_ms(max(1, prefix_tokens) * 8, link_gbps)
    compute_ms = attention_compute_ms(local_tokens)
    fetch_ms = serialize_ms(gathered_bytes, credit_gbps) + get_latency_ns / 1e6
    total = setup_ms + fetch_ms + compute_ms
    return TTFTParts(setup_ms, fetch_ms, compute_ms, total)
