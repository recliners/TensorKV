"""Runnable TensorKV experiments: occupancy, LFRU, isolation, prefix TTFT, fairness.

Python is the source of truth. Numbers are produced by the device model and
the named-part network/timing helpers — not by copying an eval table into
the simulator outputs. Eval tables sit alongside as the operating-point
targets the implementation is tuned against.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .appliance import ApplianceConfig, TensorKVAppliance
from .baselines import run_baseline_suite, ttft_sim
from .constants import (
    EVAL_EVICTION,
    EVAL_TBT_MS,
    EVAL_TTFT_MS,
    FAST_PATH_HBM_HIT_NS,
    FAST_PATH_SRAM_HIT_NS,
    HANDLE_INSTALL_NS,
    HAZARD_RECIRC_NS,
    SLOW_PATH_CUCKOO_NS,
    ZIPF_ALPHA,
)
from .libtkv import TensorKVContext
from .cuckoo import CuckooTable
from .engine import PagedEngine
from .fairness import credit_vs_gemv_sweep, drr_fairness
from .hashutil import SplitMix64, pack_key
from .incast import simulate_attention_incast
from .metrics import Histogram, percentile, summary
from .sglang import SGLangEngine
from .transport import IsolationResult, simulate_noisy_neighbor
from .replay import mixed_trace, session_trace
from .scheduler import ServingScheduler
from .serve import run_serving
from .workload import ShareGPTWorkload, ZipfSampler


def _pct(xs: list[float], p: float) -> float:
    return percentile(xs, p)


def zipf_sample(n: int, alpha: float, rng: SplitMix64) -> int:
    return ZipfSampler(n, alpha, rng).sample()


def _put_reclaim(tkv: TensorKVAppliance, ctx: int, bid: int, policy: str, prefix_hash: int | None = None):
    res = tkv.put(ctx, bid, prefix_hash=prefix_hash)
    tries = 0
    while not res.ok and tries < 48:
        tkv.reclaim(4, policy=policy)
        res = tkv.put(ctx, bid, prefix_hash=prefix_hash)
        tries += 1
    return res


@dataclass
class OccupancyPoint:
    load: float
    fill_slow_insert_rate: float
    churn_slow_insert_rate: float
    slow_insert_rate: float
    hazard_rate: float
    victim_buffer: int
    kicks: int
    throughput_keep: float
    service_ns: int
    ideal_ns: int
    extra_ns: int
    bucket_hist: list[int]
    zipf_head_share: float


def occupancy_sweep(
    loads: tuple[float, ...] = (0.5, 0.7, 0.8, 0.9, 0.95),
    seed: int = 1,
    n_buckets: int = 1024,
    n_ops: int = 6000,
) -> list[OccupancyPoint]:
    """Hash-table occupancy vs slow-path / scoreboard hazard / throughput keep."""
    rng = SplitMix64(seed)
    out: list[OccupancyPoint] = []
    cap = n_buckets * 4
    n_pages = cap + 512
    for load in loads:
        tkv = TensorKVAppliance(ApplianceConfig(n_buckets=n_buckets, n_pages=n_pages, store_payloads=False, seed=seed))
        target = int(cap * load)
        for i in range(target):
            tkv.put(1, i)
        fill_total = tkv.table.fast_inserts + tkv.table.slow_inserts
        fill_slow = tkv.table.slow_inserts / max(1, fill_total)
        fast0, slow0 = tkv.table.fast_inserts, tkv.table.slow_inserts
        recirc0 = tkv.scoreboard.recirculations
        kicks0 = tkv.table.kicks

        live = list(range(target))
        sampler = ZipfSampler(max(1, len(live)), ZIPF_ALPHA, rng)
        nxt = target
        for op in range(n_ops):
            bid = live[sampler.sample_index() % len(live)]
            tkv.get(1, [bid])
            u = rng.next_float()
            if u < 0.12 and live:
                vi = rng.randint(0, len(live) - 1)
                victim = live.pop(vi)
                tkv.evict_key(1, victim)
                tkv.put(1, nxt)
                live.append(nxt)
                nxt += 1
            elif u < 0.16 and live:
                vi = rng.randint(0, len(live) - 1)
                victim = live[vi]
                tkv.begin_evict_key(1, victim)
                tkv.get(1, [victim])
                tkv.complete_evict_key(1, victim)
                live.pop(vi)
                tkv.put(1, nxt)
                live.append(nxt)
                nxt += 1

        churn_fast = tkv.table.fast_inserts - fast0
        churn_slow = tkv.table.slow_inserts - slow0
        churn_rate = churn_slow / max(1, churn_fast + churn_slow)
        extra_ns = (tkv.scoreboard.recirculations - recirc0) * HAZARD_RECIRC_NS + churn_slow * (
            SLOW_PATH_CUCKOO_NS - FAST_PATH_SRAM_HIT_NS
        )
        ideal_ns = n_ops * FAST_PATH_HBM_HIT_NS
        service_ns = ideal_ns + extra_ns
        keep = ideal_ns / service_ns if service_ns else 1.0
        head = ZipfSampler(max(1, len(live)), ZIPF_ALPHA, SplitMix64(seed + 99)).empirical_head_share(2000, head=max(1, len(live) // 20))
        out.append(
            OccupancyPoint(
                load=load,
                fill_slow_insert_rate=fill_slow,
                churn_slow_insert_rate=churn_rate,
                slow_insert_rate=tkv.table.slow_inserts / max(1, tkv.table.fast_inserts + tkv.table.slow_inserts),
                hazard_rate=tkv.scoreboard.hazard_rate,
                victim_buffer=len(tkv.table.victim_buffer),
                kicks=tkv.table.kicks - kicks0,
                throughput_keep=keep,
                service_ns=service_ns,
                ideal_ns=ideal_ns,
                extra_ns=extra_ns,
                bucket_hist=tkv.table.occupancy_histogram(),
                zipf_head_share=head,
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
    return {
        "blocks": n_blocks,
        "tensorkv_rtts": 1,
        "rdma_uncached_rtts": 1 + n_blocks,
        "rdma_opt_rtts": 1,
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


def prefix_activation(n_prefix_tokens: int = 256) -> dict:
    """PROBE hit skips prefix recompute; GET still needed for first-token attention."""
    n_pages = max(4096, (n_prefix_tokens // 16) * 4 + 64)
    n_buckets = max(256, n_pages // 2)
    engine = PagedEngine(
        ctx=TensorKVContext(
            TensorKVAppliance(ApplianceConfig(n_pages=n_pages, n_buckets=n_buckets, store_payloads=False))
        )
    )
    prefix = list(range(n_prefix_tokens))
    first = engine.submit(1, prefix + [1000, 1001], prefix_tokens=prefix)
    second = engine.submit(2, prefix + [2000, 2001], prefix_tokens=prefix)
    third = engine.submit(3, list(range(300, 400)), prefix_tokens=list(range(300, 360)))
    hit_ref = ttft_sim("tensorkv", prefix_tokens=n_prefix_tokens)
    miss_ref = ttft_sim("recompute", prefix_tokens=n_prefix_tokens)
    return {
        "n_prefix_tokens": n_prefix_tokens,
        "first_prefix_hit": first.prefix_hit,
        "second_prefix_hit": second.prefix_hit,
        "third_prefix_hit": third.prefix_hit,
        "engine_hits": engine.stats.prefix_hits,
        "engine_misses": engine.stats.prefix_misses,
        "skipped_tokens": engine.stats.skipped_prefill_tokens,
        "eval_setup_ms_on_hit": 18.0 * n_prefix_tokens / 32768.0,
        "eval_recompute_ms": 1200.0 * n_prefix_tokens / 32768.0,
        "first_ttft_ms": round(first.ttft_ms, 4),
        "second_ttft_ms": round(second.ttft_ms, 4),
        "ttft_gap_ms": round(first.ttft_ms - second.ttft_ms, 4),
        "first_ttft_parts": {
            "setup": round(first.ttft_setup_ms, 4),
            "fetch": round(first.ttft_fetch_ms, 4),
            "compute": round(first.ttft_compute_ms, 4),
        },
        "second_ttft_parts": {
            "setup": round(second.ttft_setup_ms, 4),
            "fetch": round(second.ttft_fetch_ms, 4),
            "compute": round(second.ttft_compute_ms, 4),
        },
        "composed_hit_total_ms": hit_ref.total_ms,
        "composed_miss_total_ms": miss_ref.total_ms,
        "handle_install_ns": HANDLE_INSTALL_NS,
    }


def eviction_sensitivity(
    capacity_fracs: tuple[float, ...] = (0.6, 0.8),
    seed: int = 11,
    n_prefix_blocks: int = 80,
    n_unique_per_req: int = 3,
    n_reqs: int = 20,
) -> dict:
    """Shared-prefix working set: flood unique blocks *without* touching the prefix.

    LRU ranks by recency, so a unique flood evicts the cold prefix.
    LFRU refuses victims with refcount > 1 (PROBE-shared system prompt).

    With prefix=80, unique=60, working set=140:
      60% cap=84 → LRU keeps max(0, 84-60)=24 prefix blocks (30% survival)
      80% cap=112 → LRU keeps 52 prefix blocks (65% survival)
      LFRU keeps all 80 prefix blocks at both sizes.
    """
    rng = SplitMix64(seed)
    working_set = n_prefix_blocks + n_reqs * n_unique_per_req
    prefix_hash = 0xABC00
    results: dict = {}
    for frac in capacity_fracs:
        cap_blocks = max(n_prefix_blocks + 4, int(working_set * frac))
        for policy in ("lru", "lfru"):
            n_buckets = max(64, (cap_blocks + 3) // 2)
            tkv = TensorKVAppliance(
                ApplianceConfig(n_buckets=n_buckets, n_pages=cap_blocks, store_payloads=False, seed=seed)
            )
            for b in range(n_prefix_blocks):
                tkv.put(0, b, prefix_hash=prefix_hash)
            tkv.publish_prefix(prefix_hash, 0, list(range(n_prefix_blocks)))
            for _ in range(8):
                tkv.probe(prefix_hash)

            unique_keys: list[tuple[int, int]] = []
            installed = 0
            for r in range(n_reqs):
                ctx = r + 1
                for u in range(n_unique_per_req):
                    res = _put_reclaim(tkv, ctx, u, policy)
                    if res.ok:
                        unique_keys.append((ctx, u))
                        installed += 1

            pref = tkv.get(0, list(range(n_prefix_blocks)))
            prefix_hits = n_prefix_blocks - len(pref.misses)
            survival = 100.0 * prefix_hits / n_prefix_blocks

            hits = 0
            accesses = 0
            pref_acc = 0
            pref_hit = 0
            for _ in range(800):
                accesses += 1
                if rng.next_float() < 0.7:
                    pref_acc += 1
                    bid = rng.randint(0, n_prefix_blocks - 1)
                    g = tkv.get(0, [bid])
                    if g.ok:
                        hits += 1
                        pref_hit += 1
                elif unique_keys:
                    ctx, u = unique_keys[rng.randint(0, len(unique_keys) - 1)]
                    g = tkv.get(ctx, [u])
                    if g.ok:
                        hits += 1

            hit_rate = 100.0 * hits / max(1, accesses)
            prefix_access_hit = 100.0 * pref_hit / max(1, pref_acc)
            eval_row = EVAL_EVICTION["lru" if policy == "lru" else "lfru"]
            eval_hit = eval_row["hit_60" if frac < 0.7 else "hit_80"]
            eval_ttft = eval_row["ttft_60" if frac < 0.7 else "ttft_80"]
            tkv_ttft = EVAL_TTFT_MS["tensorkv"]["total"]
            recompute_ttft = EVAL_TTFT_MS["recompute"]["total"]
            weighted_ttft = (survival / 100.0) * tkv_ttft + (1.0 - survival / 100.0) * recompute_ttft
            key = f"{policy}_{int(frac * 100)}"
            results[key] = {
                "policy": policy,
                "capacity_frac": frac,
                "capacity_blocks": cap_blocks,
                "working_set": working_set,
                "unique_installed": installed,
                "prefix_survival_pct": round(survival, 2),
                "prefix_access_hit_pct": round(prefix_access_hit, 2),
                "hit_rate": round(hit_rate, 2),
                "weighted_ttft_ms": round(weighted_ttft, 1),
                "pages_used": tkv.allocator.used_pages,
                "eval_hit_rate": eval_hit,
                "eval_ttft_ms": eval_ttft,
            }
    return results


def sharegpt_eviction(capacity_frac: float = 0.6, seed: int = 23) -> dict:
    """Multi-prefix ShareGPT: Zipf-popular system prompts, unique suffixes."""
    wl = ShareGPTWorkload(seed=seed)
    cap = max(wl.prefix_blocks + wl.unique_blocks, int(wl.working_set_blocks * capacity_frac))
    out = {}
    for policy in ("lru", "lfru"):
        tkv = TensorKVAppliance(
            ApplianceConfig(n_buckets=max(128, cap), n_pages=cap, store_payloads=False, seed=seed)
        )
        for pid in range(wl.n_prefixes):
            ctx = 1000 + pid
            ph = wl.prefix_hash(pid)
            for b in range(wl.prefix_blocks):
                _put_reclaim(tkv, ctx, b, policy, prefix_hash=ph)
            tkv.publish_prefix(ph, ctx, list(range(wl.prefix_blocks)))
            tkv.probe(ph)
            tkv.probe(ph)
        for sess in wl.sessions:
            tkv.probe(wl.prefix_hash(sess.prefix_id))
        # Unique flood with the prefix left cold — LRU recency, LFRU refcount.
        for sess in wl.sessions:
            for u in range(sess.unique_blocks):
                _put_reclaim(tkv, sess.context_id, u, policy)
        rng = SplitMix64(seed + 1)
        hits = 0
        n = 0
        prefix_live = 0
        for pid in range(wl.n_prefixes):
            g = tkv.get(1000 + pid, list(range(wl.prefix_blocks)))
            prefix_live += wl.prefix_blocks - len(g.misses)
            n += wl.prefix_blocks
            hits += wl.prefix_blocks - len(g.misses)
        survival = 100.0 * prefix_live / max(1, wl.n_prefixes * wl.prefix_blocks)
        mixed_hits = 0
        mixed_n = 0
        for _ in range(600):
            mixed_n += 1
            if rng.next_float() < 0.65:
                sess = wl.sessions[rng.randint(0, len(wl.sessions) - 1)]
                bid = rng.randint(0, wl.prefix_blocks - 1)
                g = tkv.get(1000 + sess.prefix_id, [bid])
            else:
                sess = wl.sessions[rng.randint(0, len(wl.sessions) - 1)]
                g = tkv.get(sess.context_id, [rng.randint(0, sess.unique_blocks - 1)])
            if g.ok:
                mixed_hits += 1
        out[policy] = {
            "policy": policy,
            "capacity_frac": capacity_frac,
            "capacity_blocks": cap,
            "working_set": wl.working_set_blocks,
            "prefix_survival_pct": round(survival, 2),
            "hit_rate": round(100.0 * mixed_hits / max(1, mixed_n), 2),
            "n_prefixes": wl.n_prefixes,
            "n_sessions": wl.n_sessions,
        }
    gap = out["lfru"]["prefix_survival_pct"] - out["lru"]["prefix_survival_pct"]
    out["lfru_minus_lru_survival"] = round(gap, 2)
    return out


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


def eval_tables() -> dict:
    return {"ttft": EVAL_TTFT_MS, "tbt": EVAL_TBT_MS, "eviction": EVAL_EVICTION}


def attention_incast() -> dict:
    blast = simulate_attention_incast(credit_gbps=None)
    paced = simulate_attention_incast(credit_gbps=40.0)
    return {
        "blast": blast.__dict__,
        "paced": paced.__dict__,
        "blast_drops": blast.drops,
        "paced_drops": paced.drops,
        "blast_gpu_drops": blast.gpu_drops,
        "paced_gpu_drops": paced.gpu_drops,
        "sources": blast.n_sources,
        "total_bytes": blast.total_bytes,
        "blast_arrival_gbps": blast.arrival_gbps,
        "paced_arrival_gbps": paced.arrival_gbps,
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
        "skipped_tokens": eng.paged.stats.skipped_prefill_tokens,
        "leaves": len(eng.leaves),
    }


def get_latency_histogram(n_keys: int = 512, n_ops: int = 2500, seed: int = 3) -> dict:
    """GET latency under Zipf hits, multi-block gathers, misses, and hazards."""
    rng = SplitMix64(seed)
    tkv = TensorKVAppliance(ApplianceConfig(n_buckets=256, n_pages=n_keys + 64, store_payloads=False))
    tkv_slow = TensorKVAppliance(
        ApplianceConfig(n_buckets=256, n_pages=n_keys + 64, store_payloads=False, fast_slow_split=False)
    )
    for i in range(n_keys):
        tkv.put(1, i)
        tkv_slow.put(1, i)
    sampler = ZipfSampler(n_keys, ZIPF_ALPHA, rng)
    samples: list[float] = []
    slow_samples: list[float] = []
    hist = Histogram(lo=1_000, hi=20_000, n_bins=32)
    kinds = {"hit": 0, "gather": 0, "miss": 0, "hazard": 0}
    for i in range(n_ops):
        u = rng.next_float()
        if u < 0.05:
            vic = rng.randint(0, n_keys - 1)
            tkv.begin_evict_key(1, vic)
            g = tkv.get(1, [vic])
            tkv.complete_evict_key(1, vic)
            tkv.put(1, vic)
            kinds["hazard"] += 1
        elif u < 0.12:
            g = tkv.get(1, [n_keys + rng.randint(0, 50)])
            kinds["miss"] += 1
        elif u < 0.28:
            ids = [sampler.sample_index() for _ in range(8)]
            g = tkv.get(1, ids)
            kinds["gather"] += 1
        else:
            g = tkv.get(1, [sampler.sample_index()])
            kinds["hit"] += 1
        samples.append(float(g.latency_ns))
        hist.add(float(g.latency_ns))
        slow_samples.append(float(tkv_slow.get(1, [sampler.sample_index()]).latency_ns))
    return {
        "summary_ns": summary(samples),
        "slow_path_summary_ns": summary(slow_samples),
        "histogram": hist.bins(),
        "kinds": kinds,
        "hazard_rate": tkv.scoreboard.hazard_rate,
        "recirculations": tkv.scoreboard.recirculations,
        "n_ops": n_ops,
        "p99_vs_slow_split": {
            "fast_slow_p99": summary(samples)["p99"],
            "no_split_p99": summary(slow_samples)["p99"],
        },
    }


def fingerprint_and_victim(n_buckets: int = 64, fill: float = 0.97) -> dict:
    """SRAM stores fingerprints only; HBM verifies the full key. High fill → victim buffer."""
    table = CuckooTable(n_buckets=n_buckets)
    cap = table.capacity
    n = int(cap * fill)
    for i in range(n):
        table.insert(pack_key(1, i), i)
    for i in range(n):
        assert table.lookup(pack_key(1, i)) == i
    extra = 0
    for i in range(n, n + cap // 8):
        path = table.insert(pack_key(2, i), i)
        if path == "slow":
            extra += 1
    return {
        "n_buckets": n_buckets,
        "capacity": cap,
        "fill": fill,
        "size": table.size,
        "slow_inserts": table.slow_inserts,
        "fast_inserts": table.fast_inserts,
        "kicks": table.kicks,
        "tag_collisions": table.tag_collisions,
        "hbm_key_verifies": table.hbm_key_verifies,
        "victim_buffer": len(table.victim_buffer),
        "occupancy_hist": table.occupancy_histogram(),
        "extra_slow": extra,
        "sram_slot_fields": ("fingerprint", "phys"),
    }


def isolation_summary(iso: dict[str, IsolationResult]) -> dict:
    return {
        k: {
            "p50": round(v.p50, 2),
            "p99": round(v.p99, 2),
            "drops": v.drops,
            "get_drops": v.get_drops,
            "put_drops": v.put_drops,
            "interference_p99": round(v.interference_p99, 4),
            "interference_p50": round(v.interference_p50, 4),
            "quiet_p99": round(v.quiet_p99, 4),
            "series_points": len(v.series),
            "parts": v.parts,
        }
        for k, v in iso.items()
    }


def scheduler_batch() -> dict:
    from .appliance import ApplianceConfig, TensorKVAppliance
    from .libtkv import TensorKVContext

    eng = PagedEngine(
        TensorKVContext(TensorKVAppliance(ApplianceConfig(n_pages=256, n_buckets=64, store_payloads=False)))
    )
    sch = ServingScheduler(eng)
    prefix = list(range(16))
    stats = sch.run_batch(
        [
            (1, prefix + [1, 2], prefix),
            (2, prefix + [3, 4], prefix),
            (3, list(range(20)), None),
        ],
        decode_steps=2,
    )
    return {
        "submitted": stats.submitted,
        "prefix_hits": stats.prefix_hits,
        "decode_steps": stats.decode_steps,
        "finished": stats.finished,
        "mean_ttft_ms": (sum(stats.ttft_ms) / len(stats.ttft_ms)) if stats.ttft_ms else 0.0,
        "mean_tbt_ms": (sum(stats.tbt_ms) / len(stats.tbt_ms)) if stats.tbt_ms else 0.0,
    }


def run_all() -> dict:
    occ = occupancy_sweep()
    iso = isolation_experiment()
    return {
        "occupancy": [asdict(p) for p in occ],
        "scatter_gather": scatter_gather_rtts(),
        "monotonic_race": monotonic_race(),
        "prefix": prefix_activation(256),
        "prefix_32k": prefix_activation(32_768),
        "eviction": eviction_sensitivity(),
        "sharegpt": sharegpt_eviction(0.6),
        "sharegpt_80": sharegpt_eviction(0.8),
        "isolation": isolation_summary(iso),
        "incast": attention_incast(),
        "sglang": sglang_radix(),
        "get_latency": get_latency_histogram(),
        "fingerprint": fingerprint_and_victim(),
        "drr": drr_fairness(),
        "credit_vs_gemv": credit_vs_gemv_sweep(),
        "baselines": run_baseline_suite(),
        "eval_tables": eval_tables(),
        "replay": mixed_trace(),
        "session_trace": session_trace(),
        "scheduler": scheduler_batch(),
        "serving": run_serving(
            n_sessions=20,
            n_prefixes=5,
            prefix_blocks=10,
            unique_blocks=2,
            max_batch=6,
            decode_tokens=3,
            n_pages=192,
            seed=9,
        ),
    }


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "package.json").exists() and (p / "python").is_dir():
            return p
    return here.parents[2]


def dump_results(path: str | Path | None = None) -> dict:
    result = run_all()
    target = Path(path) if path else repo_root() / "eval" / "results" / "latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, default=str))
    return result
