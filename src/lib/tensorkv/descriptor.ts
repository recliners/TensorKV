import { DESCRIPTOR_BYTES } from "./types";

export { DESCRIPTOR_BYTES };

const OP: Record<string, number> = { PUT: 1, GET: 2, PROBE: 3, EVICT: 4 };
const OP_NAME = ["", "PUT", "GET", "PROBE", "EVICT"];

export type Descriptor = {
  opcode: string;
  contextId: number;
  nBlocks: number;
  creditGbps: number;
  policy: number;
  seqId: number;
  gpuPtr: bigint;
  promptHash: bigint;
  blockIds: number[];
};

export function encodeDescriptor(d: Descriptor): Uint8Array {
  const buf = new ArrayBuffer(DESCRIPTOR_BYTES);
  const view = new DataView(buf);
  view.setUint8(0, OP[d.opcode] ?? 0);
  view.setUint8(1, d.creditGbps ? 1 : 0);
  view.setUint16(2, d.nBlocks & 0xffff, true);
  view.setUint32(4, d.contextId >>> 0, true);
  view.setUint16(8, Math.round(d.creditGbps * 100) & 0xffff, true);
  view.setUint8(10, d.policy & 0xff);
  view.setUint8(11, 0);
  view.setUint32(12, d.seqId >>> 0, true);
  view.setBigUint64(16, d.gpuPtr, true);
  view.setBigUint64(24, d.promptHash, true);
  const ids = [...d.blockIds, 0, 0, 0, 0, 0, 0, 0, 0].slice(0, 8);
  for (let i = 0; i < 8; i++) view.setUint32(32 + i * 4, ids[i] >>> 0, true);
  return new Uint8Array(buf);
}

export function putDescriptor(contextId: number, seqId: number, gpuPtr = 0n): Descriptor {
  return {
    opcode: "PUT",
    contextId,
    nBlocks: 1,
    creditGbps: 0,
    policy: 0,
    seqId,
    gpuPtr,
    promptHash: 0n,
    blockIds: [seqId],
  };
}

export function getDescriptor(contextId: number, blockIds: number[], creditGbps = 40, gpuPtr = 0n): Descriptor {
  return {
    opcode: "GET",
    contextId,
    nBlocks: blockIds.length,
    creditGbps,
    policy: 0,
    seqId: 0,
    gpuPtr,
    promptHash: 0n,
    blockIds: blockIds.slice(0, 8),
  };
}

export function probeDescriptor(promptHash: bigint): Descriptor {
  return {
    opcode: "PROBE",
    contextId: 0,
    nBlocks: 0,
    creditGbps: 0,
    policy: 0,
    seqId: 0,
    gpuPtr: 0n,
    promptHash,
    blockIds: [],
  };
}

export function evictDescriptor(contextId: number, policy = 0): Descriptor {
  return {
    opcode: "EVICT",
    contextId,
    nBlocks: 0,
    creditGbps: 0,
    policy,
    seqId: 0,
    gpuPtr: 0n,
    promptHash: 0n,
    blockIds: [],
  };
}

export function decodeDescriptor(raw: Uint8Array): Descriptor {
  if (raw.length !== DESCRIPTOR_BYTES) throw new Error(`descriptor must be ${DESCRIPTOR_BYTES} bytes`);
  const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
  const op = view.getUint8(0);
  const flags = view.getUint8(1);
  const nBlocks = view.getUint16(2, true);
  const contextId = view.getUint32(4, true);
  const credit = view.getUint16(8, true);
  const policy = view.getUint8(10);
  const seqId = view.getUint32(12, true);
  const gpuPtr = view.getBigUint64(16, true);
  const promptHash = view.getBigUint64(24, true);
  const blockIds: number[] = [];
  const n = Math.max(0, Math.min(8, nBlocks));
  for (let i = 0; i < n; i++) blockIds.push(view.getUint32(32 + i * 4, true));
  return {
    opcode: OP_NAME[op] ?? "PUT",
    contextId,
    nBlocks,
    creditGbps: flags ? credit / 100 : 0,
    policy,
    seqId,
    gpuPtr,
    promptHash,
    blockIds,
  };
}
