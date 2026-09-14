"""64-byte libtkv descriptors (paper § Driver, Listing).

Deployment submits these through a kernel-bypass Ethernet queue. The CPU
never copies tensor payloads; the descriptor names a registered GPU buffer.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

DESCRIPTOR_BYTES = 64
_PACK = struct.Struct("<BBHIHBBIQQ8I")
assert _PACK.size == DESCRIPTOR_BYTES

OP_PUT = 1
OP_GET = 2
OP_PROBE = 3
OP_EVICT = 4

_OP_NAME = {OP_PUT: "PUT", OP_GET: "GET", OP_PROBE: "PROBE", OP_EVICT: "EVICT"}
_OP_CODE = {v: k for k, v in _OP_NAME.items()}


@dataclass
class Descriptor:
    opcode: str
    context_id: int
    n_blocks: int = 0
    credit_gbps: float = 40.0
    policy: int = 0
    seq_id: int = 0
    gpu_ptr: int = 0
    prompt_hash: int = 0
    block_ids: list[int] = field(default_factory=list)

    def encode(self) -> bytes:
        ids = (self.block_ids + [0] * 8)[:8]
        credit_centi = int(round(self.credit_gbps * 100))
        return _PACK.pack(
            _OP_CODE[self.opcode],
            1 if self.credit_gbps else 0,
            self.n_blocks & 0xFFFF,
            self.context_id & 0xFFFFFFFF,
            credit_centi & 0xFFFF,
            self.policy & 0xFF,
            0,
            self.seq_id & 0xFFFFFFFF,
            self.gpu_ptr & ((1 << 64) - 1),
            self.prompt_hash & ((1 << 64) - 1),
            *ids,
        )

    @classmethod
    def decode(cls, raw: bytes) -> "Descriptor":
        if len(raw) != DESCRIPTOR_BYTES:
            raise ValueError(f"descriptor must be {DESCRIPTOR_BYTES} bytes, got {len(raw)}")
        op, flags, n_blocks, ctx, credit, policy, _rsv, seq, gpu, ph, *ids = _PACK.unpack(raw)
        opcode = _OP_NAME.get(op)
        if opcode is None:
            raise ValueError(f"unknown opcode {op}")
        n = int(n_blocks)
        return cls(
            opcode=opcode,
            context_id=int(ctx),
            n_blocks=n,
            credit_gbps=(credit / 100.0) if flags else 0.0,
            policy=int(policy),
            seq_id=int(seq),
            gpu_ptr=int(gpu),
            prompt_hash=int(ph),
            block_ids=[int(x) for x in ids[: max(0, min(8, n))]],
        )


def put_descriptor(context_id: int, seq_id: int, gpu_ptr: int = 0) -> Descriptor:
    return Descriptor(opcode="PUT", context_id=context_id, n_blocks=1, seq_id=seq_id, gpu_ptr=gpu_ptr, block_ids=[seq_id])


def get_descriptor(context_id: int, block_ids: list[int], credit_gbps: float = 40.0, gpu_ptr: int = 0) -> Descriptor:
    return Descriptor(
        opcode="GET",
        context_id=context_id,
        n_blocks=len(block_ids),
        credit_gbps=credit_gbps,
        gpu_ptr=gpu_ptr,
        block_ids=list(block_ids[:8]),
    )


def probe_descriptor(prompt_hash: int) -> Descriptor:
    return Descriptor(opcode="PROBE", context_id=0, prompt_hash=prompt_hash)


def evict_descriptor(context_id: int, policy: int = 0) -> Descriptor:
    return Descriptor(opcode="EVICT", context_id=context_id, policy=policy)
