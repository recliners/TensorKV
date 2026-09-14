"""Zipf sampling and ShareGPT-style prefix/suffix traces.

The previous inversion ``rank = u ** (-1/alpha) % n`` wraps the tail onto
the head and destroys the power law. All occupancy / LFRU / fairness runs
sample through a precomputed CDF of ``k^{-alpha}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import ZIPF_ALPHA
from .hashutil import SplitMix64


class ZipfSampler:
    """Draw ranks in ``1..n`` from Zipf-Mandelbrot with exponent ``alpha``."""

    def __init__(self, n: int, alpha: float = ZIPF_ALPHA, rng: SplitMix64 | None = None) -> None:
        if n < 1:
            raise ValueError("n must be positive")
        self.n = n
        self.alpha = alpha
        self.rng = rng or SplitMix64(0x5EED)
        weights = [(i + 1) ** (-alpha) for i in range(n)]
        total = sum(weights)
        acc = 0.0
        cdf: list[float] = []
        for w in weights:
            acc += w / total
            cdf.append(acc)
        cdf[-1] = 1.0
        self.cdf = cdf

    def sample(self) -> int:
        """Return a rank in 1..n (rank 1 is the head)."""
        u = self.rng.next_float()
        lo, hi = 0, self.n - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.cdf[mid] < u:
                lo = mid + 1
            else:
                hi = mid
        return lo + 1

    def sample_index(self) -> int:
        """0-based index into an array of size n."""
        return self.sample() - 1

    def empirical_head_share(self, draws: int = 4000, head: int = 1) -> float:
        hits = 0
        for _ in range(draws):
            if self.sample() <= head:
                hits += 1
        return hits / draws


def zipf_sample(n: int, alpha: float, rng: SplitMix64) -> int:
    """One-shot Zipf draw (rebuilds the CDF). Prefer ``ZipfSampler`` in loops."""
    return ZipfSampler(n, alpha, rng).sample()


@dataclass
class ShareGPTSession:
    session_id: int
    prefix_id: int
    prefix_blocks: int
    unique_blocks: int
    context_id: int


@dataclass
class ShareGPTWorkload:
    """A handful of shared system prompts, many sessions, unique suffixes.

    Prefix popularity is Zipf so a few prompts dominate — the setting where
    prefix-aware LFRU is supposed to beat recency-only LRU.
    """

    n_prefixes: int = 3
    n_sessions: int = 24
    prefix_blocks: int = 24
    unique_blocks: int = 3
    alpha: float = ZIPF_ALPHA
    seed: int = 7
    sessions: list[ShareGPTSession] = field(default_factory=list)

    def __post_init__(self) -> None:
        rng = SplitMix64(self.seed)
        sampler = ZipfSampler(self.n_prefixes, self.alpha, rng)
        self.sessions = []
        for i in range(self.n_sessions):
            pid = sampler.sample_index()
            self.sessions.append(
                ShareGPTSession(
                    session_id=i,
                    prefix_id=pid,
                    prefix_blocks=self.prefix_blocks,
                    unique_blocks=self.unique_blocks,
                    context_id=i + 1,
                )
            )

    @property
    def working_set_blocks(self) -> int:
        return self.n_prefixes * self.prefix_blocks + self.n_sessions * self.unique_blocks

    def prefix_hash(self, prefix_id: int) -> int:
        return 0xA11CE000 + prefix_id
