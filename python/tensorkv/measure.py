"""Measure GET latency from a live TensorKVAppliance.

Ablation numbers come from this histogram, not from a literal table.
"""

from __future__ import annotations

from .appliance import ApplianceConfig, TensorKVAppliance
from .hashutil import SplitMix64
from .metrics import summary
from .workload import ZipfSampler

# Small enough for the baseline suite and unit tests; large enough for a stable P99.
DEFAULT_KEYS = 128
DEFAULT_OPS = 360
DEFAULT_SEED = 7


def measure_get_latencies(
    *,
    fast_slow_split: bool = True,
    zero_copy_dma: bool = True,
    n_keys: int = DEFAULT_KEYS,
    n_ops: int = DEFAULT_OPS,
    seed: int = DEFAULT_SEED,
) -> list[float]:
    rng = SplitMix64(seed)
    tkv = TensorKVAppliance(
        ApplianceConfig(
            n_buckets=64,
            n_pages=n_keys + 32,
            store_payloads=False,
            seed=seed,
            fast_slow_split=fast_slow_split,
            zero_copy_dma=zero_copy_dma,
        )
    )
    for i in range(n_keys):
        tkv.put(1, i)
    sampler = ZipfSampler(n_keys, rng=rng)
    samples: list[float] = []
    for _ in range(n_ops):
        u = rng.next_float()
        if u < 0.06:
            vic = rng.randint(0, n_keys - 1)
            tkv.begin_evict_key(1, vic)
            g = tkv.get(1, [vic])
            tkv.complete_evict_key(1, vic)
            tkv.put(1, vic)
        elif u < 0.14:
            g = tkv.get(1, [n_keys + rng.randint(0, 20)])
        elif u < 0.32:
            ids = [sampler.sample_index() for _ in range(8)]
            g = tkv.get(1, ids)
        else:
            g = tkv.get(1, [sampler.sample_index()])
        samples.append(float(g.latency_ns))
    return samples


def measure_get_summary(**kwargs) -> dict[str, float]:
    return summary(measure_get_latencies(**kwargs))


def measure_get_p99_us(**kwargs) -> float:
    return measure_get_summary(**kwargs)["p99"] / 1e3


_P99_CACHE: dict[tuple[bool, bool], float] = {}


def cached_get_p99_us(fast_slow_split: bool, zero_copy_dma: bool) -> float:
    key = (fast_slow_split, zero_copy_dma)
    if key not in _P99_CACHE:
        _P99_CACHE[key] = measure_get_p99_us(
            fast_slow_split=fast_slow_split, zero_copy_dma=zero_copy_dma
        )
    return _P99_CACHE[key]


def clear_measure_cache() -> None:
    _P99_CACHE.clear()
