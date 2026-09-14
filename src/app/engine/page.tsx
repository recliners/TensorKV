"use client";

import { useMemo, useState } from "react";
import { SiteShell } from "@/components/site-shell";
import { TensorKVAppliance, TensorKVContext } from "@/lib/tensorkv/appliance";
import { promptHash } from "@/lib/tensorkv/experiments";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type Step = { title: string; detail: string };

export default function EnginePage() {
  const [steps, setSteps] = useState<Step[]>([]);
  const [stats, setStats] = useState<Record<string, number | string>>({});

  const run = () => {
    const device = new TensorKVAppliance({ nBuckets: 64, nPages: 256, blockSize: 64 });
    const tkv = new TensorKVContext(device);
    const log: Step[] = [];
    const prefix = [11, 22, 33, 44, 55, 66, 77, 88, 99, 100, 101, 102, 103, 104, 105, 106];
    const ph = promptHash(prefix);

    log.push({ title: "请求 A · Prefill", detail: "PROBE 共享 system prompt → Miss，本地计算后 PUT 前缀块。" });
    for (let i = 0; i < 2; i++) tkv.putAsync(1, i, new Uint8Array(8).fill(i), ph);
    tkv.device.publishPrefix(ph, 1, [0, 1]);
    const p0 = tkv.probe(ph);
    log.push({ title: "登记前缀", detail: `PromptHash 已写入 Bloom + 前缀表。自检 PROBE hit=${p0.hit} ref=${p0.refcount}` });

    log.push({ title: "请求 B · Scheduler", detail: "第二次 PROBE 命中，只返回句柄，不读 HBM。" });
    const p1 = tkv.probe(ph);
    log.push({
      title: "PROBE HIT",
      detail: `handles=[${p1.handles}] ref=${p1.refcount} hbmAccessed=${p1.hbmAccessed}（论文：跳过重算，TTFT 仍含后续 GET+Attention）`,
    });

    tkv.putAsync(2, 0, new Uint8Array([7, 7, 7]));
    const g = tkv.getAsync(1, p1.handles, 40);
    log.push({
      title: "Worker · TKV_GET",
      detail: `从拥有者上下文 gather 前缀 ${g.hits.length} 块，连续 ${g.gatheredBytes}B，供 GEMV 使用。`,
    });

    const raceCtx = 3;
    tkv.putAsync(raceCtx, 0, new TextEncoder().encode("KV0"));
    tkv.device.beginEvictKey(raceCtx, 0);
    const mid = tkv.device.get(raceCtx, [0]);
    tkv.device.completeEvictKey(raceCtx, 0);
    log.push({
      title: "请求结束 · EVICT",
      detail: `Scoreboard 回收期间 GET recirc=${mid.recirculations}；完成后为 Miss，满足单调读。`,
    });

    setSteps(log);
    setStats(device.stats());
  };

  const statEntries = useMemo(() => Object.entries(stats), [stats]);

  return (
    <SiteShell>
      <h1 className="text-2xl font-semibold tracking-tight">推理引擎工作流</h1>
      <p className="mt-2 mb-6 max-w-3xl text-sm text-muted-foreground">
        对应论文的 vLLM 集成：调度器做 PROBE，缓存引擎做 PUT/EVICT，Attention 路径做 JIT GET。这里用缩小的 token 块演示同一套控制流。
      </p>
      <Button onClick={run}>跑一遍共享前缀场景</Button>
      <div className="mt-6 grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
        <Card className="bg-card/80">
          <CardHeader>
            <CardTitle className="text-base">控制平面步骤</CardTitle>
            <CardDescription>Scheduler → libtkv → 器件</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {steps.length === 0 ? (
              <p className="text-sm text-muted-foreground">点击上方按钮，模拟两个请求共享同一段 prefix。</p>
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
            <CardTitle className="text-base">器件计数器</CardTitle>
          </CardHeader>
          <CardContent className="grid grid-cols-2 gap-2 text-sm">
            {statEntries.length === 0 ? (
              <p className="col-span-2 text-muted-foreground">尚无数据</p>
            ) : (
              statEntries.slice(0, 12).map(([k, v]) => (
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
