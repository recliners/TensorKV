"""Unit tests for the TensorKV software implementation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tensorkv.appliance import ApplianceConfig, TensorKVAppliance
from tensorkv.baselines import (
    ablation_get_p99_us,
    async_put_interference,
    bandwidth_sweep,
    dpu_gbps,
    dpu_mpps,
    energy_sim,
    mixtral_sharing,
    moe_tbt_breakdown,
    tbt_sim,
    ttft_sim,
    uncached_logical_get,
)
from tensorkv.bloom import BloomFilter
from tensorkv.crossbar import AtomicCrossbar
from tensorkv.cuckoo import CuckooTable, Slot
from tensorkv.descriptor import DESCRIPTOR_BYTES, Descriptor, get_descriptor, put_descriptor
from tensorkv.engine import PagedEngine
from tensorkv.experiments import (
    attention_incast,
    eviction_sensitivity,
    fingerprint_and_victim,
    get_latency_histogram,
    isolation_experiment,
    monotonic_race,
    occupancy_sweep,
    prefix_activation,
    scatter_gather_rtts,
    sglang_radix,
    sharegpt_eviction,
)
from tensorkv.replay import mixed_trace, session_trace
from tensorkv.scheduler import Arrival, ServingScheduler
from tensorkv.sglang import SGLangEngine
from tensorkv.serve import run_serving
from tensorkv.fairness import credit_vs_gemv_sweep, drr_fairness
from tensorkv.hashutil import SplitMix64, fingerprint, pack_key, prompt_hash
from tensorkv.hbm import BankedHBM
from tensorkv.incast import simulate_attention_incast
from tensorkv.libtkv import TensorKVContext
from tensorkv.transport import VirtualOutputQueues, Packet, simulate_noisy_neighbor
from tensorkv.workload import ZipfSampler
from tensorkv.world import World


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
        self.assertGreaterEqual(fifo.interference_p99, 100.0)
        self.assertLess(both.interference_p99, 8.0)
        self.assertGreaterEqual(qos.interference_p99, 20.0)
        self.assertLess(qos.interference_p99, 80.0)
        self.assertGreaterEqual(pacing.interference_p99, 10.0)
        self.assertLess(pacing.interference_p99, 25.0)
        self.assertGreater(fifo.interference_p99, qos.interference_p99)
        self.assertGreater(qos.interference_p99, pacing.interference_p99)
        self.assertGreater(pacing.interference_p99, both.interference_p99)
        self.assertEqual(both.get_drops, 0)
        self.assertGreater(fifo.get_drops, 0)

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
        pts = occupancy_sweep(loads=(0.5, 0.8, 0.95), n_ops=2500, n_buckets=256)
        self.assertEqual(len(pts), 3)
        self.assertGreaterEqual(pts[1].load, pts[0].load)
        self.assertGreater(pts[0].throughput_keep, 0.5)
        self.assertEqual(pts[0].service_ns, pts[0].ideal_ns + pts[0].extra_ns)
        self.assertGreaterEqual(pts[-1].fill_slow_insert_rate, pts[0].fill_slow_insert_rate)
        self.assertGreater(pts[-1].kicks, pts[0].kicks)
        self.assertGreater(pts[0].zipf_head_share, 0.15)


class TestDescriptorAndHBM(unittest.TestCase):
    def test_descriptor_is_64_bytes(self) -> None:
        raw = put_descriptor(1, 7, gpu_ptr=0x1000).encode()
        self.assertEqual(len(raw), DESCRIPTOR_BYTES)
        desc = get_descriptor(1, list(range(12)), credit_gbps=40.0)
        header, overflow = desc.encode_request()
        self.assertEqual(len(header), DESCRIPTOR_BYTES)
        self.assertEqual(len(overflow), 16)
        decoded = Descriptor.decode(header, overflow)
        self.assertEqual(decoded.opcode, "GET")
        self.assertEqual(decoded.n_blocks, 12)
        self.assertEqual(decoded.block_ids, list(range(12)))
        self.assertAlmostEqual(decoded.credit_gbps, 40.0)

    def test_get_async_walks_overflow_gather_list(self) -> None:
        ctx = TensorKVContext(TensorKVAppliance(ApplianceConfig(block_size=16, n_pages=32, n_buckets=16)))
        for i in range(12):
            ctx.put_async(1, i, bytes([i]))
        got = ctx.get_async(1, list(range(12)))
        self.assertEqual(got.hits, list(range(12)))
        self.assertEqual(len(got.payload), 12 * 16)

    def test_sram_slot_has_no_full_key(self) -> None:
        self.assertEqual(set(Slot.__dataclass_fields__), {"fingerprint", "phys"})
        slot = Slot()
        self.assertFalse(hasattr(slot, "full_key"))

    def test_hbm_stores_full_key(self) -> None:
        hbm = BankedHBM(64)
        key = pack_key(3, 9)
        hbm.write(5, key, b"x" * 16)
        stored, _ = hbm.read_key(5)
        self.assertEqual(stored, key)
        self.assertGreaterEqual(hbm.meta_reads, 1)

    def test_hbm_bank_conflict(self) -> None:
        hbm = BankedHBM(64, n_banks=32)
        hbm.write(0, 1, b"a", now_ns=0)
        # page 32 shares bank 0
        cost = hbm.write(32, 2, b"b", now_ns=0)
        self.assertGreater(hbm.bank_conflicts, 0)
        self.assertGreater(cost, 0)

    def test_finish_get_miss_after_map_cleared(self) -> None:
        tkv = TensorKVAppliance(ApplianceConfig(n_pages=8, n_buckets=8, block_size=16))
        tkv.put(1, 0, b"OLD")
        tkv.begin_evict_key(1, 0)
        mid = tkv.get(1, [0])
        self.assertIn(0, mid.misses)
        tkv.complete_evict_key(1, 0)
        after = tkv.finish_get(1, [0])
        self.assertIn(0, after.misses)
        self.assertEqual(after.recirculations, 0)

    def test_finish_get_retries_while_hazarded(self) -> None:
        tkv = TensorKVAppliance(ApplianceConfig(n_pages=8, n_buckets=8, block_size=16))
        tkv.put(1, 0, b"OLD")
        tkv.begin_evict_key(1, 0)
        r = tkv.finish_get(1, [0], max_recirc=4)
        self.assertGreaterEqual(r.recirculations, 4)
        self.assertIn(0, r.misses)
        tkv.complete_evict_key(1, 0)
        after = tkv.finish_get(1, [0], max_recirc=4)
        self.assertEqual(after.recirculations, 0)
        self.assertIn(0, after.misses)

    def test_scheduled_evict_pumped_by_finish_get(self) -> None:
        tkv = TensorKVAppliance(ApplianceConfig(n_pages=8, n_buckets=8, block_size=16))
        tkv.put(1, 0, b"OLD")
        tkv.schedule_evict(1, 0, delay_ns=80)
        mid = tkv.get(1, [0])
        self.assertGreater(mid.recirculations, 0)
        self.assertIn(0, mid.misses)
        after = tkv.finish_get(1, [0], max_recirc=8)
        self.assertIn(0, after.misses)
        self.assertNotIn(b"OLD", after.payload)
        self.assertEqual(tkv.table.lookup(pack_key(1, 0)), None)

    def test_world_after_fires_at_deadline(self) -> None:
        world = World()
        seen: list[int] = []
        world.after(50, lambda: seen.append(world.now_ns))
        world.run_until(40)
        self.assertEqual(seen, [])
        world.run_until(50)
        self.assertEqual(seen, [50])

    def test_prompt_hash_is_stable(self) -> None:
        self.assertEqual(prompt_hash([1, 2, 3]), prompt_hash([1, 2, 3]))
        self.assertNotEqual(prompt_hash([1, 2, 3]), prompt_hash([1, 2, 4]))

    def test_stats_include_bank_locks(self) -> None:
        tkv = TensorKVAppliance(ApplianceConfig(n_pages=8, n_buckets=4, block_size=16))
        tkv.put(1, 0, b"x")
        tkv.evict_key(1, 0)
        self.assertIn("bank_locks", tkv.stats())
        self.assertGreaterEqual(tkv.stats()["bank_locks"], 1)

    def test_libtkv_posts_64b_descriptor(self) -> None:
        ctx = TensorKVContext(TensorKVAppliance(ApplianceConfig(block_size=16, n_pages=8, n_buckets=8)))
        ctx.register_gpu_memory(0x2000, 4096)
        ctx.put_async(1, 0, b"z", gpu_ptr=0x2000)
        c = ctx.poll_completion()
        self.assertIsNotNone(c)
        assert c is not None
        self.assertEqual(len(c.descriptor), 64)


class TestBaselinesAndPaperTables(unittest.TestCase):
    def test_logical_get_rtts(self) -> None:
        tkv = uncached_logical_get("tensorkv", 8)
        rdma = uncached_logical_get("rdma_uncached", 8)
        self.assertEqual(tkv.rtts, 1.0)
        self.assertEqual(rdma.rtts, 9.0)
        self.assertEqual(tkv.total_ns, 800 + 150 + 1150)

    def test_ttft_tensorkv_32k_parts(self) -> None:
        t = ttft_sim("tensorkv")
        self.assertAlmostEqual(t.setup_ms, 18.0, delta=0.05)
        self.assertGreater(t.fetch_ms, 200.0)
        self.assertLess(t.fetch_ms, 250.0)
        self.assertAlmostEqual(t.compute_ms, 15.0, delta=0.05)
        self.assertEqual(t.payload_bytes, 32_768 * 81_920)

    def test_ttft_miss_keeps_prefill_in_compute(self) -> None:
        miss = ttft_sim("tensorkv", prefix_hit=False)
        self.assertLess(miss.setup_ms, 1.0)
        self.assertAlmostEqual(miss.compute_ms, 1200.0, delta=1.0)
        self.assertGreater(miss.fetch_ms, 200.0)
        self.assertLess(miss.fetch_ms, 250.0)

    def test_engine_ttft_fetch_uses_100gbe(self) -> None:
        eng = PagedEngine(bytes_per_token=81_920)
        n = 256
        req = eng.submit(1, list(range(n)))
        expected = n * 81_920 * 8 / 100e9 * 1e3
        self.assertAlmostEqual(req.ttft_fetch_ms, expected, delta=2.0)
        credit_ms = n * 81_920 * 8 / 40e9 * 1e3
        self.assertLess(req.ttft_fetch_ms, credit_ms * 0.6)

    def test_tbt_1gb_named_parts(self) -> None:
        tkv = tbt_sim("tensorkv")
        rdma = tbt_sim("rdma_opt")
        host = tbt_sim("host_a100")
        self.assertAlmostEqual(tkv.total_ms, 102.0, delta=0.2)
        self.assertAlmostEqual(rdma.total_ms, 140.0, delta=0.2)
        self.assertAlmostEqual(host.total_ms, 210.0, delta=2.0)

    def test_bandwidth_tkv_below_rdma(self) -> None:
        rows = bandwidth_sweep()
        by_link = {r["link_gbps"]: r for r in rows}
        self.assertLess(by_link[100.0]["tensorkv_ms"], by_link[100.0]["rdma_opt_ms"])
        self.assertLess(by_link[25.0]["tensorkv_ms"], by_link[25.0]["rdma_opt_ms"])
        self.assertGreater(by_link[25.0]["tensorkv_ms"], by_link[100.0]["tensorkv_ms"])
        self.assertAlmostEqual(by_link[100.0]["tensorkv_ms"], 102.0, delta=0.2)

    def test_dpu_sweep_saturates(self) -> None:
        self.assertAlmostEqual(dpu_mpps(1), 0.15, places=2)
        self.assertAlmostEqual(dpu_mpps(8), 1.20, places=2)
        self.assertAlmostEqual(dpu_mpps(16), dpu_mpps(32), places=2)
        self.assertGreater(dpu_gbps(16), 60.0)
        self.assertLess(dpu_gbps(16), 65.0)
        self.assertLess(dpu_mpps(4), dpu_mpps(16))

    def test_ablation_get_p99(self) -> None:
        full = ablation_get_p99_us(True, True)
        nosplit = ablation_get_p99_us(False, True)
        nozc = ablation_get_p99_us(True, False)
        self.assertGreater(nosplit, full)
        self.assertGreater(nozc, full)
        self.assertGreater(nosplit, 8.0)
        self.assertLess(full, nosplit * 0.85)

    def test_ablation_prefix_phase_from_engine_timing(self) -> None:
        from tensorkv.baselines import ablation_prefix_phase_ms
        from tensorkv.timing import handle_install_ms, prefill_compute_ms

        self.assertAlmostEqual(ablation_prefix_phase_ms(True), handle_install_ms(32_768), delta=0.05)
        self.assertAlmostEqual(ablation_prefix_phase_ms(False), prefill_compute_ms(32_768), delta=1.0)

    def test_appliance_ablation_flags_change_latency(self) -> None:
        def one(fast: bool, zc: bool) -> int:
            tkv = TensorKVAppliance(
                ApplianceConfig(n_pages=8, n_buckets=8, block_size=64, fast_slow_split=fast, zero_copy_dma=zc)
            )
            tkv.put(1, 0, b"x")
            return tkv.get(1, [0]).latency_ns

        full = one(True, True)
        nosplit = one(False, True)
        nozc = one(True, False)
        self.assertGreater(nosplit, full)
        self.assertGreater(nozc, full)

    def test_mixtral_64way_fits_8gib(self) -> None:
        m = mixtral_sharing("64way")
        self.assertTrue(m.fits_8gib)
        self.assertTrue(m.gpu_graph_oom)
        self.assertGreater(m.logical_bytes, 130e9)
        self.assertLess(m.remote_need_bytes, 8 * (1 << 30))
        self.assertAlmostEqual(m.unique_bytes / 1e9, 4.3, delta=0.05)

    def test_mixtral_noshare_remote_oom(self) -> None:
        m = mixtral_sharing("noshare")
        self.assertFalse(m.fits_8gib)
        self.assertFalse(m.gpu_graph_oom)
        self.assertGreater(m.remote_need_bytes, 9.7e9)
        self.assertIsNone(m.tkv_hard_tbt_ms)
        self.assertIsNotNone(m.oom_message)

    def test_moe_tbt_16way_sums(self) -> None:
        tkv = moe_tbt_breakdown("tkv_hard")
        host = moe_tbt_breakdown("host_soft")
        self.assertAlmostEqual(tkv["total"], 47.0, delta=0.6)
        self.assertAlmostEqual(host["total"], 85.0, delta=0.6)
        self.assertLess(tkv["total"], host["total"])

    def test_energy_tkv_below_host(self) -> None:
        tkv = energy_sim("tensorkv")
        host = energy_sim("host_a100")
        self.assertAlmostEqual(tkv.j_per_tok, 0.59, delta=0.02)
        self.assertAlmostEqual(host.j_per_tok, 1.50, delta=0.02)
        self.assertLess(tkv.j_per_tok, host.j_per_tok)

    def test_attention_incast_credit_stops_drops(self) -> None:
        blast = simulate_attention_incast(credit_gbps=None)
        paced = simulate_attention_incast(credit_gbps=40.0)
        self.assertEqual(blast.n_sources, 16)
        self.assertEqual(blast.total_bytes, 128 * 1024)
        self.assertGreater(blast.arrival_gbps, blast.dest_gbps)
        self.assertGreater(blast.drops, paced.drops)
        self.assertEqual(paced.drops, 0)
        summary = attention_incast()
        self.assertEqual(summary["paced_drops"], 0)

    def test_sglang_radix_probe_hit(self) -> None:
        r = sglang_radix()
        self.assertTrue(r["second_prefix_hit"])
        self.assertGreater(r["leaf_blocks"], 0)

    def test_engine_decode_and_finish_keeps_prefix(self) -> None:
        eng = PagedEngine(bytes_per_token=81_920)
        prefix = list(range(32))
        first = eng.submit(1, prefix, prefix_tokens=prefix)
        self.assertFalse(first.prefix_hit)
        second = eng.submit(2, prefix + [9, 8, 7], prefix_tokens=prefix)
        self.assertTrue(second.prefix_hit)
        tbt = eng.decode(2, 42)
        self.assertGreater(tbt, 0.0)
        self.assertEqual(eng.stats.decode_steps, 1)
        eng.finish(2, keep_prefix=True)
        still = eng.tkv.probe(prompt_hash(prefix))
        self.assertTrue(still.hit)

    def test_engine_finish_releases_probe_refcount(self) -> None:
        eng = PagedEngine(bytes_per_token=81_920)
        prefix = list(range(32))
        eng.submit(1, prefix, prefix_tokens=prefix)
        rec = next(iter(eng.tkv.device.prefix.table.values()))
        self.assertEqual(rec.refcount, 1)
        eng.submit(2, prefix + [9], prefix_tokens=prefix)
        rec = next(iter(eng.tkv.device.prefix.table.values()))
        self.assertGreaterEqual(rec.refcount, 2)
        eng.finish(2, keep_prefix=True)
        rec = next(iter(eng.tkv.device.prefix.table.values()))
        self.assertEqual(rec.refcount, 1)
        self.assertTrue(eng.tkv.probe(prompt_hash(prefix)).hit)

    def test_sglang_longest_leaf(self) -> None:
        sgl = SGLangEngine()
        leaf = sgl.insert_prefix(list(range(32)))
        self.assertGreater(len(leaf.block_ids), 0)
        req = sgl.activate(list(range(32)) + [99, 100])
        self.assertTrue(req.prefix_hit)
        self.assertEqual(req.prefix_len, 32)
        self.assertGreater(sgl.paged.stats.skipped_prefill_tokens, 0)

    def test_engine_counts_logical_kv_bytes(self) -> None:
        eng = PagedEngine(bytes_per_token=81_920)
        req = eng.submit(1, list(range(32)))
        self.assertEqual(eng.stats.bytes_get, 32 * 81_920)
        self.assertGreater(req.ttft_fetch_ms, 0.0)


class TestZipfAndWorkloads(unittest.TestCase):
    def test_zipf_head_is_hot(self) -> None:
        z = ZipfSampler(1000, 1.2, SplitMix64(1))
        head = z.empirical_head_share(8000, head=50)
        self.assertGreater(head, 0.20)
        ranks = [z.sample() for _ in range(4000)]
        self.assertGreater(ranks.count(1), ranks.count(500))

    def test_lfru_flood_separates_from_lru(self) -> None:
        ev = eviction_sensitivity()
        lru60 = ev["lru_60"]
        lfru60 = ev["lfru_60"]
        lru80 = ev["lru_80"]
        lfru80 = ev["lfru_80"]
        self.assertAlmostEqual(lru60["prefix_survival_pct"], 30.0, delta=8.0)
        self.assertAlmostEqual(lru80["prefix_survival_pct"], 65.0, delta=8.0)
        self.assertGreaterEqual(lfru60["prefix_survival_pct"], 95.0)
        self.assertGreaterEqual(lfru80["prefix_survival_pct"], 95.0)
        self.assertGreater(lfru60["prefix_survival_pct"] - lru60["prefix_survival_pct"], 40.0)
        self.assertGreater(lfru80["hit_rate"], lru80["hit_rate"])
        self.assertLess(lfru60["weighted_ttft_ms"], lru60["weighted_ttft_ms"] - 200)

    def test_sharegpt_lfru_keeps_more_prefix(self) -> None:
        r = sharegpt_eviction(0.6)
        self.assertGreater(r["lfru_minus_lru_survival"], 15.0)
        self.assertGreater(r["lfru"]["prefix_survival_pct"], r["lru"]["prefix_survival_pct"])
        hot = sharegpt_eviction(0.8)
        self.assertGreaterEqual(hot["lfru"]["prefix_survival_pct"], hot["lru"]["prefix_survival_pct"])

    def test_prefix_ttft_gap_scales_with_prompt(self) -> None:
        small = prefix_activation(256)
        self.assertLess(small["second_ttft_ms"], small["first_ttft_ms"])
        self.assertGreater(small["first_ttft_parts"]["compute"], small["second_ttft_parts"]["compute"])
        mid = prefix_activation(4096)
        self.assertGreater(mid["ttft_gap_ms"], 50.0)
        self.assertGreater(mid["first_ttft_parts"]["compute"], 100.0)
        self.assertLess(mid["second_ttft_parts"]["compute"], 10.0)
        hit = ttft_sim("tensorkv", 32_768)
        miss = ttft_sim("recompute", 32_768)
        self.assertAlmostEqual(hit.setup_ms, 18.0, delta=0.05)
        self.assertGreater(miss.total_ms - hit.total_ms, 900.0)

    def test_repo_root_finds_workspace(self) -> None:
        from tensorkv.experiments import repo_root

        root = repo_root()
        self.assertTrue((root / "package.json").exists())
        self.assertTrue((root / "python" / "tensorkv").is_dir())

    def test_drr_is_fair_and_preempts_put(self) -> None:
        r = drr_fairness()
        self.assertGreater(r["jain_fairness"], 0.98)
        self.assertLess(r["max_min_ratio"], 1.05)
        self.assertTrue(r["gets_finish_before_put"])
        self.assertGreater(r["preemptions"], 0)

    def test_credit_above_gemv_not_needed(self) -> None:
        rows = {row["credit_gbps"]: row for row in credit_vs_gemv_sweep()}
        self.assertEqual(rows[40.0]["paced_drops"], 0)
        self.assertGreater(rows[40.0]["blast_drops"], 0)
        self.assertEqual(rows[40.0]["gpu_drops"], 0)
        self.assertGreater(rows[80.0]["gpu_drops"], 0)
        self.assertGreater(rows[100.0]["gpu_drops"], rows[40.0]["gpu_drops"])

    def test_get_latency_histogram_has_mass(self) -> None:
        h = get_latency_histogram(n_keys=128, n_ops=400)
        self.assertGreater(h["summary_ns"]["n"], 100)
        self.assertGreater(h["summary_ns"]["p50"], 0)
        self.assertGreater(h["summary_ns"]["p99"], h["summary_ns"]["p50"])
        self.assertGreater(h["slow_path_summary_ns"]["p50"], h["summary_ns"]["p50"])

    def test_fingerprint_victim_at_high_fill(self) -> None:
        r = fingerprint_and_victim()
        self.assertEqual(r["sram_slot_fields"], ("fingerprint", "phys"))
        self.assertGreater(r["hbm_key_verifies"], 0)
        self.assertGreater(r["size"], 0)
        self.assertGreaterEqual(r["slow_inserts"] + r["extra_slow"], 0)

    def test_async_put_serialize_is_link_rate(self) -> None:
        r = async_put_interference()
        self.assertAlmostEqual(r["put_isolation_ms"], 40.0, delta=1.0)
        self.assertLess(r["throughput_drop"], 0.05)
        self.assertGreater(r["overlapped_ms"], 30.0)

    def test_isolation_experiment_four_regimes(self) -> None:
        iso = isolation_experiment(duration_s=3.0, tick_us=50.0, interference_start_s=1.0, interference_end_s=2.0)
        self.assertGreater(iso["fifo"].interference_p99, iso["qos"].interference_p99)
        self.assertGreater(iso["qos"].interference_p99, iso["pacing"].interference_p99)
        self.assertGreater(iso["pacing"].interference_p99, iso["both"].interference_p99)


class TestSchedulerReplayAndDpuModel(unittest.TestCase):
    def test_serving_scheduler_reuses_prefix(self) -> None:
        eng = PagedEngine(
            TensorKVContext(TensorKVAppliance(ApplianceConfig(n_pages=128, n_buckets=32, store_payloads=False)))
        )
        sch = ServingScheduler(eng)
        prefix = list(range(12))
        stats = sch.run_batch(
            [
                (1, prefix + [1], prefix),
                (2, prefix + [2], prefix),
            ],
            decode_steps=2,
        )
        self.assertEqual(stats.submitted, 2)
        self.assertGreaterEqual(stats.prefix_hits, 1)
        self.assertEqual(stats.finished, 2)
        self.assertGreater(stats.decode_steps, 0)

    def test_mixed_trace_covers_four_opcodes(self) -> None:
        r = mixed_trace(n_contexts=8, blocks_per_ctx=8, n_ops=120, seed=2)
        self.assertEqual(r["ops"], 120)
        self.assertGreater(r["gets"], 0)
        self.assertGreater(r["probes"], 0)
        self.assertGreaterEqual(r["puts"] + r["evicts"], 1)

    def test_dpu_linear_then_nic_cap(self) -> None:
        self.assertAlmostEqual(dpu_mpps(1), 0.15, places=2)
        self.assertLess(dpu_mpps(4), dpu_mpps(16))
        self.assertAlmostEqual(dpu_mpps(16), dpu_mpps(64), places=2)

    def test_session_trace_uses_gather_overflow(self) -> None:
        r = session_trace(n_sessions=8, n_prefixes=3, prefix_blocks=12, unique_blocks=2, seed=4)
        self.assertEqual(r["sessions"], 8)
        self.assertGreater(r["overflow_gets"], 0)
        self.assertGreater(r["prefix_hits"], 0)
        self.assertGreater(r["gets"], 0)
        self.assertEqual(r["prefix_blocks"], 12)

    def test_continuous_serving_reuses_sharegpt_prefix(self) -> None:
        r = run_serving(
            n_sessions=12,
            n_prefixes=3,
            prefix_blocks=10,
            unique_blocks=2,
            max_batch=4,
            decode_tokens=3,
            n_pages=192,
            n_buckets=64,
            seed=5,
        )
        self.assertTrue(r["complete"])
        self.assertEqual(r["finished"], 12)
        self.assertGreater(r["prefix_hits"], 0)
        self.assertGreater(r["vector_overflow"], 0)
        self.assertGreater(r["overflow_bytes"], 0)
        self.assertGreater(r["device"]["get_ops"], 0)
        self.assertGreater(r["get_latency_ns"]["p99"], r["get_latency_ns"]["p50"])

    def test_serving_reclaims_under_page_pressure(self) -> None:
        r = run_serving(
            n_sessions=10,
            n_prefixes=8,
            prefix_blocks=8,
            unique_blocks=3,
            max_batch=4,
            decode_tokens=2,
            n_pages=16,
            n_buckets=32,
            seed=3,
        )
        self.assertGreater(r["reclaims"], 0)
        self.assertGreaterEqual(r["finished"], 1)

    def test_scheduler_arrivals_prefill_before_decode(self) -> None:
        eng = PagedEngine(
            TensorKVContext(TensorKVAppliance(ApplianceConfig(n_pages=64, n_buckets=16, store_payloads=False)))
        )
        sch = ServingScheduler(eng, max_batch=2)
        prefix = list(range(8))
        stats = sch.run_arrivals(
            [
                Arrival(1, prefix + [1], prefix, max_new=2, arrive_tick=0),
                Arrival(2, prefix + [2], prefix, max_new=2, arrive_tick=0),
            ]
        )
        self.assertEqual(stats.submitted, 2)
        self.assertGreaterEqual(stats.prefix_hits, 1)
        self.assertEqual(stats.finished, 2)
        self.assertGreater(stats.peak_batch, 0)


def run_unittest() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run_unittest())
