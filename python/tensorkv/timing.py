"""Named-part timing: serialization, SRAM/HBM pipeline, prefill vs decode compute."""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    COMPUTE_MS_AT_32K,
    DEFAULT_CREDIT_GBPS,
    HANDLE_INSTALL_NS,
    LINK_GBPS,
    PREFILL_COMPUTE_MS_AT_32K,
    PREFILL_TOKENS,
    TOKENS_PER_BLOCK,
)


def serialize_ns(n_bytes: int, gbps: float) -> int:
    """Wire serialization time for `n_bytes` at `gbps` gigabits/sec."""
    if n_bytes <= 0 or gbps <= 0:
        return 0
    return int(n_bytes * 8 / gbps)


def serialize_ms(n_bytes: int, gbps: float) -> float:
    return serialize_ns(n_bytes, gbps) / 1e6


def attention_compute_ms(n_tokens: int) -> float:
    """First-token / decode attention once KV is resident (15 ms @ 32K)."""
    return COMPUTE_MS_AT_32K * max(0, n_tokens) / PREFILL_TOKENS


def prefill_compute_ms(n_tokens: int) -> float:
    """Prompt prefill when the prefix is not cached (1200 ms @ 32K)."""
    return PREFILL_COMPUTE_MS_AT_32K * max(0, n_tokens) / PREFILL_TOKENS


def handle_install_ms(n_tokens: int) -> float:
    n_blocks = max(1, (max(0, n_tokens) + TOKENS_PER_BLOCK - 1) // TOKENS_PER_BLOCK)
    return n_blocks * HANDLE_INSTALL_NS / 1e6


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
    """Compose TTFT from handle install + credit-shaped gather + compute.

    Prefix hit: PROBE returns handles; setup is 18 ms-scaled handle install;
    compute is the resident-KV first-token kernel over the full context.
    Prefix miss: whole-prompt prefill (1200 ms @ 32K), then gather.
    """
    context_tokens = prefix_tokens + max(0, local_tokens) if prefix_hit else max(prefix_tokens, local_tokens)
    setup_ms = probe_latency_ns / 1e6
    if prefix_hit:
        setup_ms += handle_install_ms(prefix_tokens)
        compute_ms = attention_compute_ms(max(context_tokens, prefix_tokens))
    else:
        compute_ms = prefill_compute_ms(max(local_tokens, prefix_tokens, 1))
    fetch_ms = serialize_ms(gathered_bytes, credit_gbps) + get_latency_ns / 1e6
    total = setup_ms + fetch_ms + compute_ms
    return TTFTParts(setup_ms, fetch_ms, compute_ms, total)
