"""TensorKV constants: PagedAttention layout, datapath timing, and eval tables."""

from __future__ import annotations

# PagedAttention (vLLM): 16 tokens per KV block.
TOKENS_PER_BLOCK = 16

# Microbenchmark payload size used throughout the FPGA evaluation.
BLOCK_SIZE_BYTES = 4096

# Llama-3-70B, KIVI-style INT4 KV: 81.9 KB per token.
BYTES_PER_TOKEN_LLAMA70B_INT4 = 81_920

# Mixtral-8x7B FP8 KV: 32 layers * 8 KV heads * 128 dim * 2 (K,V) * 1 byte.
BYTES_PER_TOKEN_MIXTRAL_FP8 = 65_536

# Hierarchical allocator: control cores refill a hardware FIFO in batches of 64.
ALLOC_FIFO_BATCH = 64
ALLOC_FIFO_WATERMARK = 16

# Fast-path SRAM: four 64-bit {fingerprint, physical-pointer} slots per 512-bit bucket.
SLOTS_PER_BUCKET = 4
SLOT_BITS = 64
BUCKET_BITS = 512
FINGERPRINT_BITS = 32
PHYS_PTR_BITS = 32

# Cuckoo displacement budget before an insert is handed to the slow-path victim buffer.
CUCKOO_MAX_KICKS = 32

# Bloom filter for TKV_PROBE (prefix existence). Sized for modest prefix cardinality.
BLOOM_BITS = 1 << 16
BLOOM_HASHES = 3

# Dual-path timing model (microbenchmarks / recirculation analysis).
FPGA_CLOCK_HZ = 250_000_000
FPGA_CYCLE_NS = 4  # 250 MHz
FAST_PATH_SRAM_HIT_NS = 2_150
FAST_PATH_HBM_HIT_NS = 2_420
RMT_LOOKUP_NS = 150
CROSSBAR_NOC_CYCLES = 20
ATOMIC_COMMIT_CYCLES = 1
HAZARD_RECIRC_NS = 80
SLOW_PATH_CUCKOO_NS = 12_400  # ablation: w/o fast-slow split, GET P99 ~ 12.4 us
# 32K-token kernel compute ≈ 15 ms after KV is resident.
COMPUTE_MS_AT_32K = 15.0
# Whole-prompt prefill when the prefix is not in the cache (recompute path).
PREFILL_COMPUTE_MS_AT_32K = 1200.0
PREFILL_TOKENS = 32_768
# Prefix-hit setup: 18 ms / (32768/16) blocks to install handles.
HANDLE_INSTALL_NS = 8_789

# Prototype capacity.
HBM_CAPACITY_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB U280
LOCAL_KV_POOL_BYTES = 32 * 1024 * 1024 * 1024  # ~32 GB usable KV on A100 after weights

# Transport.
LINK_GBPS = 100.0
DEFAULT_CREDIT_GBPS = 40.0  # GET credit matched to GEMV drain
HIGH_PRIORITY_OPCODES = frozenset({"GET", "PROBE"})
LOW_PRIORITY_OPCODES = frozenset({"PUT"})
DRR_QUANTUM_BYTES = 16_384

# Default Zipfian access (microbenchmarks).
ZIPF_ALPHA = 1.2

# End-to-end eval numbers (A100, 32K prefix / 1.0 GB decode step).
EVAL_TTFT_MS = {
    "recompute": {"setup": 0, "fetch": 0, "compute": 1200, "total": 1200},
    "host_a100": {"setup": 420, "fetch": 80, "compute": 20, "total": 520},
    "host_h100": {"setup": 400, "fetch": 40, "compute": 15, "total": 455},
    "rdma": {"setup": 140, "fetch": 250, "compute": 15, "total": 405},
    "rpc": {"setup": 120, "fetch": 240, "compute": 15, "total": 375},
    "dpu": {"setup": 80, "fetch": 235, "compute": 15, "total": 330},
    "tensorkv": {"setup": 18, "fetch": 230, "compute": 15, "total": 263},
}

EVAL_TBT_MS = {
    0.25: {"host_a": 52, "host_h": 45, "rdma": 38, "rpc": 36, "dpu": 35, "tkv": 33},
    0.5: {"host_a": 110, "host_h": 95, "rdma": 75, "rpc": 70, "dpu": 65, "tkv": 58},
    1.0: {"host_a": 210, "host_h": 180, "rdma": 140, "rpc": 132, "dpu": 120, "tkv": 102},
    1.5: {"host_a": 290, "host_h": 250, "rdma": 210, "rpc": 195, "dpu": 175, "tkv": 145},
}

EVAL_EVICTION = {
    "lru": {"hit_60": 30, "hit_80": 65, "ttft_60": 810, "ttft_80": 245},
    "lfru": {"hit_60": 78, "hit_80": 92, "ttft_60": 135, "ttft_80": 42},
}
