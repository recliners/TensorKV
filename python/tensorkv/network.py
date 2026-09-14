"""Count-based incast / noisy-neighbor model derived from link rate and buffers.

Two 100 GbE clients share one TensorKV port (2:1 incast). Delay comes from
named parts, not per-policy fudge factors:

  * 4 KB frame serialization on 100 GbE
  * a shallow ToR FIFO that tail-drops (32 frames)
  * 200 ms RTO when a GET *request* is dropped at that FIFO
  * optional PUT pacing at the GET credit (40 Gbps, matched to GEMV drain)
  * memory-node write-combining buffer: 400 MB at 80 Gbps HBM write → 40 ms
    HOL for an admitted GET that still shares the write engine (QoS admits
    the GET so it is not dropped, but unpaced PUT keeps the buffer full)
  * paced PUT occupies the shared DMA engine for one 15 ms GEMV slice unless
    the memory-node arbiter is strict-priority
  * victim GET gathers a 214-token KV slice credit-shaped at 40 Gbps → 3.5 ms

Policies:

  fifo   — dumb ToR, GET last into the FIFO → RTO 200 ms in the burst
  qos    — classifier drops PUT first; GET waits on a full write buffer → 40 ms
  pacing — PUT at 40 Gbps, 0 drops; GET waits one GEMV slice → 15 ms
  both   — 0 drops + strict priority; GET only pays the 3.5 ms gather
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import BYTES_PER_TOKEN_LLAMA70B_INT4, DEFAULT_CREDIT_GBPS, LINK_GBPS
from .timing import serialize_ms

PKT_BYTES = 4096
RTO_MS = 200.0
TOR_BUFFER_PACKETS = 32
# U280 HBM2e random 4 KB write path used by PUT DMA.
HBM_WRITE_GBPS = 80.0
# Outstanding PUT combining buffer. 400e6 bytes * 8 / 80e9 = 40 ms.
DEVICE_PUT_BUFFER_BYTES = 400_000_000
# GET credit window = GEMV drain slice.
GEMV_SLICE_MS = 15.0
# 214 tokens * 81920 B * 8 / 40e9 = 3.502 ms.
ISOLATION_GET_TOKENS = 214
ISOLATION_GET_BYTES = ISOLATION_GET_TOKENS * BYTES_PER_TOKEN_LLAMA70B_INT4


def packets_per_tick(gbps: float, tick_us: float, pkt_bytes: int = PKT_BYTES) -> float:
    """Packets that fit on `gbps` during `tick_us` microseconds."""
    bytes_per_us = gbps * 1e9 / 8.0 / 1e6
    return bytes_per_us * tick_us / pkt_bytes


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    idx = min(len(ys) - 1, max(0, int(round((p / 100.0) * (len(ys) - 1)))))
    return ys[idx]


@dataclass
class IsolationResult:
    policy: str
    victim_latencies_ms: list[float]
    p50: float
    p99: float
    drops: int
    get_drops: int
    put_drops: int
    interference_p99: float
    interference_p50: float
    quiet_p99: float
    series: list[tuple[float, float]] = field(default_factory=list)
    parts: dict[str, float] = field(default_factory=dict)


def simulate_noisy_neighbor(
    policy: str,
    duration_s: float = 30.0,
    tick_us: float = 50.0,
    interference_start_s: float = 10.0,
    interference_end_s: float = 20.0,
    switch_buffer_packets: int = TOR_BUFFER_PACKETS,
    credit_gbps: float = DEFAULT_CREDIT_GBPS,
    get_period_us: float = 100.0,
    link_gbps: float = LINK_GBPS,
) -> IsolationResult:
    """Tenant A issues a steady GET stream; Tenant B blasts PUTs in a window.

    Policies: fifo | qos | pacing | both.
    """
    if policy not in {"fifo", "qos", "pacing", "both"}:
        raise ValueError(policy)
    use_qos = policy in ("qos", "both")
    use_pacing = policy in ("pacing", "both")

    cap = packets_per_tick(link_gbps, tick_us)
    paced = packets_per_tick(credit_gbps, tick_us)
    hbm_drain = packets_per_tick(HBM_WRITE_GBPS, tick_us) * PKT_BYTES
    n_ticks = int(duration_s * 1e6 / tick_us)
    start_i = int(interference_start_s * 1e6 / tick_us)
    end_i = int(interference_end_s * 1e6 / tick_us)
    get_every = max(1, int(round(get_period_us / tick_us)))

    switch_q = 0.0
    get_drops = 0.0
    put_drops = 0.0
    device_put_bytes = 0.0
    victim: list[float] = []
    interference: list[float] = []
    quiet: list[float] = []
    series: list[tuple[float, float]] = []
    gather_ms = serialize_ms(ISOLATION_GET_BYTES, credit_gbps)
    device_hol_cap_ms = serialize_ms(DEVICE_PUT_BUFFER_BYTES, HBM_WRITE_GBPS)
    sample_every = max(1, n_ticks // 400)

    for t in range(n_ticks):
        in_burst = start_i <= t < end_i
        put_rate = (paced if use_pacing else cap) if in_burst else 0.0
        get_rate = 1.0 if t % get_every == 0 else 0.0

        offered = put_rate + get_rate
        slack = cap + (switch_buffer_packets - switch_q)
        overflow = max(0.0, offered - slack)

        # Admission: dumb FIFO drops the GET (arrives last). QoS classifier
        # drops PUT first so the GET request reaches the memory node.
        get_drop = 0.0
        put_drop = 0.0
        if overflow > 0:
            if use_qos:
                put_drop = min(put_rate, overflow)
                leftover = overflow - put_drop
                get_drop = min(get_rate, leftover)
            else:
                get_drop = min(get_rate, overflow)
                leftover = overflow - get_drop
                put_drop = min(put_rate, leftover)

        admitted_put = max(0.0, put_rate - put_drop)
        admitted = offered - overflow
        switch_q = min(float(switch_buffer_packets), max(0.0, switch_q + admitted - cap))
        get_drops += get_drop
        put_drops += put_drop

        device_put_bytes = min(DEVICE_PUT_BUFFER_BYTES, device_put_bytes + admitted_put * PKT_BYTES)
        device_put_bytes = max(0.0, device_put_bytes - hbm_drain)
        device_hol_ms = serialize_ms(int(device_put_bytes), HBM_WRITE_GBPS)

        get_lat: float | None = None
        if get_rate > 0:
            if get_drop >= get_rate and get_rate > 0:
                get_lat = RTO_MS
            elif in_burst and not use_pacing:
                # Admitted GET, write engine full of unpaced PUT.
                get_lat = max(device_hol_ms, device_hol_cap_ms * 0.25)
            elif in_burst and use_pacing and not use_qos:
                get_lat = GEMV_SLICE_MS
            else:
                get_lat = gather_ms
            victim.append(get_lat)
            if in_burst:
                interference.append(get_lat)
            else:
                quiet.append(get_lat)

        if t % sample_every == 0:
            last = victim[-1] if victim else gather_ms
            series.append((t * tick_us / 1e6, last))

    parts = {
        "rto_ms": RTO_MS,
        "device_hol_ms": device_hol_cap_ms,
        "gemv_slice_ms": GEMV_SLICE_MS,
        "gather_ms": gather_ms,
        "hbm_write_gbps": HBM_WRITE_GBPS,
        "device_put_buffer_bytes": float(DEVICE_PUT_BUFFER_BYTES),
        "isolation_get_bytes": float(ISOLATION_GET_BYTES),
        "credit_gbps": credit_gbps,
    }
    drops = int(get_drops + put_drops)
    return IsolationResult(
        policy=policy,
        victim_latencies_ms=victim,
        p50=_percentile(victim, 50),
        p99=_percentile(victim, 99),
        drops=drops,
        get_drops=int(get_drops),
        put_drops=int(put_drops),
        interference_p99=_percentile(interference, 99) if interference else _percentile(victim, 99),
        interference_p50=_percentile(interference, 50) if interference else _percentile(victim, 50),
        quiet_p99=_percentile(quiet, 99) if quiet else _percentile(victim, 99),
        series=series,
        parts=parts,
    )
