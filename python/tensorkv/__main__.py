"""CLI: python -m tensorkv [demo|experiment|engine|baselines|selftest|report]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tensorkv.appliance import ApplianceConfig, TensorKVAppliance  # noqa: E402
from tensorkv.engine import PagedEngine  # noqa: E402
from tensorkv.experiments import dump_results, run_all  # noqa: E402
from tensorkv.libtkv import TensorKVContext  # noqa: E402


def cmd_demo() -> int:
    ctx = TensorKVContext(TensorKVAppliance(ApplianceConfig(block_size=64, n_pages=128, n_buckets=32)))
    print("== TKV_PUT ==")
    for i in range(4):
        r = ctx.put_async(1, i, bytes([i]) * 8)
        print(f"  block {i} -> page {r.phys} path={r.path}")
    print("== TKV_GET scatter-gather [0,2,3] ==")
    g = ctx.get_async(1, [0, 2, 3])
    print(f"  hits={g.hits} misses={g.misses} bytes={g.gathered_bytes} ok={g.ok}")
    print("== TKV_PROBE miss then publish ==")
    print("  ", ctx.probe(0xDEAD).hit)
    ctx.device.publish_prefix(0xDEAD, 1, [0, 1, 2, 3])
    p = ctx.probe(0xDEAD)
    print(f"  hit={p.hit} handles={p.handles} ref={p.refcount} hbm={p.hbm_accessed}")
    print("== TKV_EVICT oldest 2 ==")
    e = ctx.evict(1, policy="oldest", k=2)
    print("  evicted", e.evicted)
    g2 = ctx.get_async(1, [0, 1, 2, 3])
    print(f"  after evict hits={g2.hits} misses={g2.misses}")
    print("== stats ==")
    print(json.dumps(ctx.device.stats(), indent=2))
    return 0


def cmd_engine() -> int:
    eng = PagedEngine()
    prefix = list(range(64))
    a = eng.submit(1, prefix + [9, 8, 7], prefix_tokens=prefix)
    b = eng.submit(2, prefix + [1, 2, 3], prefix_tokens=prefix)
    eng.decode(1, 42)
    eng.decode(2, 43)
    print(
        json.dumps(
            {"a_hit": a.prefix_hit, "b_hit": b.prefix_hit, "stats": eng.stats.__dict__, "ttft": [a.ttft_ms, b.ttft_ms]},
            indent=2,
        )
    )
    return 0


def _print_report(result: dict) -> None:
    print("== occupancy (slow-path fill rate should rise with load) ==")
    for p in result["occupancy"]:
        print(
            f"  load={p['load']:.2f} fill_slow={p['fill_slow_insert_rate']*100:.2f}% "
            f"churn_slow={p['churn_slow_insert_rate']*100:.2f}% kicks={p['kicks']} "
            f"keep={p['throughput_keep']*100:.1f}% hazard={p['hazard_rate']*100:.2f}% "
            f"victim={p['victim_buffer']} zipf_head={p['zipf_head_share']*100:.1f}%"
        )
    print("== LFRU flood (prefix survival) ==")
    for k, row in result["eviction"].items():
        print(
            f"  {k}: survival={row['prefix_survival_pct']}% hit={row['hit_rate']}% "
            f"ttft={row['weighted_ttft_ms']}ms  eval_hit={row['eval_hit_rate']}% eval_ttft={row['eval_ttft_ms']}ms"
        )
    print("== isolation (interference P99, four regimes) ==")
    for k, v in result["isolation"].items():
        print(
            f"  {k:7} intP99={v['interference_p99']:8.2f} ms  quietP99={v['quiet_p99']:7.2f} "
            f"get_drops={v['get_drops']} put_drops={v['put_drops']}"
        )
    pfx = result["prefix_32k"]
    print(
        f"== 32K prefix TTFT  miss={pfx['first_ttft_ms']:.1f}ms hit={pfx['second_ttft_ms']:.1f}ms "
        f"gap={pfx['ttft_gap_ms']:.1f}ms =="
    )
    print(f"== DRR jain={result['drr']['jain_fairness']} incast paced_drops={result['incast']['paced_drops']} ==")


def cmd_experiment() -> int:
    result = run_all()
    _print_report(result)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_report() -> int:
    result = dump_results()
    _print_report(result)
    print("wrote eval/results/latest.json")
    return 0


def cmd_baselines() -> int:
    from tensorkv.baselines import run_baseline_suite

    print(json.dumps(run_baseline_suite(), indent=2, default=str))
    return 0


def cmd_selftest() -> int:
    from tests.test_tensorkv import run_unittest

    return run_unittest()


def main() -> int:
    parser = argparse.ArgumentParser(description="TensorKV software implementation")
    parser.add_argument(
        "command",
        choices=["demo", "experiment", "engine", "baselines", "selftest", "report"],
        nargs="?",
        default="demo",
    )
    args = parser.parse_args()
    return {
        "demo": cmd_demo,
        "experiment": cmd_experiment,
        "engine": cmd_engine,
        "baselines": cmd_baselines,
        "selftest": cmd_selftest,
        "report": cmd_report,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
