"use client";

import { useMemo, useState } from "react";
import { SiteShell } from "@/components/site-shell";
import { TensorKVAppliance, TensorKVContext } from "@/lib/tensorkv/appliance";
import { PagedEngine } from "@/lib/tensorkv/engine";
import { SGLangEngine } from "@/lib/tensorkv/sglang";
import { promptHash } from "@/lib/tensorkv/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type Step = { title: string; detail: string };

export default function EnginePage() {
  const [steps, setSteps] = useState<Step[]>([]);
  const [stats, setStats] = useState<Record<string, number | string>>({});

  const run = () => {
    const device = new TensorKVAppliance({ nBuckets: 128, nPages: 512, storePayloads: false });
    const engine = new PagedEngine(new TensorKVContext(device));
    const log: Step[] = [];
    const prefix = Array.from({ length: 32 }, (_, i) => 11 + i);

    const a = engine.submit(1, [...prefix, 201, 202], prefix);
    log.push({
      title: "请求 A · Prefill",
      detail: `PROBE Miss，把 ${prefix.length} token 前缀物化并登记。TTFT setup ${a.ttftSetupMs.toFixed(3)} ms · fetch ${a.ttftFetchMs.toFixed(2)} ms · compute ${a.ttftComputeMs.toFixed(2)} ms（合计 ${a.ttftMs.toFixed(2)} ms）。命中=${a.prefixHit}`,
    });

    const b = engine.submit(2, [...prefix, 301, 302, 303], prefix);
    log.push({
      title: "请求 B · Scheduler PROBE HIT",
      detail: `跳过 ${engine.stats.skippedPrefillTokens} 个前缀 token。owner ctx=${b.prefixOwner} handles=${b.prefixBlocks.length}。TTFT setup ${b.ttftSetupMs.toFixed(3)} ms · fetch ${b.ttftFetchMs.toFixed(2)} ms · compute ${b.ttftComputeMs.toFixed(2)} ms（合计 ${b.ttftMs.toFixed(2)} ms）。`,
    });

    const tbt1 = engine.decode(2, 401);
    const tbt2 = engine.decode(2, 402);
    log.push({
      title: "请求 B · Decode",
      detail: `两步 TBT ${tbt1.toFixed(3)} ms / ${tbt2.toFixed(3)} ms（gather 走 40 Gbps GEMV credit）。generated=${engine.requests.get(2)?.generated}`,
    });

    const sgl = new SGLangEngine();
    const leaf = sgl.insertPrefix(prefix);
    const act = sgl.activate([...prefix, 9, 8, 7]);
    log.push({
      title: "SGLang radix 叶",
      detail: `insert_prefix 叶 ${leaf.blockIds.length} 块；activate 更长 prompt 时 longest-leaf PROBE hit=${act.prefixHit}，skipped=${sgl.paged.stats.skippedPrefillTokens}`,
    });

    const refBeforeFinish = [...device.prefix.table.values()][0]?.refcount ?? 0;
    engine.finish(2, true);
    const still = device.probe(promptHash(prefix));
    log.push({
      title: "请求 B · finish(keep_prefix)",
      detail: `后缀已回收。finish 前前缀 ref=${refBeforeFinish}，release 后再 PROBE hit=${still.hit} handles=${still.handles.length} ref=${still.refcount}。逻辑 GET 字节 ${engine.stats.bytesGet}`,
    });

    sgl.finish(act.reqId, true);
    log.push({
      title: "SGLang · finish",
      detail: `radix 叶请求 ${act.reqId} 已结束。叶仍登记 ${sgl.leaves.size} 条，器件 PROBE hits=${sgl.tkv.device.prefix.hits}`,
    });

    setSteps(log);
    setStats({
      ...device.stats(),
      enginePrefills: engine.stats.prefills,
      enginePrefixHits: engine.stats.prefixHits,
      enginePrefixMisses: engine.stats.prefixMisses,
      engineDecodeSteps: engine.stats.decodeSteps,
      skippedPrefillTokens: engine.stats.skippedPrefillTokens,
      sglangLeaves: sgl.leaves.size,
    });
  };

  const statEntries = useMemo(() => Object.entries(stats), [stats]);

  return (
    <SiteShell>
      <h1 className="text-2xl font-semibold tracking-tight">推理引擎工作流</h1>
      <p className="mt-2 mb-6 max-w-3xl text-sm text-muted-foreground">
        这一页跑的是与 Python <code>PagedEngine</code> / <code>SGLangEngine</code> 同一套控制流：调度器 PROBE，缓存引擎 PUT/EVICT，Attention 路径 JIT GET，radix 叶走最长前缀匹配。
      </p>
      <Button onClick={run}>跑一遍共享前缀 + radix 叶</Button>
      <div className="mt-6 grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
        <Card className="bg-card/80">
          <CardHeader>
            <CardTitle className="text-base">控制平面步骤</CardTitle>
            <CardDescription>PagedEngine.submit / decode / finish · SGLang.activate</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {steps.length === 0 ? (
              <p className="text-sm text-muted-foreground">点击上方按钮，模拟两个请求共享同一段 prefix，再挂一个 radix 叶。</p>
            ) : (
              steps.map((s, i) => (
                <div key={i} className="rounded-lg border border-border/60 p-3">
                  <div className="mb-1 flex items-center gap-2">
                    <Badge variant="outline">{i + 1}</Badge>
                    <span className="font-medium">{s.title}</span>
                  </div>
                  <p className="text-sm text-muted-foreground">{s.detail}</p>
                </div>
              ))
            )}
          </CardContent>
        </Card>
        <Card className="bg-card/80">
          <CardHeader>
            <CardTitle className="text-base">引擎与器件计数器</CardTitle>
          </CardHeader>
          <CardContent className="grid grid-cols-2 gap-2 text-sm">
            {statEntries.length === 0 ? (
              <p className="col-span-2 text-muted-foreground">尚无数据</p>
            ) : (
              statEntries.slice(0, 16).map(([k, v]) => (
                <div key={k} className="rounded-md bg-muted/50 p-2">
                  <div className="text-[11px] text-muted-foreground">{k}</div>
                  <div className="font-mono">{String(v)}</div>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>
    </SiteShell>
  );
}
