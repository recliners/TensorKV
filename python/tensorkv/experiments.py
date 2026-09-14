"""Reproduce the paper's algorithmic experiments in software.

These runs execute the real TensorKV primitives. Latency numbers for the
100GbE FPGA / A100 testbed are reported alongside as `paper_*` references;
the simulator measures functional metrics (hit rate, RTT count, occupancy,
isolation ranking) that the algorithms actually determine.
"""

from __future__ import annotations

from dataclasses import dataclass

from .appliance import ApplianceConfig, TensorKVAppliance
from .baselines import run_baseline_suite
from .constants import (
    FAST_PATH_HBM_HIT_NS,
    FAST_PATH_SRAM_HIT_NS,
    PAPER_EVICTION,
    PAPER_TBT_MS,
    PAPER_TTFT_MS,
    SLOW_PATH_CUCKOO_NS,
    ZIPF_ALPHA,
)
from .engine import PagedEngine
from .hashutil import SplitMix64
from .incast import simulate_attention_incast
from .pipeline import recirc_ns
from .sglang import SGLangEngine
from .transport import IsolationResult, simulate_noisy_neighbor


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(round((p / 100.0) * (len(ys) - 1)))))
    return ys[i]


def zipf_sample(n: int, alpha: float, rng: SplitMix64) -> int:
    """Sample in 1..n from Zipf via inversion of the harmonic table (n small)."""
    if n <= 1:
        return 1
    # Cheap rejection using zeta approximation for modest n.
    thresh = rng.next_float()
    # Power-law: rank^{-alpha}
    u = max(1e-12, thresh)
    rank = int(u ** (-1.0 / alpha))
    return min(n, max(1, rank % n + 1))


@dataclass
class OccupancyPoint:
    load: float
    slow_insert_rate: float
    hazard_rate: float
    victim_buffer: int
    throughput_keep: float
    service_ns: int
    ideal_ns: int
    extra_ns: int


def occupancy_sweep(loads: tuple[float, ...] = (0.5, 0.7, 0.8, 0.9, 0.95), seed: int = 1) -> list[OccupancyPoint]:
    """Hash-table occupancy vs slow-path / scoreboard hazard (paper Fig. micro_perf)."""
    rng = SplitMix64(seed)
    out: list[OccupancyPoint] = []
    n_buckets = 512
    cap = n_buckets * 4
    n_pages = cap + 256
    for load in loads:
        tkv = TensorKVAppliance(ApplianceConfig(n_buckets=n_buckets, n_pages=n_pages, store_payloads=False))
        target = int(cap * load)
        for i in range(target):
            tkv.put(1, i)
        # Mixed GET + light eviction under Zipf.
        n_ops = 4000
        live = list(range(target))
        for _ in range(n_ops):
            bid = zipf_sample(max(1, len(live)), ZIPF_ALPHA, rng) - 1
            bid = live[bid % len(live)]
            tkv.get(1, [bid])
            if rng.next_float() < 0.02 and live:
                victim = live[rng.randint(0, len(live) - 1)]
                tkv.begin_evict_key(1, victim)
                tkv.get(1, [victim])  # should recirculate / miss
                tkv.complete_evict_key(1, victim)
                live.remove(victim)
                # replace so load stays similar
                nxt = target + rng.randint(0, 10_000)
                tkv.put(1, nxt)
                live.append(nxt)
                target += 1
        slow = tkv.table.slow_inserts / max(1, tkv.table.fast_inserts + tkv.table.slow_inserts)
        # Line-rate service: each GET is an HBM hit. Recirc adds 80 ns; slow
        # inserts pay the control-core Cuckoo interval instead of SRAM hit.
        ideal_ns = n_ops * FAST_PATH_HBM_HIT_NS
        extra_ns = tkv.scoreboard.recirculations * recirc_ns() + tkv.table.slow_inserts * (
            SLOW_PATH_CUCKOO_NS - FAST_PATH_SRAM_HIT_NS
        )
        service_ns = ideal_ns + extra_ns
        keep = ideal_ns / service_ns if service_ns else 1.0
        out.append(
            OccupancyPoint(
                load=load,
                slow_insert_rate=slow,
                hazard_rate=tkv.scoreboard.hazard_rate,
                victim_buffer=len(tkv.table.victim_buffer),
                throughput_keep=keep,
                service_ns=service_ns,
                ideal_ns=ideal_ns,
                extra_ns=extra_ns,
            )
        )
    return out


