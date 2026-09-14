"""Unit tests for the TensorKV algorithm replica."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tensorkv.appliance import ApplianceConfig, TensorKVAppliance
from tensorkv.bloom import BloomFilter
from tensorkv.crossbar import AtomicCrossbar
from tensorkv.cuckoo import CuckooTable
from tensorkv.engine import PagedEngine
from tensorkv.experiments import monotonic_race, occupancy_sweep, prefix_activation, scatter_gather_rtts
from tensorkv.hashutil import fingerprint, pack_key
from tensorkv.libtkv import TensorKVContext
from tensorkv.transport import VirtualOutputQueues, Packet, simulate_noisy_neighbor


class TestPrimitives(unittest.TestCase):
    def setUp(self) -> None:
        self.tkv = TensorKVAppliance(ApplianceConfig(n_buckets=32, n_pages=64, block_size=32))

    def test_put_get_roundtrip(self) -> None:
        self.tkv.put(1, 0, b"alpha")
        self.tkv.put(1, 1, b"bravo")
        got = self.tkv.get(1, [0, 1])
        self.assertTrue(got.ok)
        self.assertEqual(got.hits, [0, 1])
        self.assertTrue(got.payload.startswith(b"alpha"))
        self.assertIn(b"bravo", got.payload)

    def test_scatter_gather_order(self) -> None:
        for i, name in enumerate((b"A", b"B", b"C")):
            self.tkv.put(2, i, name)
        got = self.tkv.get(2, [2, 0])
        self.assertEqual(got.hits, [2, 0])
        self.assertTrue(got.payload.startswith(b"C"))
        self.assertIn(b"A", got.payload[32:64] if len(got.payload) >= 64 else got.payload)

    def test_probe_miss_then_hit(self) -> None:
        miss = self.tkv.probe(12345)
        self.assertFalse(miss.hit)
        self.assertFalse(miss.hbm_accessed)
        self.tkv.put(4, 0, b"p0")
        self.tkv.put(4, 1, b"p1")
        self.tkv.publish_prefix(12345, 4, [0, 1])
        hit = self.tkv.probe(12345)
        self.assertTrue(hit.hit)
        self.assertEqual(hit.handles, [0, 1])
        self.assertEqual(hit.refcount, 2)
        self.assertFalse(hit.hbm_accessed)

    def test_evict_then_miss(self) -> None:
        self.tkv.put(5, 0, b"keep")
        self.tkv.put(5, 1, b"drop")
        self.tkv.evict_key(5, 1)
        got = self.tkv.get(5, [0, 1])
        self.assertEqual(got.hits, [0])
        self.assertEqual(got.misses, [1])
        self.assertNotIn(b"drop", got.payload)

    def test_range_oldest_evict(self) -> None:
        for i in range(6):
            self.tkv.put(8, i, bytes([i]))
        res = self.tkv.evict(8, policy="oldest", k=2)
        self.assertEqual(res.evicted, [0, 1])
        got = self.tkv.get(8, list(range(6)))
        self.assertEqual(got.misses, [0, 1])
        self.assertEqual(got.hits, [2, 3, 4, 5])


class TestConsistency(unittest.TestCase):
    def test_monotonic_read_race(self) -> None:
        result = monotonic_race()
        self.assertTrue(result["monotonic"])
        self.assertTrue(result["post_evict_miss"])
        self.assertFalse(result["stale_read"])
        self.assertTrue(result["no_payload_during_hazard"])

    def test_reallocated_page_not_visible(self) -> None:
        tkv = TensorKVAppliance(ApplianceConfig(n_pages=2, n_buckets=8, block_size=16))
        tkv.put(1, 0, b"OLD")
        tkv.begin_evict_key(1, 0)
        during = tkv.get(1, [0])
        self.assertGreater(during.recirculations, 0)
        self.assertIn(0, during.misses)
        self.assertNotIn(b"OLD", during.payload)
        self.assertFalse(during.ok)
        tkv.complete_evict_key(1, 0)
        tkv.put(1, 1, b"NEW")
        after = tkv.get(1, [0])
        self.assertIn(0, after.misses)
        self.assertNotIn(b"OLD", after.payload)
        self.assertIn(b"NEW", tkv.get(1, [1]).payload)

    def test_monotonic_read_requires_recirc_and_miss(self) -> None:
        result = monotonic_race()
        self.assertTrue(result["monotonic"])
        self.assertTrue(result["recirculated"])
        self.assertTrue(result["miss_during_hazard"])
        self.assertTrue(result["no_payload_during_hazard"])
        self.assertTrue(result["post_evict_miss"])
        self.assertFalse(result["stale_read"])


class TestCuckooAndBloom(unittest.TestCase):
    def test_cuckoo_high_load(self) -> None:
        table = CuckooTable(n_buckets=32)
        cap = table.capacity
        ok = 0
        for i in range(int(cap * 0.9)):
            path = table.insert(pack_key(1, i), i)
            self.assertIn(path, ("fast", "slow"))
            ok += 1
        self.assertEqual(table.size, ok)
        self.assertLess(table.slow_inserts / ok, 0.2)
        self.assertIsNotNone(table.lookup(pack_key(1, 0)))
        self.assertGreater(table.hbm_key_verifies, 0)

    def test_fingerprint_collision_checks_full_key(self) -> None:
        table = CuckooTable(n_buckets=8)
        table.insert(pack_key(1, 0), 10)
        table.lookup(pack_key(1, 0))
        self.assertGreaterEqual(table.hbm_key_verifies, 1)

    def test_fingerprint_nonzero(self) -> None:
        for i in range(1000):
            self.assertNotEqual(fingerprint(pack_key(i, i * 3)), 0)

    def test_bloom_no_false_negative(self) -> None:
        bf = BloomFilter(n_bits=4096, n_hashes=3)
        keys = [i * 99991 for i in range(200)]
        for k in keys:
            bf.add(k)
        for k in keys:
            self.assertTrue(bf.maybe_contains(k))


class TestAllocator(unittest.TestCase):
    def test_fifo_batch_refill(self) -> None:
        tkv = TensorKVAppliance(ApplianceConfig(n_pages=200, n_buckets=64, store_payloads=False))
        self.assertGreaterEqual(tkv.allocator.slow_refills, 1)
        self.assertLessEqual(len(tkv.allocator.fifo), 64)
        for i in range(80):
            self.assertTrue(tkv.put(1, i).ok)
        self.assertGreater(tkv.allocator.slow_refills, 1)
        self.assertEqual(tkv.allocator.used_pages, 80)


class TestLFRU(unittest.TestCase):
    def test_lfru_protects_shared_prefix(self) -> None:
        tkv = TensorKVAppliance(ApplianceConfig(n_pages=12, n_buckets=16, store_payloads=False))
        for i in range(6):
            tkv.put(0, i, prefix_hash=99)
        tkv.publish_prefix(99, 0, list(range(6)))
        # Probe bumps refcount above 1.
        tkv.probe(99)
        for i in range(6):
            tkv.put(1, i)
        # Pool is full (12 pages). Reclaim 4 under LFRU — should take unique ctx 1.
        victims = tkv.reclaim(4, policy="lfru")
        prefix_got = tkv.get(0, list(range(6)))
        self.assertEqual(prefix_got.misses, [])
        self.assertTrue(len(victims) == 4)


class TestTransport(unittest.TestCase):
    def test_voq_preempts_put(self) -> None:
        voq = VirtualOutputQueues(quantum=4096)
        voq.enqueue(Packet(0, "PUT", 1, 4096, 1))
        voq.enqueue(Packet(0, "PUT", 1, 4096, 2))
        voq.enqueue(Packet(0, "GET", 2, 4096, 3))
        first = voq.dequeue()
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.opcode, "GET")
        self.assertGreaterEqual(voq.preemptions, 1)

    def test_isolation_ranking(self) -> None:
        kw = dict(duration_s=4.0, tick_us=50.0, interference_start_s=1.0, interference_end_s=3.0)
        fifo = simulate_noisy_neighbor("fifo", **kw)
        qos = simulate_noisy_neighbor("qos", **kw)
        pacing = simulate_noisy_neighbor("pacing", **kw)
        both = simulate_noisy_neighbor("both", **kw)
        self.assertGreater(fifo.interference_p99, both.interference_p99)
        self.assertGreater(qos.interference_p99, both.interference_p99)
        self.assertGreater(fifo.drops, both.drops)
        self.assertLess(pacing.interference_p99, fifo.interference_p99)
        self.assertLessEqual(both.interference_p99, pacing.interference_p99)

    def test_voq_used_by_appliance(self) -> None:
        tkv = TensorKVAppliance(ApplianceConfig(n_pages=16, n_buckets=8, block_size=32))
        tkv.put(1, 0, b"x")
        tkv.get(1, [0])
        self.assertGreater(tkv.voq.dequeued_low + tkv.voq.dequeued_high, 0)

    def test_crossbar_stalls_lookup_during_commit(self) -> None:
        xb = AtomicCrossbar()
        xb.commit()
        stall = xb.lookup_gate()
        self.assertGreaterEqual(stall, 4)
        self.assertEqual(xb.lookup_stalls, 1)
        xb.release()
        self.assertEqual(xb.lookup_gate(), 0)


class TestLibTkvAndEngine(unittest.TestCase):
    def test_async_cq(self) -> None:
        ctx = TensorKVContext(TensorKVAppliance(ApplianceConfig(block_size=16, n_pages=16, n_buckets=8)))
        ctx.put_async(1, 0, b"x")
        ctx.get_async(1, [0])
        c1 = ctx.poll_completion()
        c2 = ctx.poll_completion()
        self.assertEqual(c1.op, "PUT")
        self.assertEqual(c2.op, "GET")
        self.assertTrue(c2.ok)

    def test_prefix_reuse_engine(self) -> None:
        result = prefix_activation()
        self.assertFalse(result["first_prefix_hit"])
        self.assertTrue(result["second_prefix_hit"])
        self.assertGreater(result["skipped_tokens"], 0)
        self.assertLess(result["second_ttft_ms"], 18.0)
        self.assertGreater(result["second_ttft_parts"]["fetch"], 0.0)

    def test_scatter_gather_one_rtt(self) -> None:
        r = scatter_gather_rtts(6)
        self.assertEqual(r["tensorkv_rtts"], 1)
        self.assertTrue(r["gathered_ok"])

    def test_occupancy_runs(self) -> None:
        pts = occupancy_sweep(loads=(0.5, 0.8))
        self.assertEqual(len(pts), 2)
        self.assertGreaterEqual(pts[1].load, pts[0].load)


def run_unittest() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run_unittest())
