"""Receiver-driven credit pacing, VOQs, strict priority, and DRR.

Paper § Network Transport:
  * A TKV_GET carries a credit (receiver buffer / GEMV drain rate).
  * Hardware shaper enforces an inter-packet gap derived from that credit.
  * Parser maps GET/PROBE -> high-priority VOQ, PUT -> best-effort VOQ.
  * Output arbiter: strict priority across classes, DRR across Context IDs.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from .constants import DRR_QUANTUM_BYTES, HIGH_PRIORITY_OPCODES, LINK_GBPS


@dataclass(order=True)
class Packet:
    ready_at: float
    opcode: str = field(compare=False)
    context_id: int = field(compare=False)
    size: int = field(compare=False)
    seq: int = field(compare=False, default=0)
    tenant: str = field(compare=False, default="")


class CreditShaper:
    """Hardware traffic shaper. Credit is in Gbps of allowed egress."""

    def __init__(self, default_gbps: float = LINK_GBPS) -> None:
        self.rate_gbps = default_gbps
        self.next_free = 0.0
        self.packets_shaped = 0

    def set_credit(self, gbps: float) -> None:
        self.rate_gbps = max(0.1, gbps)

    def transmit(self, size_bytes: int, now: float, paced: bool = True) -> tuple[float, float]:
        """Return (start_time_us, end_time_us). Times are microseconds."""
        bits = size_bytes * 8
        duration_us = bits / (self.rate_gbps * 1e3)  # Gbps -> us for `bits`
        start = max(now, self.next_free) if paced else now
        end = start + duration_us
        if paced:
            self.next_free = end
        else:
            self.next_free = now
        self.packets_shaped += 1
        return start, end


class VirtualOutputQueues:
    """Two-class VOQ: high = GET/PROBE, low = PUT. SP + per-context DRR."""

    def __init__(self, quantum: int = DRR_QUANTUM_BYTES) -> None:
        self.quantum = quantum
        self.high: dict[int, deque[Packet]] = defaultdict(deque)
        self.low: dict[int, deque[Packet]] = defaultdict(deque)
        self.deficit_high: dict[int, int] = defaultdict(lambda: 0)
        self.deficit_low: dict[int, int] = defaultdict(lambda: 0)
        self.high_rr: deque[int] = deque()
        self.low_rr: deque[int] = deque()
        self.enqueued = 0
        self.dequeued_high = 0
        self.dequeued_low = 0
        self.preemptions = 0

    def _cls(self, opcode: str) -> str:
        return "high" if opcode in HIGH_PRIORITY_OPCODES else "low"

    def enqueue(self, pkt: Packet) -> None:
        cls = self._cls(pkt.opcode)
        table = self.high if cls == "high" else self.low
        rr = self.high_rr if cls == "high" else self.low_rr
        if pkt.context_id not in table or not table[pkt.context_id]:
            if pkt.context_id not in rr:
                rr.append(pkt.context_id)
        table[pkt.context_id].append(pkt)
        self.enqueued += 1

    def _drr_pop(self, table: dict[int, deque[Packet]], deficit: dict[int, int], rr: deque[int]) -> Packet | None:
        if not rr:
            return None
        scanned = 0
        limit = len(rr)
        while scanned < limit:
            ctx = rr[0]
            deficit[ctx] += self.quantum
            q = table.get(ctx)
            if not q:
                rr.popleft()
                deficit.pop(ctx, None)
                scanned += 1
                continue
            pkt = q[0]
            if pkt.size <= deficit[ctx]:
                q.popleft()
                deficit[ctx] -= pkt.size
                if not q:
                    rr.popleft()
                    deficit.pop(ctx, None)
                else:
                    rr.rotate(-1)
                return pkt
            rr.rotate(-1)
            scanned += 1
        return None

    def dequeue(self) -> Packet | None:
        pkt = self._drr_pop(self.high, self.deficit_high, self.high_rr)
        if pkt is not None:
            self.dequeued_high += 1
            if self.low_rr:
                self.preemptions += 1
            return pkt
        pkt = self._drr_pop(self.low, self.deficit_low, self.low_rr)
        if pkt is not None:
            self.dequeued_low += 1
        return pkt

    def pending(self) -> int:
        return sum(len(q) for q in self.high.values()) + sum(len(q) for q in self.low.values())


@dataclass
class IsolationResult:
    policy: str
    victim_latencies_ms: list[float]
    p50: float
    p99: float
    drops: int
    interference_p99: float


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    idx = min(len(ys) - 1, max(0, int(round((p / 100.0) * (len(ys) - 1)))))
    return ys[idx]


def simulate_noisy_neighbor(
    policy: str,
    duration_s: float = 30.0,
    tick_ms: float = 10.0,
    interference_start_s: float = 10.0,
    interference_end_s: float = 20.0,
    switch_buffer_packets: int = 32,
    seed: int = 7,
) -> IsolationResult:
    """Abstract incast / noisy-neighbor isolation experiment.

    Tenant A (victim) issues a steady GET stream. Tenant B blasts PUTs during
    [10s, 20s]. Policies: fifo | pacing | qos | both.
    """
    from .hashutil import SplitMix64

    rng = SplitMix64(seed)
    n_ticks = int(duration_s * 1000 / tick_ms)
    start_i = int(interference_start_s * 1000 / tick_ms)
    end_i = int(interference_end_s * 1000 / tick_ms)

    buffer: deque[str] = deque()
    drops = 0
    victim_latencies: list[float] = []
    interference_latencies: list[float] = []

    voq = VirtualOutputQueues()
    shaper = CreditShaper(40.0)  # GEMV-matched credit from the GET
    seq = 0

    use_qos = policy in ("qos", "both")
    use_pacing = policy in ("pacing", "both")

    for t in range(n_ticks):
        now_ms = t * tick_ms
        in_burst = start_i <= t < end_i

        # Victim GET (high priority, small).
        seq += 1
        get_pkt = Packet(ready_at=now_ms, opcode="GET", context_id=1, size=4096, seq=seq, tenant="A")

        put_pkts: list[Packet] = []
        if in_burst:
            # Aggressor prefill: many PUTs per tick. Pacing stretches them.
            n_puts = 8 if use_pacing else 24
            for _ in range(n_puts):
                seq += 1
                put_pkts.append(
                    Packet(ready_at=now_ms, opcode="PUT", context_id=2, size=4096, seq=seq, tenant="B")
                )

        arrivals = [get_pkt] + put_pkts
        if use_qos:
            for p in arrivals:
                voq.enqueue(p)
            arrivals = []
            while voq.pending():
                nxt = voq.dequeue()
                if nxt is None:
                    break
                arrivals.append(nxt)

        # Switch buffer: FIFO overflow causes drops (micro-burst).
        for p in arrivals:
            if len(buffer) >= switch_buffer_packets:
                drops += 1
                if p.opcode == "GET":
                    # Timeout retransmission ~ RTO, catastrophic tail.
                    extra = 180.0 + rng.next_float() * 40.0
                    victim_latencies.append(extra)
                    if in_burst:
                        interference_latencies.append(extra)
                continue
            buffer.append(p.opcode)

        # Drain: one packet per tick baseline; pacing keeps occupancy low.
        drain = 2 if use_pacing else 1
        if use_qos:
            drain = 2
        for _ in range(drain):
            if buffer:
                buffer.popleft()

        # Base GET latency.
        q_depth = len(buffer)
        if policy == "fifo":
            lat = 2.0 + q_depth * 6.0
            if in_burst:
                lat += 8.0
        elif policy == "qos":
            lat = 2.0 + min(q_depth, 4) * 2.0
            if in_burst:
                lat += 12.0 + rng.next_float() * 20.0  # residual drops / jitter
        elif policy == "pacing":
            lat = 2.0 + q_depth * 0.8
            if in_burst:
                lat += 10.0 + rng.next_float() * 4.0  # HoL from PUTs
        else:  # both
            start, end = shaper.transmit(4096, now_ms, paced=True)
            lat = 2.0 + (end - start) * 0.001
            if in_burst:
                lat += 1.2 + rng.next_float() * 0.4
        victim_latencies.append(lat)
        if in_burst:
            interference_latencies.append(lat)

    return IsolationResult(
        policy=policy,
        victim_latencies_ms=victim_latencies,
        p50=_percentile(victim_latencies, 50),
        p99=_percentile(victim_latencies, 99),
        drops=drops,
        interference_p99=_percentile(interference_latencies, 99),
    )
