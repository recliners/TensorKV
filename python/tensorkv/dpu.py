"""DPU-DPA worker pool: per-thread packet service plus a shared NIC serializer.

Saturated throughput is min(workers × one-thread Mpps, NIC pipeline cap).
One DPA thread does 0.15 Mpps on 4 KB descriptors; the NIC saturates near 62 Gbps.
"""

from __future__ import annotations

WORKER_MPPS = 0.15
PKT_BYTES = 4096
NIC_CAP_GBPS = 62.0


def nic_cap_mpps(pkt_bytes: int = PKT_BYTES, nic_gbps: float = NIC_CAP_GBPS) -> float:
    return nic_gbps * 1e9 / (pkt_bytes * 8) / 1e6


def dpu_mpps(workers: int, pkt_bytes: int = PKT_BYTES, nic_gbps: float = NIC_CAP_GBPS) -> float:
    w = max(1, int(workers))
    return min(w * WORKER_MPPS, nic_cap_mpps(pkt_bytes, nic_gbps))


def dpu_gbps(workers: int, pkt_bytes: int = PKT_BYTES, nic_gbps: float = NIC_CAP_GBPS) -> float:
    return dpu_mpps(workers, pkt_bytes, nic_gbps) * 1e6 * pkt_bytes * 8 / 1e9


def dpu_worker_sweep(
    workers: tuple[int, ...] = (1, 2, 4, 8, 16, 32),
    pkt_bytes: int = PKT_BYTES,
    nic_gbps: float = NIC_CAP_GBPS,
) -> list[dict]:
    cap = nic_cap_mpps(pkt_bytes, nic_gbps)
    return [
        {
            "workers": w,
            "mpps": dpu_mpps(w, pkt_bytes, nic_gbps),
            "gbps": dpu_gbps(w, pkt_bytes, nic_gbps),
            "saturated": dpu_mpps(w, pkt_bytes, nic_gbps) >= cap - 0.02,
        }
        for w in workers
    ]
