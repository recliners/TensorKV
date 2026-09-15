"""ShareGPT-style continuous-batch serving on the TensorKV software stack.

Arrivals are Zipf-popular system prompts plus unique suffixes. The scheduler
PROBE/prefill/decode path issues real TKV_PUT / TKV_GET / TKV_PROBE / TKV_EVICT
on TensorKVAppliance. GET P99 in the report is the appliance histogram, not a
constant.
"""

from __future__ import annotations

from dataclasses import asdict

from .appliance import ApplianceConfig, TensorKVAppliance
from .constants import TOKENS_PER_BLOCK
from .engine import PagedEngine
from .hashutil import SplitMix64
from .libtkv import TensorKVContext
from .metrics import summary
from .scheduler import Arrival, ServingScheduler
from .workload import ShareGPTWorkload


def sharegpt_arrivals(
    *,
    n_sessions: int = 32,
    n_prefixes: int = 6,
    prefix_blocks: int = 10,
    unique_blocks: int = 2,
    decode_tokens: int = 4,
    seed: int = 5,
    tpb: int = TOKENS_PER_BLOCK,
) -> tuple[ShareGPTWorkload, list[Arrival]]:
    wl = ShareGPTWorkload(
        n_prefixes=n_prefixes,
        n_sessions=n_sessions,
        prefix_blocks=prefix_blocks,
        unique_blocks=unique_blocks,
        seed=seed,
    )
    rng = SplitMix64(seed + 17)
    arrivals: list[Arrival] = []
    t = 0
    for s in wl.sessions:
        prefix = list(range(s.prefix_id * 256, s.prefix_id * 256 + s.prefix_blocks * tpb))
        suffix = list(
            range(
                1_000_000 + s.session_id * 256,
                1_000_000 + s.session_id * 256 + s.unique_blocks * tpb,
            )
        )
        t += rng.randint(0, 2)
        arrivals.append(
            Arrival(
                req_id=s.session_id + 1,
                tokens=prefix + suffix,
                prefix_tokens=prefix,
                max_new=max(2, decode_tokens),
                arrive_tick=t,
            )
        )
    return wl, arrivals


def run_serving(
    n_sessions: int = 32,
    n_prefixes: int = 6,
    prefix_blocks: int = 10,
    unique_blocks: int = 2,
    max_batch: int = 8,
    decode_tokens: int = 4,
    n_pages: int = 256,
    n_buckets: int = 128,
    seed: int = 5,
) -> dict:
    wl, arrivals = sharegpt_arrivals(
        n_sessions=n_sessions,
        n_prefixes=n_prefixes,
        prefix_blocks=prefix_blocks,
        unique_blocks=unique_blocks,
        decode_tokens=decode_tokens,
        seed=seed,
    )
    pages = max(n_pages, 32)
    tkv = TensorKVAppliance(
        ApplianceConfig(n_buckets=n_buckets, n_pages=pages, store_payloads=False, seed=seed)
    )
    eng = PagedEngine(TensorKVContext(tkv))
    sch = ServingScheduler(eng, max_batch=max_batch)
    stats = sch.run_arrivals(arrivals)
    st = tkv.stats()
    get_lat = [float(x) for x in tkv.get_latencies]
    prefix_hit_rate = (stats.prefix_hits / stats.submitted) if stats.submitted else 0.0
    return {
        "n_sessions": n_sessions,
        "n_prefixes": n_prefixes,
        "prefix_blocks": prefix_blocks,
        "unique_blocks": unique_blocks,
        "working_set_blocks": wl.working_set_blocks,
        "n_pages": pages,
        "max_batch": max_batch,
        "submitted": stats.submitted,
        "finished": stats.finished,
        "prefix_hits": stats.prefix_hits,
        "prefix_hit_rate": prefix_hit_rate,
        "decode_steps": stats.decode_steps,
        "reclaims": stats.reclaims,
        "ticks": stats.ticks,
        "peak_batch": stats.peak_batch,
        "vector_overflow": stats.vector_overflow,
        "ttft": summary(stats.ttft_ms),
        "tbt": summary(stats.tbt_ms),
        "get_latency_ns": summary(get_lat),
        "overflow_bytes": eng.tkv.overflow_bytes,
        "descriptors_posted": eng.tkv.descriptors_posted,
        "device": {
            "hash_load": st["hash_load"],
            "pages_used": st["pages_used"],
            "get_ops": st["gets"],
            "put_ops": st["puts"],
            "probe_ops": st["probes"],
            "evict_ops": st["evicts"],
            "gathered_bytes": st["gathered_bytes"],
            "slow_inserts": st["slow_inserts"],
        },
        "engine": asdict(eng.stats),
        "complete": stats.finished == n_sessions,
    }


def serving_report(**kwargs) -> dict:
    return run_serving(**kwargs)
