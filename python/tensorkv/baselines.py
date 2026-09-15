"""Runnable models of the evaluated offload paths.

These are not A100 wall-clock measurements. Each path composes named parts:

  * payload serialization at a named link (PCIe Gen4/5 or 100GbE)
  * the metadata/request/completion intervals from the 1.0 GB decode step
  * the uncached logical-GET breakdown
  * Mixtral-8x7B sharing topologies
  * DPU-DPA worker sweep, energy table, and GET ablation

Eval tables live in ``constants.EVAL_*``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    BYTES_PER_TOKEN_LLAMA70B_INT4,
    BYTES_PER_TOKEN_MIXTRAL_FP8,
    COMPUTE_MS_AT_32K,
    HANDLE_INSTALL_NS,
    HBM_CAPACITY_BYTES,
    LINK_GBPS,
    PREFILL_COMPUTE_MS_AT_32K,
    PREFILL_TOKENS,
    TOKENS_PER_BLOCK,
)
from .dpu import dpu_gbps, dpu_mpps, dpu_worker_sweep
from .measure import cached_get_p99_us
from .timing import handle_install_ms, prefill_compute_ms, serialize_ms

# Paper Figure micro_perf / latency_breakdown (nanoseconds).
TKV_NETWORK_NS = 800
TKV_RMT_NS = 150
TKV_PCIE_DMA_NS = 1_150
RDMA_NETWORK_NS = 2_100
RDMA_POINTER_CHASE_NS = 4_500
RDMA_SOFT_ALLOC_NS = 6_500
RDMA_PCIE_DMA_NS = 1_000
RDMA_DIRECT_NS = 6_500
DPU_MEDIAN_NS = 3_800
RPC_MEDIAN_NS = 4_800
HOST_SWAP_P50_NS = 12_000
HOST_SWAP_P999_NS = 450_000
TKV_SRAM_HIT_NS = 2_150
TKV_HBM_HIT_NS = 2_420

PCIE_GEN4_GBPS = 32.0 * 8  # 32 GB/s
PCIE_GEN5_GBPS = 64.0 * 8

# Paper Table tbt_breakdown at 1.0 GB requested fetch (milliseconds).
TBT_1GB_MS: dict[str, dict[str, float]] = {
    "host_a100": {"dma": 32, "meta": 165, "sync": 3, "compute": 10, "residual": 0},
    "host_h100": {"dma": 16, "meta": 145, "sync": 3, "compute": 10, "residual": 6},
    "rdma_opt": {"dma": 87, "meta": 26, "sync": 14, "compute": 10, "residual": 3},
    "rpc": {"dma": 87, "meta": 3, "sync": 32, "compute": 10, "residual": 0},
    "dpu": {"dma": 87, "meta": 21, "sync": 2, "compute": 10, "residual": 0},
    "tensorkv": {"dma": 87, "meta": 5, "sync": 0, "compute": 10, "residual": 0},
}

# DPA one-thread rate and NIC cap; sweep is min(workers × 0.15 Mpps, cap).

REF_FETCH_BYTES = 1_000_000_000

# Mixtral / A100 accounting from the paper.
A100_LOCAL_KV_BYTES = int(25.7e9)
REMOTE_TIER_BYTES = HBM_CAPACITY_BYTES  # 8 GiB U280
GRAPH_WORKSPACE_GIB = 2.80
A100_ALLOCATED_NOAPC_GIB = 75.95
A100_FREE_NOAPC_GIB = 0.45

# Paper Table moe_breakdown (16-way, batch 32), milliseconds.
MOE_16WAY_MS = {
    "host_soft": {"router": 3.2, "gemm": 18.1, "meta": 42.5, "dma": 21.0},
    "rdma_soft": {"router": 3.2, "gemm": 18.1, "meta": 12.4, "dma": 24.5},
    "tkv_hard": {"router": 3.2, "gemm": 18.1, "meta": 1.2, "dma": 24.1},
}

# Whole-system energy (Table energy_efficiency), named watts and tok/s.
ENERGY_PARTS = {
    "host_a100": {"compute_w": 480.0, "mem_w": 0.0, "switch_w": 0.0, "tok_s": 320.0},
    "rpc": {"compute_w": 575.0, "mem_w": 320.0, "switch_w": 25.0, "tok_s": 1400.0},
    "dpu": {"compute_w": 585.0, "mem_w": 235.0, "switch_w": 25.0, "tok_s": 1420.0},
    "tensorkv": {"compute_w": 590.0, "mem_w": 328.0, "switch_w": 25.0, "tok_s": 1600.0},
}

# NVSHMEM is a distinct 400 Gbps / 2 MB-chunk reference, not mixed into 4 KB GET.
NVSHMEM_EFFECTIVE_GB_S = 42.0
NVSHMEM_P99_US = 18.0
NVSHMEM_UNCAACHED_P99_US = 45.0


@dataclass
class LogicalGet:
    path: str
    n_blocks: int
    rtts: float
    network_ns: int
    meta_ns: int
    dma_ns: int
    total_ns: int
    mappings_cached: bool


@dataclass
class TTFTSim:
    path: str
    setup_ms: float
    fetch_ms: float
    compute_ms: float
    total_ms: float
    prefix_tokens: int
    payload_bytes: int


@dataclass
class TBTSim:
    path: str
    dma_ms: float
    meta_ms: float
    sync_ms: float
    compute_ms: float
    residual_ms: float
    total_ms: float
    payload_bytes: int
    link_gbps: float


@dataclass
class KnownAddressRtt:
    path: str
    median_us: float


def uncached_logical_get(path: str, n_blocks: int, mappings_cached: bool = False) -> LogicalGet:
    """One logical GET for `n_blocks` scattered 4 KB pages."""
    if path == "tensorkv":
        rtts = 1.0
        net = TKV_NETWORK_NS
        meta = TKV_RMT_NS
        dma = TKV_PCIE_DMA_NS
    elif path == "rdma_uncached":
        rtts = 1.0 + n_blocks  # metadata + per-block reads (evaluated path)
        net = RDMA_NETWORK_NS * int(round(rtts))
        meta = RDMA_POINTER_CHASE_NS + RDMA_SOFT_ALLOC_NS
        dma = RDMA_PCIE_DMA_NS * n_blocks
    elif path == "rdma_opt":
        rtts = 1.0 if mappings_cached else 2.4
        wr_batches = (n_blocks + 15) // 16  # doorbell batching up to 16 WRs
        net = RDMA_NETWORK_NS * (1 if mappings_cached else 2)
        meta = 400 * wr_batches
        dma = RDMA_PCIE_DMA_NS * max(1, wr_batches)
    elif path == "rpc":
        rtts = 1.0
        net = RDMA_NETWORK_NS
        meta = RPC_MEDIAN_NS
        dma = TKV_PCIE_DMA_NS
    elif path == "dpu":
        rtts = 1.0
        net = RDMA_NETWORK_NS
        meta = DPU_MEDIAN_NS
        dma = TKV_PCIE_DMA_NS
    elif path in ("host_a100", "host_h100"):
        rtts = float(n_blocks)
        net = 0
        meta = HOST_SWAP_P50_NS * n_blocks
        dma = 0
    else:
        raise ValueError(path)
    total = net + meta + dma
    return LogicalGet(path, n_blocks, rtts, net, meta, dma, total, mappings_cached)


def known_address_rtt(path: str) -> KnownAddressRtt:
    """Median RTT after the payload address is known (Figure micro_perf (b))."""
    table = {
        "sram": TKV_SRAM_HIT_NS / 1e3,
        "hbm": TKV_HBM_HIT_NS / 1e3,
        "dpu": DPU_MEDIAN_NS / 1e3,
        "rpc": RPC_MEDIAN_NS / 1e3,
        "rdma": RDMA_DIRECT_NS / 1e3,
    }
    if path not in table:
        raise ValueError(path)
    return KnownAddressRtt(path, table[path])


def _payload_bytes(tokens: int, bytes_per_token: int) -> int:
    return max(0, tokens) * bytes_per_token


def ttft_sim(
    path: str,
    prefix_tokens: int = PREFILL_TOKENS,
    bytes_per_token: int = BYTES_PER_TOKEN_LLAMA70B_INT4,
    recompute: bool = False,
    prefix_hit: bool = True,
    link_gbps: float = LINK_GBPS,
) -> TTFTSim:
    """Compose TTFT: prefix-activation setup + payload fetch + first-token compute."""
    payload = _payload_bytes(prefix_tokens, bytes_per_token)
    n_blocks = max(1, (prefix_tokens + TOKENS_PER_BLOCK - 1) // TOKENS_PER_BLOCK)
    scale = prefix_tokens / PREFILL_TOKENS

    if recompute or path == "recompute":
        compute = PREFILL_COMPUTE_MS_AT_32K * scale
        return TTFTSim("recompute", 0.0, 0.0, compute, compute, prefix_tokens, 0)

    if path == "tensorkv":
        setup = (TKV_NETWORK_NS + TKV_RMT_NS) / 1e6
        if prefix_hit:
            setup += n_blocks * HANDLE_INSTALL_NS / 1e6
            compute = COMPUTE_MS_AT_32K * scale
        else:
            compute = PREFILL_COMPUTE_MS_AT_32K * scale
        fetch = serialize_ms(payload, link_gbps)
    elif path == "host_a100":
        setup = 420.0 * scale
        fetch = serialize_ms(payload, PCIE_GEN4_GBPS)
        compute = 20.0 * scale
    elif path == "host_h100":
        setup = 400.0 * scale
        fetch = serialize_ms(payload, PCIE_GEN5_GBPS)
        compute = 15.0 * scale
    elif path == "rdma_opt":
        setup = 140.0 * scale
        fetch = serialize_ms(payload, link_gbps)
        compute = 15.0 * scale
    elif path == "rpc":
        setup = 120.0 * scale
        fetch = serialize_ms(payload, link_gbps)
        compute = 15.0 * scale
    elif path == "dpu":
        setup = 80.0 * scale
        fetch = serialize_ms(payload, link_gbps)
        compute = 15.0 * scale
    else:
        raise ValueError(path)
    return TTFTSim(path, setup, fetch, compute, setup + fetch + compute, prefix_tokens, payload)


def tbt_sim(path: str, fetch_bytes: int = REF_FETCH_BYTES, link_gbps: float = LINK_GBPS) -> TBTSim:
    """Decode step: DMA scales with bytes and link; meta/sync scale from the 1 GB table.

    Networked DMA at 100 Gbps is the table's `dma` column (87 ms for 1 GB). Other
    link rates scale that interval by 100/link. Host-Swap DMA is PCIe serialize.
    """
    scale = fetch_bytes / REF_FETCH_BYTES
    parts = TBT_1GB_MS[path]
    if path == "host_a100":
        dma = serialize_ms(fetch_bytes, PCIE_GEN4_GBPS)
    elif path == "host_h100":
        dma = serialize_ms(fetch_bytes, PCIE_GEN5_GBPS)
    else:
        dma = parts["dma"] * scale * (LINK_GBPS / link_gbps)
    meta = parts["meta"] * scale
    sync = parts["sync"] * scale
    compute = parts["compute"] * scale
    residual = parts.get("residual", 0.0) * scale
    total = dma + meta + sync + compute + residual
    return TBTSim(path, dma, meta, sync, compute, residual, total, fetch_bytes, link_gbps)


def bandwidth_sweep(links: tuple[float, ...] = (25.0, 50.0, 75.0, 100.0)) -> list[dict]:
    rows = []
    for gbps in links:
        tkv = tbt_sim("tensorkv", REF_FETCH_BYTES, gbps)
        rdma = tbt_sim("rdma_opt", REF_FETCH_BYTES, gbps)
        rows.append(
            {
                "link_gbps": gbps,
                "tensorkv_ms": tkv.total_ms,
                "rdma_opt_ms": rdma.total_ms,
                "tensorkv_dma_ms": tkv.dma_ms,
                "rdma_dma_ms": rdma.dma_ms,
            }
        )
    return rows


@dataclass
class MoEFootprint:
    topology: str
    n_prefix: int
    n_req: int
    prefix_tokens: int
    suffix_tokens: int
    logical_bytes: int
    unique_bytes: int
    remote_need_bytes: int
    remote_alloc_bytes: int
    fits_8gib: bool
    gpu_graph_oom: bool
    host_noapc_tbt_ms: float | None
    host_soft_tbt_ms: float | None
    rdma_soft_tbt_ms: float | None
    tkv_hard_tbt_ms: float | None
    oom_message: str | None = None


def mixtral_sharing(topology: str) -> MoEFootprint:
    """Paper Table moe_prefix. Unique bytes from Mixtral FP8 KV; remote 8 GiB bound."""
    bpt = BYTES_PER_TOKEN_MIXTRAL_FP8
    if topology == "64way":
        n_prefix, n_req, pfx, sfx = 1, 64, 32_768, 512
        overhead = 5.2 / 4.3
        host_noapc, host_soft, rdma, tkv = None, 88.0, 78.0, 52.0
        gpu_oom = True
    elif topology == "16way":
        n_prefix, n_req, pfx, sfx = 2, 32, 32_768, 512
        overhead = 6.5 / 5.4
        host_noapc = None
        host_soft = moe_tbt_breakdown("host_soft")["total"]
        rdma = moe_tbt_breakdown("rdma_soft")["total"]
        tkv = moe_tbt_breakdown("tkv_hard")["total"]
        gpu_oom = True
    elif topology == "noshare":
        n_prefix, n_req, pfx, sfx = 32, 32, 16_384, 512
        overhead = 1.0
        host_noapc, host_soft, rdma, tkv = 105.0, 105.0, None, None
        gpu_oom = False
    else:
        raise ValueError(topology)

    logical = n_req * (pfx + sfx) * bpt
    unique = (n_prefix * pfx + n_req * sfx) * bpt
    if topology == "noshare":
        remote_need = unique - A100_LOCAL_KV_BYTES
        remote_alloc = REMOTE_TIER_BYTES
        fits = remote_need <= REMOTE_TIER_BYTES
        oom = None if fits else (
            "libtkv Error: Remote allocation failed. "
            f"Requested 2.00 MB, {REMOTE_TIER_BYTES // (1 << 20)} MB allocated"
        )
    else:
        remote_need = int(unique * overhead)
        remote_alloc = remote_need
        fits = remote_need <= REMOTE_TIER_BYTES
        oom = None

    return MoEFootprint(
        topology=topology,
        n_prefix=n_prefix,
        n_req=n_req,
        prefix_tokens=pfx,
        suffix_tokens=sfx,
        logical_bytes=logical,
        unique_bytes=unique,
        remote_need_bytes=remote_need,
        remote_alloc_bytes=remote_alloc,
        fits_8gib=fits,
        gpu_graph_oom=gpu_oom,
        host_noapc_tbt_ms=host_noapc,
        host_soft_tbt_ms=host_soft,
        rdma_soft_tbt_ms=rdma,
        tkv_hard_tbt_ms=tkv,
        oom_message=oom,
    )


def moe_tbt_breakdown(path: str) -> dict[str, float]:
    """16-way Mixtral decode TBT from named components (Table moe_breakdown)."""
    parts = MOE_16WAY_MS[path]
    total = sum(parts.values())
    return {**parts, "total": total}


def try_remote_alloc(need_bytes: int, capacity: int = REMOTE_TIER_BYTES) -> tuple[bool, str]:
    if need_bytes > capacity:
        return False, (
            "libtkv Error: Remote allocation failed. "
            f"Requested {need_bytes} bytes, {capacity} allocated"
        )
    return True, "ok"


def ablation_get_p99_us(fast_slow_split: bool, zero_copy_dma: bool) -> float:
    """P99 GET latency in microseconds, measured on TensorKVAppliance."""
    return cached_get_p99_us(fast_slow_split, zero_copy_dma)


def ablation_prefix_phase_ms(prefix_dedup: bool, tokens: int = PREFILL_TOKENS) -> float:
    """Prefix-phase time from the same handle-install / prefill model the engine uses."""
    if prefix_dedup:
        return handle_install_ms(tokens)
    return prefill_compute_ms(tokens)


def ablation_table() -> list[dict]:
    full = ablation_get_p99_us(True, True)
    nosplit = ablation_get_p99_us(False, True)
    nozc = ablation_get_p99_us(True, False)
    rdma = uncached_logical_get("rdma_uncached", 1).total_ns / 1e3
    return [
        {
            "config": "full",
            "get_p99_us": round(full, 3),
            "prefix_phase_ms": round(ablation_prefix_phase_ms(True), 3),
            "source": "appliance",
        },
        {
            "config": "no_prefix_dedup",
            "get_p99_us": round(full, 3),
            "prefix_phase_ms": round(ablation_prefix_phase_ms(False), 3),
            "source": "engine_timing",
        },
        {
            "config": "no_fast_slow",
            "get_p99_us": round(nosplit, 3),
            "prefix_phase_ms": round(ablation_prefix_phase_ms(True), 3),
            "source": "appliance",
        },
        {
            "config": "no_zero_copy",
            "get_p99_us": round(nozc, 3),
            "prefix_phase_ms": round(ablation_prefix_phase_ms(True), 3),
            "source": "appliance",
        },
        {
            "config": "rdma_uncached",
            "get_p99_us": round(rdma, 3),
            "prefix_phase_ms": round(ttft_sim("rdma_opt").setup_ms, 3),
            "source": "logical_get",
        },
    ]


@dataclass
class EnergySim:
    path: str
    compute_w: float
    mem_w: float
    switch_w: float
    wall_w: float
    tok_s: float
    j_per_tok: float


def energy_sim(path: str) -> EnergySim:
    p = ENERGY_PARTS[path]
    wall = p["compute_w"] + p["mem_w"] + p["switch_w"]
    jpt = wall / p["tok_s"] if p["tok_s"] else 0.0
    return EnergySim(path, p["compute_w"], p["mem_w"], p["switch_w"], wall, p["tok_s"], jpt)


def async_put_interference(put_bytes: int = 500_000_000, decode_ms: float = 42.1) -> dict:
    """Table async_interference: 0.5 GB PUT overlapped with decode.

    Isolation transfer of 0.5 GB on 100GbE is ~40 ms serialize. Overlap with
    42.1 ms of decode leaves a small exposed tail plus a 0.5 ms copy-engine /
    PCIe contention leftover.
    """
    serial = serialize_ms(put_bytes, LINK_GBPS)
    put_ms = serial
    overlapped = min(decode_ms, put_ms)
    exposed = max(0.0, put_ms - decode_ms)
    tbt = decode_ms + exposed + 0.5  # copy-engine / PCIe contention leftover
    tok_decode = 1515.0
    tok_both = tok_decode * (decode_ms / tbt)
    return {
        "decode_only_ms": decode_ms,
        "put_isolation_ms": put_ms,
        "overlapped_ms": overlapped,
        "tbt_ms": tbt,
        "tokens_per_s_decode": tok_decode,
        "tokens_per_s_both": tok_both,
        "throughput_drop": 1.0 - (tok_both / tok_decode),
    }


def nvshmem_reference() -> dict:
    return {
        "chunk_bytes": 2 * 1024 * 1024,
        "link": "400Gbps RoCEv2",
        "effective_gb_s": NVSHMEM_EFFECTIVE_GB_S,
        "p99_us": NVSHMEM_P99_US,
        "uncached_scattered_p99_us": NVSHMEM_UNCAACHED_P99_US,
        "note": "Distinct 400Gbps / 2MB-chunk platform; not mixed into 4KB GET ablation.",
    }


def run_baseline_suite() -> dict:
    moe = {t: mixtral_sharing(t).__dict__ for t in ("64way", "16way", "noshare")}
    return {
        "ttft_32k": {p: ttft_sim(p).__dict__ for p in ("tensorkv", "host_a100", "host_h100", "rdma_opt", "rpc", "dpu")},
        "tbt_1gb": {p: tbt_sim(p).__dict__ for p in TBT_1GB_MS},
        "logical_get": {
            "tensorkv": uncached_logical_get("tensorkv", 8).__dict__,
            "rdma_uncached": uncached_logical_get("rdma_uncached", 8).__dict__,
        },
        "known_address_us": {p: known_address_rtt(p).median_us for p in ("sram", "hbm", "dpu", "rpc", "rdma")},
        "dpu_sweep": dpu_worker_sweep(),
        "bandwidth": bandwidth_sweep(),
        "moe": moe,
        "moe_tbt_16way": {k: moe_tbt_breakdown(k) for k in MOE_16WAY_MS},
        "ablation": ablation_table(),
        "energy": {k: energy_sim(k).__dict__ for k in ENERGY_PARTS},
        "async_put": async_put_interference(),
        "nvshmem": nvshmem_reference(),
    }
