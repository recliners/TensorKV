"""64-byte libtkv doorbell plus an optional gather overflow list.

The Ethernet SQ posts a 64-byte descriptor. Up to eight BlockIDs sit inline.
Longer vectorized GETs carry the remaining IDs in a DMA gather list that the
device walks in the same request — the CPU still does not copy tensor bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

DESCRIPTOR_BYTES = 64
INLINE_IDS = 8
_PACK = struct.Struct("<BBHIHBBIQQ8I")
assert _PACK.size == DESCRIPTOR_BYTES

OP_PUT = 1
OP_GET = 2
OP_PROBE = 3
OP_EVICT = 4

_OP_NAME = {OP_PUT: "PUT", OP_GET: "GET", OP_PROBE: "PROBE", OP_EVICT: "EVICT"}
_OP_CODE = {v: k for k, v in _OP_NAME.items()}


def pack_gather_list(block_ids: list[int]) -> bytes:
    extra = [int(x) & 0xFFFFFFFF for x in block_ids[INLINE_IDS:]]
    if not extra:
        return b""
    return struct.pack(f"<{len(extra)}I", *extra)


def unpack_gather_list(inline: list[int], n_blocks: int, overflow: bytes) -> list[int]:
    n = max(0, int(n_blocks))
    head = list(inline[: min(INLINE_IDS, n)])
    if n <= INLINE_IDS:
        return head
    need = n - INLINE_IDS
    if len(overflow) < need * 4:
        raise ValueError(f"gather overflow too short: {len(overflow)} < {need * 4}")
    extra = list(struct.unpack(f"<{need}I", overflow[: need * 4]))
    return head + extra


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
        ids = list(self.block_ids)
        n = self.n_blocks if self.n_blocks else len(ids)
        inline = (ids + [0] * INLINE_IDS)[:INLINE_IDS]
        credit_centi = int(round(self.credit_gbps * 100))
        return _PACK.pack(
            _OP_CODE[self.opcode],
            1 if self.credit_gbps else 0,
            n & 0xFFFF,
            self.context_id & 0xFFFFFFFF,
            credit_centi & 0xFFFF,
            self.policy & 0xFF,
            0,
            self.seq_id & 0xFFFFFFFF,
            self.gpu_ptr & ((1 << 64) - 1),
            self.prompt_hash & ((1 << 64) - 1),
            *inline,
        )

    def overflow_bytes(self) -> bytes:
        return pack_gather_list(self.block_ids)

    def encode_request(self) -> tuple[bytes, bytes]:
        return self.encode(), self.overflow_bytes()

    @classmethod
    def decode(cls, raw: bytes, overflow: bytes = b"") -> "Descriptor":
        if len(raw) != DESCRIPTOR_BYTES:
            raise ValueError(f"descriptor must be {DESCRIPTOR_BYTES} bytes, got {len(raw)}")
        op, flags, n_blocks, ctx, credit, policy, _rsv, seq, gpu, ph, *ids = _PACK.unpack(raw)
        opcode = _OP_NAME.get(op)
        if opcode is None:
            raise ValueError(f"unknown opcode {op}")
        n = int(n_blocks)
        block_ids = unpack_gather_list([int(x) for x in ids], n, overflow)
        return cls(
            opcode=opcode,
            context_id=int(ctx),
            n_blocks=n,
            credit_gbps=(credit / 100.0) if flags else 0.0,
            policy=int(policy),
            seq_id=int(seq),
            gpu_ptr=int(gpu),
            prompt_hash=int(ph),
            block_ids=block_ids,
        )


def put_descriptor(context_id: int, seq_id: int, gpu_ptr: int = 0) -> Descriptor:
    return Descriptor(
        opcode="PUT",
        context_id=context_id,
        n_blocks=1,
        seq_id=seq_id,
        gpu_ptr=gpu_ptr,
        block_ids=[seq_id],
    )


def get_descriptor(context_id: int, block_ids: list[int], credit_gbps: float = 40.0, gpu_ptr: int = 0) -> Descriptor:
    ids = list(block_ids)
    return Descriptor(
        opcode="GET",
        context_id=context_id,
        n_blocks=len(ids),
        credit_gbps=credit_gbps,
        gpu_ptr=gpu_ptr,
        block_ids=ids,
    )


def probe_descriptor(prompt_hash: int) -> Descriptor:
    return Descriptor(opcode="PROBE", context_id=0, prompt_hash=prompt_hash)


def evict_descriptor(context_id: int, policy: int = 0) -> Descriptor:
    return Descriptor(opcode="EVICT", context_id=context_id, policy=policy)
