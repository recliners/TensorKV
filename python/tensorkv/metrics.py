"""Percentiles, histograms, and small report tables."""

from __future__ import annotations

from dataclasses import dataclass, field


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(round((p / 100.0) * (len(ys) - 1)))))
    return ys[i]


def summary(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "n": float(len(xs)),
        "mean": sum(xs) / len(xs),
        "p50": percentile(xs, 50),
        "p90": percentile(xs, 90),
        "p99": percentile(xs, 99),
        "max": max(xs),
    }


@dataclass
class Histogram:
    """Linear buckets over ``[lo, hi]`` plus overflow."""

    lo: float
    hi: float
    n_bins: int = 24
    counts: list[int] = field(default_factory=list)
    overflow: int = 0
    underflow: int = 0
    n: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * self.n_bins

    def add(self, x: float) -> None:
        self.n += 1
        if x < self.lo:
            self.underflow += 1
            return
        if x >= self.hi:
            self.overflow += 1
            return
        width = (self.hi - self.lo) / self.n_bins
        idx = min(self.n_bins - 1, int((x - self.lo) / width))
        self.counts[idx] += 1

    def bins(self) -> list[dict]:
        width = (self.hi - self.lo) / self.n_bins
        return [
            {
                "lo": self.lo + i * width,
                "hi": self.lo + (i + 1) * width,
                "count": self.counts[i],
            }
            for i in range(self.n_bins)
        ]
