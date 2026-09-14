"""Mini PagedAttention serving engine on top of TensorKV.

PagedAttention integration workflow:
  1. Scheduler: TKV_PROBE before prefill (prefix matching)
  2. Cache engine: TKV_PUT / TKV_EVICT
  3. Worker: TKV_GET (JIT gather into a contiguous attention matrix)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import BYTES_PER_TOKEN_LLAMA70B_INT4, DEFAULT_CREDIT_GBPS, LINK_GBPS, TOKENS_PER_BLOCK
from .hashutil import prompt_hash
from .libtkv import TensorKVContext
from .timing import attention_compute_ms, serialize_ms, ttft_breakdown

__all__ = ["PagedEngine", "Request", "EngineStats", "prompt_hash"]


@dataclass
class Request:
    req_id: int
    tokens: list[int]
    prefix_len: int = 0
    context_id: int = 0
    prefix_owner: int | None = None
    prefix_blocks: list[int] = field(default_factory=list)
    prefix_hit: bool = False
    prefix_hash: int | None = None
    generated: int = 0
    ttft_ms: float = 0.0
    ttft_setup_ms: float = 0.0
    ttft_fetch_ms: float = 0.0
    ttft_compute_ms: float = 0.0
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
    def __init__(
        self,
        ctx: TensorKVContext | None = None,
        tokens_per_block: int = TOKENS_PER_BLOCK,
        bytes_per_token: int = BYTES_PER_TOKEN_LLAMA70B_INT4,
    ) -> None:
        self.tkv = ctx or TensorKVContext()
        self.tpb = tokens_per_block
        self.bytes_per_token = bytes_per_token
        self.stats = EngineStats()
        self.requests: dict[int, Request] = {}
        self._next_ctx = 1

    def _logical_bytes(self, n_tokens: int) -> int:
        """Llama-70B INT4 (or Mixtral FP8) KV bytes. Not the 4 KB stub stored in the appliance."""
        return max(0, n_tokens) * self.bytes_per_token

    def _n_blocks(self, n_tokens: int) -> int:
        return (n_tokens + self.tpb - 1) // self.tpb

    def submit(self, req_id: int, tokens: list[int], prefix_tokens: list[int] | None = None) -> Request:
        """Prefill path with optional shared-prefix PROBE."""
        ctx_id = self._next_ctx
        self._next_ctx += 1
        req = Request(req_id=req_id, tokens=list(tokens), context_id=ctx_id)

        probe_ns = 0
        local_tokens = len(tokens)

        prefix = prefix_tokens or []
        if prefix:
            ph = prompt_hash(prefix)
            req.prefix_hash = ph
            probe = self.tkv.probe(ph)
            probe_ns = probe.latency_ns
            req.prefix_len = len(prefix)
            if probe.hit and probe.context_id is not None:
                req.prefix_hit = True
                req.prefix_owner = probe.context_id
                req.prefix_blocks = list(probe.handles)
                self.stats.prefix_hits += 1
                self.stats.skipped_prefill_tokens += len(prefix)
                suffix = tokens[len(prefix) :]
                req.tokens = list(prefix) + list(suffix)
                local_tokens = len(suffix)
                for i, chunk in enumerate(_chunks(suffix, self.tpb)):
                    data = bytes((b & 0xFF) for b in chunk)
                    self.tkv.put_async(req.context_id, i, data)
                    self.stats.bytes_put += self.tkv.device.cfg.block_size
            else:
                self.stats.prefix_misses += 1
                local_tokens = len(tokens)
                self._materialize(req, tokens, ph)
        else:
            self._materialize(req, tokens, None)
            local_tokens = len(tokens)

        self.stats.prefills += 1
        fetch = self._gather(req)
        logical = self._logical_bytes(len(req.tokens))
        self.stats.bytes_get += logical
        parts = ttft_breakdown(
            prefix_hit=req.prefix_hit,
            prefix_tokens=req.prefix_len,
            local_tokens=local_tokens,
            gathered_bytes=logical,
            get_latency_ns=fetch.latency_ns,
            probe_latency_ns=probe_ns,
            credit_gbps=LINK_GBPS,
        )
        req.ttft_ms = parts.total_ms
        req.ttft_setup_ms = parts.setup_ms
        req.ttft_fetch_ms = parts.fetch_ms
        req.ttft_compute_ms = parts.compute_ms
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
        self._gather(req)
        logical = self._logical_bytes(len(req.tokens))
        self.stats.bytes_get += logical
        tbt = serialize_ms(logical, DEFAULT_CREDIT_GBPS) + attention_compute_ms(len(req.tokens)) * 0.2
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
        if req.prefix_hit and req.prefix_hash is not None:
            self.tkv.device.release_prefix(req.prefix_hash)
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
