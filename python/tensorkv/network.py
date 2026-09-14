"""Count-based incast / noisy-neighbor model derived from link rate and buffers.

No per-policy magic latency additives. Delay and drops come from:

  * two 100 GbE clients sharing one 100 GbE TensorKV port (2:1 incast)
  * 4 KB packet serialization
  * a shallow ToR/NIC FIFO that tail-drops (QoS lives on the device, too late)
  * optional PUT pacing at the GET credit (default 40 Gbps)
  * memory-node VOQ: GET/PROBE over PUT, or a shared FIFO (head-of-line)
  * 200 ms retransmission timeout when a GET request is dropped

Time is advanced in ticks. Packet counts are fluid so a 30 s run is O(ticks).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import DEFAULT_CREDIT_GBPS, FAST_PATH_HBM_HIT_NS, LINK_GBPS

PKT_BYTES = 4096
RTO_MS = 200.0  # timeout-based retransmission after a drop (paper incast)


def packets_per_tick(gbps: float, tick_us: float, pkt_bytes: int = PKT_BYTES) -> float:
    """Packets that fit on `gbps` during `tick_us` microseconds."""
    bytes_per_us = gbps * 1e9 / 8.0 / 1e6
    return bytes_per_us * tick_us / pkt_bytes


def serialize_ms(pkt_bytes: int = PKT_BYTES, gbps: float = LINK_GBPS) -> float:
    return (pkt_bytes * 8) / (gbps * 1e6)


@dataclass
class IsolationResult:
    policy: str
    victim_latencies_ms: list[float]
    p50: float
    p99: float
    drops: int
    interference_p99: float
    series: list[tuple[float, float]] = field(default_factory=list)


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    idx = min(len(ys) - 1, max(0, int(round((p / 100.0) * (len(ys) - 1)))))
    return ys[idx]


def simulate_noisy_neighbor(
    policy: str,
    duration_s: float = 30.0,
    tick_us: float = 50.0,
    interference_start_s: float = 10.0,
    interference_end_s: float = 20.0,
    switch_buffer_packets: int = 32,
    credit_gbps: float = DEFAULT_CREDIT_GBPS,
    get_period_us: float = 100.0,
    link_gbps: float = LINK_GBPS,
) -> IsolationResult:
    """Tenant A issues a steady GET stream; Tenant B blasts PUTs in a window.

    Policies: fifo | qos | pacing | both.

    The ToR is always a dumb FIFO. ``qos`` / ``both`` enable strict-priority
    VOQ on the memory node (after the ToR). Pacing limits Tenant B to the GET
    credit so the 2:1 incast disappears.
    """
    if policy not in {"fifo", "qos", "pacing", "both"}:
        raise ValueError(policy)
    use_qos = policy in ("qos", "both")
    use_pacing = policy in ("pacing", "both")

    cap = packets_per_tick(link_gbps, tick_us)
    paced = packets_per_tick(credit_gbps, tick_us)
    n_ticks = int(duration_s * 1e6 / tick_us)
    start_i = int(interference_start_s * 1e6 / tick_us)
    end_i = int(interference_end_s * 1e6 / tick_us)
    get_every = max(1, int(round(get_period_us / tick_us)))

    switch_q = 0.0
    drops = 0.0
    victim: list[float] = []
    interference: list[float] = []
    series: list[tuple[float, float]] = []
    serial = serialize_ms(PKT_BYTES, link_gbps)
    hol_ns = FAST_PATH_HBM_HIT_NS

    sample_every = max(1, n_ticks // 400)

    for t in range(n_ticks):
        in_burst = start_i <= t < end_i
        put_rate = (paced if use_pacing else cap) if in_burst else 0.0
        get_rate = 1.0 if t % get_every == 0 else 0.0

        offered = put_rate + get_rate
        slack = cap + (switch_buffer_packets - switch_q)
        overflow = max(0.0, offered - slack)
        admitted = offered - overflow
        switch_q = min(float(switch_buffer_packets), max(0.0, switch_q + admitted - cap))
        drops += overflow

        # Dumb ToR FIFO: a line-rate PUT burst occupies the serializer, so
        # excess is the GET that arrived last (lock-out). Device-side QoS
        # cannot resurrect a request that already died at the ToR.
        get_drop = min(get_rate, overflow) if overflow > 0 else 0.0

        get_lat: float | None = None
        if get_rate > 0:
            if get_drop >= get_rate:
                get_lat = RTO_MS
            else:
                if use_qos:
                    hol_ms = 0.0
                else:
                    hol_ms = put_rate * (hol_ns / 1e6)
                get_lat = serial + hol_ms
            victim.append(get_lat)
            if in_burst:
                interference.append(get_lat)

        if t % sample_every == 0:
            last = victim[-1] if victim else serial
            series.append((t * tick_us / 1e6, last))

    return IsolationResult(
        policy=policy,
        victim_latencies_ms=victim,
        p50=_percentile(victim, 50),
        p99=_percentile(victim, 99),
        drops=int(drops),
        interference_p99=_percentile(interference, 99) if interference else _percentile(victim, 99),
        series=series,
    )
