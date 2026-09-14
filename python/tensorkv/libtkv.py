"""libtkv: host-side asynchronous driver matching the paper listing.

The CPU submits 64-byte semantic descriptors; tensor payloads are not copied
by the CPU. Completions are polled from a completion queue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .appliance import GetResult, ProbeResult, PutResult, TensorKVAppliance


@dataclass
class Completion:
    op: str
    ok: bool
    payload: bytes = b""
    meta: dict[str, Any] = field(default_factory=dict)


class TensorKVContext:
    def __init__(self, appliance: TensorKVAppliance | None = None) -> None:
        self.device = appliance or TensorKVAppliance()
        self._cq: list[Completion] = []
        self.registered: list[tuple[int, int]] = []  # (ptr_token, size)
        self.submitted = 0

    def register_gpu_memory(self, gpu_ptr: int, size: int) -> None:
        self.registered.append((gpu_ptr, size))

    def put_async(self, context_id: int, seq_id: int, data: bytes | None = None, prefix_hash: int | None = None) -> PutResult:
        result = self.device.put(context_id, seq_id, data, prefix_hash=prefix_hash)
        self.submitted += 1
        self._cq.append(
            Completion("PUT", result.ok, meta={"context_id": context_id, "seq_id": seq_id, "phys": result.phys})
        )
        return result

    def get_async(self, context_id: int, block_ids: list[int], credit_gbps: float | None = 40.0) -> GetResult:
        result = self.device.get(context_id, block_ids, credit_gbps=credit_gbps)
        self.submitted += 1
        self._cq.append(
            Completion("GET", result.ok, payload=result.payload, meta={"hits": result.hits, "misses": result.misses})
        )
        return result

    def probe(self, prompt_hash: int) -> ProbeResult:
        result = self.device.probe(prompt_hash)
        self.submitted += 1
        self._cq.append(Completion("PROBE", result.hit, meta={"handles": result.handles, "ref": result.refcount}))
        return result

    def evict(self, context_id: int, policy: str = "lru", k: int | None = None):
        result = self.device.evict(context_id, policy=policy, k=k)
        self.submitted += 1
        self._cq.append(Completion("EVICT", True, meta={"evicted": result.evicted}))
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
