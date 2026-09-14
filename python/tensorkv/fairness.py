"""DRR fairness and GET-credit vs GEMV drain experiments."""

from __future__ import annotations

from collections import defaultdict

from .constants import DEFAULT_CREDIT_GBPS, DRR_QUANTUM_BYTES, LINK_GBPS
from .incast import simulate_attention_incast
from .transport import Packet, VirtualOutputQueues


def drr_fairness(
    n_tenants: int = 4,
    packets_each: int = 64,
    pkt_bytes: int = 4096,
    quantum: int = DRR_QUANTUM_BYTES,
) -> dict:
    """Equal-quantum DRR: each tenant's GET stream should get ~1/N of the port."""
    voq = VirtualOutputQueues(quantum=quantum)
    seq = 0
    for round_i in range(packets_each):
        for t in range(n_tenants):
            voq.enqueue(Packet(ready_at=float(round_i), opcode="GET", context_id=t, size=pkt_bytes, seq=seq))
            seq += 1
        # One low-priority PUT tenant trying to steal the port.
        voq.enqueue(Packet(ready_at=float(round_i), opcode="PUT", context_id=100, size=pkt_bytes, seq=seq, tenant="put"))
        seq += 1

    served = defaultdict(int)
    put_before_gets_done = 0
    total_gets = n_tenants * packets_each
    gets_done = 0
    while voq.pending():
        pkt = voq.dequeue()
        if pkt is None:
            break
        served[pkt.context_id] += pkt.size
        if pkt.opcode == "PUT" and gets_done < total_gets:
            put_before_gets_done += 1
        if pkt.opcode == "GET":
            gets_done += 1

    tenant_bytes = [served[t] for t in range(n_tenants)]
    mean = sum(tenant_bytes) / max(1, n_tenants)
    jain = (sum(tenant_bytes) ** 2) / (n_tenants * sum(b * b for b in tenant_bytes)) if tenant_bytes else 0.0
    return {
        "n_tenants": n_tenants,
        "bytes_per_tenant": tenant_bytes,
        "mean_bytes": mean,
        "max_min_ratio": (max(tenant_bytes) / min(tenant_bytes)) if min(tenant_bytes) else float("inf"),
        "jain_fairness": round(jain, 4),
        "put_bytes": served[100],
        "get_bytes": sum(tenant_bytes),
        "preemptions": voq.preemptions,
        "puts_while_gets_pending": put_before_gets_done,
        "gets_finish_before_put": put_before_gets_done == 0,
    }


def credit_vs_gemv_sweep(
    gemv_gbps: float = DEFAULT_CREDIT_GBPS,
    credits: tuple[float, ...] = (10.0, 20.0, 40.0, 80.0, 100.0),
) -> list[dict]:
    """Attention incast: credit below GEMV drain leaves slack; above it overflows."""
    rows = []
    for c in credits:
        blast = simulate_attention_incast(credit_gbps=None, gemv_gbps=gemv_gbps)
        paced = simulate_attention_incast(credit_gbps=c, gemv_gbps=gemv_gbps)
        rows.append(
            {
                "credit_gbps": c,
                "gemv_gbps": gemv_gbps,
                "link_gbps": LINK_GBPS,
                "paced_drops": paced.drops,
                "blast_drops": blast.drops,
                "gpu_drops": paced.gpu_drops,
                "paced_arrival_gbps": paced.arrival_gbps,
                "overflow_bytes": paced.overflow_bytes,
                "gpu_overflow_bytes": paced.gpu_overflow_bytes,
                "credit_matches_drain": abs(c - gemv_gbps) < 1e-6,
                "over_credit": c > gemv_gbps + 1e-6,
            }
        )
    return rows
