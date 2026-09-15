"""Mixed Zipf/ShareGPT traces through the appliance (software workload)."""

from __future__ import annotations

from .appliance import ApplianceConfig, TensorKVAppliance
from .descriptor import INLINE_IDS, Descriptor, get_descriptor
from .hashutil import SplitMix64, prompt_hash
from .workload import ShareGPTWorkload, ZipfSampler


def mixed_trace(
    n_contexts: int = 24,
    blocks_per_ctx: int = 32,
    n_ops: int = 800,
    seed: int = 11,
) -> dict:
    rng = SplitMix64(seed)
    pages = n_contexts * blocks_per_ctx + 128
    tkv = TensorKVAppliance(
        ApplianceConfig(n_buckets=128, n_pages=pages, store_payloads=False, seed=seed)
    )
    for ctx in range(n_contexts):
        for bid in range(blocks_per_ctx):
            tkv.put(ctx, bid)
        tkv.publish_prefix(prompt_hash(list(range(8)) + [ctx]), ctx, list(range(8)))

    sampler = ZipfSampler(n_contexts * blocks_per_ctx, rng=rng)
    share = ShareGPTWorkload(n_prefixes=8, n_sessions=24, prefix_blocks=8, unique_blocks=2, seed=seed + 3)
    gets = puts = probes = evicts = 0
    hits = misses = 0
    for _ in range(n_ops):
        u = rng.next_float()
        if u < 0.55:
            flat = sampler.sample_index()
            ctx, bid = divmod(flat, blocks_per_ctx)
            g = tkv.get(ctx, [bid])
            gets += 1
            hits += len(g.hits)
            misses += len(g.misses)
        elif u < 0.70:
            ctx = rng.randint(0, n_contexts - 1)
            bid = blocks_per_ctx + rng.randint(0, 3)
            tkv.put(ctx, bid)
            puts += 1
        elif u < 0.88:
            sess = share.sessions[rng.randint(0, len(share.sessions) - 1)]
            tkv.probe(share.prefix_hash(sess.prefix_id))
            probes += 1
        else:
            ctx = rng.randint(0, n_contexts - 1)
            tkv.evict(ctx, policy="oldest", k=1)
            evicts += 1

    st = tkv.stats()
    return {
        "ops": n_ops,
        "gets": gets,
        "puts": puts,
        "probes": probes,
        "evicts": evicts,
        "get_hits": hits,
        "get_misses": misses,
        "hash_load": st["hash_load"],
        "slow_inserts": st["slow_inserts"],
        "pages_used": st["pages_used"],
        "voq_high": st.get("voq_high", st.get("voq_dequeued_high", 0)),
        "gathered_bytes": st["gathered_bytes"],
    }


def session_trace(
    n_sessions: int = 16,
    n_prefixes: int = 4,
    prefix_blocks: int = 12,
    unique_blocks: int = 2,
    seed: int = 4,
) -> dict:
    """One ShareGPT session after another: PROBE, materialize on miss, vector GET, EVICT suffix.

    ``prefix_blocks`` defaults above the 8-id doorbell so the gather overflow list is used.
    """
    wl = ShareGPTWorkload(
        n_prefixes=n_prefixes,
        n_sessions=n_sessions,
        prefix_blocks=prefix_blocks,
        unique_blocks=unique_blocks,
        seed=seed,
    )
    pages = wl.working_set_blocks + 64
    tkv = TensorKVAppliance(
        ApplianceConfig(n_buckets=64, n_pages=pages, store_payloads=False, seed=seed)
    )
    prefix_ctx: dict[int, int] = {}
    probes = hits = misses = gets = puts = evicts = 0
    overflow_gets = 0
    gathered = 0
    for s in wl.sessions:
        ph = wl.prefix_hash(s.prefix_id)
        pr = tkv.probe(ph)
        probes += 1
        if not pr.hit:
            ctx = 1 + s.prefix_id
            prefix_ctx[s.prefix_id] = ctx
            for bid in range(s.prefix_blocks):
                tkv.put(ctx, bid)
                puts += 1
            tkv.publish_prefix(ph, ctx, list(range(s.prefix_blocks)))
            misses += 1
        else:
            hits += 1
            prefix_ctx.setdefault(s.prefix_id, pr.context_id or (1 + s.prefix_id))
        uctx = 1000 + s.session_id
        for i in range(s.unique_blocks):
            tkv.put(uctx, i)
            puts += 1
        ids = list(range(s.prefix_blocks))
        desc = get_descriptor(prefix_ctx[s.prefix_id], ids)
        header, overflow = desc.encode_request()
        walked = Descriptor.decode(header, overflow)
        if walked.n_blocks > INLINE_IDS:
            overflow_gets += 1
        g = tkv.get(prefix_ctx[s.prefix_id], walked.block_ids)
        gets += 1
        gathered += g.gathered_bytes
        tkv.get(uctx, list(range(s.unique_blocks)))
        gets += 1
        tkv.evict(uctx, policy="all")
        evicts += 1
    st = tkv.stats()
    return {
        "sessions": n_sessions,
        "n_prefixes": n_prefixes,
        "prefix_blocks": prefix_blocks,
        "probes": probes,
        "prefix_hits": hits,
        "prefix_misses": misses,
        "gets": gets,
        "puts": puts,
        "evicts": evicts,
        "overflow_gets": overflow_gets,
        "gathered_bytes": gathered,
        "pages_used": st["pages_used"],
        "hash_load": st["hash_load"],
        "prefix_hit_rate": hits / n_sessions if n_sessions else 0.0,
    }
