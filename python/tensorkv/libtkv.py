"""libtkv: host-side asynchronous driver.

The CPU submits 64-byte semantic descriptors; tensor payloads are not copied
by the CPU. Completions are polled from a completion queue after Ethernet
RTT and device execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .appliance import GetResult, ProbeResult, PutResult, TensorKVAppliance
from .baselines import TKV_NETWORK_NS, TKV_PCIE_DMA_NS
from .descriptor import DESCRIPTOR_BYTES, Descriptor, evict_descriptor, get_descriptor, probe_descriptor, put_descriptor
from .world import World


@dataclass
class Completion:
    op: str
    ok: bool
    payload: bytes = b""
    meta: dict[str, Any] = field(default_factory=dict)
    ticket: int = 0
    descriptor: bytes = b""
    complete_at_ns: int = 0


class TensorKVContext:
    def __init__(self, appliance: TensorKVAppliance | None = None, auto_flush: bool = True) -> None:
        self.device = appliance or TensorKVAppliance()
        self.world = self.device.world
        self.auto_flush = auto_flush
        self._cq: list[Completion] = []
        self._pending: dict[int, Completion] = {}
        self.registered: list[tuple[int, int]] = []
        self.submitted = 0
        self._ticket = 0
        self.sq_depth = 0
        self.descriptors_posted = 0
        self.overflow_bytes = 0

    def register_gpu_memory(self, gpu_ptr: int, size: int) -> None:
        self.registered.append((gpu_ptr, size))

    def _post(self, op: str, desc_bytes: bytes, run) -> int:
        self._ticket += 1
        ticket = self._ticket
        self.submitted += 1
        self.sq_depth += 1
        self.descriptors_posted += 1
        self.world.advance(TKV_NETWORK_NS)
        result = run()
        self.world.advance(TKV_PCIE_DMA_NS)
        ok = bool(getattr(result, "ok", True) if op != "PROBE" else getattr(result, "hit", False))
        if op == "PROBE":
            ok = bool(result.hit)
        payload = getattr(result, "payload", b"") if op == "GET" else b""
        meta: dict[str, Any]
        if op == "PUT":
            meta = {"context_id": None, "phys": result.phys, "path": result.path}
        elif op == "GET":
            meta = {"hits": result.hits, "misses": result.misses, "recirc": result.recirculations}
        elif op == "PROBE":
            meta = {"handles": result.handles, "ref": result.refcount}
        else:
            meta = {"evicted": result.evicted}
        c = Completion(op, ok, payload=payload if isinstance(payload, (bytes, bytearray)) else b"", meta=meta, ticket=ticket, descriptor=desc_bytes, complete_at_ns=self.world.now_ns)
        self.sq_depth = max(0, self.sq_depth - 1)
        if self.auto_flush:
            self._cq.append(c)
        else:
            self._pending[ticket] = c
            self._cq.append(c)
        return ticket

    def put_async(self, context_id: int, seq_id: int, data: bytes | None = None, prefix_hash: int | None = None, gpu_ptr: int = 0) -> PutResult:
        desc = put_descriptor(context_id, seq_id, gpu_ptr=gpu_ptr)
        raw = desc.encode()
        assert len(raw) == DESCRIPTOR_BYTES
        result: PutResult | None = None

        def run() -> PutResult:
            nonlocal result
            result = self.device.put(context_id, seq_id, data, prefix_hash=prefix_hash)
            return result

        self._post("PUT", raw, run)
        assert result is not None
        return result

    def get_async(self, context_id: int, block_ids: list[int], credit_gbps: float | None = 40.0, gpu_ptr: int = 0) -> GetResult:
        desc = get_descriptor(context_id, block_ids, credit_gbps=credit_gbps or 0.0, gpu_ptr=gpu_ptr)
        raw, overflow = desc.encode_request()
        self.overflow_bytes += len(overflow)
        result: GetResult | None = None

        def run() -> GetResult:
            nonlocal result
            walked = Descriptor.decode(raw, overflow)
            result = self.device.get(context_id, walked.block_ids, credit_gbps=credit_gbps)
            return result

        self._post("GET", raw, run)
        assert result is not None
        return result

    def probe(self, prompt_hash: int) -> ProbeResult:
        desc = probe_descriptor(prompt_hash)
        raw = desc.encode()
        result: ProbeResult | None = None

        def run() -> ProbeResult:
            nonlocal result
            result = self.device.probe(prompt_hash)
            return result

        self._post("PROBE", raw, run)
        assert result is not None
        return result

    def evict(self, context_id: int, policy: str = "lru", k: int | None = None):
        desc = evict_descriptor(context_id)
        raw = desc.encode()
        result = None

        def run():
            nonlocal result
            result = self.device.evict(context_id, policy=policy, k=k)
            return result

        self._post("EVICT", raw, run)
        return result

    def poll_completion(self) -> Completion | None:
        if not self._cq:
            return None
        return self._cq.pop(0)

    def drain(self) -> list[Completion]:
        out = list(self._cq)
        self._cq.clear()
        return out


def open_device(dev_id: int = 0) -> TensorKVContext:  # noqa: ARG001
    return TensorKVContext()