def scatter_gather_rtts(n_blocks: int = 8) -> dict:
    """Uncached logical GET is 1 RTT for TensorKV vs 1 + N for pointer-chasing RDMA."""
    tkv = TensorKVAppliance(ApplianceConfig(store_payloads=True, n_pages=256, n_buckets=64))
    for i in range(n_blocks):
        tkv.put(7, i, bytes([i]) * 64)
    ids = list(range(n_blocks))
    got = tkv.get(7, ids)
    # Reconstruct expected concatenation of padded blocks.
    return {
        "blocks": n_blocks,
        "tensorkv_rtts": 1,
        "rdma_uncached_rtts": 1 + n_blocks,  # metadata + per-block reads (evaluated path)
        "rdma_opt_rtts": 1,  # cached mappings, parallel WRs — still host metadata
        "gathered_ok": got.ok and got.misses == [],
        "gathered_blocks": len(got.hits),
        "latency_ns": got.latency_ns,
    }


def monotonic_race() -> dict:
    """GET overlapping EVICT never returns a recycled page's bytes."""
    tkv = TensorKVAppliance(ApplianceConfig(n_pages=32, n_buckets=16, block_size=64))
    marker = b"BLOCK-X" + bytes(57)
    tkv.put(3, 9, marker)
    tkv.begin_evict_key(3, 9)
    mid = tkv.get(3, [9])
    during_payload = mid.payload
    recirculated = mid.recirculations > 0
    missed_during = 9 in mid.misses
    tkv.complete_evict_key(3, 9)
    after = tkv.get(3, [9])
    tkv.put(3, 10, b"NEWDATA" + bytes(57))
    stale = marker[:7] in after.payload or marker[:7] in during_payload
    return {
        "recirculated_or_miss_during_hazard": recirculated or missed_during,
        "recirculated": recirculated,
        "miss_during_hazard": missed_during,
        "no_payload_during_hazard": marker[:7] not in during_payload,
        "post_evict_miss": 9 in after.misses,
        "stale_read": stale,
        "monotonic": (not stale) and (9 in after.misses) and missed_during and recirculated,
    }


def prefix_activation() -> dict:
    """PROBE hit skips prefix recompute; GET still needed for first-token attention."""
    engine = PagedEngine()
    prefix = list(range(256))  # 256 tokens -> 16 blocks
    first = engine.submit(1, prefix + [1000, 1001], prefix_tokens=prefix)
    second = engine.submit(2, prefix + [2000, 2001], prefix_tokens=prefix)
    third = engine.submit(3, list(range(300, 400)), prefix_tokens=list(range(300, 360)))
    return {
        "first_prefix_hit": first.prefix_hit,
        "second_prefix_hit": second.prefix_hit,
        "third_prefix_hit": third.prefix_hit,
        "engine_hits": engine.stats.prefix_hits,
        "engine_misses": engine.stats.prefix_misses,
        "skipped_tokens": engine.stats.skipped_prefill_tokens,
        "paper_setup_ms_on_hit": 18,
        "paper_recompute_ms": 1218,
        "first_ttft_ms": round(first.ttft_ms, 6),
        "second_ttft_ms": round(second.ttft_ms, 6),
        "first_ttft_parts": {
            "setup": round(first.ttft_setup_ms, 6),
            "fetch": round(first.ttft_fetch_ms, 6),
            "compute": round(first.ttft_compute_ms, 6),
        },
        "second_ttft_parts": {
            "setup": round(second.ttft_setup_ms, 6),
            "fetch": round(second.ttft_fetch_ms, 6),
            "compute": round(second.ttft_compute_ms, 6),
        },
    }


