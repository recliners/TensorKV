"""Mini PagedAttention serving engine on top of TensorKV.

Mirrors the vLLM integration workflow in the paper:
  1. Scheduler: TKV_PROBE before prefill (prefix matching)
  2. Cache engine: TKV_PUT / TKV_EVICT
  3. Worker: TKV_GET (JIT gather into a contiguous attention matrix)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import TOKENS_PER_BLOCK
from .hashutil import mix64
from .libtkv import TensorKVContext


def prompt_hash(tokens: list[int]) -> int:
    h = 0x243F6A8885A308D3
    for t in tokens:
        h = mix64(h ^ (t & 0xFFFFFFFF))
    return h


@dataclass
class Request:
    req_id: int
    tokens: list[int]
    prefix_len: int = 0
    context_id: int = 0
    prefix_owner: int | None = None
    prefix_blocks: list[int] = field(default_factory=list)
    prefix_hit: bool = False
    generated: int = 0
    ttft_ms: float = 0.0
    tbt_ms: list[float] = field(default_factory=list)


@dataclass
class EngineStats:
    prefills: int = 0
    prefix_hits: int = 0
    prefix_misses: int = 0
    decode_steps: int = 0
    tokens_generated: int = 0
    bytes_put: int = 0
    bytes_get: int = 0
    skipped_prefill_tokens: int = 0


class PagedEngine:
    def __init__(self, ctx: TensorKVContext | None = None, tokens_per_block: int = TOKENS_PER_BLOCK) -> None:
        self.tkv = ctx or TensorKVContext()
        self.tpb = tokens_per_block
        self.stats = EngineStats()
        self.requests: dict[int, Request] = {}
        self._next_ctx = 1

    def _n_blocks(self, n_tokens: int) -> int:
        return (n_tokens + self.tpb - 1) // self.tpb

    def submit(self, req_id: int, tokens: list[int], prefix_tokens: list[int] | None = None) -> Request:
        """Prefill path with optional shared-prefix PROBE."""
        ctx_id = self._next_ctx
        self._next_ctx += 1
        req = Request(req_id=req_id, tokens=list(tokens), context_id=ctx_id)

        setup_ms = 2.0
        fetch_ms = 0.0
        compute_ms = 0.0

        prefix = prefix_tokens or []
        if prefix:
            ph = prompt_hash(prefix)
            probe = self.tkv.probe(ph)
            req.prefix_len = len(prefix)
            if probe.hit and probe.context_id is not None:
                req.prefix_hit = True
                req.prefix_owner = probe.context_id
                req.prefix_blocks = list(probe.handles)
                self.stats.prefix_hits += 1
                self.stats.skipped_prefill_tokens += len(prefix)
                setup_ms = 18.0  # paper: prefix-activation setup
                suffix = tokens[len(prefix) :]
                req.tokens = list(prefix) + list(suffix)
                for i, chunk in enumerate(_chunks(suffix, self.tpb)):
                    data = bytes((b & 0xFF) for b in chunk)
                    self.tkv.put_async(req.context_id, i, data)
                    self.stats.bytes_put += self.tkv.device.cfg.block_size
                compute_ms = 15.0
            else:
                self.stats.prefix_misses += 1
                compute_ms = 15.0 + 0.03 * len(prefix)  # local prefill cost (scaled)
                self._materialize(req, tokens, ph)
                setup_ms = 18.0
        else:
            self._materialize(req, tokens, None)
            compute_ms = 15.0 + 0.03 * len(tokens)

        self.stats.prefills += 1
        fetch = self._gather(req)
        fetch_ms = 0.02 * max(1, fetch.gathered_bytes / 4096)
        self.stats.bytes_get += fetch.gathered_bytes
        req.ttft_ms = setup_ms + fetch_ms + compute_ms
        self.requests[req_id] = req
        return req

    def _materialize(self, req: Request, tokens: list[int], prefix_hash_val: int | None) -> None:
        for i, chunk in enumerate(_chunks(tokens, self.tpb)):
            data = bytes([(b & 0xFF) for b in chunk])
            self.tkv.put_async(req.context_id, i, data, prefix_hash=prefix_hash_val)
            self.stats.bytes_put += self.tkv.device.cfg.block_size
        if prefix_hash_val is not None:
            n = self._n_blocks(len(tokens) if not req.prefix_len else req.prefix_len)
            # Register only the prefix span when prefix_len is set.
            n_prefix_blocks = self._n_blocks(req.prefix_len) if req.prefix_len else n
            self.tkv.device.publish_prefix(prefix_hash_val, req.context_id, list(range(n_prefix_blocks)))

    def _gather(self, req: Request):
        """Vectorized GET: prefix handles from the owner context, suffix from this request."""
        from .appliance import GetResult

        parts: list[GetResult] = []
        if req.prefix_hit and req.prefix_owner is not None and req.prefix_blocks:
            parts.append(self.tkv.get_async(req.prefix_owner, req.prefix_blocks, credit_gbps=40.0))
            suffix_tokens = req.tokens[req.prefix_len :]
            n_suffix = self._n_blocks(len(suffix_tokens)) if suffix_tokens else 0
            if n_suffix:
                parts.append(self.tkv.get_async(req.context_id, list(range(n_suffix)), credit_gbps=40.0))
        else:
            n_blocks = self._n_blocks(len(req.tokens))
            parts.append(self.tkv.get_async(req.context_id, list(range(n_blocks)), credit_gbps=40.0))
        payload = b"".join(p.payload for p in parts)
        hits = [h for p in parts for h in p.hits]
        misses = [m for p in parts for m in p.misses]
        gathered = sum(p.gathered_bytes for p in parts)
        return GetResult(
            ok=all(p.ok for p in parts) if parts else False,
            payload=payload,
            hits=hits,
            misses=misses,
            events=[e for p in parts for e in p.events],
            latency_ns=sum(p.latency_ns for p in parts),
            recirculations=sum(p.recirculations for p in parts),
            gathered_bytes=gathered,
        )

    def decode(self, req_id: int, new_token: int = 1) -> float:
        """One decode step: gather all KV blocks, then append the new token's KV."""
        req = self.requests[req_id]
        gathered = self._gather(req)
        self.stats.bytes_get += gathered.gathered_bytes
        n_blocks = self._n_blocks(len(req.tokens))
        tbt = 0.01 * max(1, n_blocks) + 10.0  # gather + dense attention stub
        req.tokens.append(new_token)
        req.generated += 1
        suffix_len = len(req.tokens) - req.prefix_len
        if suffix_len > 0 and suffix_len % self.tpb == 1:
            bid = self._n_blocks(suffix_len) - 1
            self.tkv.put_async(req.context_id, bid, bytes([new_token & 0xFF]))
            self.stats.bytes_put += self.tkv.device.cfg.block_size
        req.tbt_ms.append(tbt)
        self.stats.decode_steps += 1
        self.stats.tokens_generated += 1
        return tbt

    def finish(self, req_id: int, keep_prefix: bool = True) -> None:
        req = self.requests.pop(req_id, None)
        if req is None:
            return
        if keep_prefix and req.prefix_hit:
            self.tkv.evict(req.context_id, policy="all")
        elif keep_prefix and req.prefix_len:
            start = self._n_blocks(req.prefix_len)
            chain = list(self.tkv.device.contexts.get(req.context_id, []))
            for bid in chain:
                if bid >= start:
                    self.tkv.device.evict_key(req.context_id, bid)
        else:
            self.tkv.evict(req.context_id, policy="all")


def _chunks(items: list[int], n: int) -> list[list[int]]:
    if not items:
        return []
    return [items[i : i + n] for i in range(0, len(items), n)]
