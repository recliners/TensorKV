"""SGLang RadixAttention leaf mapped onto TKV_PROBE / TKV_PUT.

Paper § Cross-Engine Integration: replace local radix-leaf allocation with
PROBE + PUT. Firmware and the wire protocol stay unchanged; this module is
the software replica of that 350-line integration.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import PagedEngine, Request, prompt_hash
from .libtkv import TensorKVContext


@dataclass
class RadixLeaf:
    prompt_hash: int
    context_id: int
    block_ids: list[int]
    tokens: list[int]


@dataclass
class SGLangEngine:
    """Radix tree whose leaves are TensorKV prefix records, not GPU pages."""

    tkv: TensorKVContext = field(default_factory=TensorKVContext)
    leaves: dict[int, RadixLeaf] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.paged = PagedEngine(ctx=self.tkv)

    def insert_prefix(self, tokens: list[int]) -> RadixLeaf:
        ph = prompt_hash(tokens)
        existing = self.tkv.probe(ph)
        if existing.hit and existing.context_id is not None:
            leaf = RadixLeaf(ph, existing.context_id, list(existing.handles), list(tokens))
            self.leaves[ph] = leaf
            return leaf
        req = self.paged.submit(len(self.leaves) + 1, tokens, prefix_tokens=tokens)
        n_blocks = self.paged._n_blocks(len(tokens))
        leaf = RadixLeaf(ph, req.context_id, list(range(n_blocks)), list(tokens))
        self.leaves[ph] = leaf
        return leaf

    def _longest_leaf(self, tokens: list[int]) -> RadixLeaf | None:
        best: RadixLeaf | None = None
        for leaf in self.leaves.values():
            n = len(leaf.tokens)
            if n and tokens[:n] == leaf.tokens and (best is None or n > len(best.tokens)):
                best = leaf
        return best

    def activate(self, tokens: list[int]) -> Request:
        """Prefill, binding the longest radix leaf through TKV_PROBE."""
        leaf = self._longest_leaf(tokens)
        prefix = leaf.tokens if leaf is not None else None
        return self.paged.submit(len(self.paged.requests) + 1, tokens, prefix_tokens=prefix)

    def decode_remote(self, req_id: int, new_token: int = 1) -> float:
        return self.paged.decode(req_id, new_token)