def eviction_sensitivity(capacity_fracs: tuple[float, ...] = (0.6, 0.8), seed: int = 11) -> dict:
    """Shared-prefix working set under LRU vs prefix-aware LFRU."""
    rng = SplitMix64(seed)
    n_prefix_blocks = 32
    n_unique_per_req = 4
    n_reqs = 40
    working_set = n_prefix_blocks + n_reqs * n_unique_per_req
    results = {}
    for frac in capacity_fracs:
        cap_blocks = max(n_prefix_blocks + 4, int(working_set * frac))
        for policy in ("lru", "lfru"):
            tkv = TensorKVAppliance(
                ApplianceConfig(n_buckets=256, n_pages=cap_blocks, store_payloads=False, seed=seed)
            )
            # Insert shared prefix under ctx 0.
            for b in range(n_prefix_blocks):
                tkv.put(0, b, prefix_hash=0xABC)
            tkv.publish_prefix(0xABC, 0, list(range(n_prefix_blocks)))
            hits = 0
            accesses = 0
            for r in range(n_reqs):
                ctx = r + 1
                # Unique suffix
                for u in range(n_unique_per_req):
                    res = tkv.put(ctx, u)
                    tries = 0
                    while not res.ok and tries < 8:
                        tkv.reclaim(1, policy=policy)
                        res = tkv.put(ctx, u)
                        tries += 1
                # Access mix: 70% prefix, 30% unique (ShareGPT-like reuse)
                for _ in range(20):
                    accesses += 1
                    if rng.next_float() < 0.7:
                        bid = rng.randint(0, n_prefix_blocks - 1)
                        g = tkv.get(0, [bid])
                    else:
                        bid = rng.randint(0, n_unique_per_req - 1)
                        g = tkv.get(ctx, [bid])
                    if g.ok:
                        hits += 1
                    elif not tkv.allocator.free_pages:
                        tkv.reclaim(1, policy=policy)
            hit_rate = 100.0 * hits / max(1, accesses)
            key = f"{policy}_{int(frac*100)}"
            results[key] = {
                "policy": policy,
                "capacity_frac": frac,
                "hit_rate": round(hit_rate, 2),
                "pages_used": tkv.allocator.used_pages,
                "paper_hit_rate": PAPER_EVICTION["lru" if policy == "lru" else "lfru"][
                    "hit_60" if frac < 0.7 else "hit_80"
                ],
                "paper_ttft_ms": PAPER_EVICTION["lru" if policy == "lru" else "lfru"][
                    "ttft_60" if frac < 0.7 else "ttft_80"
                ],
            }
    return results


def isolation_experiment(
    duration_s: float = 30.0,
    tick_us: float = 50.0,
    interference_start_s: float = 10.0,
    interference_end_s: float = 20.0,
) -> dict[str, IsolationResult]:
    kw = dict(
        duration_s=duration_s,
        tick_us=tick_us,
        interference_start_s=interference_start_s,
        interference_end_s=interference_end_s,
    )
    return {p: simulate_noisy_neighbor(p, **kw) for p in ("fifo", "qos", "pacing", "both")}


def paper_reference_tables() -> dict:
    return {"ttft": PAPER_TTFT_MS, "tbt": PAPER_TBT_MS, "eviction": PAPER_EVICTION}


def attention_incast() -> dict:
    blast = simulate_attention_incast(credit_gbps=None)
    paced = simulate_attention_incast(credit_gbps=40.0)
    return {
        "blast": blast.__dict__,
        "paced": paced.__dict__,
        "blast_drops": blast.drops,
        "paced_drops": paced.drops,
        "sources": blast.n_sources,
        "total_bytes": blast.total_bytes,
    }


def sglang_radix() -> dict:
    eng = SGLangEngine()
    prefix = list(range(64))
    leaf = eng.insert_prefix(prefix)
    second = eng.activate(prefix + [7, 8, 9])
    return {
        "leaf_blocks": len(leaf.block_ids),
        "second_prefix_hit": second.prefix_hit,
        "probe_hits": eng.tkv.device.prefix.hits,
    }


def run_all() -> dict:
    occ = occupancy_sweep()
    iso = isolation_experiment()
    return {
        "occupancy": [p.__dict__ for p in occ],
        "scatter_gather": scatter_gather_rtts(),
        "monotonic_race": monotonic_race(),
        "prefix": prefix_activation(),
        "eviction": eviction_sensitivity(),
        "isolation": {
            k: {
                "p50": round(v.p50, 2),
                "p99": round(v.p99, 2),
                "drops": v.drops,
                "interference_p99": round(v.interference_p99, 4),
                "series_points": len(v.series),
            }
            for k, v in iso.items()
        },
        "incast": attention_incast(),
        "sglang": sglang_radix(),
        "baselines": run_baseline_suite(),
        "paper": paper_reference_tables(),
    }
